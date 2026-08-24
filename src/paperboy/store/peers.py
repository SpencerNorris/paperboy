"""Peer projection: any `user`/`channel`/`chat` id ever encountered, with min-provenance.

A `min` object (Telegram's stripped-down peer shape, seen inline in other
responses) carries only enough to identify the id — never enough to be
authoritative. If a fuller (non-min) row already exists, a `min` observation
updates only *where we last saw it referenced* (`seen_in_chat`/`seen_in_msg`,
used later for `inputPeerFromMessage`) and `last_seen`, never clobbering the
richer stored profile with blanks.
"""

from __future__ import annotations

from paperboy.ids import channel_uri, chat_uri, primary_username, user_uri
from paperboy.store.db import Store, dumps

_FLAG_KEYS = (
    "verified", "scam", "fake", "restricted", "bot", "premium", "deleted",
    "broadcast", "megagroup", "gigagroup", "forum", "join_to_send", "join_request",
)


def _classify(obj: dict) -> tuple[str, str, int]:
    """Map a TL peer-ish dict's `_` discriminator to (kind, uri, numeric id).

    Telethon's `to_dict()` uses the PascalCase class name (`"Channel"`,
    `"ChannelForbidden"`, ...), not the lowercase TL constructor name — this
    lowercases before matching so both that and any hand-authored lowercase
    test fixture work.
    """
    tag = obj["_"].lower()
    id_ = obj["id"]
    if tag.startswith("user"):
        return "user", user_uri(id_), id_
    if tag.startswith("channel"):
        return "channel", channel_uri(id_), id_
    if tag.startswith("chat"):
        return "chat", chat_uri(id_), id_
    raise ValueError(f"not a peer object: {tag!r}")


def upsert_peer(
    store: Store,
    obj: dict,
    source_raw_id: int,
    observed_at: str,
    *,
    seen_in_chat: int | None,
    seen_in_msg: int | None,
) -> str:
    kind, uri, id_ = _classify(obj)
    is_min = bool(obj.get("min"))

    existing = store.conn.execute("SELECT is_min FROM peers WHERE uri=?", (uri,)).fetchone()
    if is_min and existing is not None and not existing["is_min"]:
        store.conn.execute(
            "UPDATE peers SET seen_in_chat=?, seen_in_msg=?, source_raw_id=?, last_seen=? "
            "WHERE uri=?",
            (seen_in_chat, seen_in_msg, source_raw_id, observed_at, uri),
        )
        return uri

    flags = {k: obj[k] for k in _FLAG_KEYS if k in obj}
    flags_json = dumps(flags) if flags else None

    store.conn.execute(
        """
        INSERT INTO peers (
            uri, kind, id, access_hash, is_min, seen_in_chat, seen_in_msg,
            username, first_name, last_name, title, flags_json, source_raw_id,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
            kind=excluded.kind,
            id=excluded.id,
            access_hash=excluded.access_hash,
            is_min=excluded.is_min,
            seen_in_chat=excluded.seen_in_chat,
            seen_in_msg=excluded.seen_in_msg,
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            title=excluded.title,
            flags_json=excluded.flags_json,
            source_raw_id=excluded.source_raw_id,
            last_seen=excluded.last_seen
        """,
        (
            uri, kind, id_, obj.get("access_hash"), int(is_min), seen_in_chat, seen_in_msg,
            primary_username(obj), obj.get("first_name"), obj.get("last_name"), obj.get("title"),
            flags_json, source_raw_id, observed_at, observed_at,
        ),
    )
    return uri
