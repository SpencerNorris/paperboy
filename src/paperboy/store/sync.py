"""Sync bookkeeping: opaque per-scope state (`pts`, cursors) and verified-range gap math.

`sync_ranges` records message-id spans a collector has fully walked (every id
in the span was either stored or probed) — the complement within a queried
span is the set of gap candidates for `channels.getMessages` probing
(spec §7).
"""

from __future__ import annotations

import json

from paperboy.store.db import Store, dumps


def get_state(store: Store, scope: str, key: str) -> dict | None:
    row = store.conn.execute(
        "SELECT value_json FROM sync_state WHERE scope=? AND key=?", (scope, key)
    ).fetchone()
    return json.loads(row["value_json"]) if row else None


def set_state(store: Store, scope: str, key: str, value: dict) -> None:
    store.conn.execute(
        "INSERT INTO sync_state(scope, key, value_json) VALUES (?, ?, ?) "
        "ON CONFLICT(scope, key) DO UPDATE SET value_json=excluded.value_json",
        (scope, key, dumps(value)),
    )
    # Prime the is_self cache the moment the collecting account is recorded, so
    # projections in the same run (which may run before the cache would lazily
    # load) see it without a re-read (issue #12).
    if scope == "account" and key == "self":
        store.__dict__["_self_uri_cache"] = value.get("uri")


_UNSET = object()


def record_profile_attempt(
    store: Store, uri: str, attempted_at: str, outcome: str, detail: str | None = None
) -> None:
    """The `profiles` collector's rotation key (`profile_attempts`): the newest
    `users.getFullUser` attempt for `uri`, whatever its outcome. The collector
    writes `outcome='attempted'` the moment a budget slot is spent — before
    the RPC answers — and the arm that finishes the attempt replaces it, so an
    arm that forgets to report still advanced the queue. Newest write wins."""
    store.conn.execute(
        "INSERT INTO profile_attempts (uri, attempted_at, outcome, detail) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(uri) DO UPDATE SET attempted_at = excluded.attempted_at, "
        "outcome = excluded.outcome, detail = excluded.detail",
        (uri, attempted_at, outcome, detail),
    )


def self_uri(store: Store) -> str | None:
    """The collecting account's peer URI (from `sync_state('account','self')`).

    Cached per `Store`: primed by `set_state` when the `channel` phase records
    self, and lazily loaded from the DB on a resumed run where that phase did
    not run this process. `None` until the account is known.
    """
    cached = store.__dict__.get("_self_uri_cache", _UNSET)
    if cached is _UNSET:
        state = get_state(store, "account", "self")
        cached = state.get("uri") if state else None
        store.__dict__["_self_uri_cache"] = cached
    return cached


def is_self(store: Store, uri: str | None) -> bool:
    """True when `uri` is the collecting account — the projection layer's single
    chokepoint for keeping the collector out of the dataset (issue #12).
    """
    return uri is not None and uri == self_uri(store)


def add_range(store: Store, channel_id: int, lo: int, hi: int) -> None:
    """Record `[lo, hi]` as verified-complete, coalescing with any touching range.

    "Touching" includes adjacency (`existing.hi + 1 == lo`), not just overlap,
    so two ranges walked in separate pages merge into one contiguous span
    instead of leaving a phantom seam.
    """
    rows = store.conn.execute(
        "SELECT id, lo, hi FROM sync_ranges WHERE channel_id=? ORDER BY lo", (channel_id,)
    ).fetchall()
    merged_lo, merged_hi = lo, hi
    to_delete = []
    for r in rows:
        if r["lo"] <= merged_hi + 1 and r["hi"] >= merged_lo - 1:
            merged_lo = min(merged_lo, r["lo"])
            merged_hi = max(merged_hi, r["hi"])
            to_delete.append(r["id"])
    if to_delete:
        store.conn.executemany(
            "DELETE FROM sync_ranges WHERE id=?", [(i,) for i in to_delete]
        )
    store.conn.execute(
        "INSERT INTO sync_ranges(channel_id, lo, hi) VALUES (?, ?, ?)",
        (channel_id, merged_lo, merged_hi),
    )


def missing_ids(store: Store, channel_id: int, lo: int, hi: int) -> list[int]:
    """Ids in `[lo, hi]` not covered by any verified-complete range for this channel."""
    rows = store.conn.execute(
        "SELECT lo, hi FROM sync_ranges WHERE channel_id=? AND hi >= ? AND lo <= ? ORDER BY lo",
        (channel_id, lo, hi),
    ).fetchall()
    missing: list[int] = []
    cursor = lo
    for r in rows:
        rlo, rhi = max(r["lo"], lo), min(r["hi"], hi)
        if rlo > cursor:
            missing.extend(range(cursor, rlo))
        cursor = max(cursor, rhi + 1)
    if cursor <= hi:
        missing.extend(range(cursor, hi + 1))
    return missing
