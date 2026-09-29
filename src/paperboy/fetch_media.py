"""`fetch-media`: download media for an ordered cross-channel list (#68).

The list is classified offline (`media_list.classify_rows`), the rows still
`pending` are grouped into `(priority, channel)` segments, and each segment is
one ordinary `collect_channel` pass over the `channel` + `media` collectors
with `media_msgs` narrowed to the segment's ids - so budget, guardrails,
dedup, custody, streaming and `--media-max-mb` all apply unchanged, and
`reproject` replays a segment as it would any other run (ADR-0005).

One gateway (one MTProto session, one `Budget`) serves the whole command. A
channel is resolved once: later segments of it reuse the `ChannelContext` and
append a `ChannelContextReused` marker instead of re-running `channel`.

Stop policy (docs/features/fetch-media.md):

* a `channel` phase that SKIPS (private channel, renamed handle, handle
  resolving to another channel) marks that channel's remaining rows
  `not_attempted` and the command continues with other channels;
* ANY `PhaseStop` - in the `channel` phase (e.g. a FLOOD_WAIT on
  `contacts.resolveUsername`) or the `media` phase (free-disk floor, a
  FLOOD_WAIT over the ceiling, a sink error, repeated failures) - or a
  `HardStop` ends the command. A persisted flood cooldown is slept
  unconditionally by the next RPC (`Budget._pace`), which would defeat
  `--max-flood-sleep` on the next channel, and disk/sink errors are not
  channel-specific.

Unreached rows are `not_attempted`; re-running resumes because finished rows
classify `already_stored`. The report is written in every case.
"""

from __future__ import annotations

import csv
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import ChannelContext, CollectContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.media import MediaCollector
from paperboy.media_list import ClassifiedRow, Segment, plan_segments
from paperboy.recipes import collect_channel_with_context
from paperboy.targets import parse_target

if TYPE_CHECKING:
    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.store.db import Store

REPORT_COLUMNS = ("line_no", "uri", "outcome", "sha256", "key")
# Outcomes for which the report names the stored file.
_HAS_FILE = frozenset({"downloaded", "duplicate", "already_stored"})


@dataclass
class RowResult:
    """The final outcome of one input row."""

    classified: ClassifiedRow
    outcome: str


@dataclass
class FetchSummary:
    results: list[RowResult]
    stop_reason: str | None = None
    bytes_downloaded: int = 0

    @property
    def counts(self) -> Counter[str]:
        return Counter(r.outcome for r in self.results)

    @property
    def complete(self) -> bool:
        """True iff every row reached a final outcome (exit code 0)."""
        return self.counts["not_attempted"] == 0


class _ExpectChannel:
    """The `channel` collector, then a guard that it resolved the channel the
    segment asked for. A renamed handle can resolve to a DIFFERENT channel;
    letting `media` run would fetch this segment's message ids from the wrong
    one, so a mismatch clears the context and skips the phase."""

    name = "channel"

    def __init__(self, expected_channel_id: int) -> None:
        self._inner = ChannelCollector()
        self._expected = expected_channel_id

    def applies_to(self, target) -> bool:
        return self._inner.applies_to(target)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        result = await self._inner.collect(ctx)
        if ctx.channel_id != self._expected:
            resolved = ctx.channel_id
            ctx.input_channel = None
            ctx.channel_id = None
            raise SkipAndRecord(
                f"handle resolved to channel {resolved}, expected {self._expected}; "
                "not fetching under the wrong channel"
            )
        return result


def initial_results(classified: list[ClassifiedRow]) -> list[RowResult]:
    """Offline outcomes as final; `pending` rows start as `not_attempted`."""
    return [
        RowResult(c, "not_attempted" if c.outcome == "pending" else c.outcome)
        for c in classified
    ]


