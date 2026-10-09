"""`fetch-from-list`: fetch the posts of an ordered cross-channel list, then
their media (#68, extended by #91).

The list is classified offline (`media_list.classify_rows`), the rows still
`pending` are grouped into `(priority, channel)` segments, and each segment is
a few ordinary `collect_channel` runs over the standard collectors. A channel
is first established ALONE (`channel`; once per command), and only then is
`--exclude-target` applied to it - a group of an excluded parent can be
recognised only by the `ChatFull` it reports. Then run 1 is `posts`, which
fetches EVERY listed id of the segment by `channels.getMessages` and projects
it through the same code `history` uses (already-stored posts are re-fetched,
so edits and counters are current), and run 2 is `media`, over the ids the
fresh posts show to need it (`_media_ids_after_posts`; skipped by `--no-media`
or when nothing needs it). Budget, guardrails, dedup, custody, streaming and
`--media-max-mb` all apply unchanged, and `reproject` replays every run as it
would any other (ADR-0005).

One gateway (one MTProto session, one `Budget`) serves the whole command. A
segment targets its channel BY ID (`parse_target(str(channel_id))`), so the
standard `channel` collector reaches it through the #84 access routes (saved
key, from-message, verified stored handle) and records a `ChannelAccess`
receipt. The one exception is a handle row whose channel the store has never
seen: it has no id to address, so the segment targets the handle and the
channel is resolved live, through the same phase (route `handle`, recorded as a
receipt). A channel is established once: its later runs reuse the
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
    # Also under --no-media: the report says whether the run's store holds a file.
    media_store = build_media_store(settings, profile)
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
        if cid not in contexts:
            # An id row: establish the channel ALONE first, exactly as a handle
            # row does, and only then decide exclusion. A group of an excluded
            # parent is recognisable only from the `ChatFull` it reports
            # (`linked_chat_id`), recorded by this phase as a `linked_group`
            # edge; fetching posts in the same run would be fail-open. The
            # channel's own metadata is therefore stored even for a channel
            # that turns out to be excluded (docs: "Exclusion").
            phase_results, established = await collect_channel_with_context(
                gateway, store, settings, parse_target(str(cid)), ["channel"], log,
                collectors=[ChannelCollector()], profile=profile,
            )
            stop = _stop_check(phase_results, ("channel",), label, log)
            if stop is not None and stop[0] == "end":
                return stop[1]
            if stop is not None or established is None:
                reason = stop[1] if stop else "the channel could not be established"
                dead_channels[address] = dead_channels[cid] = reason
                _mark_no_access(seg_rows, reason)
                continue
            contexts[cid] = established
            if _excludes(store, excluded, cid):
                log.info("%s: channel %s turned out to be excluded", label, cid)
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
        refreshed = False
        try:
            # Run 1: the posts. Media eligibility depends on what they just
            # stored, so it is decided only now.
            phase_results, _ = await collect_channel_with_context(
                gateway, store, seg_settings, target, ["posts"], log,
                collectors=[PostsCollector(outcomes=post_outcomes)],
                profile=profile, channel_context=contexts[cid],
            )
            stop = _stop_check(phase_results, ("posts",), label, log)
            if stop is not None and stop[0] == "end":
                end_reason = stop[1]
            elif stop is None:
                media_ids = _refresh_rows(store, media_store, cid, seg_rows, post_outcomes)
                refreshed = True
                if with_media and media_ids:
                    end_reason = await _run_media_step(
                        gateway, store, seg_settings, media_ids, cid, contexts[cid],
                        media_outcomes, log, profile, label,
                    )
        except MediaStoreError as exc:
            log.warning("%s: cannot reach the media store: %s; ending the command", label, exc)
            end_reason = f"media store unreachable ({type(exc).__name__})"
        finally:
            # Merge even when an unexpected error escapes mid-segment: rows the
            # posts/media phases already finished and recorded must be reported
            # as such, not left `not_attempted`. Rows of a posts run that
            # stopped after some batches are refreshed too: their report must
            # describe the post as stored now, not as classified offline.
            if not refreshed:
                _refresh_or_forget(
                    store, media_store, cid, seg_rows, post_outcomes, log, label
                )
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
    media_ids: list[int],
    cid: int,
    context: ChannelContext,
    media_outcomes: dict[str, str],
    log: logging.Logger,
    profile: str,
    label: str,
) -> str | None:
    """Run 2 of a segment: the `media` phase over exactly `media_ids` (a separate
    run that reuses the channel: the selection can only be known once the posts
    are in). Returns why the command must end, else `None`."""
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


def _refresh_or_forget(
    store: Store, media_store: MediaStore, channel_id: int, seg_rows: list[RowResult],
    post_outcomes: dict[str, str], log: logging.Logger, label: str,
) -> None:
    """`_refresh_rows` for the cleanup path (a `finally`): it must never raise, or
    it would hide the error that brought us here. If it fails, the offline flags
    on the fetched rows are stale (they describe the post BEFORE this run), so
    they are cleared: `_settle` then cannot report `already_stored` with an old
    file. The failure is logged with its traceback."""
    try:
        _refresh_rows(store, media_store, channel_id, seg_rows, post_outcomes)
    except Exception:
        log.exception(
            "%s: cannot refresh the fetched rows; their stored-file info is dropped", label
        )
        for r in seg_rows:
            if post_outcomes.get(r.classified.uri) == "fetched":
                r.classified.media_held, r.classified.stored = False, None


def _refresh_rows(
    store: Store, media_store: MediaStore, channel_id: int, seg_rows: list[RowResult],
    post_outcomes: dict[str, str],
) -> list[int]:
    """Re-judge every fetched row on the post as the posts phase just stored it
    (spec 2.2 step 3), whether or not a media phase follows: the content key of
    the post's CURRENT media, never an earlier version's. Sets each row's
    `in_store`/`stored`/`media_held` (what the report and the outcome use) and
    returns the ids the media phase must walk:

    * file not held by this run's store: download it;
    * held under a custody sighting of THIS channel (a same-channel repost) but
      this message has no sighting of its own yet: walk it, the media phase
      records a `duplicate` custody row and skips the download;
    * held only under ANOTHER channel (a cross-channel repost, ADR-0009): not
      walked - reported `already_stored` with the holding file, no custody row,
      and decided before any size cap, so `--media-max-mb` cannot touch it.

    The two indexes are the very ones the media phase uses (its per-channel
    index is `load_content_index(conn, channel_id)`), so the selection and the
    phase agree on what is a download and what a duplicate."""
    global_index = load_content_index(store.conn)
    channel_index = load_content_index(store.conn, channel_id)
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
        key = content_key(json.loads(msg["media_json"])) if msg["media_json"] else None
        same_channel = key is not None and key in channel_index
        file = held_file(
            store, media_store, msg["media_kind"], msg["media_json"],
            channel_index if same_channel else global_index,
        )
        c.in_store, c.stored, c.media_held = True, file, file is not None
        if file is None or (same_channel and not _has_sighting(store, c.uri, msg["media_json"])):
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
