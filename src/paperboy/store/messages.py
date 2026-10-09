"""Message projection: current state + append-only revisions/metrics/tombstones.

A message observed with a new `content_hash` appends a `message_revisions`
row and updates the entity; identical content only advances `last_seen`
(spec §7). Deletion evidence ranks `update` (unambiguous) > `empty`
(`messageEmpty` inside a verified-complete range) > `gap` (never observed);
`deleted_at` is set only for `update`/`empty` (ADR-0004).
"""

from __future__ import annotations

import contextlib
import hashlib
import json

from paperboy.ids import iso_or_none, msg_uri, peer_ref_uri
from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self

_TOMBSTONE_SETS_DELETED_AT = {"update", "empty"}


# Telegram re-issues this on every fetch of an unchanged media object (#96), so
# it is a transport token, not content: it never counts as an edit.
_VOLATILE_KEYS = frozenset({"file_reference"})


def _without_volatile(node: object) -> object:
    """`node` with every `_VOLATILE_KEYS` dict key dropped, at any depth."""
    if isinstance(node, dict):
        return {k: _without_volatile(v) for k, v in node.items() if k not in _VOLATILE_KEYS}
    if isinstance(node, list):
        return [_without_volatile(v) for v in node]
    return node


def content_hash(text: str, media_json: str | None) -> str:
    """Revision fingerprint: sha256 hex of the text + NUL + the media json (or empty).

    The media json is hashed with the volatile `file_reference` tokens removed
    (everything else - ids, size, mime, attributes, webpage fields - stays), so
    re-observing an unchanged media message is not a new revision. Only the
    hash input is normalised; stored `media_json` stays verbatim.
    """
    if media_json:
        # Not JSON: hash it verbatim rather than guess.
        with contextlib.suppress(ValueError):
            media_json = dumps(_without_volatile(json.loads(media_json)))
    payload = f"{text}\x00{media_json or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def latest_revision_hash(store: Store, uri: str) -> str | None:
    """Fingerprint of a message's latest revision, recomputed under the current scheme.

    Re-hashes the revision's stored `text`/`media_json` rather than trusting its
    stored `content_hash`, which an older release computed with `file_reference`
    included; comparing against that would add one phantom revision per message
    on the first re-observation after the fix. The single place that answers
    "what did we last record?" for `upsert_message` and `observe_message`.
    """
    row = store.conn.execute(
        "SELECT text, media_json FROM message_revisions WHERE message_uri=? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (uri,),
    ).fetchone()
    return content_hash(row["text"] or "", row["media_json"]) if row else None


