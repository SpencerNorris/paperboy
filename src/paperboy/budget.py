"""The `Budget` gate: every Telegram RPC passes through here (ADR-0003).

Enforces, in order, on every call: a per-run RPC cap, a per-method minimum
call interval, and any persisted flood cooldown for that method — then runs
the call and classifies failures per spec §8. A short `FLOOD_WAIT` is slept
through and retried once; everything else becomes one of `SkipAndRecord`,
`PhaseStop`, or `HardStop` so the recipe layer can react without a collector
ever having to know a Telethon error class.
"""

from __future__ import annotations

import inspect
import time as time_module
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from paperboy.config import Settings
from paperboy.errors import Disposition, classify
from paperboy.ids import to_iso
from paperboy.store.db import Store

T = TypeVar("T")

# Conservative default pacing between two calls to the *same* method, absent
# any other guidance. Spec §13.10 (sequential `getFullUser` flood onset) is
# unverified at the time of writing; 1 req/s is a safe starting point.
DEFAULT_MIN_INTERVAL_SECONDS = 1.0


class HardStop(Exception):
    """The run must end now (spec §8: PEER_FLOOD, FROZEN_METHOD_INVALID, ...)."""


class PhaseStop(Exception):
    """The current phase must stop; other phases may still run (long FLOOD_WAIT).

    `counts` carries whatever the phase completed before stopping. A page-budget
    stop is the routine outcome on a large target rather than an error, so a
    phase that stored hundreds of messages before hitting it must still report
    them — otherwise the operator reads an empty result for a run that did real
    work, and `run_events` preserves nothing to resume reasoning from.
    """

    def __init__(self, *args: object, counts: dict[str, int] | None = None) -> None:
        super().__init__(*args)
        self.counts: dict[str, int] = dict(counts or {})


class SkipAndRecord(Exception):
    """This one RPC is skipped (e.g. CHAT_ADMIN_REQUIRED); the phase continues."""


class _Clock(Protocol):
    def time(self) -> float: ...


class _RealClock:
    def time(self) -> float:
        return time_module.time()


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


class Budget:
    """Paces, throttles, and classifies every RPC made through it.

    `sleeper` is injectable so tests never actually block: it may be a plain
    sync callable (called and ignored, for tests that just want to record
    calls) or an async one like `asyncio.sleep` (awaited) — `Budget` detects
    which by checking whether the call returns an awaitable.

    `method_intervals` overrides the pace for specific methods
    (`--profile-interval` → `users.getFullUser`/`photos.getUserPhotos`);
    flood cooldowns and the run cap apply regardless.
    """

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        clock: _Clock | None = None,
        sleeper: Callable[[float], object] | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
        method_intervals: dict[str, float] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._clock: _Clock = clock or _RealClock()
        self._sleeper: Callable[[float], object] = sleeper or self._default_sleep
        self._min_interval = min_interval
        self._method_intervals: dict[str, float] = dict(method_intervals or {})
        self._count = 0
        self._last_call: dict[str, float] = {}

    @staticmethod
    def _default_sleep(seconds: float) -> Awaitable[None]:
        import asyncio

        return asyncio.sleep(seconds)

    def _now_iso(self) -> str:
        return to_iso(datetime.fromtimestamp(self._clock.time(), tz=UTC))

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await _maybe_await(self._sleeper(seconds))

    def _record_flood(self, method: str, seconds: int) -> None:
        until = self._clock.time() + seconds
        self.store.conn.execute(
            "INSERT INTO flood_log(method, until, seconds, recorded_at) VALUES (?, ?, ?, ?)",
            (method, to_iso(datetime.fromtimestamp(until, tz=UTC)), seconds, self._now_iso()),
        )

    def _active_cooldown_seconds(self, method: str) -> float:
        row = self.store.conn.execute(
            "SELECT until FROM flood_log WHERE method=? ORDER BY until DESC LIMIT 1",
            (method,),
        ).fetchone()
        if row is None:
            return 0.0
        until_dt = datetime.fromisoformat(row["until"])
        remaining = (until_dt - datetime.fromtimestamp(self._clock.time(), tz=UTC)).total_seconds()
        return max(0.0, remaining)

    @staticmethod
    def _to_exception(disposition: Disposition, exc: BaseException) -> Exception:
        if disposition is Disposition.SKIP:
            return SkipAndRecord(str(exc))
        if disposition is Disposition.PHASE_STOP:
            return PhaseStop(str(exc))
        if disposition is Disposition.HARD_STOP:
            return HardStop(str(exc))
        # RETRY should never reach here — call() handles it before this point.
        raise AssertionError(f"unexpected disposition for re-raise: {disposition}")

    async def call(self, method: str, factory: Callable[[], Awaitable[T]]) -> T:
        if self._count >= self.settings.max_rpc_per_run:
            raise HardStop(
                f"max_rpc_per_run ({self.settings.max_rpc_per_run}) reached at {method!r}"
            )
        self._count += 1

        interval = self._method_intervals.get(method, self._min_interval)
        last = self._last_call.get(method)
        if last is not None:
            delta = self._clock.time() - last
            if delta < interval:
                await self._sleep(interval - delta)

        cooldown = self._active_cooldown_seconds(method)
        if cooldown:
            await self._sleep(cooldown)

        self._last_call[method] = self._clock.time()

        try:
            return await factory()
        except Exception as exc:
            threshold = self.settings.flood_sleep_threshold
            disposition = classify(exc, threshold=threshold)
            if disposition is Disposition.PHASE_STOP:
                # A FLOOD_WAIT over threshold: persist the cooldown (so a
                # later call to this method — this run or the next — waits
                # it out via `_active_cooldown_seconds`) and stop the phase.
                self._record_flood(method, getattr(exc, "seconds", 0))
                raise self._to_exception(disposition, exc) from exc
            if disposition is not Disposition.RETRY:
                raise self._to_exception(disposition, exc) from exc

            # `seconds` is only meaningful for a genuine FloodWaitError/
            # FakeFlood retry; a transient ConnectionError/TimeoutError/OSError
            # also classifies as RETRY but has no `.seconds`, so `getattr`
            # falls back to 0 — guard `_record_flood` so those don't pollute
            # `flood_log` with spurious (until=now, seconds=0) rows.
            seconds = getattr(exc, "seconds", 0)
            if seconds > 0:
                self._record_flood(method, seconds)
            await self._sleep(seconds)
            self._last_call[method] = self._clock.time()
            try:
                return await factory()
            except Exception as exc2:
                d2 = classify(exc2, threshold=threshold)
                if d2 is Disposition.RETRY:
                    # A second consecutive flood wait is not retried again in
                    # the same call — treat it as a phase stop so the caller
                    # doesn't spin. The next run will see the persisted
                    # cooldown via `_active_cooldown_seconds`.
                    seconds2 = getattr(exc2, "seconds", 0)
                    if seconds2 > 0:
                        self._record_flood(method, seconds2)
                    raise PhaseStop(str(exc2)) from exc2
                if d2 is Disposition.PHASE_STOP:
                    self._record_flood(method, getattr(exc2, "seconds", 0))
                raise self._to_exception(d2, exc2) from exc2
