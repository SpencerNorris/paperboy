"""Channel projection: current state (`channels`) + an append-only observation history
(`channel_snapshots`) of the counters/metadata that drift over time.
"""

from __future__ import annotations

import hashlib

from paperboy.ids import channel_uri, primary_username
from paperboy.store.db import Store, dumps

# `min` is a serialization marker (this payload was a reduced object), not a
# channel property — it is recorded on `peers.is_min` and excluded from a
# channel's flags.
_FLAG_EXCLUDE = frozenset({"min"})


def _channel_flags(full: dict, chan: dict) -> dict[str, bool]:
    """Every boolean flag on the ChannelFull and Channel.

    Telegram's `flags.N?true` fields serialise as real booleans, so capturing
    every bool-valued key projects the full flag set — rather than a fixed
    allow-list, which silently dropped 20 of 28 flags and hid the very
    `join_to_send`/`join_request` the join decision rests on (issue #20). `chan`
    is applied last so the authoritative Channel wins any overlap with the
    ChannelFull.
    """
    flags: dict[str, bool] = {}
    for obj in (full, chan):
        for k, v in obj.items():
            if isinstance(v, bool) and k not in _FLAG_EXCLUDE:
                flags[k] = v
    return flags


def _channel_kind(chan: dict) -> str | None:
    if chan.get("broadcast"):
        return "broadcast"
    if chan.get("gigagroup"):
        return "gigagroup"
    if chan.get("megagroup"):
        return "forum" if chan.get("forum") else "megagroup"
    return None


def upsert_channel(
    store: Store,
    full: dict,
    chan: dict,
    source_raw_id: int,
    observed_at: str,
) -> str:
    """Project `channels.getFullChannel`'s `(full_chat, chat)` pair; returns the channel URI.

    Writes current state to `channels` and always appends a `channel_snapshots`
    row — snapshots are an observation time series, not deduplicated, so
    counters like `participants_count` can be plotted over time.
    """
    id_ = chan["id"]
    uri = channel_uri(id_)
    title = chan.get("title")
    username = primary_username(chan)
    about = full.get("about")
    kind = _channel_kind(chan)
    # `chan["date"]` is a join/observed date, not creation; left for a future
    # collector to populate from a more authoritative source.
    created_at = None
    linked_chat_id = full.get("linked_chat_id") or None  # 0 means "no linked chat"
    participants_count = full.get("participants_count")
    restriction = chan.get("restriction_reason")
    restriction_json = dumps(restriction) if restriction else None
    flags = _channel_flags(full, chan)
    flags_json = dumps(flags) if flags else None

    store.conn.execute(
        """
        INSERT INTO channels (
            id, uri, username, title, about, kind, created_at, linked_chat_id,
            participants_count, flags_json, restriction_json, source_raw_id,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            username=excluded.username,
            title=excluded.title,
            about=excluded.about,
            kind=excluded.kind,
            linked_chat_id=excluded.linked_chat_id,
            participants_count=excluded.participants_count,
            flags_json=excluded.flags_json,
            restriction_json=excluded.restriction_json,
            source_raw_id=excluded.source_raw_id,
            last_seen=excluded.last_seen
        """,
        (
            id_, uri, username, title, about, kind, created_at, linked_chat_id,
            participants_count, flags_json, restriction_json, source_raw_id,
            observed_at, observed_at,
        ),
    )

    about_hash = hashlib.sha256(about.encode("utf-8")).hexdigest() if about else None
    store.conn.execute(
        "INSERT INTO channel_snapshots "
        "(channel_id, observed_at, participants_count, online_count, title, username, "
        "about_hash, source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id_, observed_at, participants_count, full.get("online_count"), title, username,
            about_hash, source_raw_id,
        ),
    )

    return uri
