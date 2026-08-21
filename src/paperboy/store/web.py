"""`web_snapshots` projection (migration `0002_web`): `t.me/s/` post captures
and Wayback CDX index rows, both written by `paperboy.collectors.web`.
"""

from __future__ import annotations

from paperboy.store.db import Store, dumps


def insert_tme_snapshot(
    store: Store,
    *,
    url: str,
    fetched_at: str,
    channel_username: str,
    msg_id: int | None,
    timestamp: str | None,
    content_hash: str | None,
    raw: dict,
    meta: dict | None,
) -> int:
    """Insert one `t.me/s/<name>` post capture. Append-only (not upserted) —
    `web_snapshots` is an observation log, like `channel_snapshots`, not
    current-state; the same post fetched on a later run gets its own row.
    """
    cur = store.conn.execute(
        "INSERT INTO web_snapshots "
        "(source, url, fetched_at, channel_username, msg_id, timestamp, content_hash, "
        "raw, meta_json) VALUES ('tme', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            url, fetched_at, channel_username, msg_id, timestamp, content_hash,
            dumps(raw), dumps(meta) if meta is not None else None,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def insert_wayback_snapshot(
    store: Store,
    *,
    url: str,
    fetched_at: str,
    channel_username: str,
    timestamp: str | None,
    content_hash: str | None,
    raw: dict,
    meta: dict | None,
) -> int:
    """Insert one Wayback CDX index row (no `msg_id` — this indexes whole-page
    snapshots of `t.me/s/<name>*`, not individual posts).
    """
    cur = store.conn.execute(
        "INSERT INTO web_snapshots "
        "(source, url, fetched_at, channel_username, msg_id, timestamp, content_hash, "
        "raw, meta_json) VALUES ('wayback', ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            url, fetched_at, channel_username, timestamp, content_hash,
            dumps(raw), dumps(meta) if meta is not None else None,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid
