"""Reaction vectors on a GROUP (spec §6.2; plan D8): the zero-RPC
`MessageReactions.recent_reactions` sample Telegram inlines in every reacted
message (a handful of `{peer_id, date, reaction}` per message — projected
from stored raw payloads exactly like `recent_repliers`), plus the
bookkeeping the `participants` collector needs to spend its bounded
`messages.getMessageReactionsList` budget without re-fetching a message it
already listed. Reactors on a BROADCAST are `BROADCAST_FORBIDDEN` and never
requested (guardrail)."""

from __future__ import annotations

import json

from paperboy.ids import iso_or_none, msg_uri, peer_ref_uri, peer_stub
from paperboy.store.db import Store
from paperboy.store.edges import add_edge_once
from paperboy.store.peers import upsert_peer

REACTED_TO = "reacted_to"


def _raw_messages(store: Store, channel_id: int):
    return store.conn.execute(
        "SELECT id, observed_at, payload_json FROM raw_records "
        "WHERE lower(kind) = 'message' AND json_extract(context_json, '$.channel_id') = ? "
        "ORDER BY id",
        (channel_id,),
    ).fetchall()


def backfill_recent_reactions(store: Store, channel_id: int, tier: str) -> int:
    """Project every `recent_reactions` reactor into `peers` (min, with the
    message as provenance) and a `reacted_to` edge. Returns DISTINCT
    reactors. Idempotent (`add_edge_once`; the stub's stamp is the raw
    record's `observed_at`)."""
    seen: set[str] = set()
    for row in _raw_messages(store, channel_id):
        payload = json.loads(row["payload_json"])
        sample = (payload.get("reactions") or {}).get("recent_reactions") or []
        msg_id = payload.get("id")
        if not sample or msg_id is None:
            continue
        for reaction in sample:
            stub = peer_stub(reaction.get("peer_id"))
            uri = peer_ref_uri(reaction.get("peer_id"))
            if stub is None or uri is None:
                continue
            if upsert_peer(
                store, stub, row["id"], row["observed_at"],
                seen_in_chat=channel_id, seen_in_msg=msg_id,
            ) is None:
                continue  # the collecting account reacting (#12)
            add_edge_once(
                store, uri, REACTED_TO, msg_uri(channel_id, msg_id), row["observed_at"], tier,
                row["id"],
                {"source": "recent_reactions", "reaction": reaction.get("reaction"),
                 "date": iso_or_none(reaction.get("date"))},
            )
            seen.add(uri)
    return len(seen)


def reacted_message_ids(store: Store, channel_id: int) -> list[int]:
    """Message ids that carry at least one reaction in their stored raw
    payload (raw, not `message_metrics`: archives captured before the
    reactions-only metrics fix have them only in raw), newest first."""
    ids: set[int] = set()
    for row in _raw_messages(store, channel_id):
        payload = json.loads(row["payload_json"])
        results = (payload.get("reactions") or {}).get("results") or []
        if any((r.get("count") or 0) > 0 for r in results) and payload.get("id") is not None:
            ids.add(payload["id"])
    return sorted(ids, reverse=True)


def fetched_reaction_lists(store: Store, channel_id: int) -> set[int]:
    """Message ids whose full reactor list was already fetched (any run) —
    derived from the raw log so repeated runs converge instead of re-spending."""
    rows = store.conn.execute(
        "SELECT DISTINCT json_extract(context_json, '$.msg_id') AS m FROM raw_records "
        "WHERE lower(kind) LIKE '%messagereactionslist' "
        "AND json_extract(context_json, '$.channel_id') = ?",
        (channel_id,),
    ).fetchall()
    return {int(r["m"]) for r in rows if r["m"] is not None}
