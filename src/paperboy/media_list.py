"""`fetch-media` input: parse and classify an ordered list of message URIs (#68).

Two input shapes, chosen by the first meaningful line:

* **CSV** whose header contains a `uri` column. Every other column is ignored
  except an optional `priority`, kept only as an opaque grouping label.
* **Plain text**, one entry per line; blank lines and `#` comments ignored.

Each entry is `tg:msg:<channel_id>/<msg_id>` (paperboy's own message uri),
`https://t.me/c/<channel_id>/<msg_id>`, or `https://t.me/<username>/<msg_id>`
(scheme optional). Row order is fetch order. A malformed row fails the whole
list up front, naming every bad line: nothing is fetched from a half-parsed
list.

`classify_rows` then decides, offline against the store, what can still
happen to each row (`already_stored`, `not_in_store`, ...), and
`plan_segments` groups the rows that remain `pending` into the ordered
`(priority, channel)` passes the driver runs (`fetch_media.py`).
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.collectors.media import DOWNLOADABLE_KINDS, content_key, recorded_size
from paperboy.ids import msg_uri, parse_uri
from paperboy.store.channels import find_channel_id
from paperboy.targets import TargetKind, UnsupportedTarget, parse_target

if TYPE_CHECKING:
    from paperboy.store.db import Store

_TG_MSG_RE = re.compile(r"^tg:msg:(\d+)/(\d+)$")
_LINK_C_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/c/(\d+)/(\d+)/?$", re.IGNORECASE)
_LINK_USER_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/([A-Za-z][A-Za-z0-9_]{0,31})/(\d+)/?$", re.IGNORECASE
)

# The outcome vocabulary of a fetch-media report (docs/features/fetch-media.md).
OFFLINE_OUTCOMES = (
    "duplicate_row", "excluded", "not_in_store", "deleted", "no_media",
    "already_stored", "pending",
)


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


def classify_rows(
    store: Store, rows: Iterable[ListRow], *, excluded_ids: frozenset[int] = frozenset()
) -> list[ClassifiedRow]:
    """Offline outcome for every row, first match wins:
    `duplicate_row`, `excluded` (its channel is in `excluded_ids`; a username
    row is resolved first), `not_in_store`, `deleted`, `no_media`,
    `already_stored`, else `pending`. No network, no gateway."""
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
                out.append(ClassifiedRow(row, "not_in_store", row.uri))
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
            "SELECT media_kind, media_json, deleted_at FROM messages WHERE uri = ?", (uri,)
        ).fetchone()
        if msg is None:
            out.append(ClassifiedRow(row, "not_in_store", uri, channel_id))
            continue
        media = json.loads(msg["media_json"]) if msg["media_json"] else {}
        declared = recorded_size(media)
        if msg["deleted_at"] is not None:
            out.append(ClassifiedRow(row, "deleted", uri, channel_id, declared))
            continue
        if (msg["media_kind"] or "").lower() not in DOWNLOADABLE_KINDS:
            out.append(ClassifiedRow(row, "no_media", uri, channel_id, declared))
            continue
        key = content_key(media)
        if uri in stored_uris or (key is not None and key in index):
            out.append(ClassifiedRow(
                row, "already_stored", uri, channel_id, declared,
                index.get(key) if key is not None else None,
            ))
            continue
        out.append(ClassifiedRow(row, "pending", uri, channel_id, declared))
    return out


# ── Segments ───────────────────────────────────────────────────────────────


@dataclass
class Segment:
    """One ordinary collect pass: the pending rows of one channel at one
    priority. Within a segment the collector fetches by ascending message id;
    list order decides only the order of segments."""

    priority: str | None
    channel_id: int
    rows: list[ClassifiedRow] = field(default_factory=list)

    @property
    def msg_ids(self) -> list[int]:
        return sorted({c.row.msg_id for c in self.rows})


def plan_segments(classified: Iterable[ClassifiedRow]) -> list[Segment]:
    """Group `pending` rows by `(priority, channel_id)` in order of first
    appearance. The channel id is the address (#68 amendment): no handle."""
    segments: dict[tuple[str | None, int], Segment] = {}
    for c in classified:
        if c.outcome != "pending":
            continue
        assert c.channel_id is not None
        key = (c.row.priority, c.channel_id)
        seg = segments.get(key)
        if seg is None:
            seg = segments[key] = Segment(c.row.priority, c.channel_id)
        seg.rows.append(c)
    return list(segments.values())
