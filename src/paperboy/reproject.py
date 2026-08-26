"""The reproject recipe (spec §6): enumerate targets and phases from the raw
log, then run the NORMAL collectors against the replay pair into a fresh
store. Everything collector-shaped is reused; this module only wires."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from paperboy.clock import ReplayClock
from paperboy.collectors.base import CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.collectors.web import WebCollector
from paperboy.config import Settings
from paperboy.recipes import collect_channel
from paperboy.replay import RawReplayGateway, RawReplayWebClient, ReplayRun, ReplaySource
from paperboy.store.db import Store
from paperboy.targets import parse_target


class ReprojectError(Exception):
    """Operator-facing reproject failure (empty source, bad --out)."""


REPROJECT_TABLES = (
    "raw_records", "channels", "channel_snapshots", "peers", "messages",
    "message_revisions", "message_metrics", "message_tombstones", "edges",
    "media", "custody_log", "web_snapshots",
)


@dataclass
class ReprojectSummary:
    phases: list[str]
    results: dict[str, list[CollectResult]]
    table_counts: dict[str, tuple[int, int]]


def _reset_incremental_backfill_state(out_store: Store) -> None:
    """Clear `HistoryCollector`'s incremental-vs-full-sweep bookkeeping
    (`sync_state` scopes `history`/`history_sweep`) before replaying a run.

    Found running the R6 real-archive smoke (ADR-0005): `RawReplayGateway.
    iter_history` is scoped to ONE run's own raw_records window, so it
    naturally runs out ("no more pages") the moment that window is
    exhausted — a purely REPLAY artifact of the run boundary, not a
    Telegram-side fact. `history.py` reads that natural end as "reached the
    real end of the channel's history" and commits `backfill_complete=True`
    for a LIVE gateway that is correct (Telegram's history can only grow
    forward; a later session can never legitimately find OLDER messages
    than a completed full sweep already saw). Left uncleared across REPLAYED
    runs, that flag wrongly survives into the next run and switches its
    sweep to incremental-only (ids above the previous run's high-water
    mark) — silently dropping every one of that run's own, all-older
    messages, which is exactly the shape a real multi-session backward
    backfill takes. Resetting before every run costs nothing here (no
    network, no rate limit to economize on the way live incremental sync
    does) and makes each run independently sweep its own full raw window;
    idempotent projection upserts make any resulting re-processing of
    already-seen messages harmless.
    """
    out_store.conn.execute("DELETE FROM sync_state WHERE scope IN ('history', 'history_sweep')")


def detect_phases(source: ReplaySource, run: ReplayRun) -> list[str]:
    """The phase set ONE historical run executed, inferred from the raw kinds
    it left behind (spec §3: a run that never did graph reprojects without
    graph; ADR-0005: scoped to `run`, not the whole source — a source can mix
    runs with different phase sets). The inference is necessarily raw-only
    (spec §8) and conservative: a phase whose every RPC was skipped leaves no
    raw and is treated as never-run for that run; --phases overrides.
    """
    phases = ["channel", "history"]
    linked = source.linked_group_ids(run)
    if linked and source.has_context_channel(run, linked):
        phases.append("discussion")
    if source.has_kind(
        run, "chats", "chatsslice", "chatinvite", "chatinvitealready",
        "chatinvitepeek", "sponsoredmessage",
    ):
        phases.append("graph")
    if source.has_kind(run, "tme_page", "wayback_cdx"):
        phases.append("web")
    if source.has_kind(run, "mediadownload"):
        phases.append("media")
    return phases


async def reproject(
    source: ReplaySource,
    out_store: Store,
    settings: Settings,
    profile: str,
    phases: list[str] | None,
    log: logging.Logger,
) -> ReprojectSummary:
    """Replay every historical collect pass in the source, one run at a time
    (ADR-0005): each run gets its own `ReplayClock`/`RawReplayGateway`/
    `RawReplayWebClient` scoped to that run's raw_records rowid range, and
    stamps the target store with that run's own `run_id` — so a reprojected
    DB carries the same pass structure as its source and is itself
    faithfully re-reprojectable. `out_store` accumulates state across
    replayed runs exactly as the live store did across the real runs
    (`sync_state`, snapshot/metric time series, ...).
    """
    runs = source.runs()
    if not runs:
        raise ReprojectError("source raw log is empty — nothing to reproject")
    # allow_join=True so a source whose original run used --join replays its
    # discussion sweep; RawReplayGateway.join_channel is a synthetic no-op
    # (D4.3) — nothing is joined, nothing leaves this machine.
    replay_settings = settings.model_copy(update={"allow_join": True})

    results: dict[str, list[CollectResult]] = {}
    phases_seen: list[str] = []
    replayed_any = False
    for run in runs:
        _reset_incremental_backfill_state(out_store)
        run_phases = phases if phases is not None else detect_phases(source, run)
        for p in run_phases:
            if p not in phases_seen:
                phases_seen.append(p)
        for raw_target in source.resolve_targets(run):
            replayed_any = True
            clock = ReplayClock()
            gateway = RawReplayGateway(source, clock, run)
            web_client = RawReplayWebClient(source, clock, run)
            collectors = [
                ChannelCollector(), HistoryCollector(), DiscussionCollector(),
                GraphCollector(),
                WebCollector(client=web_client, min_interval=0.0, sleep=lambda s: None),
                MediaCollector(),
            ]
            try:
                run_results = await collect_channel(
                    gateway, out_store, replay_settings, parse_target(raw_target),
                    list(run_phases), log,
                    collectors=collectors, profile=profile, clock=clock,
                    run_id=run.run_id,
                )
            except Exception as exc:
                # collect_channel was designed for exactly one target per run
                # and has no notion of "this target, among several, turned
                # out bad" — e.g. a historically-resolved target that later
                # resolves to a non-channel peer crashes channel.collect
                # even on a live run (found exercising this against a real
                # archive — DoD smoke, docs/features/reproject.md). A
                # multi-target, multi-run reproject must not let one such
                # target/run discard every other target's or run's
                # projections already committed to out_store. The failure is
                # recorded, not swallowed.
                log.error(
                    "reproject: target %r (run %s) failed: %s",
                    raw_target, run.run_id, exc,
                )
                run_results = [
                    CollectResult(name="target", counts={}, stopped=f"error: {exc}")
                ]
            results.setdefault(raw_target, []).extend(run_results)
    if not replayed_any:
        raise ReprojectError(
            "source has no resolve records in raw_records — nothing to reproject"
        )

    counts = {
        t: (
            source.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
            out_store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
        )
        for t in REPROJECT_TABLES
    }
    return ReprojectSummary(phases_seen, results, counts)
