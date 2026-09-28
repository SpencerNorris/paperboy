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
from paperboy.store.sync import is_self

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
    message as provenance) and a `reacted_to` edge. Returns the number of
    reactors whose edge was NEW in THIS call (new-this-call, like
    `message_peers.backfill_message_referenced_peers` and
    `participants.project_join_service_messages`): a re-run over an unchanged
    channel returns 0. Idempotent (`add_edge_once`; the stub's stamp is the
    raw record's `observed_at`)."""
    new: set[str] = set()
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
            known = store.conn.execute(
                "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
            ).fetchone()
            if known is None or known["seen_in_chat"] is None or known["seen_in_msg"] is None:
                # Fill-only provenance: a reaction is not a documented
                # `inputPeerFromMessage` context (research §8.7 lists author,
                # forward header and mention), so it never replaces one that
                # is.
                if upsert_peer(
                    store, stub, row["id"], row["observed_at"],
                    seen_in_chat=channel_id, seen_in_msg=msg_id,
                ) is None:
                    continue  # the collecting account reacting (#12)
            elif is_self(store, uri):
                continue
            if add_edge_once(
                store, uri, REACTED_TO, msg_uri(channel_id, msg_id), row["observed_at"], tier,
                row["id"],
                {"source": "recent_reactions", "reaction": reaction.get("reaction"),
                 "date": iso_or_none(reaction.get("date"))},
            ):
                new.add(uri)
    return len(new)


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


def reaction_resume_offsets(store: Store, channel_id: int) -> dict[int, str]:
    """For each message whose reactor list is NOT yet complete, the
    `next_offset` of its most recently recorded page — the point a later run
    should resume from instead of restarting at page 1. Lets a list truncated
    by the per-message page cap CONVERGE across runs (each run advances a
    further cap's worth of pages) rather than re-walking the same head
    forever. A message with no recorded page, or whose latest page ended the
    list (falsy `next_offset`), is absent from the map."""
    rows = store.conn.execute(
        "SELECT json_extract(context_json, '$.msg_id') AS m, payload_json FROM raw_records "
        "WHERE lower(kind) LIKE '%messagereactionslist' "
        "AND json_extract(context_json, '$.channel_id') = ? "
        "ORDER BY id",
        (channel_id,),
    ).fetchall()
    last: dict[int, str | None] = {}
    for row in rows:
        if row["m"] is None:
            continue
        last[int(row["m"])] = json.loads(row["payload_json"]).get("next_offset")
    return {mid: off for mid, off in last.items() if off}


def fetched_reaction_lists(store: Store, channel_id: int) -> set[int]:
    """Message ids whose full reactor list was already fetched — derived from
    the raw log so repeated runs converge instead of re-spending.

    A message counts as done only when its MOST RECENTLY recorded page ended
    the list (a falsy `next_offset`). A message interrupted mid-list — a
    `HardStop`, or the participants collector's hard per-message page cap —
    is deliberately NOT done, so a later run revisits it instead of silently
    treating a partial reactor list as complete forever (round-3 review). The
    resumed fetch restarts from page 1 rather than the interrupted offset —
    every write along the way is idempotent, so re-walking pages already
    seen is wasted RPC, not a correctness risk."""
    rows = store.conn.execute(
        "SELECT json_extract(context_json, '$.msg_id') AS m, payload_json FROM raw_records "
        "WHERE lower(kind) LIKE '%messagereactionslist' "
        "AND json_extract(context_json, '$.channel_id') = ? "
        "ORDER BY id",
        (channel_id,),
    ).fetchall()
    # Iterate in raw-log (insertion) order so the last assignment per msg_id
    # wins — that is always this message's most recently recorded page.
    last_offset: dict[int, str | None] = {}
    for row in rows:
        if row["m"] is None:
            continue
        payload = json.loads(row["payload_json"])
        last_offset[int(row["m"])] = payload.get("next_offset")
    return {mid for mid, offset in last_offset.items() if not offset}
