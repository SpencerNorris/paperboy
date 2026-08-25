"""Harvest `messageReplies.recent_repliers` from already-stored payloads.

Telegram attaches a handful of recent commenters to every post that has a
comment thread, and it costs nothing: the field arrives inside the `Message`
object the `history` collector already wrote to `raw_records`. Projecting it is
therefore pure store work — no gateway, no RPC, no join — which is why it lives
beside the other projections rather than inside a collector.

The sample only survives on recent posts, so this complements the full
discussion sweep and never replaces it. On the live capture it yields 31
distinct commenter peers from 53 posts before a single new RPC.
"""

from __future__ import annotations

import json

from paperboy.ids import msg_uri, peer_ref_uri, utc_now_iso
from paperboy.store.db import Store
from paperboy.store.edges import add_edge
from paperboy.store.peers import upsert_peer

_COMMENTED_ON = "commented_on"

# `Peer*` discriminator -> (constructor tag, id field) for a projectable stub.
# The live capture contains both: an anonymous admin commenting as the channel
# arrives as `PeerChannel`, not `PeerUser`. Anything else (`PeerChat`, or a
# future discriminator) is skipped rather than guessed at.
_PEER_STUB_KIND = {"peeruser": ("User", "user_id"), "peerchannel": ("Channel", "channel_id")}


def backfill_recent_repliers(store: Store, channel_id: int, tier: str) -> int:
    """Project every `recent_repliers` peer in this channel's stored `Message`
    payloads into `peers`, with a `commented_on` edge to the post.

    Returns the number of **distinct** peers projected, not the number of
    occurrences — one person commenting on ten posts is one peer and ten edges.

    Idempotent. This runs on every `discussion` invocation and re-scans the
    whole raw log each time, so an unguarded insert would append a fresh edge
    per replier per run — a new `observed_at` attached to evidence the run never
    re-gathered. The guard is on the full `(subject, predicate, object)` triple
    and lives here rather than in `add_edge`, because `channel`, `history` and
    `graph` all still rely on that function's append-only semantics.
    """
    rows = store.conn.execute(
        # The store is one SQLite file per *profile*, not per channel, so a
        # second target's payloads share this table. `add_raw` tags each record
        # with its channel; filter on that rather than trusting the argument to
        # describe what happens to be in the table. `kind` is matched
        # case-insensitively — `add_raw` records the TL discriminator verbatim,
        # and both `Message` and `message` occur in practice.
        "SELECT id, payload_json FROM raw_records "
        "WHERE lower(kind) = 'message' "
        "AND json_extract(context_json, '$.channel_id') = ?",
        (channel_id,),
    ).fetchall()

    seen: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload_json"])
        repliers = (payload.get("replies") or {}).get("recent_repliers") or []
        post_id = payload.get("id")
        if not repliers or post_id is None:
            continue

        observed_at = utc_now_iso()
        post_uri = msg_uri(channel_id, post_id)
        for peer in repliers:
            stub = _peer_stub(peer)
            if stub is None:
                continue
            uri = peer_ref_uri(peer)
            if uri is None:
                continue
            # A bare peer reference carries no name or username, so record it
            # as `min` rather than writing a hollow authoritative row. The
            # provenance points at the post the reference was found on, which
            # is what `inputPeerFromMessage` later needs.
            upsert_peer(
                store, stub, row["id"], observed_at,
                seen_in_chat=channel_id, seen_in_msg=post_id,
            )
            if not _edge_exists(store, uri, _COMMENTED_ON, post_uri):
                add_edge(
                    store, uri, _COMMENTED_ON, post_uri, observed_at, tier, row["id"],
                    # Two producers emit `commented_on` — this backfill and the
                    # sweep's `_write_thread_edges`. The source marker is the
                    # only way a consumer can tell them apart.
                    {"source": "recent_repliers"},
                )
            seen.add(uri)
    return len(seen)


def _edge_exists(store: Store, subject: str, predicate: str, object_: str) -> bool:
    return (
        store.conn.execute(
            "SELECT 1 FROM edges WHERE subject_uri=? AND predicate=? AND object_uri=? LIMIT 1",
            (subject, predicate, object_),
        ).fetchone()
        is not None
    )


def _peer_stub(peer: dict) -> dict | None:
    """A minimal `min` peer object for a bare `Peer*` reference, or None."""
    mapped = _PEER_STUB_KIND.get((peer.get("_") or "").lower())
    if mapped is None:
        return None
    tag, id_field = mapped
    peer_id = peer.get(id_field)
    return None if peer_id is None else {"_": tag, "id": peer_id, "min": True}
