"""The `Budget` gate: every Telegram RPC passes through here (ADR-0003).

Enforces, in order, on every call: a per-run RPC cap (every attempt counts), a
per-method minimum call interval (base x `pacing_factor`), and any persisted
flood cooldown for that method — then runs the call and classifies failures
per spec §8. Patient by design (ADR-0003 amendment, #69): a `FLOOD_WAIT` is
waited as `ceil(s x 1.1) + 5` seconds and retried, up to 3 consecutive floods
per call, as long as that applied wait is within the `flood_sleep_threshold`
ceiling; transient network errors retry 3 times at 5/10/20 s x factor.
Everything else becomes one of `SkipAndRecord`, `PhaseStop`, or `HardStop` so
the recipe layer can react without a collector ever having to know a Telethon
error class.
"""

from __future__ import annotations

import inspect
import logging
import time as time_module
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from paperboy.config import Settings
from paperboy.errors import Disposition, classify, is_flood_wait
from paperboy.ids import to_iso
from paperboy.store.db import Store

T = TypeVar("T")

log = logging.getLogger("paperboy.budget")

# A server-mandated FLOOD_WAIT of `s` seconds is waited as ceil(s x 1.1) + 5.
# The margin (10 % plus 5 s) exists so we never re-call at the exact instant the
# server's window closes; it is a margin, not a multiplier, because the server's
# number is not a guess of ours.
FLOOD_MARGIN_NUMERATOR = 11  # x1.1, kept in integers to avoid float ceil error
FLOOD_MARGIN_DENOMINATOR = 10
FLOOD_MARGIN_EXTRA_SECONDS = 5

MAX_FLOOD_RETRIES = 3  # consecutive floods slept and retried per call; a 4th stops the phase
MAX_TRANSIENT_RETRIES = 3  # network-error retries per call; a 4th stops the phase
TRANSIENT_BACKOFF_SECONDS = (5, 10, 20)  # base delays, x pacing_factor
HEARTBEAT_SECONDS = 60  # long sleeps are chunked and logged at this cadence


