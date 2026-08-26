"""`collect_channel`: the ordered-collectors recipe orchestrator.

Runs `channel` (which populates `CollectContext.input_channel`/`channel_id`/
`tier` for everything after it), then `history` (backfill, immediately
followed by one `pts` catch-up so the channel's sync state is current as of
*now*, not as of whenever backfill started — both folded into one `history`
CollectResult), then `graph` (similar-channel recommendations, entity-derived
mentions, invite-link previews, sponsored-message provenance — consumes
`history`'s stored messages / `channel`'s context). `media` (download +
content-address every stored message's media) and `web` (`t.me/s/` + Wayback
CDX capture over plain HTTP — no `Gateway`/`Budget`, a different trust
boundary) are OPT-IN — off by default, on via `collect_channel(..., media=True
/ web=True)` or by naming them in `phases`. `graph` runs by default; all of
`graph`/`media`/`web` run after `history` so they have messages to walk.
`SkipAndRecord` and `PhaseStop` are each recorded and that phase's result is
marked stopped, but later phases still run; `HardStop` is recorded and the
whole run ends there (spec §8). A `run_events` row is written for every phase.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from paperboy.budget import HardStop, PhaseStop, SkipAndRecord
from paperboy.clock import LiveClock
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.collectors.web import WebCollector
from paperboy.progress import Progress
from paperboy.store.events import record_run_event

if TYPE_CHECKING:
    from paperboy.clock import Clock
    from paperboy.collectors.base import Collector
    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.store.db import Store
    from paperboy.targets import Target


def _default_collectors(*, include_media: bool, include_web: bool) -> list[Collector]:
    # The default set is all-MTProto and cheap-ish: channel + history + graph.
    # `media` (heavy downloads) and `web` (external HTTP to t.me/archive.org —
    # a different trust boundary than the authenticated MTProto session) are
    # OPT-IN (--media / --web, or named in --phases), so a plain `collect`
    # stays Telegram-only: metadata + history + graph.
    collectors: list[Collector] = [
        ChannelCollector(), HistoryCollector(), DiscussionCollector(),
        GraphCollector(),
    ]
    if include_web:
        collectors.append(WebCollector())
    if include_media:
        collectors.append(MediaCollector())
    return collectors


_record_run_event = record_run_event


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
        try:
            catchup_result = await collector.catch_up(ctx)
        except PhaseStop as exc:
            # catch_up now loops (issue #25), so it can PhaseStop mid-work — its
            # page budget, or a flood on a later page. The backfill above already
            # completed; its counts must still reach the phase report, or a
            # history phase that stored hundreds of messages reads as near-empty
            # (2a40754, and PhaseStop's own contract). Fold them into the stop.
            raise PhaseStop(
                str(exc), counts=_merge_counts(result.counts, exc.counts)
            ) from exc
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
    media: bool = False,
    web: bool = False,
    profile: str = "default",
    clock: Clock | None = None,
) -> list[CollectResult]:
    """Run `channel`, then `history` (+ its `catch_up`), then `graph`, against `target`.

    `phases` filters which collectors run by name (`None` runs all of the
    *active* set). `media` (or naming `"media"` in `phases`) opts the `media`
    collector into the active set — it's excluded by default (see
    `_default_collectors`). `profile` is threaded into `CollectContext` only
    for `media`'s content-addressed download path. `collectors` overrides the
    default active list entirely — used by tests to inject a stub that raises
    `HardStop`/`PhaseStop`/`SkipAndRecord` without needing a real gateway
    failure to trigger one. `clock` (default `LiveClock()`) is where every
    projection's `observed_at` comes from — `reproject` passes a `ReplayClock`
    fed by the replay gateway so timestamps are reproduced from raw (spec §5).
    """
    ctx = CollectContext(
        gateway, store, settings, target, None, None, "stranger", log, profile,
        clock or LiveClock(),
    )
    include_media = media or (phases is not None and "media" in phases)
    include_web = web or (phases is not None and "web" in phases)
    active = collectors if collectors is not None else _default_collectors(
        include_media=include_media, include_web=include_web
    )
    selected = set(phases) if phases is not None else {c.name for c in active}

    results: list[CollectResult] = []
    progress = Progress(store, log)
    progress.begin()
    try:
        for collector in active:
            if collector.name not in selected or not collector.applies_to(target):
                continue
            progress.start_phase(collector.name)
            try:
                result = await _run_one(collector, ctx)
            except SkipAndRecord as exc:
                # Disposition.SKIP (e.g. ChannelPrivateError, ChatAdminRequiredError,
                # MsgIdInvalidError, BroadcastForbiddenError, PremiumAccountRequiredError):
                # skip this one collector, the run continues (spec §8) — this must
                # never abort the whole run, unlike PhaseStop/HardStop below.
                progress.end_phase(collector.name, None, stopped="skip")
                log.warning("phase %s skipped: %s", collector.name, exc)
                _record_run_event(
                    store, ctx.channel_id, collector.name, "skip", {"error": str(exc)}
                )
                results.append(CollectResult(name=collector.name, counts={}, stopped="skip"))
                continue
            except PhaseStop as exc:
                # A stopped phase may still have done real work — a page-budget
                # stop is the normal outcome on a large target — so report what
                # it collected rather than a bare `{}`.
                stopped_counts = getattr(exc, "counts", {}) or {}
                progress.end_phase(collector.name, stopped_counts or None, stopped="phase_stop")
                log.warning("phase %s stopped: %s", collector.name, exc)
                _record_run_event(
                    store, ctx.channel_id, collector.name, "phase_stop",
                    {"error": str(exc), "counts": stopped_counts},
                )
                results.append(
                    CollectResult(name=collector.name, counts=stopped_counts, stopped="phase_stop")
                )
                continue
            except HardStop as exc:
                progress.end_phase(collector.name, None, stopped="hard_stop")
                log.error("hard stop during %s: %s", collector.name, exc)
                _record_run_event(
                    store, ctx.channel_id, collector.name, "hard_stop", {"error": str(exc)}
                )
                results.append(CollectResult(name=collector.name, counts={}, stopped="hard_stop"))
                break
            else:
                progress.end_phase(collector.name, result.counts)
                _record_run_event(
                    store, ctx.channel_id, collector.name, "complete",
                    {"counts": result.counts, "stopped": result.stopped},
                )
                results.append(result)
    finally:
        await progress.close()

    return results
