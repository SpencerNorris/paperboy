"""`fetch-from-list`: fetch the posts of an ordered cross-channel list, then
their media (#68, extended by #91).

The list is classified offline (`media_list.classify_rows`), the rows still
`pending` are grouped into `(priority, channel)` segments, and each segment is
one ordinary `collect_channel` pass over the `channel`, `posts` and `media`
collectors: `posts` fetches EVERY listed id of the segment by
`channels.getMessages` and projects it through the same code `history` uses
(already-stored posts are re-fetched, so edits and counters are current), and
`media` then walks the ids whose file the run's store does not hold yet - so
budget, guardrails, dedup, custody, streaming and `--media-max-mb` all apply
unchanged, and `reproject` replays a segment as it would any other run
(ADR-0005).

One gateway (one MTProto session, one `Budget`) serves the whole command. A
segment targets its channel BY ID (`parse_target(str(channel_id))`), so the
standard `channel` collector reaches it through the #84 access routes (saved
key, from-message, verified stored handle) and records a `ChannelAccess`
receipt. The one exception is a handle row whose channel the store has never
seen: it has no id to address, so the segment targets the handle and the
channel is resolved live, through the same phase (route `handle`, recorded as a
receipt). A channel is established once: later segments of it reuse the
`ChannelContext` and append a `ChannelContextReused` marker instead of
re-running `channel`.

Stop policy (docs/features/fetch-from-list.md):

* a `channel` or `posts` phase that SKIPS (no route to the channel, access
  refused, private) marks that segment's rows AND the channel's later
  segments' `no_access` (the routes come from the store, which does not change
  within one command, so there is nothing to retry) and the command continues
  with other channels;
* ANY `PhaseStop` - in `channel` (e.g. a FLOOD_WAIT on
  `contacts.resolveUsername`), `posts`, or `media` (free-disk floor, a
  FLOOD_WAIT over the ceiling, a sink error, repeated failures) - or a
  `HardStop` ends the command. A persisted flood cooldown is slept
  unconditionally by the next RPC (`Budget._pace`), which would defeat
  `--max-flood-sleep` on the next channel, and disk/sink errors are not
  channel-specific.

Unreached rows are `not_attempted`; re-running resumes (posts are fetched
again, media already in the run's store is not). The report is written in
every case.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.collectors.base import ChannelContext, CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.media import (
    DOWNLOADABLE_KINDS,
    MediaCollector,
    content_key,
    content_key_text,
    load_content_index,
)
from paperboy.collectors.posts import PostsCollector
from paperboy.ids import msg_uri
from paperboy.media_list import (
    ClassifiedRow,
    Segment,
    held_file,
    linked_discussion_groups,
    plan_segments,
)
from paperboy.media_store import MediaStore, MediaStoreError, build_media_store
from paperboy.recipes import collect_channel_with_context
from paperboy.targets import parse_target

if TYPE_CHECKING:
    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.store.db import Store

REPORT_COLUMNS = ("line_no", "uri", "outcome", "post", "sha256", "key", "reason")
EXCLUDED_REASON = "channel excluded by --exclude-target"
# Outcomes for which the report names the stored file.
_HAS_FILE = frozenset({"downloaded", "duplicate", "already_stored"})


@dataclass
class RowResult:
    """The final outcome of one input row. `post` is what the posts step did:
    `fetched`, `deleted_upstream` (Telegram answered `MessageEmpty`) or
    `skipped` (not reached, or the channel was refused)."""

    classified: ClassifiedRow
    outcome: str
    reason: str = ""
    post: str = "skipped"


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


def initial_results(classified: list[ClassifiedRow]) -> list[RowResult]:
    """Offline outcomes as final; `pending` rows start as `not_attempted`."""
    return [
        RowResult(
            c,
            "not_attempted" if c.outcome == "pending" else c.outcome,
            EXCLUDED_REASON if c.outcome == "excluded" else "",
        )
        for c in classified
    ]


def _mark_no_access(rows: list[RowResult], reason: str) -> None:
    """`no_access` for every row of a refused segment that no phase finished."""
    for r in rows:
        if r.outcome == "not_attempted":
            r.outcome, r.reason = "no_access", reason


def _adopt_channel(rows: list[RowResult], channel_id: int) -> None:
    """After a handle segment resolved: give its rows the `tg:msg:` uri of the
    channel the handle resolved to (taken from the run's `ChannelContext`, never
    from a username lookup - a channel may be reached by any of its handles), so
    the outcome dicts and the report speak about the same message."""
    for r in rows:
        r.classified.channel_id = channel_id
        r.classified.uri = msg_uri(channel_id, r.classified.row.msg_id)


def _excludes(store: Store, excluded: set[int], channel_id: int) -> bool:
    """Whether `channel_id` is covered by `--exclude-target`: named itself, or the
    linked discussion group of an excluded channel (a group follows its parent,
    never the reverse; either `linked_group` edge direction, #70 -
    `linked_discussion_groups`). Reads the store's edges, so it is accurate once
    the `channel` phase has recorded them; a newly excluded channel's own group
    joins `excluded`, so later segments of the group are caught too."""
    if channel_id not in excluded:
        if not any(channel_id in linked_discussion_groups(store, p) for p in excluded):
            return False
        excluded.add(channel_id)
    excluded |= linked_discussion_groups(store, channel_id)
    return True


def _stored_media_kind(store: Store, uri: str) -> str | None:
    """`media_kind` of the stored message, `None` if absent."""
    row = store.conn.execute(
        "SELECT media_kind FROM messages WHERE uri = ?", (uri,)
    ).fetchone()
    return row["media_kind"] if row else None


def _settle(
    store: Store,
    r: RowResult,
    post: str | None,
    media: str | None,
    *,
    with_media: bool,
) -> None:
    """Give one row its final outcome after its segment ran. Precedence: the
    media phase's own outcome, `deleted_upstream` (Telegram's current answer
    wins over what the store holds), `already_stored` (this run's store held the
    file), `post_only` (`--no-media`), `no_media`; a row no phase reached stays
    `not_attempted`."""
    if post is not None:
        r.post = post
    if media is not None:
        r.outcome = media
    elif post is None:
        return
    elif post == "deleted_upstream":
        r.outcome = "deleted_upstream"
    elif r.classified.media_held:
        r.outcome = "already_stored"
    elif not with_media:
        r.outcome = "post_only"
    elif (_stored_media_kind(store, r.classified.uri) or "").lower() not in DOWNLOADABLE_KINDS:
        r.outcome = "no_media"
    # else: downloadable but the media phase did not reach it -> not_attempted


def _stop_check(
    phase_results: list[CollectResult], phases: tuple[str, ...], label: str,
    log: logging.Logger,
) -> tuple[str, str] | None:
    """Apply the stop policy to the phases just run. `("end", why)` ends the
    command; `("refused", why)` marks the channel `no_access`; `None` carries on."""
    by_name = {r.name: r for r in phase_results}
    if any(r.stopped == "hard_stop" for r in phase_results):
        return "end", "hard_stop"
    for phase in phases:
        result = by_name.get(phase)
        if result is None or result.stopped is None:
            continue
        if result.stopped == "phase_stop":
            reason = type(result.stop_exc).__name__
            log.warning("%s: %s phase stopped (%s); ending the command", label, phase, reason)
            return "end", f"{phase} phase_stop ({reason})"
        reason = str(result.stop_exc)
        log.warning(
            "%s: %s phase %s (%s); its rows are no_access", label, phase, result.stopped, reason
        )
        return "refused", reason
    return None


async def _run_segments(
    gateway: Gateway,
    store: Store,
    settings: Settings,
    segments: list[Segment],
    results: list[RowResult],
    log: logging.Logger,
    profile: str,
    *,
    with_media: bool,
    excluded_ids: frozenset[int] = frozenset(),
) -> str | None:
    """Run the segments in order; fill `results`; return why the command
    ended early (`None` if it ran to the end)."""
    by_uri = {r.classified.uri: r for r in results if r.classified.outcome == "pending"}
    contexts: dict[int | str, ChannelContext] = {}
    dead_channels: dict[int | str, str] = {}
    # `@handle -> channel id` for every handle this command has resolved: the
    # same unknown handle under another priority must not resolve again
    # (`contacts.resolveUsername` is flood-limited, and a channel is established
    # once per command).
    resolved_handles: dict[str, int] = {}
    media_store = build_media_store(settings, profile) if with_media else None
    excluded = set(excluded_ids)
    # (channel_id, msg_id) of every row already given to a segment: a row that
    # names the same message through another form (a handle and a `tg:msg:`) is
    # only known to be a duplicate once the handle has resolved.
    claimed: set[tuple[int, int]] = set()

    for i, seg in enumerate(segments, start=1):
        seg_rows = [by_uri[c.uri] for c in seg.rows if c.uri in by_uri]
        address = seg.address  # the handle form, before a handle resolves
        label = (
            f"fetch-from-list: segment {i}/{len(segments)} priority={seg.priority} "
            f"channel={address} rows={len(seg.rows)}"
        )
        if address in dead_channels:
            log.info("%s: no_access (the channel was refused earlier in this run)", label)
            _mark_no_access(seg_rows, dead_channels[address])
            continue

        if seg.channel_id is None and str(address) in resolved_handles:
            seg.channel_id = resolved_handles[str(address)]
            _adopt_channel(seg_rows, seg.channel_id)
            log.info("%s: handle already resolved to channel %s", label, seg.channel_id)
        if seg.channel_id is None:
            # A handle row: the channel has no known id, so establish it ALONE
            # first (route `handle`, a receipt), take its id from the resulting
            # context, and only then decide exclusion and what to fetch.
            log.info("%s: resolving the handle", label)
            phase_results, resolved = await collect_channel_with_context(
                gateway, store, settings, parse_target(str(address)), ["channel"], log,
                collectors=[ChannelCollector()], profile=profile,
            )
            stop = _stop_check(phase_results, ("channel",), label, log)
            if stop is not None and stop[0] == "end":
                return stop[1]
            if stop is not None or resolved is None:
                reason = stop[1] if stop else "the handle did not resolve"
                dead_channels[address] = reason
                _mark_no_access(seg_rows, reason)
                continue
            seg.channel_id = resolved.channel_id
            _adopt_channel(seg_rows, resolved.channel_id)
            contexts[resolved.channel_id] = resolved
            resolved_handles[str(address)] = resolved.channel_id

        cid = seg.channel_id
        assert cid is not None
        if cid in dead_channels:
            _mark_no_access(seg_rows, dead_channels[cid])
            continue
        if _excludes(store, excluded, cid):
            log.info("%s: channel %s is excluded by --exclude-target", label, cid)
            for r in seg_rows:
                if r.outcome == "not_attempted":
                    r.outcome, r.reason = "excluded", EXCLUDED_REASON
            continue
        for r in list(seg_rows):
            key = (cid, r.classified.row.msg_id)
            if key in claimed:
                r.outcome, r.reason = "duplicate_row", ""
                seg_rows.remove(r)
                seg.rows.remove(r.classified)
            claimed.add(key)
        if not seg.rows:
            continue

        log.info("%s start", label)
        post_outcomes: dict[str, str] = {}
        media_outcomes: dict[str, str] = {}
        # The explicit list is the selection: a collect-era date window must not
        # silently filter list rows out (they would stay not_attempted forever).
        seg_settings = settings.model_copy(
            update={"post_msgs": seg.msg_ids, "media_since": None, "media_msgs": None}
        )
        target = parse_target(str(cid))
        end_reason: str | None = None
        stop: tuple[str, str] | None = None
        try:
            # Step 1: the channel and the posts. Media eligibility depends on
            # what the posts phase just stored, so it is decided only now.
            phase_results, established = await collect_channel_with_context(
                gateway, store, seg_settings, target, ["channel", "posts"], log,
                collectors=[ChannelCollector(), PostsCollector(outcomes=post_outcomes)],
                profile=profile, channel_context=contexts.get(cid),
            )
            stop = _stop_check(phase_results, ("channel", "posts"), label, log)
            if stop is not None and stop[0] == "end":
                end_reason = stop[1]
            elif stop is None:
                if established is not None:
                    contexts.setdefault(cid, established)
                if media_store is not None and cid in contexts:
                    end_reason = await _run_media_step(
                        gateway, store, seg_settings, seg, seg_rows, cid, contexts[cid],
                        post_outcomes, media_outcomes, media_store, log, profile, label,
                    )
        except MediaStoreError as exc:
            log.warning("%s: cannot reach the media store: %s; ending the command", label, exc)
            end_reason = f"media store unreachable ({type(exc).__name__})"
        finally:
            # Merge even when an unexpected error escapes mid-segment: rows the
            # posts/media phases already finished and recorded must be reported
            # as such, not left `not_attempted`.
            for r in seg_rows:
                _settle(
                    store, r, post_outcomes.get(r.classified.uri),
                    media_outcomes.get(r.classified.uri), with_media=with_media,
                )
        log.info(
            "%s end: posts %s; media %s", label,
            dict(Counter(post_outcomes.values())) or "none fetched",
            dict(Counter(media_outcomes.values())) or "no files considered",
        )
        if end_reason is not None:
            return end_reason
        if stop is not None:
            dead_channels[address] = dead_channels[cid] = stop[1]
            _mark_no_access(seg_rows, stop[1])
    return None


async def _run_media_step(
    gateway: Gateway,
    store: Store,
    seg_settings: Settings,
    seg: Segment,
    seg_rows: list[RowResult],
    cid: int,
    context: ChannelContext,
    post_outcomes: dict[str, str],
    media_outcomes: dict[str, str],
    media_store: MediaStore,
    log: logging.Logger,
    profile: str,
    label: str,
) -> str | None:
    """Step 2 of a segment: decide, from the posts just stored, which files this
    run's store lacks, and run the `media` phase over exactly those messages (a
    separate run that reuses the channel: the selection can only be known once
    the posts are in). Returns why the command must end, else `None`."""
    media_ids = _media_ids_after_posts(store, media_store, seg_rows, post_outcomes)
    if not media_ids:
        return None
    media_settings = seg_settings.model_copy(update={"media_msgs": media_ids})
    results, _ = await collect_channel_with_context(
        gateway, store, media_settings, parse_target(str(cid)), ["media"], log,
        collectors=[MediaCollector(outcomes=media_outcomes)], profile=profile,
        channel_context=context,
    )
    if any(r.stopped == "hard_stop" for r in results):
        return "hard_stop"
    media_result = {r.name: r for r in results}.get("media")
    if media_result is not None and media_result.stopped == "phase_stop":
        reason = type(media_result.stop_exc).__name__
        log.warning("%s: media phase stopped (%s); ending the command", label, reason)
        return f"media phase_stop ({reason})"
    return None


def _media_ids_after_posts(
    store: Store, media_store: MediaStore, seg_rows: list[RowResult],
    post_outcomes: dict[str, str],
) -> list[int]:
    """The message ids whose file this run's store does not hold yet, judged on
    the rows the posts phase has just written (spec 2.2 step 3): the content key
    of the post's CURRENT media, not of any earlier version of it. Also refreshes
    each fetched row's `media_held`/`stored`, which the report and the outcome
    use. Walking a held row would add a `duplicate` custody row per re-run."""
    index = load_content_index(store.conn)
    ids: set[int] = set()
    for r in seg_rows:
        c = r.classified
        if post_outcomes.get(c.uri) != "fetched":
            continue  # not reached, or deleted upstream: nothing to download
        msg = store.conn.execute(
            "SELECT media_kind, media_json FROM messages WHERE uri = ?", (c.uri,)
        ).fetchone()
        if msg is None:
            continue
        file = held_file(store, media_store, msg["media_kind"], msg["media_json"], index)
        c.in_store, c.stored, c.media_held = True, file, file is not None
        if file is None or not _has_sighting(store, c.uri, msg["media_json"]):
            # Nothing held yet -> download. Held, but THIS message never got its
            # own custody row for the content (a repost of a file stored under
            # another message) -> the media phase records the sighting and skips
            # the download, as `collect` does. A message already recorded is not
            # walked again (no `duplicate` custody row per re-run).
            ids.add(c.row.msg_id)
    return sorted(ids)


def _has_sighting(store: Store, uri: str, media_json: str | None) -> bool:
    """Whether `uri` already has a custody row for the content its current media
    names: a sighting (where and when a file appeared) is provenance, recorded
    once per message and content."""
    key = content_key(json.loads(media_json)) if media_json else None
    if key is None:
        return True  # no content id: nothing a sighting could be keyed by
    return store.conn.execute(
        "SELECT 1 FROM custody_log WHERE source_message_uri = ? AND content_key = ?",
        (uri, content_key_text(key)),
    ).fetchone() is not None


def _stored_ref(store: Store, r: RowResult) -> tuple[str, str] | None:
    """`(sha256, key)` of the file the row's outcome is about. An `already_stored`
    row names the file its CURRENT media resolved to (`stored`, set from the
    content key after the posts phase). Otherwise it is the newest `custody_log`
    sighting of the message - the file this run just stored or matched - and,
    failing that, the message's own `media` row. Never "the oldest file the
    message ever had": an edited post's report must not name its old photo."""
    uri = r.classified.uri
    if r.outcome == "already_stored" and r.classified.stored is not None:
        return r.classified.stored
    row = store.conn.execute(
        "SELECT sha256, path FROM custody_log WHERE source_message_uri = ? "
        "ORDER BY id DESC LIMIT 1",
        (uri,),
    ).fetchone()
    if row is None:
        row = store.conn.execute(
            "SELECT sha256, path FROM media WHERE message_uri = ?", (uri,)
        ).fetchone()
    return (row["sha256"], row["path"]) if row else None