async def _run_segments(
    gateway: Gateway,
    store: Store,
    settings: Settings,
    segments: list[Segment],
    results: list[RowResult],
    log: logging.Logger,
    profile: str,
) -> str | None:
    """Run the segments in order; fill `results`; return why the command
    ended early (`None` if it ran to the end)."""
    by_uri = {r.classified.uri: r for r in results if r.classified.outcome == "pending"}
    contexts: dict[int, ChannelContext] = {}
    dead_channels: set[int] = set()

    for i, seg in enumerate(segments, start=1):
        label = (
            f"fetch-media: segment {i}/{len(segments)} priority={seg.priority} "
            f"channel={seg.channel_id} rows={len(seg.rows)}"
        )
        if seg.channel_id in dead_channels:
            log.info("%s skipped: the channel stopped earlier in this run", label)
            continue
        log.info("%s start", label)
        outcomes: dict[str, str] = {}
        seg_settings = settings.model_copy(update={
                # The explicit list is the selection: a collect-era date window
                # must not silently filter list rows out (they would stay
                # not_attempted forever).
                "media_msgs": seg.msg_ids,
                "media_since": None,
            }
        )
        cached = contexts.get(seg.channel_id)
        phase_results, established = await collect_channel_with_context(
            gateway, store, seg_settings, parse_target(f"@{seg.username}"),
            ["channel", "media"], log,
            collectors=[_ExpectChannel(seg.channel_id), MediaCollector(outcomes=outcomes)],
            profile=profile, channel_context=cached,
        )
        for uri, outcome in outcomes.items():
            if uri in by_uri:
                by_uri[uri].outcome = outcome
        log.info(
            "%s end: %s", label, dict(Counter(outcomes.values())) or "no files considered"
        )

        by_name = {r.name: r for r in phase_results}
        channel_result = by_name.get("channel")
        media_result = by_name.get("media")
        if any(r.stopped == "hard_stop" for r in phase_results):
            return "hard_stop"
        if channel_result is not None and channel_result.stopped == "phase_stop":
            reason = type(channel_result.stop_exc).__name__
            log.warning("%s: channel phase stopped (%s); ending the command", label, reason)
            return f"channel phase_stop ({reason})"
        if channel_result is not None and channel_result.stopped is not None:
            dead_channels.add(seg.channel_id)
            log.warning(
                "%s: channel phase %s; its remaining rows stay not_attempted",
                label, channel_result.stopped,
            )
            continue
        if established is not None:
            contexts.setdefault(seg.channel_id, established)
        if media_result is not None and media_result.stopped == "phase_stop":
            reason = type(media_result.stop_exc).__name__
            log.warning("%s: media phase stopped (%s); ending the command", label, reason)
            return f"media phase_stop ({reason})"
    return None


def _stored_ref(store: Store, uri: str) -> tuple[str, str] | None:
    """`(sha256, key)` recorded for `uri`: its own `media` row, else its latest
    `custody_log` row."""
    row = store.conn.execute(
        "SELECT sha256, path FROM media WHERE message_uri = ?", (uri,)
    ).fetchone()
    if row is None:
        row = store.conn.execute(
            "SELECT sha256, path FROM custody_log WHERE source_message_uri = ? "
            "ORDER BY id DESC LIMIT 1",
            (uri,),
        ).fetchone()
    return (row["sha256"], row["path"]) if row else None


def write_report(path: Path, store: Store, results: list[RowResult]) -> None:
    """CSV `line_no,uri,outcome,sha256,key` for every input row, in list order."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            sha = key = ""
            if r.outcome in _HAS_FILE:
                ref = _stored_ref(store, r.classified.uri) or r.classified.stored
                if ref is not None:
                    sha, key = ref
            writer.writerow([r.classified.row.line_no, r.classified.uri, r.outcome, sha, key])


def _bytes_downloaded(store: Store, results: list[RowResult]) -> int:
    total = 0
    for r in results:
        if r.outcome == "downloaded":
            row = store.conn.execute(
                "SELECT size FROM media WHERE message_uri = ?", (r.classified.uri,)
            ).fetchone()
            total += row["size"] if row and row["size"] else 0
    return total


async def fetch_media(
    gateway: Gateway | None,
    store: Store,
    settings: Settings,
    classified: list[ClassifiedRow],
    log: logging.Logger,
    *,
    profile: str,
    report_path: Path,
) -> FetchSummary:
    """Run every pending segment and write the report (also on a stop or an
    unexpected error). `gateway` may be `None` only when nothing is pending."""
    results = initial_results(classified)
    segments = plan_segments(store, classified)
    summary = FetchSummary(results)
    try:
        if segments:
            if gateway is None:
                raise ValueError("fetch_media: pending rows need a gateway")
            summary.stop_reason = await _run_segments(
                gateway, store, settings, segments, results, log, profile
            )
    finally:
        summary.bytes_downloaded = _bytes_downloaded(store, results)
        write_report(report_path, store, results)
        log.info(
            "fetch-media: %s; %d bytes downloaded; report %s",
            dict(summary.counts), summary.bytes_downloaded, report_path.name,
        )
    return summary
