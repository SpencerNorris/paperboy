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

from paperboy.ids import channel_uri, chat_uri, parse_uri, primary_username, user_uri
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
            -- Composed richness ∘ recency (ADR-0005 §6, amended 2026-08-26,
            -- round-2 finding A): identity/profile columns (plus
            -- source_raw_id, which records the observation that produced
            -- them) move on recency ALONE between two full observations, but
            -- richness overrides recency when the stored row is currently
            -- `min` and this observation is `full` — the best profile data
            -- known wins even if the full observation is older, because a
            -- min row never had trustworthy profile data to defend. Between
            -- two `min` observations recency alone applies (no richness
            -- distinction between them). Provenance columns
            -- (`seen_in_chat`/`seen_in_msg`) always keep the plain recency
            -- guard — they record where THIS observation was witnessed, not
            -- how rich it was. `full ← min` (existing full row, incoming
            -- min) never reaches this INSERT..ON CONFLICT at all; it is
            -- handled by the early-return branch above, which never moves
            -- identity/profile and gates provenance on recency alone — the
            -- other half of the same composed lattice.
            kind = CASE WHEN excluded.last_seen >= peers.last_seen
                        OR (peers.is_min AND NOT excluded.is_min)
                        THEN excluded.kind ELSE peers.kind END,
            id = CASE WHEN excluded.last_seen >= peers.last_seen
                      OR (peers.is_min AND NOT excluded.is_min)
                      THEN excluded.id ELSE peers.id END,
            access_hash = CASE WHEN excluded.last_seen >= peers.last_seen
                               OR (peers.is_min AND NOT excluded.is_min)
                               THEN excluded.access_hash ELSE peers.access_hash END,
            is_min = CASE WHEN excluded.last_seen >= peers.last_seen
                          OR (peers.is_min AND NOT excluded.is_min)
                          THEN excluded.is_min ELSE peers.is_min END,
            seen_in_chat = CASE WHEN excluded.last_seen >= peers.last_seen
                                THEN excluded.seen_in_chat ELSE peers.seen_in_chat END,
            seen_in_msg = CASE WHEN excluded.last_seen >= peers.last_seen
                               THEN excluded.seen_in_msg ELSE peers.seen_in_msg END,
            username = CASE WHEN excluded.last_seen >= peers.last_seen
                            OR (peers.is_min AND NOT excluded.is_min)
                            THEN excluded.username ELSE peers.username END,
            first_name = CASE WHEN excluded.last_seen >= peers.last_seen
                              OR (peers.is_min AND NOT excluded.is_min)
                              THEN excluded.first_name ELSE peers.first_name END,
            last_name = CASE WHEN excluded.last_seen >= peers.last_seen
                             OR (peers.is_min AND NOT excluded.is_min)
                             THEN excluded.last_name ELSE peers.last_name END,
            title = CASE WHEN excluded.last_seen >= peers.last_seen
                         OR (peers.is_min AND NOT excluded.is_min)
                         THEN excluded.title ELSE peers.title END,
            flags_json = CASE WHEN excluded.last_seen >= peers.last_seen
                              OR (peers.is_min AND NOT excluded.is_min)
                              THEN excluded.flags_json ELSE peers.flags_json END,
            source_raw_id = CASE WHEN excluded.last_seen >= peers.last_seen
                                 OR (peers.is_min AND NOT excluded.is_min)
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


def input_user_ref(store: Store, uri: str) -> dict | None:
    """The store side of spec §5's `_input_user` builder: the dict the gateway
    turns into an `InputUser`/`InputPeerUser`.

    1. A non-`min` row with a real `access_hash` — in `users` (a triaged/
       enriched person) first, else `peers` (seen in a full `users` vector) —
       → `{"user_id", "access_hash"}`.
    2. Else a `min` stub with `(seen_in_chat, seen_in_msg)` provenance into a
       channel whose own hash `peers` knows → `{"user_id", "from_msg": {...}}`
       for `inputUserFromMessage` (research §1.9/§8.7 — the ONLY way a
       message-discovered stub is ever enrichable).
    3. Else `None`: unresolvable (a `min` hash is only good for photo
       downloads and is never offered here).
    """
    kind, ids = parse_uri(uri)
    if kind != "user":
        raise ValueError(f"input_user_ref expects a user URI, got {uri!r}")
    user_id = ids[0]
    user = store.conn.execute(
        "SELECT is_min, access_hash FROM users WHERE uri=?", (uri,)
    ).fetchone()
    if user is not None and not user["is_min"] and user["access_hash"]:
        return {"user_id": user_id, "access_hash": user["access_hash"]}
    peer = store.conn.execute(
        "SELECT is_min, access_hash, seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
    ).fetchone()
    if peer is None:
        return None
    if not peer["is_min"] and peer["access_hash"]:
        return {"user_id": user_id, "access_hash": peer["access_hash"]}
    if peer["seen_in_chat"] and peer["seen_in_msg"]:
        chan = store.conn.execute(
            "SELECT access_hash FROM peers WHERE uri=?", (channel_uri(peer["seen_in_chat"]),)
        ).fetchone()
        if chan is not None and chan["access_hash"]:
            return {
                "user_id": user_id,
                "from_msg": {
                    "channel_id": peer["seen_in_chat"], "access_hash": chan["access_hash"],
                    "msg_id": peer["seen_in_msg"],
                },
            }
    return None