def write_report(path: Path, store: Store, results: list[RowResult]) -> None:
    """CSV `line_no,uri,outcome,post,sha256,key,reason` for every input row, in list order."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            sha = key = ""
            if r.outcome in _HAS_FILE:
                ref = _stored_ref(store, r)
                if ref is not None:
                    sha, key = ref
            writer.writerow([
                r.classified.row.line_no, r.classified.uri, r.outcome, r.post, sha, key,
                r.reason,
            ])


def _bytes_downloaded(store: Store, results: list[RowResult]) -> int:
    total = 0
    for r in results:
        if r.outcome == "downloaded":
            # Via custody, not `media.message_uri`: a file first stored by an earlier
            # run (another store, #63) has no `media` row of its own for this message.
            row = store.conn.execute(
                "SELECT m.size FROM custody_log c JOIN media m ON m.sha256 = c.sha256 "
                "WHERE c.source_message_uri = ? ORDER BY c.id DESC LIMIT 1",
                (r.classified.uri,),
            ).fetchone()
            total += row["size"] if row and row["size"] else 0
    return total


async def fetch_from_list(
    gateway: Gateway | None,
    store: Store,
    settings: Settings,
    classified: list[ClassifiedRow],
    log: logging.Logger,
    *,
    profile: str,
    report_path: Path,
    with_media: bool = True,
    excluded_ids: frozenset[int] = frozenset(),
) -> FetchSummary:
    """Run every pending segment and write the report (also on a stop or an
    unexpected error). `gateway` may be `None` only when nothing is pending.
    `with_media=False` (`--no-media`) fetches and projects the posts only."""
    results = initial_results(classified)
    segments = plan_segments(classified)
    summary = FetchSummary(results)
    try:
        if segments:
            if gateway is None:
                raise ValueError("fetch_from_list: pending rows need a gateway")
            summary.stop_reason = await _run_segments(
                gateway, store, settings, segments, results, log, profile,
                with_media=with_media, excluded_ids=excluded_ids,
            )
    finally:
        summary.bytes_downloaded = _bytes_downloaded(store, results)
        write_report(report_path, store, results)
        log.info(
            "fetch-from-list: %s; %d bytes downloaded; report %s",
            dict(summary.counts), summary.bytes_downloaded, report_path.name,
        )
    return summary
