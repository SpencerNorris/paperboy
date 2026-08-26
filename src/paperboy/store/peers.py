"""Peer projection: any `user`/`channel`/`chat` id ever encountered, with min-provenance.

A `min` object (Telegram's stripped-down peer shape, seen inline in other
responses) carries only enough to identify the id — never enough to be
authoritative. If a fuller (non-min) row already exists, a `min` observation
updates only *where we last saw it referenced* (`seen_in_chat`/`seen_in_msg`,
used later for `inputPeerFromMessage`) and `last_seen`, never clobbering the
richer stored profile with blanks.

Observations can arrive out of order — a live re-run, and especially replay
(ADR-0005 §6, which replays one historical run at a time and can revisit an
id observed earlier by a later run) — so every upsert here is
newest-observation-wins, not last-write-wins: current-state columns only
move when the incoming `observed_at` is at least as new as the stored
`last_seen`, while `first_seen`/`last_seen` themselves always widen to the
true min/max window regardless of arrival order.
"""

from __future__ import annotations

from paperboy.ids import channel_uri, chat_uri, primary_username, user_uri
from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self

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
) -> str | None:
    """Project a peer, returning its URI — or `None` if it is the collecting
    account, which is never a subject of interest and is kept out of the store
    entirely (issue #12; the id lives only in `sync_state('account','self')`).
    Callers that use the return value as an edge endpoint must skip a `None`.
    """
    kind, uri, id_ = _classify(obj)
    if is_self(store, uri):
        return None
    is_min = bool(obj.get("min"))

    existing = store.conn.execute("SELECT is_min FROM peers WHERE uri=?", (uri,)).fetchone()
    if is_min and existing is not None and not existing["is_min"]:
        # Newest-observation-wins (ADR-0005 §6): only move provenance forward
        # when this observation is at least as new as what is stored; the
        # window always widens.
        store.conn.execute(
            "UPDATE peers SET "
            "seen_in_chat = CASE WHEN ? >= last_seen THEN ? ELSE seen_in_chat END, "
            "seen_in_msg  = CASE WHEN ? >= last_seen THEN ? ELSE seen_in_msg  END, "
            "source_raw_id = CASE WHEN ? >= last_seen THEN ? ELSE source_raw_id END, "
            "first_seen = MIN(first_seen, ?), "
            "last_seen = MAX(last_seen, ?) "
            "WHERE uri=?",
            (
                observed_at, seen_in_chat, observed_at, seen_in_msg,
                observed_at, source_raw_id, observed_at, observed_at, uri,
            ),
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
            -- newest-observation-wins (ADR-0005 §6): current-state columns
            -- only move forward when this observation is at least as new as
            -- what is stored; the seen window always widens (MIN/MAX below).
            kind = CASE WHEN excluded.last_seen >= peers.last_seen
                        THEN excluded.kind ELSE peers.kind END,
            id = CASE WHEN excluded.last_seen >= peers.last_seen
                      THEN excluded.id ELSE peers.id END,
            access_hash = CASE WHEN excluded.last_seen >= peers.last_seen
                               THEN excluded.access_hash ELSE peers.access_hash END,
            is_min = CASE WHEN excluded.last_seen >= peers.last_seen
                          THEN excluded.is_min ELSE peers.is_min END,
            seen_in_chat = CASE WHEN excluded.last_seen >= peers.last_seen
                                THEN excluded.seen_in_chat ELSE peers.seen_in_chat END,
            seen_in_msg = CASE WHEN excluded.last_seen >= peers.last_seen
                               THEN excluded.seen_in_msg ELSE peers.seen_in_msg END,
            username = CASE WHEN excluded.last_seen >= peers.last_seen
                            THEN excluded.username ELSE peers.username END,
            first_name = CASE WHEN excluded.last_seen >= peers.last_seen
                              THEN excluded.first_name ELSE peers.first_name END,
            last_name = CASE WHEN excluded.last_seen >= peers.last_seen
                             THEN excluded.last_name ELSE peers.last_name END,
            title = CASE WHEN excluded.last_seen >= peers.last_seen
                         THEN excluded.title ELSE peers.title END,
            flags_json = CASE WHEN excluded.last_seen >= peers.last_seen
                              THEN excluded.flags_json ELSE peers.flags_json END,
            source_raw_id = CASE WHEN excluded.last_seen >= peers.last_seen
                                 THEN excluded.source_raw_id ELSE peers.source_raw_id END,
            first_seen = MIN(peers.first_seen, excluded.first_seen),
            last_seen = MAX(peers.last_seen, excluded.last_seen)
        """,
        (
            uri, kind, id_, obj.get("access_hash"), int(is_min), seen_in_chat, seen_in_msg,
            primary_username(obj), obj.get("first_name"), obj.get("last_name"), obj.get("title"),
            flags_json, source_raw_id, observed_at, observed_at,
        ),
    )
    return uri
