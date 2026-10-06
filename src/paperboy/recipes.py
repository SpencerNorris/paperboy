"""`collect_channel`: the ordered-collectors recipe orchestrator.

Runs `channel` (which populates `CollectContext.input_channel`/`channel_id`/
`tier` for everything after it), then `history` (backfill, immediately
followed by one `pts` catch-up so the channel's sync state is current as of
*now*, not as of whenever backfill started — both folded into one `history`
CollectResult), then `discussion` (linked-group comment threads), then
`participants` (roster membership) and `profiles` (cheap `getUsers` triage
of every discovered person, full `getFullUser` enrichment only under
`--profiles`), then `graph` (similar-channel recommendations, entity-derived
mentions, invite-link previews, sponsored-message provenance — consumes
`history`'s stored messages / `channel`'s context). `media` (download +
content-address every stored message's media) and `web` (`t.me/s/` + Wayback
CDX capture over plain HTTP — no `Gateway`/`Budget`, a different trust
boundary) are OPT-IN — off by default, on via `collect_channel(..., media=True
/ web=True)` or by naming them in `phases`. `discussion`/`participants`/
`profiles`/`graph` all run by default; every phase after `channel` runs
after `history` so it has messages to walk.
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
from paperboy.collectors.base import ChannelContext, CollectContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.collectors.participants import ParticipantsCollector
from paperboy.collectors.profiles import ProfilesCollector
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
    # `participants` (roster) and `profiles` (cheap `getUsers` triage; full
    # enrichment only under `--profiles`) are default-on too: read-only and
    # bounded. `media` (heavy downloads) and `web` (external HTTP to
    # t.me/archive.org — a different trust boundary than the authenticated
    # MTProto session) are OPT-IN (--media / --web, or named in --phases).
    collectors: list[Collector] = [
        ChannelCollector(), HistoryCollector(), DiscussionCollector(),
        ParticipantsCollector(), ProfilesCollector(),
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


async def collect_channel_with_context(
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
    run_id: str | None = None,
    channel_context: ChannelContext | None = None,
) -> tuple[list[CollectResult], ChannelContext | None]:
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
    `run_id` (ADR-0005) marks every raw record this call writes as belonging
    to one collect pass — `None` (live callers) mints a fresh opaque id;
    `reproject` passes the SOURCE run's id so a reprojected DB carries the
    same pass boundaries and is itself re-reprojectable.

    `channel_context` (#68) is a channel this process already resolved: the
    `channel` phase is skipped (no `resolveUsername`/`getFullChannel`) and one
    `ChannelContextReused` raw marker (channel id + the establishing run's id,
    no access hash) is appended first, so `reproject` can rebuild the context
    for this otherwise channel-less run. Returns the results plus the
    `ChannelContext` this run ended with (`None` if `channel` did not complete),
    for the caller to reuse on the next segment.
    """
    store.begin_run(run_id)
    ctx = CollectContext(
        gateway, store, settings, target, None, None, "stranger", log, profile,
        clock or LiveClock(),
    )
    if channel_context is not None:
        ctx.input_channel = channel_context.input_channel
        ctx.channel_id = channel_context.channel_id
        ctx.tier = channel_context.tier
        marker = {
            "channel_id": channel_context.channel_id,
            "source_run_id": channel_context.source_run_id,
        }
        store.add_raw(
            "ChannelContextReused", marker, ctx.tier,
            {"channel_id": channel_context.channel_id},
            observed_at=ctx.clock.for_payload(marker),
        )
    include_media = media or (phases is not None and "media" in phases)
    include_web = web or (phases is not None and "web" in phases)
    active = collectors if collectors is not None else _default_collectors(
        include_media=include_media, include_web=include_web
    )
    if channel_context is not None:
        active = [c for c in active if c.name != "channel"]
    selected = set(phases) if phases is not None else {c.name for c in active}
    results: list[CollectResult] = []
    progress = Progress(store, log)
    progress.begin()
    try:
        for collector in active:
            if collector.name not in selected or not collector.applies_to(target):
                continue
            progress.start_phase(collector.name)
            if (
                collector.name == "media"
                and settings.media_msgs is not None
                and ctx.channel_id is not None
            ):
                # The media phase will walk only these ids, in the channel the
                # run established. Recording both keeps the raw log sufficient to
                # replay exactly the same rows (a reproject would otherwise walk
                # every stored media message and re-derive dedup custody rows the
                # live run never produced) and lets replay tell "access granted"
                # from "refused" (#68 spec 9.2-9.3). Written here, not up front,
                # because only now is the channel known.
                selection = {
                    "channel_id": ctx.channel_id,
                    "msg_ids": sorted(set(settings.media_msgs)),
                }
                store.add_raw(
                    "MediaSelection", selection, ctx.tier, None,
                    observed_at=ctx.clock.for_payload(selection),
                )
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
                results.append(
                    CollectResult(name=collector.name, counts={}, stopped="skip", stop_exc=exc)
                )
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
                    CollectResult(
                        name=collector.name, counts=stopped_counts, stopped="phase_stop",
                        stop_exc=exc,
                    )
                )
                continue
            except HardStop as exc:
                progress.end_phase(collector.name, None, stopped="hard_stop")
                log.error("hard stop during %s: %s", collector.name, exc)
                _record_run_event(
                    store, ctx.channel_id, collector.name, "hard_stop", {"error": str(exc)}
                )
                results.append(
                    CollectResult(
                        name=collector.name, counts={}, stopped="hard_stop", stop_exc=exc
                    )
                )
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

    established: ChannelContext | None = None
    if ctx.input_channel is not None and ctx.channel_id is not None:
        established = channel_context or ChannelContext(
            ctx.input_channel, ctx.channel_id, ctx.tier, store.run_id or ""
        )
    return results, established


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
    run_id: str | None = None,
    channel_context: ChannelContext | None = None,
) -> list[CollectResult]:
    """`collect_channel_with_context` without the returned context; see there."""
    results, _ = await collect_channel_with_context(
        gateway, store, settings, target, phases, log,
        collectors=collectors, media=media, web=web, profile=profile,
        clock=clock, run_id=run_id, channel_context=channel_context,
    )
    return results
