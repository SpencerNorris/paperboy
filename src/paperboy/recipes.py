"""`collect_channel`: the ordered-collectors recipe orchestrator.

Runs `channel` (which populates `CollectContext.input_channel`/`channel_id`/
`tier` for everything after it), then `history` (backfill, immediately
followed by one `pts` catch-up so the channel's sync state is current as of
*now*, not as of whenever backfill started — both folded into one `history`
CollectResult), then `web` (`t.me/s/` + Wayback CDX capture, pure HTTP — no
`Gateway`/`Budget` involvement). `SkipAndRecord` and `PhaseStop` are each
recorded and that phase's result is marked stopped, but later phases still
run; `HardStop` is recorded and the whole run ends there (spec §8). A
`run_events` row is written for every phase.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from paperboy.budget import HardStop, PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.web import WebCollector
from paperboy.ids import utc_now_iso
from paperboy.store.db import dumps

if TYPE_CHECKING:
    from paperboy.collectors.base import Collector
    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.store.db import Store
    from paperboy.targets import Target


def _default_collectors() -> list[Collector]:
    return [ChannelCollector(), HistoryCollector(), WebCollector()]


def _record_run_event(
    store: Store, channel_id: int | None, phase: str, kind: str, detail: dict | None
) -> None:
    store.conn.execute(
        "INSERT INTO run_events(observed_at, channel_id, phase, kind, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (utc_now_iso(), channel_id, phase, kind, dumps(detail) if detail is not None else None),
    )


def _merge_counts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    merged = dict(a)
    for k, v in b.items():
        merged[k] = merged.get(k, 0) + v
    return merged


async def _run_one(collector: Collector, ctx: CollectContext) -> CollectResult:
    """Run one collector's `collect()`, and its `catch_up()` too if it's a
    `HistoryCollector` — folded into a single result so the phase list stays
    one entry per collector, not per RPC pattern.
    """
    result = await collector.collect(ctx)
    if isinstance(collector, HistoryCollector):
        catchup_result = await collector.catch_up(ctx)
        result = CollectResult(
            name=result.name,
            counts=_merge_counts(result.counts, catchup_result.counts),
            stopped=catchup_result.stopped or result.stopped,
        )
    return result


async def collect_channel(
    gateway: Gateway,
    store: Store,
    settings: Settings,
    target: Target,
    phases: list[str] | None,
    log: logging.Logger,
    *,
    collectors: Sequence[Collector] | None = None,
) -> list[CollectResult]:
    """Run `channel` then `history` (+ its `catch_up`) against `target`.

    `phases` filters which collectors run by name (`None` runs all).
    `collectors` overrides the default `[ChannelCollector(), HistoryCollector()]`
    list — used by tests to inject a stub that raises `HardStop`/`PhaseStop`
    without needing a real gateway failure to trigger one.
    """
    ctx = CollectContext(gateway, store, settings, target, None, None, "stranger", log)
    active = collectors if collectors is not None else _default_collectors()
    selected = set(phases) if phases is not None else {c.name for c in active}

    results: list[CollectResult] = []
    for collector in active:
        if collector.name not in selected or not collector.applies_to(target):
            continue
        try:
            result = await _run_one(collector, ctx)
        except SkipAndRecord as exc:
            # Disposition.SKIP (e.g. ChannelPrivateError, ChatAdminRequiredError,
            # MsgIdInvalidError, BroadcastForbiddenError, PremiumAccountRequiredError):
            # skip this one collector, the run continues (spec §8) — this must
            # never abort the whole run, unlike PhaseStop/HardStop below.
            log.warning("phase %s skipped: %s", collector.name, exc)
            detail = {"error": str(exc)}
            _record_run_event(store, ctx.channel_id, collector.name, "skip", detail)
            results.append(CollectResult(name=collector.name, counts={}, stopped="skip"))
            continue
        except PhaseStop as exc:
            log.warning("phase %s stopped: %s", collector.name, exc)
            detail = {"error": str(exc)}
            _record_run_event(store, ctx.channel_id, collector.name, "phase_stop", detail)
            results.append(CollectResult(name=collector.name, counts={}, stopped="phase_stop"))
            continue
        except HardStop as exc:
            log.error("hard stop during %s: %s", collector.name, exc)
            detail = {"error": str(exc)}
            _record_run_event(store, ctx.channel_id, collector.name, "hard_stop", detail)
            results.append(CollectResult(name=collector.name, counts={}, stopped="hard_stop"))
            break
        else:
            _record_run_event(
                store, ctx.channel_id, collector.name, "complete",
                {"counts": result.counts, "stopped": result.stopped},
            )
            results.append(result)

    return results
