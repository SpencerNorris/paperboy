"""Issue #11: every peer a stored message REFERENCES — the forward origin in
`fwd_from.from_id` and users named by `MessageEntityMentionName` — gets a
`peers` row with the `(chat, msg_id)` provenance `inputPeerFromMessage`
needs (research §8.7 explicitly sanctions both as FromMessage contexts). Until
now those existed only as edge endpoints, invisible to the very enrichment
sweep that should reach them. Zero RPC: walks `messages` only, stamps each
stub with the message's own `first_seen` (a derived row, reproject plan D3)."""

from __future__ import annotations

import json

from paperboy.ids import channel_uri, peer_stub, user_uri
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer


def backfill_message_referenced_peers(store: Store, channel_id: int) -> int:
    """Returns the number of peers that were NEW to the store in THIS call
    (the same new-this-call semantics as `participants.
    project_join_service_messages` and `reactions.backfill_recent_reactions`,
    so a collector's `counts` never mixes "N new" with "the same N as last
    run"): a re-run over an unchanged channel returns 0. Idempotent: the min
    stub's stamp is the message's `first_seen`, so a re-run re-asserts the
    same provenance and a fuller, newer row is never touched (`upsert_peer`'s
    full<-min cell)."""
    rows = store.conn.execute(
        "SELECT msg_id, fwd_json, entities_json, source_raw_id, first_seen FROM messages "
        "WHERE channel_id=? AND (fwd_json IS NOT NULL OR entities_json IS NOT NULL)",
        (channel_id,),
    ).fetchall()
    new: set[str] = set()
    for row in rows:
        stubs: list[dict] = []
        if row["fwd_json"]:
            stub = peer_stub(json.loads(row["fwd_json"]).get("from_id"))
            if stub is not None:
                stubs.append(stub)
        if row["entities_json"]:
            for entity in json.loads(row["entities_json"]) or []:
                if (entity.get("_") or "").lower() == "messageentitymentionname":
                    user_id = entity.get("user_id")
                    if user_id is not None:
                        stubs.append({"_": "User", "id": user_id, "min": True})
        for stub in stubs:
            uri = user_uri(stub["id"]) if stub["_"] == "User" else channel_uri(stub["id"])
            existed = store.conn.execute(
                "SELECT 1 FROM peers WHERE uri=?", (uri,)
            ).fetchone() is not None
            written = upsert_peer(
                store, stub, row["source_raw_id"], row["first_seen"],
                seen_in_chat=channel_id, seen_in_msg=row["msg_id"],
            )
            if written is not None and not existed:
                new.add(written)
    return len(new)