def flood_applied_seconds(seconds: int) -> int:
    """The wait actually applied for a server `FLOOD_WAIT` of `seconds`."""
    scaled = -(-seconds * FLOOD_MARGIN_NUMERATOR // FLOOD_MARGIN_DENOMINATOR)  # ceil
    return scaled + FLOOD_MARGIN_EXTRA_SECONDS

# BASE pacing between two calls to the *same* method. These are our own
# assumptions about what Telegram tolerates, not measured limits (ADR-0003
# amendment, #69): every interval actually enforced is base x
# `Settings.pacing_factor`. Spec §13.10 (sequential `getFullUser` flood onset)
# is unverified; 1 req/s is the starting assumption.
DEFAULT_MIN_INTERVAL_SECONDS = 1.0

# Per-method base overrides where we assume a method is more tightly limited.
# `contacts.resolveUsername` is among Telegram's most flood-limited methods.
BASE_METHOD_INTERVALS: dict[str, float] = {"contacts.resolveUsername": 5.0}


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

    `min_interval` and `method_intervals` are BASE intervals; the interval
    actually enforced is base x `settings.pacing_factor` (`effective_interval`).
    `method_intervals` overrides `BASE_METHOD_INTERVALS` for specific methods
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
        self._factor = settings.pacing_factor
        self._method_intervals: dict[str, float] = {
            **BASE_METHOD_INTERVALS,
            **(method_intervals or {}),
        }
        self._count = 0
        self._last_call: dict[str, float] = {}

    def effective_interval(self, method: str) -> float:
        """Seconds enforced between two calls to `method`: base x pacing factor."""
        return self._method_intervals.get(method, self._min_interval) * self._factor

    def describe_pacing(self) -> str:
        """One-line summary of the pacing in force (logged at gateway construction)."""
        default = self._min_interval * self._factor
        parts = [f"factor={self._factor}", f"default={default}s"]
        for method in sorted(self._method_intervals):
            interval = self.effective_interval(method)
            if interval != default:
                parts.append(f"{method}={interval}s")
        return " ".join(parts)

    @staticmethod
    def _default_sleep(seconds: float) -> Awaitable[None]:
        import asyncio

        return asyncio.sleep(seconds)

    def _now_iso(self) -> str:
        return to_iso(datetime.fromtimestamp(self._clock.time(), tz=UTC))

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await _maybe_await(self._sleeper(seconds))

    def _record_flood(self, method: str, seconds: int, applied: int) -> None:
        """Persist a flood: the server's `seconds`, our `applied` wait, and the
        cooldown deadline (`until`) derived from `applied`."""
        until = self._clock.time() + applied
        self.store.conn.execute(
            "INSERT INTO flood_log(method, until, seconds, applied_seconds, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                method,
                to_iso(datetime.fromtimestamp(until, tz=UTC)),
                seconds,
                applied,
                self._now_iso(),
            ),
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

    async def _pace(self, method: str) -> None:
        """Wait out this method's minimum interval and any persisted flood cooldown."""
        interval = self.effective_interval(method)
        last = self._last_call.get(method)
        if last is not None:
            delta = self._clock.time() - last
            if delta < interval:
                await self._sleep(interval - delta)

        cooldown = self._active_cooldown_seconds(method)
        if cooldown:
            await self._sleep(cooldown)

    def _take_rpc(self, method: str) -> None:
        """Count one attempt against `max_rpc_per_run` (retries count too)."""
        if self._count >= self.settings.max_rpc_per_run:
            raise HardStop(
                f"max_rpc_per_run ({self.settings.max_rpc_per_run}) reached at {method!r}"
            )
        self._count += 1

    async def _sleep_with_heartbeat(self, method: str, total: int) -> None:
        """Sleep `total` seconds in chunks, logging INFO progress for long waits.

        Driven by a counter rather than the clock, so it behaves identically on
        a fake clock. No line is logged after the final chunk, and none at all
        for a wait shorter than one heartbeat.
        """
        remaining = total
        while remaining > 0:
            chunk = min(HEARTBEAT_SECONDS, remaining)
            await self._sleep(chunk)
            remaining -= chunk
            if total >= HEARTBEAT_SECONDS and remaining > 0:
                log.info(
                    "flood wait on %s: %ds remaining of %ds total", method, remaining, total
                )

    async def call(self, method: str, factory: Callable[[], Awaitable[T]]) -> T:
        """Run one RPC through the gate, retrying flood waits and transient errors.

        `factory` is re-invoked for every attempt, so it must build a fresh
        awaitable (and reset any streaming sink) each time.
        """
        floods = 0
        transients = 0
        attempt = 0
        while True:
            attempt += 1
            self._take_rpc(method)
            if attempt == 1:
                await self._pace(method)
            self._last_call[method] = self._clock.time()
            log.debug("rpc %s attempt %d (run call #%d)", method, attempt, self._count)

            try:
                return await factory()
            except Exception as exc:
                if is_flood_wait(exc):
                    floods += 1
                    await self._handle_flood(method, exc, floods)
                    self._last_call[method] = self._clock.time()
                    continue

                disposition = classify(exc, threshold=self.settings.flood_sleep_threshold)
                if disposition is not Disposition.RETRY:
                    raise self._to_exception(disposition, exc) from exc

                # A transient ConnectionError/TimeoutError/OSError: no
                # `.seconds`, so it never touches `flood_log`.
                transients += 1
                if transients > MAX_TRANSIENT_RETRIES:
                    raise PhaseStop(str(exc)) from exc
                delay = TRANSIENT_BACKOFF_SECONDS[transients - 1] * self._factor
                log.warning(
                    "transient error on %s (%s); retry attempt %d/%d after %gs",
                    method, type(exc).__name__, transients, MAX_TRANSIENT_RETRIES, delay,
                )
                await self._sleep(delay)

    async def _handle_flood(self, method: str, exc: BaseException, floods: int) -> None:
        """Record a FLOOD_WAIT, then either sleep it out or stop the phase.

        The cooldown is persisted (based on the *applied* wait) before the
        decision, so a later call to this method — this run or the next — waits
        it out via `_active_cooldown_seconds` even when we stop here. The
        ceiling is compared against the applied wait, not the raw seconds.
        """
        seconds = getattr(exc, "seconds", 0)
        applied = flood_applied_seconds(seconds)
        self._record_flood(method, seconds, applied)
        if applied > self.settings.flood_sleep_threshold:
            raise PhaseStop(
                f"FLOOD_WAIT {seconds}s (applied {applied}s) exceeds the "
                f"{self.settings.flood_sleep_threshold}s ceiling at {method!r}"
            ) from exc
        if floods > MAX_FLOOD_RETRIES:
            raise PhaseStop(
                f"FLOOD_WAIT on {method!r} repeated {floods} times in one call"
            ) from exc
        log.warning(
            "FLOOD_WAIT %ds on %s: waiting %ds; retry attempt %d/%d",
            seconds, method, applied, floods, MAX_FLOOD_RETRIES,
        )
        await self._sleep_with_heartbeat(method, applied)
