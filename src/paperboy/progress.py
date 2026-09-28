"""Live progress for a long `collect`: a console line when each phase starts and
finishes, plus a periodic heartbeat while a phase runs — so the terminal shows
the collect is moving (and roughly how far) instead of sitting silent until the
final table.

Everything here is advisory output on top of the existing structured log; it
reads counts from the store the phases are already writing to, and never affects
what is collected. The heartbeat runs as an asyncio task that only fires at the
`await` points a collector already yields at (network I/O), so it never races a
mid-statement write on the shared SQLite connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger

    from paperboy.store.db import Store

HEARTBEAT_SECONDS = 5.0


def human_bytes(n: int | None) -> str:
    """A compact human size — `140 MB`, `1.2 GB`."""
    x = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


def phase_status(store: Store, phase: str) -> str:
    """A one-line, phase-appropriate status derived from the store — the count
    of the table this phase is filling, so the heartbeat shows real movement.
    Best-effort: any query error yields an empty string rather than raising."""

    def count(sql: str) -> int:
        row = store.conn.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    try:
        if phase == "channel":
            return "resolving…"
        if phase == "history":
            return f"{count('SELECT count(*) FROM messages')} messages"
        if phase == "graph":
            return f"{count('SELECT count(*) FROM edges')} edges"
        if phase == "participants":
            return f"{count('SELECT count(*) FROM participants')} members"
        if phase == "profiles":
            enriched = count("SELECT count(*) FROM users WHERE enriched_at IS NOT NULL")
            return f"{count('SELECT count(*) FROM users')} users · {enriched} enriched"
        if phase == "media":
            files = count("SELECT count(*) FROM media")
            size = count("SELECT coalesce(sum(size), 0) FROM media")
            return f"{files} files · {human_bytes(size)}"
        if phase == "web":
            return f"{count('SELECT count(*) FROM web_snapshots')} snapshots"
    except Exception:  # noqa: BLE001 — heartbeat must never crash the run
        return ""
    return ""


class Progress:
    """Per-phase start/finish lines plus a heartbeat task.

    `begin()` starts the heartbeat; `start_phase`/`end_phase` bracket each phase;
    `close()` cancels the heartbeat. Wrap the phase loop in `try/finally: await
    close()` so the task is always torn down.
    """

    def __init__(self, store: Store, log: Logger, interval: float = HEARTBEAT_SECONDS) -> None:
        self._store = store
        self._log = log
        self._interval = interval
        self._phase: str | None = None
        self._started = 0.0
        self._task: asyncio.Task[None] | None = None

    def begin(self) -> None:
        self._task = asyncio.create_task(self._beat())

    def start_phase(self, name: str) -> None:
        self._phase = name
        self._started = time.monotonic()
        self._log.info("▶ %s", name)

    def end_phase(
        self, name: str, counts: dict[str, int] | None, stopped: str | None = None
    ) -> None:
        elapsed = time.monotonic() - self._started
        self._phase = None
        if stopped:
            self._log.info("⏹ %s · %s · %.0fs", name, stopped, elapsed)
            return
        detail = " ".join(f"{k}={v}" for k, v in (counts or {}).items()) or "—"
        self._log.info("✓ %s · %s · %.0fs", name, detail, elapsed)

    async def _beat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                phase = self._phase
                if phase is None:
                    continue
                elapsed = time.monotonic() - self._started
                status = phase_status(self._store, phase)
                self._log.info("  … %s · %s · %.0fs", phase, status, elapsed)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
