"""The reproject recipe (spec §6): enumerate targets and phases from the raw
log, then run the NORMAL collectors against the replay pair into a fresh
store. Everything collector-shaped is reused; this module only wires."""

from __future__ import annotations

import json
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
from paperboy.store.db import Store, dumps
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
    before replaying a run — `sync_state('history', ...)`'s resume cursor
    entirely, `sync_state('history_sweep', ...)`'s per-run-artifact flags
    only (see below).

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
    backfill takes. `history`'s resume cursor (`offset_id`) is entirely a
    REPLAY-run-scoped artifact too — a fresh full sweep must always restart
    each run's own window from its newest message, never resume from
    wherever a DIFFERENT run's window left its cursor — so that scope is
    always cleared outright.

    `history_sweep`'s `max_id_seen`/`pending_high` are NOT a replay
    artifact, though: they are the true high-water mark of the highest
    message id ever observed for the channel, and must keep monotonically
    widening across replayed runs exactly as they would across live
    sessions (a live multi-session backward backfill never resets them
    either — only a completed sweep's own two flags reset per run). Blanket-
    deleting the whole scope here — as an earlier revision did — collapsed
    the final reprojected `history_sweep` down to only the LAST run's own
    local window (found in review: a two-run backward backfill of ids
    851..1000 then 701..850 reprojected to `max_id_seen=850`, discarding the
    source's true 1000). Only the two per-run-artifact flags reset here; the
    high-water mark columns are read back unchanged and carried forward —
    idempotent projection upserts make any resulting re-processing of
    already-seen messages harmless regardless.
    """
    out_store.conn.execute("DELETE FROM sync_state WHERE scope = 'history'")
    for row in out_store.conn.execute(
        "SELECT key, value_json FROM sync_state WHERE scope = 'history_sweep'"
    ).fetchall():
        value = json.loads(row["value_json"])
        value["backfill_complete"] = False
        value["incremental_in_progress"] = False
        out_store.conn.execute(
            "UPDATE sync_state SET value_json = ? WHERE scope = 'history_sweep' AND key = ?",
            (dumps(value), row["key"]),
        )


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
        run_targets = source.resolve_targets(run)
        if not run_targets:
            # A run with no ResolvedPeer at all — no target to replay a
            # collect against, so every raw row in its window is silently
            # dropped from the reprojected DB. `replayed_any` below only
            # catches the case where NO run anywhere resolved a target;
            # without a per-run warning a single zero-target run in an
            # otherwise healthy source vanishes with no signal to the
            # operator (adversarial-reviewer, round 2).
            log.warning(
                "reproject: run %s (raw ids %d-%d) has no resolve records — "
                "skipping %d raw rows",
                run.run_id, run.lo, run.hi, run.hi - run.lo + 1,
            )
        for raw_target in run_targets:
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
