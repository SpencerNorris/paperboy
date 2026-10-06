"""`fetch-from-list` input: parse and classify an ordered list of message URIs
(#68, renamed #91; the module keeps its name because it parses the list).

Two input shapes, chosen by the first meaningful line:

* **CSV** whose header contains a `uri` column. Every other column is ignored
  except an optional `priority`, kept only as an opaque grouping label.
* **Plain text**, one entry per line; blank lines and `#` comments ignored.

Each entry is `tg:msg:<channel_id>/<msg_id>` (paperboy's own message uri),
`https://t.me/c/<channel_id>/<msg_id>`, or `https://t.me/<username>/<msg_id>`
(scheme optional). Row order is fetch order. A malformed row fails the whole
list up front, naming every bad line: nothing is fetched from a half-parsed
list.

`classify_rows` then decides, offline against the store, which rows are
`duplicate_row` or `excluded` and which are `pending` - every other addressable
row is fetched (its post, then its media) - and records what the store already
knows about each (`in_store`, `media_held`, `needs_resolve`). `plan_segments`
groups the pending rows into the ordered `(priority, channel)` passes the
driver runs (`fetch_from_list.py`).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.collectors.media import DOWNLOADABLE_KINDS, content_key, recorded_size
from paperboy.ids import msg_uri, parse_uri
from paperboy.media_store import MediaStore, stored_in
from paperboy.store.channels import find_channel_id
from paperboy.targets import TargetKind, UnsupportedTarget, parse_target

if TYPE_CHECKING:
    from paperboy.store.db import Store

_TG_MSG_RE = re.compile(r"^tg:msg:(\d+)/(\d+)$")
_LINK_C_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/c/(\d+)/(\d+)/?$", re.IGNORECASE)
_LINK_USER_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/([A-Za-z][A-Za-z0-9_]{0,31})/(\d+)/?$", re.IGNORECASE
)

# The outcomes classification decides offline (docs/features/fetch-from-list.md);
# the live run turns every `pending` row into one of the report's other outcomes.
OFFLINE_OUTCOMES = ("duplicate_row", "excluded", "pending")


class MediaListError(Exception):
    """The list could not be used. `line_nos` names every offending line."""

    def __init__(self, message: str, line_nos: list[int] | None = None) -> None:
        super().__init__(message)
        self.line_nos = line_nos or []


@dataclass(frozen=True)
class ListRow:
    """One parsed entry. `uri` is the normalised `tg:msg:` uri, or
    `t.me/<username>/<id>` for a username link (resolved offline later)."""

    line_no: int
    uri: str
    channel_id: int | None
    username: str | None
    msg_id: int
    priority: str | None
    duplicate: bool = False


def _parse_uri(text: str) -> tuple[int | None, str | None, int] | None:
    """`(channel_id, username, msg_id)` for one accepted uri form, else None."""
    text = text.strip()
    if m := _TG_MSG_RE.match(text):
        return int(m.group(1)), None, int(m.group(2))
    if m := _LINK_C_RE.match(text):
        return int(m.group(1)), None, int(m.group(2))
    if m := _LINK_USER_RE.match(text):
        if m.group(1).lower() == "c":
            return None  # `t.me/c/<id>` is a private-channel link missing its message id
        return None, m.group(1).lower(), int(m.group(2))
    return None


def _row(line_no: int, uri_text: str, priority: str | None) -> ListRow | None:
    parsed = _parse_uri(uri_text)
    if parsed is None:
        return None
    channel_id, username, msg_id = parsed
    uri = msg_uri(channel_id, msg_id) if channel_id is not None else f"t.me/{username}/{msg_id}"
    return ListRow(line_no, uri, channel_id, username, msg_id, priority or None)


def _mark_duplicates(rows: list[ListRow]) -> list[ListRow]:
    seen: set[tuple[object, int]] = set()
    out: list[ListRow] = []
    for r in rows:
        key = (r.channel_id if r.channel_id is not None else r.username, r.msg_id)
        dup = key in seen
        seen.add(key)
        out.append(replace(r, duplicate=dup))
    return out


def parse_media_list(path: Path) -> list[ListRow]:
    """Parse `path` into rows in list order; raise `MediaListError` listing
    every malformed line (or an empty list)."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines(keepends=True)

    first = next(
        (ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")), None
    )
    is_csv = first is not None and "uri" in [
        f.strip().lower() for f in next(csv.reader([first]), [])
    ]

    rows: list[ListRow] = []
    bad: list[int] = []
    if is_csv:
        reader = csv.reader(lines)
        header: list[str] | None = None
        uri_idx = prio_idx = -1
        for fields in reader:
            if not fields or all(not f.strip() for f in fields):
                continue
            if header is None:
                if fields[0].lstrip().startswith("#"):
                    continue
                header = [f.strip().lower() for f in fields]
                uri_idx = header.index("uri")
                prio_idx = header.index("priority") if "priority" in header else -1
                continue
            line_no = reader.line_num
            if len(fields) < len(header):
                bad.append(line_no)
                continue
            priority = fields[prio_idx].strip() if prio_idx >= 0 else None
            row = _row(line_no, fields[uri_idx], priority)
            if row is None:
                bad.append(line_no)
            else:
                rows.append(row)
    else:
        for line_no, raw in enumerate(lines, start=1):
            text = raw.rstrip("\r\n").strip()
            if not text or text.startswith("#"):
                continue
            row = _row(line_no, text, None)
            if row is None:
                bad.append(line_no)
            else:
                rows.append(row)

    if bad:
        raise MediaListError(
            f"{len(bad)} malformed row(s) at line(s) {', '.join(map(str, bad))}", bad
        )
    if not rows:
        raise MediaListError("the list has no rows")
    return _mark_duplicates(rows)


# ── Offline classification ─────────────────────────────────────────────────


@dataclass
class ClassifiedRow:
    row: ListRow
    outcome: str
    uri: str  # the resolved `tg:msg:` uri when known, else `row.uri`
    channel_id: int | None = None
    declared_bytes: int | None = None
    # `(sha256, media key)` of the already-stored file, when known offline.
    stored: tuple[str, str] | None = None
    # The message row exists in the store (else the post is "not yet collected").
    in_store: bool = False
    # THIS run's media store already holds the file: the media phase skips the row.
    media_held: bool = False
    # A handle row whose channel the store has never seen: resolved live, by
    # handle, through the #84 handle route (never offline).
    needs_resolve: bool = False


def _global_indexes(store: Store) -> tuple[dict[tuple[str, int], tuple[str, str]], set[str]]:
    """`content_key -> (sha256, path)` over EVERY stored file, and the set of
    message uris that already have a `media`/`custody_log` record."""
    index: dict[tuple[str, int], tuple[str, str]] = {}
    for r in store.conn.execute(
        "SELECT media.sha256 AS sha256, media.path AS path, messages.media_json AS mj "
        "FROM media JOIN messages ON media.message_uri = messages.uri"
    ):
        key = content_key(json.loads(r["mj"])) if r["mj"] else None
        if key is not None:
            index[key] = (r["sha256"], r["path"])
    uris = {
        r[0] for r in store.conn.execute(
            "SELECT message_uri FROM media WHERE message_uri IS NOT NULL "
            "UNION SELECT source_message_uri FROM custody_log "
            "WHERE source_message_uri IS NOT NULL"
        )
    }
    return index, uris


def excluded_channel_ids(store: Store, specs: Iterable[str]) -> frozenset[int]:
    """The channel ids `--exclude-target` names, offline: each spec is a handle
    or a channel id (the forms `reproject` takes), looked up in the store, plus
    the channel's linked discussion group (a group follows its parent, #70).
    Raises `MediaListError` for a spec that does not parse, is not a channel
    handle/id, or names a channel the store has never seen: excluding nothing
    by accident would download what the operator meant to keep out."""
    ids: set[int] = set()
    for spec in specs:
        try:
            target = parse_target(spec)
        except UnsupportedTarget as exc:
            raise MediaListError(f"--exclude-target {spec!r}: {exc}") from None
        if target.kind not in (TargetKind.USERNAME, TargetKind.PEER_ID):
            raise MediaListError(
                f"--exclude-target {spec!r}: expected a channel handle or id"
            )
        channel_id = find_channel_id(store, target)
        if channel_id is None:
            raise MediaListError(f"--exclude-target {spec!r}: no such channel in this store")
        ids.add(channel_id)
        for edge in store.conn.execute(
            "SELECT object_uri FROM edges WHERE subject_uri = ? AND predicate = 'linked_group'",
            (f"tg:channel:{channel_id}",),
        ):
            kind, (group_id,) = parse_uri(edge["object_uri"])
            if kind == "channel":
                ids.add(group_id)
    return frozenset(ids)


def _known_files(
    store: Store, uri: str, content: tuple[str, str] | None
) -> list[tuple[str, str]]:
    """Every `(sha256, key)` the database associates with this message: the file
    its content id resolves to, its own `media` row, and its custody sightings."""
    found: list[tuple[str, str]] = [] if content is None else [content]
    for r in store.conn.execute(
        "SELECT sha256, path FROM media WHERE message_uri = ? "
        "UNION SELECT sha256, path FROM custody_log WHERE source_message_uri = ?",
        (uri, uri),
    ):
        pair = (r["sha256"], r["path"])
        if pair not in found:
            found.append(pair)
    return found


def _held_by_store(
    conn: sqlite3.Connection, media_store: MediaStore, files: list[tuple[str, str]]
) -> bool:
    """Whether the run's store already holds one of `files`. A custody row naming
    the store answers offline; otherwise one existence check per file (a bucket
    dry run therefore touches GCS - metadata GETs - but never Telegram)."""
    if any(stored_in(conn, sha, media_store.store_id) for sha, _ in files):
        return True
    return any(media_store.exists(key) for _, key in files)


def classify_rows(
    store: Store,
    rows: Iterable[ListRow],
    *,
    media_store: MediaStore,
    excluded_ids: frozenset[int] = frozenset(),
) -> list[ClassifiedRow]:
    """Offline outcome for every row, first match wins: `duplicate_row`,
    `excluded` (its channel is in `excluded_ids`; a username row is resolved
    first), else `pending` - including rows the store has never seen, tombstoned
    rows and text posts, because the live run fetches the post of every
    addressable row. The flags say what the store already holds: `in_store`,
    `media_held` (THIS run's `media_store` has the file, #63), `needs_resolve`.
    No Telegram, no gateway; a bucket store is asked for metadata."""
    index, stored_uris = _global_indexes(store)
    usernames = {
        r["username"].lower(): r["id"]
        for r in store.conn.execute("SELECT id, username FROM channels WHERE username IS NOT NULL")
    }
    seen: set[tuple[int, int]] = set()
    out: list[ClassifiedRow] = []
    for row in rows:
        if row.duplicate:
            out.append(ClassifiedRow(row, "duplicate_row", row.uri, row.channel_id))
            continue
        channel_id = row.channel_id
        if channel_id is None:
            assert row.username is not None
            channel_id = usernames.get(row.username)
            if channel_id is None:
                out.append(ClassifiedRow(row, "pending", row.uri, needs_resolve=True))
                continue
        uri = msg_uri(channel_id, row.msg_id)
        if channel_id in excluded_ids:
            out.append(ClassifiedRow(row, "excluded", uri, channel_id))
            continue
        if (channel_id, row.msg_id) in seen:  # a link and a tg:msg: for one message
            out.append(ClassifiedRow(row, "duplicate_row", uri, channel_id))
            continue
        seen.add((channel_id, row.msg_id))
        msg = store.conn.execute(
            "SELECT media_kind, media_json FROM messages WHERE uri = ?", (uri,)
        ).fetchone()
        if msg is None:
            out.append(ClassifiedRow(row, "pending", uri, channel_id))
            continue
        media = json.loads(msg["media_json"]) if msg["media_json"] else {}
        declared = recorded_size(media)
        held = False
        hint = None
        if (msg["media_kind"] or "").lower() in DOWNLOADABLE_KINDS:
            key = content_key(media)
            hint = index.get(key) if key is not None else None
            if uri in stored_uris or hint is not None:
                files = _known_files(store, uri, hint)
                # If the database knows the file but this run's store does not,
                # the media phase downloads it again into this store.
                held = _held_by_store(store.conn, media_store, files)
        out.append(ClassifiedRow(
            row, "pending", uri, channel_id, declared, hint if held else None,
            in_store=True, media_held=held,
        ))
    return out


# ── Segments ───────────────────────────────────────────────────────────────


@dataclass
class Segment:
    """One ordinary collect pass: the pending rows of one channel at one
    priority. The channel is addressed by id, or by handle for a row whose
    channel the store has never seen (`username`, resolved live). The driver
    fetches the segment's posts in ascending id order; list order decides only
    the order of segments."""

    priority: str | None
    channel_id: int | None
    rows: list[ClassifiedRow] = field(default_factory=list)
    username: str | None = None

    @property
    def msg_ids(self) -> list[int]:
        """Every listed id: all of them are fetched, stored or not."""
        return sorted({c.row.msg_id for c in self.rows})

    @property
    def media_ids(self) -> list[int]:
        """The ids the media phase walks: those whose file this run's store does
        not already hold. Walking a held row would add a `duplicate` custody row
        per re-run."""
        return sorted({c.row.msg_id for c in self.rows if not c.media_held})

    @property
    def address(self) -> int | str:
        """The channel id, or `@<handle>`: what identifies this channel in one run."""
        return self.channel_id if self.channel_id is not None else f"@{self.username}"


def plan_segments(classified: Iterable[ClassifiedRow]) -> list[Segment]:
    """Group `pending` rows by `(priority, channel)` in order of first
    appearance. A row is addressed by channel id (#68 amendment), or by handle
    when `needs_resolve` (#91)."""
    segments: dict[tuple[str | None, int | str], Segment] = {}
    for c in classified:
        if c.outcome != "pending":
            continue
        username = c.row.username if c.channel_id is None else None
        seg_key: int | str = c.channel_id if c.channel_id is not None else f"@{username}"
        key = (c.row.priority, seg_key)
        seg = segments.get(key)
        if seg is None:
            seg = segments[key] = Segment(c.row.priority, c.channel_id, username=username)
        seg.rows.append(c)
    return list(segments.values())