def upsert_message(
    store: Store,
    channel_id: int,
    msg: dict,
    source_raw_id: int,
    observed_at: str,
    tier: str,
) -> str:
    """Project one `message`/`messageService` TL dict into the store.

    Always writes current state to `messages`; appends a `message_revisions`
    row iff the content hash changed since the last recorded revision
    (including the very first observation); appends a `message_metrics` row
    iff at least one of views/forwards/replies/reactions is present on this
    observation. Returns the message's URI.
    """
    del tier  # not yet stored per-message; carried by raw_records/edges/peers
    msg_id = msg["id"]
    uri = msg_uri(channel_id, msg_id)

    text = msg.get("message", "") or ""
    media = msg.get("media")
    media_json = dumps(media) if media else None
    media_kind = media.get("_") if media else None
    entities = msg.get("entities")
    entities_json = dumps(entities) if entities is not None else None
    fwd_from = msg.get("fwd_from")
    fwd_json = dumps(fwd_from) if fwd_from else None
    reply_to = msg.get("reply_to")
    reply_to_msg_id = reply_to.get("reply_to_msg_id") if reply_to else None
    reply_to_top_id = reply_to.get("reply_to_top_id") if reply_to else None
    action = msg.get("action")
    action_json = dumps(action) if action else None

    date = iso_or_none(msg.get("date"))
    edit_date = iso_or_none(msg.get("edit_date"))
    from_uri = peer_ref_uri(msg.get("from_id"))
    if is_self(store, from_uri):
        # The collecting account is never attributed as an author (issue #12);
        # the message row stays, its from_uri is nulled.
        from_uri = None
    post_author = msg.get("post_author")
    grouped_id = msg.get("grouped_id")
    via_bot_id = msg.get("via_bot_id")
    is_service = 1 if msg.get("_", "").lower() == "messageservice" else 0
    chash = content_hash(text, media_json)

    store.conn.execute(
        """
        INSERT INTO messages (
            uri, channel_id, msg_id, date, edit_date, from_uri, post_author, text,
            entities_json, media_kind, media_json, fwd_json, reply_to_msg_id,
            reply_to_top_id, grouped_id, via_bot_id, is_service, action_json,
            content_hash, deleted_at, source_raw_id, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        ON CONFLICT(uri) DO UPDATE SET
            date=excluded.date,
            edit_date=excluded.edit_date,
            from_uri=excluded.from_uri,
            post_author=excluded.post_author,
            text=excluded.text,
            entities_json=excluded.entities_json,
            media_kind=excluded.media_kind,
            media_json=excluded.media_json,
            fwd_json=excluded.fwd_json,
            reply_to_msg_id=excluded.reply_to_msg_id,
            reply_to_top_id=excluded.reply_to_top_id,
            grouped_id=excluded.grouped_id,
            via_bot_id=excluded.via_bot_id,
            is_service=excluded.is_service,
            action_json=excluded.action_json,
            content_hash=excluded.content_hash,
            source_raw_id=excluded.source_raw_id,
            last_seen=excluded.last_seen
        """,
        (
            uri, channel_id, msg_id, date, edit_date, from_uri, post_author, text,
            entities_json, media_kind, media_json, fwd_json, reply_to_msg_id,
            reply_to_top_id, grouped_id, via_bot_id, is_service, action_json,
            chash, source_raw_id, observed_at, observed_at,
        ),
    )

    if latest_revision_hash(store, uri) != chash:
        store.conn.execute(
            "INSERT INTO message_revisions "
            "(message_uri, observed_at, edit_date, content_hash, text, entities_json, "
            "media_json, source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uri, observed_at, edit_date, chash, text, entities_json, media_json, source_raw_id),
        )

    views = msg.get("views")
    forwards = msg.get("forwards")
    replies_obj = msg.get("replies")
    replies = replies_obj.get("replies") if isinstance(replies_obj, dict) else None
    reactions = msg.get("reactions")
    reactions_json = dumps(reactions) if reactions else None
    if views is not None or forwards is not None or replies is not None or reactions:
        store.conn.execute(
            "INSERT INTO message_metrics "
            "(message_uri, observed_at, views, forwards, replies, reactions_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uri, observed_at, views, forwards, replies, reactions_json),
        )

    return uri


def mark_deleted(
    store: Store,
    channel_id: int,
    msg_id: int,
    evidence: str,
    observed_at: str,
) -> None:
    """Record deletion evidence for a message; always tombstones, sometimes deletes.

    `deleted_at` (on `messages`) is only set for `update`/`empty` evidence — a
    `gap` (never observed at all) is ambiguous and may just be hidden/service.
    A message that has never been seen locally still gets a tombstone row
    (the `UPDATE ... WHERE uri=?` on `messages` simply affects zero rows).
    """
    uri = msg_uri(channel_id, msg_id)
    store.conn.execute(
        "INSERT INTO message_tombstones (message_uri, observed_at, evidence) VALUES (?, ?, ?)",
        (uri, observed_at, evidence),
    )
    if evidence in _TOMBSTONE_SETS_DELETED_AT:
        # COALESCE: keep the earliest-known deletion time if we've already
        # recorded one (e.g. an `empty` probe followed later by an
        # unambiguous `update` delete event for the same message).
        store.conn.execute(
            "UPDATE messages SET deleted_at = COALESCE(deleted_at, ?) WHERE uri = ?",
            (observed_at, uri),
        )


def clear_deleted(store: Store, channel_id: int, msg_id: int) -> bool:
    """Un-tombstone a message that Telegram has just answered with a LIVE object.

    The `deleted_at` flag means "currently believed deleted"; a direct
    `channels.getMessages` answer that returns the message is stronger evidence
    than the earlier `update`/`empty` observation, so the flag is cleared. The
    `message_tombstones` rows are append-only history and stay, so the earlier
    belief remains visible. Returns True iff a flag was cleared.
    """
    cur = store.conn.execute(
        "UPDATE messages SET deleted_at = NULL WHERE uri = ? AND deleted_at IS NOT NULL",
        (msg_uri(channel_id, msg_id),),
    )
    return cur.rowcount > 0
