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
from paperboy.replay import RawReplayGateway, RawReplayWebClient, ReplaySource
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


def detect_phases(source: ReplaySource) -> list[str]:
    """The phase set the original run(s) executed, inferred from raw kinds
    (spec §3: a source that never ran graph reprojects without graph). The
    inference is necessarily raw-only (spec §8) and conservative: a phase
    whose every RPC was skipped leaves no raw and is treated as never-run;
    --phases overrides.
    """
    phases = ["channel", "history"]
    linked = source.linked_group_ids()
    if linked and source.has_context_channel(linked):
        phases.append("discussion")
    if source.has_kind(
        "chats", "chatsslice", "chatinvite", "chatinvitealready",
        "chatinvitepeek", "sponsoredmessage",
    ):
        phases.append("graph")
    if source.has_kind("tme_page", "wayback_cdx"):
        phases.append("web")
    if source.has_kind("mediadownload"):
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
    targets = source.resolve_targets()
    if not targets:
        raise ReprojectError(
            "source has no resolve records in raw_records — nothing to reproject"
        )
    active_phases = phases if phases is not None else detect_phases(source)
    # allow_join=True so a source whose original run used --join replays its
    # discussion sweep; RawReplayGateway.join_channel is a synthetic no-op
    # (D4.3) — nothing is joined, nothing leaves this machine.
    replay_settings = settings.model_copy(update={"allow_join": True})

    results: dict[str, list[CollectResult]] = {}
    for raw_target in targets:
        clock = ReplayClock()
        gateway = RawReplayGateway(source, clock)
        web_client = RawReplayWebClient(source, clock)
        collectors = [
            ChannelCollector(), HistoryCollector(), DiscussionCollector(),
            GraphCollector(),
            WebCollector(client=web_client, min_interval=0.0, sleep=lambda s: None),
            MediaCollector(),
        ]
        try:
            results[raw_target] = await collect_channel(
                gateway, out_store, replay_settings, parse_target(raw_target),
                list(active_phases), log,
                collectors=collectors, profile=profile, clock=clock,
            )
        except Exception as exc:
            # collect_channel was designed for exactly one target per run and
            # has no notion of "this target, among several, turned out bad" —
            # e.g. a historically-resolved target that later resolves to a
            # non-channel peer crashes channel.collect with a bare
            # ValueError even on a live run (found exercising this against a
            # real archive — DoD smoke, docs/features/reproject.md). A
            # multi-target reproject must not let one such target discard
            # every other target's projections already committed to
            # out_store this run. The failure is recorded, not swallowed.
            log.error("reproject: target %r failed: %s", raw_target, exc)
            results[raw_target] = [
                CollectResult(name="target", counts={}, stopped=f"error: {exc}")
            ]

    counts = {
        t: (
            source.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
            out_store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
        )
        for t in REPROJECT_TABLES
    }
    return ReprojectSummary(list(active_phases), results, counts)
