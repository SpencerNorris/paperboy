"""The observation clock (spec §5): where a projection's `observed_at` comes from.

Projections must be a pure function of the raw log, so their timestamps must
come from the observation being projected, not from the wall clock at
projection time. `LiveClock` IS the wall clock (a live collect observes now);
`ReplayClock` returns the ORIGINAL `observed_at` of the raw record being
replayed, fed by `RawReplayGateway`/`RawReplayWebClient` as they serve records.

Lookup is payload-keyed (canonical JSON of the served dict), because
collectors do not project in serve order: `history` consumes a whole
`iter_history` page into a list first, and `catch_up` projects messages
nested inside a `ChannelDifference` envelope.
"""

from __future__ import annotations

from typing import Protocol

from paperboy.ids import utc_now_iso
from paperboy.store.db import dumps


class ReplayClockError(Exception):
    """A replay projection asked for a timestamp before anything was served —
    a replay-wiring bug, never a data condition; fail loudly."""


class Clock(Protocol):
    def for_payload(self, payload: dict) -> str:
        """The `observed_at` for a projection derived from `payload`."""
        ...

    def now(self) -> str:
        """"Now" for a decision that has no payload of its own (e.g. the
        profiles refresh floor): the wall clock live, the last served record's
        stamp on replay — so the decision is reproducible from raw."""
        ...


class LiveClock:
    """Live collection: every observation happens now."""

    def for_payload(self, payload: dict) -> str:
        del payload
        return utc_now_iso()

    def now(self) -> str:
        return utc_now_iso()


class ReplayClock:
    """Replay: observations happened when the raw log says they did.

    `serve`/`serve_json` are called by the replay gateway per record served;
    `begin_batch` bounds the registry to one gateway response (the collector
    always projects a response fully before making the next call).
    A payload with no registered stamp (e.g. a dict nested in an envelope
    whose members were never individually recorded) inherits the most
    recently served record's stamp.
    """

    def __init__(self) -> None:
        self._current: str | None = None
        self._by_payload: dict[str, str] = {}

    def begin_batch(self) -> None:
        self._by_payload.clear()

    def serve(self, observed_at: str, *payloads: dict) -> None:
        self._current = observed_at
        for payload in payloads:
            self._by_payload[dumps(payload)] = observed_at

    def serve_json(self, observed_at: str, payload_json: str) -> None:
        """Register a record by its stored canonical JSON without re-parsing."""
        self._current = observed_at
        self._by_payload[payload_json] = observed_at

    def for_payload(self, payload: dict) -> str:
        stamp = self._by_payload.get(dumps(payload), self._current)
        if stamp is None:
            raise ReplayClockError(
                "ReplayClock.for_payload before any record was served"
            )
        return stamp

    def now(self) -> str:
        if self._current is None:
            raise ReplayClockError("ReplayClock.now before any record was served")
        return self._current
