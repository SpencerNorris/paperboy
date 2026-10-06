"""The reproject recipe (spec §6): enumerate targets and phases from the raw
log, then run the NORMAL collectors against the replay pair into a fresh
store. Everything collector-shaped is reused; this module only wires."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

from paperboy.clock import ReplayClock
from paperboy.collectors.base import ChannelContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.collectors.participants import ParticipantsCollector
from paperboy.collectors.profiles import ProfilesCollector
from paperboy.collectors.web import WebCollector
from paperboy.config import Settings, profile_dir
from paperboy.recipes import collect_channel
from paperboy.replay import (
    RawReplayGateway,
    RawReplayWebClient,
    ReplayRun,
    ReplaySource,
    ReprojectSourceError,
    ResolveRecord,
    RunMarker,
)
from paperboy.store.db import Store, dumps
from paperboy.targets import Target, TargetKind, UnsupportedTarget, parse_target


class ReprojectError(Exception):
    """Operator-facing reproject failure (empty source, bad --out)."""


@dataclass(frozen=True)
class TargetFilter:
    """Which historical `(run, raw target)` pairs a reproject replays (#70).

    `ids` are RESOLVED channel ids, never spellings: `@x` and `x` are the same
    target. A linked discussion group needs no entry of its own; it is
    collected inside its parent's pair."""

    mode: Literal["include", "exclude"]
    ids: frozenset[int]

    def replays(self, channel_id: int) -> bool:
        return (channel_id in self.ids) == (self.mode == "include")

    def decide_run(self, records: list[ResolveRecord]) -> dict[str, bool]:
        """`{raw target: replay?}` for the pairs of ONE run.

        A pair that resolved to a channel is decided by `replays`. A pair with
        no channel id (it resolved to a user, a stray intrusion, ADR-0005)
        inherits the run's channel decisions: all replayed -> replayed, none
        -> dropped, a mixed run keeps it in BOTH outputs (the caller warns).
        A stray alone in its run has no channel to follow: it replays under
        `exclude` (as an unfiltered reproject would) and not under `include`."""
        channel_decisions = {
            r.raw_target: self.replays(r.channel_id)
            for r in records if r.channel_id is not None
        }
        followed = set(channel_decisions.values())
        if len(followed) == 1:
            stray = followed.pop()
        elif followed:  # mixed run: kept in both outputs
            stray = True
        else:  # no channel pair in the run to follow
            stray = self.mode == "exclude"
        return {
            r.raw_target: channel_decisions[r.raw_target] if r.channel_id is not None else stray
            for r in records
        }


def is_mixed_run(filter_: TargetFilter, records: list[ResolveRecord]) -> bool:
    """A run whose channel pairs are split between the outputs AND that holds a
    stray (non-channel) pair, which then lands in both."""
    channel = {filter_.replays(r.channel_id) for r in records if r.channel_id is not None}
    return len(channel) > 1 and any(r.channel_id is None for r in records)


def _describe_source_targets(source: ReplaySource, records: list[ResolveRecord]) -> str:
    channels: dict[int, str | None] = {}
    for r in records:
        if r.channel_id is not None:
            channels[r.channel_id] = r.username or channels.get(r.channel_id)
    linked = source.linked_group_map()
    parts = []
    for cid, username in channels.items():
        label = f"@{username} ({cid})" if username else f"channel {cid}"
        if cid in linked:
            label += f" [+ linked group {linked[cid]}]"
        parts.append(label)
    non_channel = sum(1 for r in records if r.channel_id is None)
    return (
        "this source contains: " + (", ".join(parts) or "no channels")
        + f"; {non_channel} resolve(s) to non-channel peers"
    )


def resolve_target_filter(
    source: ReplaySource, include: list[str], exclude: list[str]
) -> TargetFilter | None:
    """Turn `--include-target`/`--exclude-target` values into a `TargetFilter`
    over resolved channel ids, or None when neither was given.

    Accepted: `@username`, `username`, `t.me/username` (case-insensitive,
    matched against the resolved channel's username AND the spelling the
    target was originally collected with) or a bare channel id. Anything the
    source never resolved to a channel is an error listing what it contains."""
    if include and exclude:
        raise ReprojectError("--include-target and --exclude-target are mutually exclusive")
    specs = include or exclude
    if not specs:
        return None
    records = source.resolve_catalogue()
    ids: set[int] = set()
    for spec in specs:
        try:
            wanted = parse_target(spec)
        except UnsupportedTarget as exc:
            raise ReprojectError(f"cannot use {spec!r} as a target: {exc}") from None
        if wanted.kind not in (TargetKind.USERNAME, TargetKind.PEER_ID):
            raise ReprojectError(
                f"cannot use {spec!r} as a target: only @username or a channel id"
            )
        matched = {
            r.channel_id for r in records
            if r.channel_id is not None and _record_matches(r, wanted)
        }
        if not matched:
            raise ReprojectError(
                f"unknown target {spec!r}; {_describe_source_targets(source, records)}"
            )
        ids |= matched
    return TargetFilter("include" if include else "exclude", frozenset(ids))


def _record_matches(record: ResolveRecord, wanted: Target) -> bool:
    if wanted.kind is TargetKind.PEER_ID:
        return record.channel_id == int(wanted.value)
    name = wanted.value.lower()
    if record.username and record.username.lower() == name:
        return True
    try:
        original = parse_target(record.raw_target)
    except UnsupportedTarget:
        return False
    return original.kind is TargetKind.USERNAME and original.value.lower() == name


REPROJECT_TABLES = (
    "raw_records", "channels", "channel_snapshots", "peers", "messages",
    "message_revisions", "message_metrics", "message_tombstones", "edges",
    "media", "custody_log", "web_snapshots",
    "users", "user_snapshots", "user_photos", "participants", "participant_snapshots",
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


def _pin_selection(
    clock: ReplayClock, selection: RunMarker | None, channel_id: int | None
) -> None:
    """Give the replayed `MediaSelection` its recorded stamp. A legacy selection
    (`{msg_ids}`, recorded before it named its channel) is replayed in the
    current shape, so its stamp is also pinned under that shape's payload."""
    if selection is None:
        return
    clock.pin_json(selection.observed_at, selection.payload_json)
    if "channel_id" not in selection.payload and channel_id is not None:
        current = {
            "channel_id": channel_id,
            "msg_ids": sorted(set(selection.payload.get("msg_ids") or [])),
        }
        clock.pin_json(selection.observed_at, dumps(current))


def _pin_store_marker(clock: ReplayClock, marker: RunMarker | None) -> None:
    """Give the replayed `MediaStore` marker (#63) its recorded stamp."""
    if marker is not None:
        clock.pin_json(marker.observed_at, marker.payload_json)


def detect_phases(source: ReplaySource, run: ReplayRun) -> list[str]:
    """The phase set ONE historical run executed, inferred from the raw kinds
    it left behind (spec §3: a run that never did graph reprojects without
    graph; ADR-0005: scoped to `run`, not the whole source — a source can mix
    runs with different phase sets). The inference is necessarily raw-only
    (spec §8) and conservative: a phase whose every RPC was skipped leaves no
    raw and is treated as never-run for that run; --phases overrides.
    """
    if source.context_markers(run):
        # A fetch-media segment that reused a resolved channel (#68): no
        # channel or history phase ran, only media.
        return ["media"]
    phases = ["channel"]
    if source.has_history_evidence(run):
        phases.append("history")
    linked = source.linked_group_ids(run)
    if linked and source.has_context_channel(run, linked):
        phases.append("discussion")
    if source.has_kind(
        run, "channels.channelparticipants", "channels.channelparticipant", "rosterwalled",
        "usernotparticipant", "messages.messagereactionslist",
    ):
        phases.append("participants")
    if source.has_context_value(run, "method", "users.getUsers") \
            or source.has_kind(run, "users.userfull"):
        phases.append("profiles")
    if source.has_kind(
        run, "chats", "chatsslice", "chatinvite", "chatinvitealready",
        "chatinvitepeek", "sponsoredmessage",
    ):
        phases.append("graph")
    if source.has_kind(run, "tme_page", "wayback_cdx"):
        phases.append("web")
    selection = source.media_selection(run)
    if source.has_kind(run, "mediadownload") or source.media_store(run) is not None or (
        selection is not None
        and source.channel_established(run, selection.payload.get("channel_id"))
    ):
        phases.append("media")
    return phases


async def reproject(
    source: ReplaySource,
    out_store: Store,
    settings: Settings,
    profile: str,
    phases: list[str] | None,
    log: logging.Logger,
    *,
    target_filter: TargetFilter | None = None,
    out_profile: str | None = None,
) -> ReprojectSummary:
    """Replay every historical collect pass in the source, one run at a time
    (ADR-0005): each run gets its own `ReplayClock`/`RawReplayGateway`/
    `RawReplayWebClient` scoped to that run's raw_records rowid range, and
    stamps the target store with that run's own `run_id` — so a reprojected
    DB carries the same pass structure as its source and is itself
    faithfully re-reprojectable. `out_store` accumulates state across
    replayed runs exactly as the live store did across the real runs
    (`sync_state`, snapshot/metric time series, ...).

    `target_filter` (#70) restricts the replay to the `(run, raw target)`
    pairs it selects; the rest are not replayed at all, so the output holds no
    raw rows, projections or media for them.

    `out_profile` (#70) names the profile the output store lives in: the media
    phase then COPIES each referenced file from the source profile into
    `<data_dir>/<out_profile>/media/` (the only case replay writes files, and
    never under the source profile). Without it media is only re-hashed.
    """
    runs = source.runs()
    if not runs:
        raise ReprojectError("source raw log is empty — nothing to reproject")
    if out_profile is not None and (
        profile_dir(settings, out_profile).resolve() == source.profile_root.resolve()
    ):
        raise ReprojectError(
            f"--out-profile {out_profile!r} is the source profile itself; "
            "a reproject never writes into its source"
        )
    media_profile = out_profile if out_profile is not None else profile

    decisions: dict[str, dict[str, bool]] = {}
    records_by_run: dict[str, list[ResolveRecord]] = {}
    catalogue: list[ResolveRecord] | None = None
    if target_filter is not None:
        catalogue = source.resolve_catalogue()
        for rec in catalogue:
            records_by_run.setdefault(rec.run_id, []).append(rec)
        decisions = {rid: target_filter.decide_run(recs) for rid, recs in records_by_run.items()}
    pairs_replayed = pairs_skipped = runs_touched = 0

    results: dict[str, list[CollectResult]] = {}
    phases_seen: list[str] = []
    replayed_any = False
    for run in runs:
        run_decisions = decisions.get(run.run_id)
        if target_filter is not None and run_decisions is not None:
            recs = records_by_run[run.run_id]
            for rec in recs:
                log.info(
                    "reproject: run=%s target=%s channel_id=%s decision=%s",
                    run.run_id, rec.raw_target,
                    rec.channel_id if rec.channel_id is not None else "none",
                    "included" if run_decisions[rec.raw_target] else "excluded",
                )
            if is_mixed_run(target_filter, recs):
                log.warning(
                    "reproject: run %s mixes channel targets that land in different outputs; "
                    "its non-channel (stray) resolve is replayed in BOTH",
                    run.run_id,
                )
            kept = sum(run_decisions.values())
            pairs_replayed += kept
            pairs_skipped += len(recs) - kept
            if not kept:
                continue  # no index build, no replay for a wholly excluded run
            runs_touched += 1
        _reset_incremental_backfill_state(out_store)
        # Per-run replay settings (plan D6): allow_join=True so a source whose
        # original run used --join replays its discussion sweep
        # (RawReplayGateway.join_channel is a synthetic no-op, D4.3 — nothing
        # is joined, nothing leaves this machine); unsafe=True because the
        # session-age gate has no RPC to protect on replay; enrich_profiles
        # follows THIS run's own raw (a --profiles original replays its
        # UserFull records, a triage-only original replays triage-only,
        # warning included); and the three person-layer budgets are lifted to
        # effectively unlimited — the live budget already bounded what was
        # RECORDED, so a smaller replay budget would silently drop recorded
        # observations past the cut (replay walks the same deterministic
        # candidate order and serves every recorded observation; an
        # unrecorded candidate is a cheap offline SkipAndRecord that projects
        # nothing).
        replay_settings = settings.model_copy(update={
            "allow_join": True, "unsafe": True,
            "enrich_profiles": source.has_kind(run, "users.userfull"),
            "profile_budget": 10**9, "participant_oracle_budget": 10**9,
            "participant_reactions_budget": 10**9,
        })
        selection = source.media_selection(run)
        if selection is not None:
            # The live media phase walked only these messages (#55/#68); walk
            # the same ones so dedup custody rows are not re-derived for
            # messages that run never considered.
            replay_settings = replay_settings.model_copy(
                update={"media_msgs": list(selection.payload.get("msg_ids") or [])}
            )
        # A bucket run's `MediaStore` marker names the store its custody rows and
        # receipts refer to (#63). A copy into another profile (`--out-profile`)
        # lands in that LOCAL profile, so it replays as a local run; a plain
        # reproject keeps the recorded store. The reproject's own env/CLI store is
        # never used for replay.
        store_marker = source.media_store(run)
        replay_store = None
        if store_marker is not None and out_profile is None:
            replay_store = store_marker.payload.get("store")
        replay_settings = replay_settings.model_copy(update={"media_store": replay_store})
        run_phases = phases if phases is not None else detect_phases(source, run)
        for p in run_phases:
            if p not in phases_seen:
                phases_seen.append(p)
        run_targets = source.resolve_targets(run)
        markers = source.context_markers(run)
        if not run_targets and markers:
            # A media-only fetch-media segment (#68): its channel was resolved
            # by an earlier run, so it has no resolve records of its own.
            if len(markers) > 1:
                raise ReprojectSourceError(
                    f"run {run.run_id} holds {len(markers)} ChannelContextReused markers; "
                    "a fetch-media segment writes exactly one"
                )
            marker = markers[0]
            channel_id = marker.payload.get("channel_id")
            source_run_id = marker.payload.get("source_run_id")
            if not isinstance(channel_id, int) or not isinstance(source_run_id, str):
                raise ReprojectSourceError(
                    f"run {run.run_id}: malformed ChannelContextReused marker"
                )
            if target_filter is not None:
                keep = target_filter.replays(channel_id)
                pairs_replayed += keep
                pairs_skipped += not keep
                log.info(
                    "reproject: run=%s marker channel_id=%s decision=%s",
                    run.run_id, channel_id, "included" if keep else "excluded",
                )
                if not keep:
                    continue
                runs_touched += 1
            replayed_any = True
            access_hash = source.resolved_access_hash(source_run_id, channel_id)
            if catalogue is None:
                catalogue = source.resolve_catalogue()
            catalogued = next(
                (
                    rec for rec in catalogue
                    if rec.run_id == source_run_id and rec.channel_id == channel_id
                ),
                None,
            )
            if catalogued is None:
                raise ReprojectSourceError(
                    f"run {run.run_id}: source run {source_run_id!r} has no resolve of "
                    f"channel {channel_id}"
                )
            clock = ReplayClock()
            clock.pin_json(marker.observed_at, marker.payload_json)
            _pin_selection(clock, selection, channel_id)
            if replay_store is not None:
                _pin_store_marker(clock, store_marker)
            context = ChannelContext(
                {"channel_id": channel_id, "access_hash": access_hash}, channel_id,
                marker.tier, source_run_id,
            )
            results.setdefault(catalogued.raw_target, []).extend(
                await _replay_one(
                    source, out_store, replay_settings, media_profile, run, list(run_phases),
                    catalogued.raw_target, clock, log,
                    out_profile=out_profile, channel_context=context,
                )
            )
            continue
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
            if run_decisions is not None and not run_decisions[str(raw_target)]:
                continue  # logged above; belongs to the other output
            replayed_any = True
            clock = ReplayClock()
            established = source.established_channel_ids(run)
            _pin_selection(clock, selection, established[0] if established else None)
            if replay_store is not None:
                _pin_store_marker(clock, store_marker)
            results.setdefault(raw_target, []).extend(
                await _replay_one(
                    source, out_store, replay_settings, media_profile, run, list(run_phases),
                    raw_target, clock, log, out_profile=out_profile,
                )
            )
    if target_filter is not None:
        log.info(
            "reproject: targets replayed=%d skipped=%d runs_touched=%d filter=%s ids=%s",
            pairs_replayed, pairs_skipped, runs_touched, target_filter.mode,
            sorted(target_filter.ids),
        )
        if not replayed_any:
            raise ReprojectError(
                f"--{target_filter.mode}-target leaves nothing to replay: every "
                "(run, target) pair in the source is filtered out"
            )
    if not replayed_any:
        raise ReprojectError(
            "source has no resolve records in raw_records — nothing to reproject"
        )

    counts = {
        t: (
            _table_count(source.conn, t),
            out_store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
        )
        for t in REPROJECT_TABLES
    }
    return ReprojectSummary(phases_seen, results, counts)


async def _replay_one(
    source: ReplaySource,
    out_store: Store,
    replay_settings: Settings,
    media_profile: str,
    run: ReplayRun,
    run_phases: list[str],
    raw_target: str,
    clock: ReplayClock,
    log: logging.Logger,
    *,
    out_profile: str | None,
    channel_context: ChannelContext | None = None,
) -> list[CollectResult]:
    """Replay ONE `(run, raw target)` pair through the normal collectors.

    `channel_context` is set for a media-only fetch-media segment (#68): the
    recipe then skips `channel` and rewrites the run's marker.
    """
    gateway = RawReplayGateway(
        source, clock, run, allowed_buckets=replay_settings.media_store_bucket_set
    )
    web_client = RawReplayWebClient(source, clock, run)
    collectors = [
        ChannelCollector(), HistoryCollector(), DiscussionCollector(),
        ParticipantsCollector(), ProfilesCollector(copy_on_replay=out_profile is not None),
        GraphCollector(),
        WebCollector(client=web_client, min_interval=0.0, sleep=lambda s: None),
        MediaCollector(copy_on_replay=out_profile is not None),
    ]
    try:
        return await collect_channel(
            gateway, out_store, replay_settings, parse_target(raw_target),
            list(run_phases), log,
            collectors=collectors, profile=media_profile, clock=clock,
            run_id=run.run_id, channel_context=channel_context,
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
        return [CollectResult(name="target", counts={}, stopped=f"error: {exc}")]


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    """Row count of `table` in a SOURCE connection, or 0 if the table doesn't
    exist there yet. The source is strictly read-only (never migrated —
    `ReplaySource`'s own docstring), so an archive captured before a table's
    migration landed (e.g. a pre-person-layer archive has no `users`) must
    read as 0 rows, not crash this purely-diagnostic summary — the same
    schema-evolution tolerance `ReplaySource.__init__`'s `_has_run_id` check
    already applies to `raw_records.run_id`."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None
    if not exists:
        return 0
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
