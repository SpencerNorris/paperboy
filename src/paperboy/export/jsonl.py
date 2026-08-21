"""JSONL export view: `channel.jsonl` / `messages.jsonl` / `edges.jsonl`.

A read view over the `Store`, never the primary record (ADR-0002) — it can
always be regenerated. Messages carry their full `message_revisions` history
inline (so an edited message's prior text isn't lost to "current state
only"), and both messages and edges scrub anything traceable to the
collecting account's own identity (spec §2/§3: "exports scrub the collecting
account") — looked up via the `account`/`self` cursor the `channel` collector
seeds from `gateway.get_self()`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.ids import parse_uri
from paperboy.store.sync import get_state

if TYPE_CHECKING:
    from paperboy.store.db import Store


def _self_uri(store: Store) -> str | None:
    state = get_state(store, "account", "self")
    return state.get("uri") if state else None


def _write_jsonl(path: Path, rows: list[dict]) -> int:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            f.write("\n")
    return len(rows)


def export_jsonl(store: Store, channel_uri: str, out_dir: Path) -> dict[str, int]:
    kind, ids = parse_uri(channel_uri)
    if kind != "channel":
        raise ValueError(f"export_jsonl expects a channel URI, got {channel_uri!r}")
    channel_id = ids[0]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    self_uri = _self_uri(store)

    channel_rows = [
        dict(r) for r in store.conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,))
    ]
    channel_count = _write_jsonl(out_dir / "channel.jsonl", channel_rows)

    message_rows: list[dict] = []
    messages = store.conn.execute(
        "SELECT * FROM messages WHERE channel_id=? ORDER BY msg_id", (channel_id,)
    )
    for row in messages:
        message = dict(row)
        if self_uri and message.get("from_uri") == self_uri:
            continue  # scrub the collecting account's own messages
        revisions = store.conn.execute(
            "SELECT observed_at, edit_date, content_hash, text, entities_json, media_json "
            "FROM message_revisions WHERE message_uri=? ORDER BY observed_at",
            (message["uri"],),
        )
        message["revisions"] = [dict(r) for r in revisions]
        message_rows.append(message)
    message_count = _write_jsonl(out_dir / "messages.jsonl", message_rows)

    edge_rows: list[dict] = []
    message_uri_pattern = f"tg:msg:{channel_id}/%"
    edges = store.conn.execute(
        "SELECT * FROM edges WHERE subject_uri=? OR subject_uri LIKE ? OR object_uri=? "
        "ORDER BY id",
        (channel_uri, message_uri_pattern, channel_uri),
    )
    for row in edges:
        edge = dict(row)
        if self_uri and self_uri in (edge.get("subject_uri"), edge.get("object_uri")):
            continue
        edge_rows.append(edge)
    edge_count = _write_jsonl(out_dir / "edges.jsonl", edge_rows)

    return {"channel": channel_count, "messages": message_count, "edges": edge_count}
