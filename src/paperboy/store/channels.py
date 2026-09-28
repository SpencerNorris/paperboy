"""Channel projection: current state (`channels`) + an append-only observation history
(`channel_snapshots`) of the counters/metadata that drift over time.
"""

from __future__ import annotations

import hashlib

from paperboy.ids import channel_uri, primary_username
from paperboy.store.db import Store, dumps

# `min` is a whole-object serialization marker (this payload was a reduced
# object), not a channel property — it is recorded on `peers.is_min` and
# excluded from a channel's flags. Only the whole-object marker is excluded;
# per-field reliability qualifiers like `stories_hidden_min` are kept, because
# they carry real metadata about a specific flag rather than misdescribing the
# channel.
_FLAG_EXCLUDE = frozenset({"min"})


def channel_flags(full: dict, chan: dict) -> dict[str, bool]:
    """Every boolean flag on the ChannelFull and Channel.

    Telegram's `flags.N?true` fields serialise as real booleans, so capturing
    every bool-valued key projects the full flag set — rather than a fixed
    allow-list, which silently dropped 20 of 28 flags and hid the very
    `join_to_send`/`join_request` the join decision rests on (issue #20). `chan`
    is applied last so the authoritative Channel wins any overlap with the
    ChannelFull.

    Public (Task 8): `participants` needs it too, for the linked group's own
    preflight `ChatFull`.
    """
    flags: dict[str, bool] = {}
    for obj in (full, chan):
        for k, v in obj.items():
            if isinstance(v, bool) and k not in _FLAG_EXCLUDE:
                flags[k] = v
    return flags


_channel_flags = channel_flags  # back-compat alias


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

    Newest-observation-wins, not last-write-wins (ADR-0005 §6): observations
    can arrive out of order (a live re-run, or replay processing historical
    runs one at a time), so current-state columns only move when this
    observation is at least as new as the stored `last_seen`, while
    `first_seen`/`last_seen` always widen to the true min/max window.
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
    flags = channel_flags(full, chan)
    flags_json = dumps(flags) if flags else None

    store.conn.execute(
        """
        INSERT INTO channels (
            id, uri, username, title, about, kind, created_at, linked_chat_id,
            participants_count, flags_json, restriction_json, source_raw_id,
            first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            -- newest-observation-wins (ADR-0005 §6): current-state columns
            -- only move forward when this observation is at least as new as
            -- what is stored; the seen window always widens (MIN/MAX below).
            username = CASE WHEN excluded.last_seen >= channels.last_seen
                            THEN excluded.username ELSE channels.username END,
            title = CASE WHEN excluded.last_seen >= channels.last_seen
                         THEN excluded.title ELSE channels.title END,
            about = CASE WHEN excluded.last_seen >= channels.last_seen
                         THEN excluded.about ELSE channels.about END,
            kind = CASE WHEN excluded.last_seen >= channels.last_seen
                        THEN excluded.kind ELSE channels.kind END,
            linked_chat_id = CASE WHEN excluded.last_seen >= channels.last_seen
                                  THEN excluded.linked_chat_id ELSE channels.linked_chat_id END,
            participants_count = CASE WHEN excluded.last_seen >= channels.last_seen
                                      THEN excluded.participants_count
                                      ELSE channels.participants_count END,
            flags_json = CASE WHEN excluded.last_seen >= channels.last_seen
                              THEN excluded.flags_json ELSE channels.flags_json END,
            restriction_json = CASE WHEN excluded.last_seen >= channels.last_seen
                                    THEN excluded.restriction_json
                                    ELSE channels.restriction_json END,
            source_raw_id = CASE WHEN excluded.last_seen >= channels.last_seen
                                 THEN excluded.source_raw_id ELSE channels.source_raw_id END,
            first_seen = MIN(channels.first_seen, excluded.first_seen),
            last_seen = MAX(channels.last_seen, excluded.last_seen)
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
