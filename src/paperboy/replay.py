"""Replay `Gateway`/web-client pair (spec §2–§4): serve `raw_records` back to
the real collectors, so a reproject is the same code path as a live collect.

`ReplaySource` is strictly read-only (`mode=ro` URI); every serve registers
the record's original `observed_at` on the shared `ReplayClock` so
projections carry capture-time stamps (spec §5). A method with no matching
raw raises `SkipAndRecord`, reproducing the phase set the original run
executed (spec §3) — with the documented deviations D4.1–D4.4 in
`docs/superpowers/plans/2026-08-26-reproject.md`.
"""

from __future__ import annotations

import bisect
import heapq
import json
import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import httpx

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.gateway import REPLAY_UNKNOWN_USER_KIND
from paperboy.ids import primary_username
from paperboy.media_keys import normalize_legacy_location, resolve_key_under
from paperboy.media_sink import MediaSink, stream_file_into
from paperboy.store.db import dumps
from paperboy.targets import parse_target

log = logging.getLogger("paperboy.replay")


class ReprojectSourceError(Exception):
    """The source raw log's run structure cannot be trusted (ADR-0005) — a
    corrupt or hand-edited DB, never a data condition a normal collect could
    produce. Operator-facing: `reproject.py`/`cli.py` catch it."""


@dataclass(frozen=True)
class ReplayRun:
    """One historical collect pass (ADR-0005), as a contiguous `raw_records`
    rowid range. `run_id` is the real stamped id for a post-migration pass,
    or an inferred `legacy-NNNN` label (capture order) for pre-migration rows
    — see `ReplaySource.runs()`."""

    run_id: str
    lo: int  # first raw rowid of the pass (inclusive)
    hi: int  # last raw rowid of the pass (inclusive)


@dataclass(frozen=True)
class RunMarker:
    """One recipe-written bookkeeping record of a run (`ChannelContextReused`,
    `MediaSelection`, #68): its stored stamp and JSON (so the replay clock can
    hand the same stamp back) plus the parsed payload and tier."""

    observed_at: str
    payload_json: str
    payload: dict
    tier: str


@dataclass(frozen=True)
class ResolveRecord:
    """One `(run, raw target)` pair as `ResolvedPeer` recorded it (#70).

    `channel_id` is the channel the target resolved to, or None when it
    resolved to a non-channel peer (a user, a basic group). `username` is that
    channel's current public username, None for a private channel."""

    run_id: str
    raw_target: str
    channel_id: int | None
    username: str | None


def _resolved_channel(payload: dict) -> tuple[int | None, str | None]:
    """`(channel id, username)` a `ResolvedPeer` payload resolved to; `(None,
    None)` when the peer is not a channel (the same test `channel.py` applies
    live, so a target the live collect would skip is unmatchable here too)."""
    channel_id = (payload.get("peer") or {}).get("channel_id")
    if not isinstance(channel_id, int):
        return None, None
    for chat in payload.get("chats") or []:
        if isinstance(chat, dict) and chat.get("id") == channel_id:
            return channel_id, primary_username(chat)
    return channel_id, None


def _kind_clause(kinds: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """A `WHERE`-fragment matching `lower(kind)` against any of `kinds`,
    tolerant of the dotted namespace prefix Telethon's `to_dict()` adds to
    some RPC-result envelope types (`contacts.resolvedPeer` for
    `ResolvedPeer`, `messages.chatFull` for `ChatFull`) but not others
    (`Message`, `ChatInvite*`, `SponsoredMessage`, `MediaDownload`, ...).
    Collectors record `payload.get("_", ...)` verbatim, so replay must match
    whichever shape the source actually stored, not assume one.
    """
    parts: list[str] = []
    params: list[str] = []
    for k in kinds:
        parts.append("(lower(kind) = ? OR lower(kind) LIKE ?)")
        params.append(k)
        params.append(f"%.{k}")
    return "(" + " OR ".join(parts) + ")", tuple(params)


def kind_matches(stored_lower: str, kinds: tuple[str, ...]) -> bool:
    """Python twin of `_kind_clause`: `stored_lower` (already `lower(kind)`)
    equals a wanted kind or carries a dotted namespace prefix before it."""
    return any(stored_lower == k or stored_lower.endswith("." + k) for k in kinds)


MatchMode = Literal["suffix", "exact", "contains"]

# Kinds whose `payload_json.$.id` is indexed (message lookups by message id).
_MESSAGE_KINDS = ("message", "messageservice", "messageempty")
_WEB_KINDS = ("tme_page", "wayback_cdx")

# Rough per-entry cost on top of the context text (RawEntry + dict + bucket
# slots); measured ~0.9 KB/row all-in on a real 27k-row run (#75).
_ENTRY_OVERHEAD_BYTES = 600
# One run's index above this logs a WARNING (no hard cap, no failure).
INDEX_WARN_BYTES = 512 * 1024 * 1024
_LOOKUP_CHUNK = 500


@dataclass(frozen=True, slots=True)
class RawEntry:
    """One `raw_records` row as the index remembers it (payload excluded —
    fetched by rowid only when a lookup actually serves it)."""

    id: int
    kind: str  # lower-cased stored kind
    tier: str
    observed_at: str
    ctx: dict  # parsed context_json; {} when NULL or not a JSON object
    payload_id: int | None  # CAST($.id AS INTEGER) — message kinds only
    url: str | None  # $.url — web kinds only


def _hashable_key(values: Iterable[object]) -> tuple | None:
    """The dict key for `values`, or None when any is NULL/unhashable — SQL
    `NULL = x` never matches, and a JSON object/array never equals a scalar."""
    key = tuple(values)
    for v in key:
        if v is None or isinstance(v, dict | list):
            return None
    return key


def _as_sqlite_json_value(value: Any) -> Any:
    """`value` as SQLite `json_extract` would return it: scalars as-is, a JSON
    object/array as its compact JSON *text* (hashable, and truthy even when
    empty) — the pre-#75 behaviour of `resolve_targets`/`linked_group_ids`."""
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return value


class RunIndex:
    """Every raw record of ONE run, read by a single rowid-range walk
    (`WALK_SQL`) and answered from memory ever after (#75). Lookups return the
    same rows, in the same `id` order, as the per-lookup SQL they replace."""

    _kind_sql, _kind_params = _kind_clause(_MESSAGE_KINDS)
    WALK_SQL = (
        "SELECT id, lower(kind), tier, observed_at, context_json, "
        f"CASE WHEN {_kind_sql} THEN CAST(json_extract(payload_json, '$.id') AS INTEGER) END, "
        "CASE WHEN lower(kind) IN ('tme_page', 'wayback_cdx') "
        "THEN json_extract(payload_json, '$.url') END "
        "FROM raw_records WHERE id BETWEEN ? AND ? ORDER BY id"
    )

    def __init__(self, all_entries: list[RawEntry]) -> None:
        self.all_entries = all_entries
        self.by_kind: dict[str, list[RawEntry]] = {}
        for e in all_entries:
            self.by_kind.setdefault(e.kind, []).append(e)
        self._entries_cache: dict[tuple[MatchMode, tuple[str, ...]], list[RawEntry]] = {}
        self._key_maps: dict[tuple, dict[tuple, list[RawEntry]]] = {}
        self._ctx_groups: dict[str, dict[object, list[RawEntry]]] = {}

    @classmethod
    def build(cls, conn: sqlite3.Connection, run: ReplayRun) -> RunIndex:
        started = time.perf_counter()
        entries: list[RawEntry] = []
        ctx_bytes = 0
        for rid, kind, tier, observed_at, ctx_json, payload_id, url in conn.execute(
            cls.WALK_SQL, (*cls._kind_params, run.lo, run.hi)
        ):
            ctx: dict = {}
            if ctx_json is not None:
                ctx_bytes += len(ctx_json)
                try:
                    parsed = json.loads(ctx_json)
                except ValueError:
                    # The pre-#75 SQL (`json_extract`) raised "malformed JSON"
                    # here, so a corrupt row failed the reproject loudly. Keep
                    # that; name the row and run, never the content.
                    raise ReprojectSourceError(
                        f"raw record id={rid} in run {run.run_id} has malformed context_json"
                    ) from None
                if isinstance(parsed, dict):
                    ctx = parsed
            entries.append(RawEntry(rid, kind, tier, observed_at, ctx, payload_id, url))
        index = cls(entries)
        approx = ctx_bytes + len(entries) * _ENTRY_OVERHEAD_BYTES
        message_rows = sum(
            len(b) for k, b in index.by_kind.items() if kind_matches(k, _MESSAGE_KINDS)
        )
        log.info(
            "replay index run=%s rows=%d message_rows=%d context_bytes=%d approx_bytes=%d "
            "elapsed=%.2fs",
            run.run_id, len(entries), message_rows, ctx_bytes, approx,
            time.perf_counter() - started,
        )
        if approx > INDEX_WARN_BYTES:
            log.warning(
                "replay index for run=%s is large (approx_bytes=%d > %d): one run is held "
                "in memory at a time",
                run.run_id, approx, INDEX_WARN_BYTES,
            )
        return index

    def entries(self, kinds: tuple[str, ...], mode: MatchMode = "suffix") -> list[RawEntry]:
        """Entries of the matching kinds, `id` ASC. `suffix` = `_kind_clause`
        semantics; `exact` = `lower(kind) IN`; `contains` = `LIKE '%k%'`."""
        cached = self._entries_cache.get((mode, kinds))
        if cached is not None:
            return cached
        if mode == "suffix":
            match = lambda k: kind_matches(k, kinds)  # noqa: E731
        elif mode == "exact":
            match = lambda k: k in kinds  # noqa: E731
        else:
            match = lambda k: any(w in k for w in kinds)  # noqa: E731
        buckets = [b for k, b in self.by_kind.items() if match(k)]
        out = buckets[0] if len(buckets) == 1 else list(heapq.merge(*buckets, key=lambda e: e.id))
        self._entries_cache[(mode, kinds)] = out
        return out

    @staticmethod
    def _field(entry: RawEntry, name: str) -> object:
        if name == "@tier":
            return entry.tier
        if name == "@payload_id":
            return entry.payload_id
        if name == "@url":
            return entry.url
        return entry.ctx.get(name)

    def lookup(
        self,
        kinds: tuple[str, ...],
        fields: tuple[str, ...],
        values: tuple[object, ...],
        mode: MatchMode = "suffix",
    ) -> list[RawEntry]:
        """Entries (`id` ASC) of `kinds` whose `fields` equal `values`. A
        `None` value (or an entry missing a field) never matches."""
        want = _hashable_key(values)
        if want is None:
            return []
        cache_key = (mode, kinds, fields)
        key_map = self._key_maps.get(cache_key)
        if key_map is None:
            key_map = {}
            for e in self.entries(kinds, mode):
                k = _hashable_key(self._field(e, f) for f in fields)
                if k is not None:
                    key_map.setdefault(k, []).append(e)
            self._key_maps[cache_key] = key_map
        return key_map.get(want, [])

    def ctx_groups(self, key: str) -> dict[object, list[RawEntry]]:
        """Every entry (any kind) grouped by its context value under `key`."""
        groups = self._ctx_groups.get(key)
        if groups is None:
            groups = {}
            for e in self.all_entries:
                v = e.ctx.get(key)
                if v is not None and not isinstance(v, dict | list):
                    groups.setdefault(v, []).append(e)
            self._ctx_groups[key] = groups
        return groups


class ReplaySourceError(Exception):
    """The source DB cannot be replayed safely (operator-actionable)."""


class ReplaySource:
    """Read-only access to a source DB's raw log + its content-addressed media.

    `profile_root` is the source *profile directory*: media keys
    (`media/<xx>/<sha><ext>`, ADR-0007) resolve against it.
    """

    def __init__(
        self, conn: sqlite3.Connection, profile_root: Path, *, opened_immutable: bool = False
    ) -> None:
        self.conn = conn
        # True when the source could only be opened with `immutable=1` (see `open`).
        self.opened_immutable = opened_immutable
        self.profile_root = profile_root
        # A real archive captured before this feature existed (ADR-0005) has
        # only pre-0003 migrations applied — `raw_records` has no `run_id`
        # column at all yet, not merely NULL values in it. `ReplaySource` is
        # strictly read-only and never applies migrations to the source, so
        # `runs()` must fall back to treating the whole log as legacy rather
        # than querying a column that may not exist.
        self._has_run_id = any(
            r[1] == "run_id" for r in conn.execute("PRAGMA table_info(raw_records)")
        )
        # Single-entry cache: reproject is sequential per run, so only the
        # current run's index is ever resident (#75).
        self._index: tuple[tuple[int, int], RunIndex] | None = None

    @classmethod
    def open(cls, db_path: Path, profile_root: Path) -> Self:
        """Open the source strictly read-only, never writing to its directory.

        Plain `mode=ro` is preferred. A WAL database in a directory the process
        cannot write to (a read-only archive, a mounted snapshot) cannot be
        opened that way when its `-shm`/`-wal` sidecars are absent: SQLite must
        create them (https://sqlite.org/wal.html, "Read-only databases"). Then
        we fall back to `immutable=1`, which skips locking and sidecars
        entirely. That is only safe if nothing writes the source while we read
        it; the caller reads `opened_immutable` and logs a WARNING (logging is
        not configured yet when the source is opened). `immutable=1` also
        ignores the WAL, so the fallback is refused (ReplaySourceError) when a
        non-empty `-wal` exists.
        """
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return cls(conn, profile_root)
        except sqlite3.OperationalError as exc:
            # Both messages occur for the missing-sidecar case (which one
            # depends on the SQLite build / first statement issued).
            conn.close()
            if not any(m in str(exc) for m in ("unable to open", "readonly database")):
                raise
            # Only the read-only-directory/missing-sidecar case may fall back.
            # A missing source file or a writable directory is a genuine
            # error and must surface as SQLite reported it.
            if not db_path.is_file() or os.access(db_path.parent, os.W_OK):
                raise
            # `immutable=1` ignores the WAL entirely: with a non-empty `-wal`
            # it would silently miss every un-checkpointed commit.
            wal = db_path.with_name(db_path.name + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                raise ReplaySourceError(
                    f"source DB has un-checkpointed data in its -wal file and its directory "
                    f"is not writable, so it cannot be read safely (immutable mode would "
                    f"ignore the WAL). Checkpoint the source from a writable location "
                    f"(PRAGMA wal_checkpoint(TRUNCATE)) or make its directory writable: "
                    f"{db_path.parent}"
                ) from exc

        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        return cls(conn, profile_root, opened_immutable=True)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def runs(self) -> list[ReplayRun]:
        """Capture-ordered collect passes (ADR-0005). Stamped rows group by
        `run_id`. Legacy NULL rows are segmented at each collect pass's
        OPENING CLUSTER — `resolve()`'s `ResolvedPeer`, `getFullChannel()`'s
        `ChatFull`, and the self `User` record, whatever order the collector
        version that captured them wrote them in (current code: self first;
        an archive can predate that invariant and write resolve/full before
        self — found running the R6 real-archive smoke, where it recurred at
        EVERY pass boundary throughout the archive's history, not just its
        first).

        A candidate new segment starts at the first opening-kind row seen
        after at least one substantive (non-opening) row, but it is not
        committed as a boundary until the whole contiguous run of
        opening-kind rows that follows is known — i.e. until the next
        substantive row, a stamped row, or the log's end ends it.

        One collect pass writes each opening role — self / ResolvedPeer /
        ChatFull — AT MOST ONCE. A contiguous run of opening-kind rows can
        therefore still span more than one pass (e.g. two back-to-back
        `--phases channel` runs, which write nothing but their own opening
        cluster and never hit a substantive row to end the pending cluster
        in between) — found running this fix's own regression battery. The
        pending cluster is split into one SUB-cluster per pass by cutting at
        the first REPEATED role: a second `self`/`ResolvedPeer`/`ChatFull`
        seen starts the next pass's sub-cluster rather than extending the
        current one.

        Each sub-cluster is then committed as a boundary only if it contains
        its own self marker (`tier='self'` `User`) — or nothing is open yet
        (the very start of the log); otherwise the sub-cluster is a foreign
        single-row intrusion — nothing in this codebase stops two `collect`
        invocations from writing to the same profile concurrently, and a
        lone `ResolvedPeer`/`ChatFull` from an unrelated short-lived process
        can land mid-run — and folds into whichever segment is already open
        instead of orphaning every row after it into a self-less segment
        that silently fails replay at `get_self()` (found on the real
        archive: a lone stray resolve mid-`MediaDownload`-loop discarded 157
        rows, 156 of them genuine historical `MediaDownload` observations).
        A genuine sub-cluster still absorbs every opening-kind row in it
        regardless of order, so all three land in the run they actually
        belong to rather than splitting off an orphan with no target to
        resolve. Runs must be contiguous rowid ranges (one sequential
        process per pass); interleaving means a corrupt source and fails
        loudly."""
        run_id_expr = "run_id" if self._has_run_id else "NULL AS run_id"
        rows = self.conn.execute(
            f"SELECT id, {run_id_expr}, tier, lower(kind) AS k FROM raw_records ORDER BY id"
        ).fetchall()
        runs: list[ReplayRun] = []
        current_id: str | None = None
        legacy_n = 0
        lo: int | None = None
        hi: int | None = None
        seen_run_ids: set[str] = set()
        # A contiguous run of legacy opening-kind rows not yet committed to
        # a boundary decision — see the docstring above.
        pending: list[sqlite3.Row] = []

        def _is_opening(row: sqlite3.Row) -> bool:
            k = row["k"]
            return (
                (row["tier"] == "self" and (k == "user" or k.endswith(".user")))
                or k == "resolvedpeer" or k.endswith(".resolvedpeer")
                or k == "chatfull" or k.endswith(".chatfull")
            )

        def _is_self_marker(row: sqlite3.Row) -> bool:
            k = row["k"]
            return row["tier"] == "self" and (k == "user" or k.endswith(".user"))

        def _opening_role(row: sqlite3.Row) -> str:
            """Which opening role `row` is — self / resolvedpeer / chatfull.
            Only ever called on rows `_is_opening()` already matched."""
            if _is_self_marker(row):
                return "self"
            k = row["k"]
            if k == "resolvedpeer" or k.endswith(".resolvedpeer"):
                return "resolvedpeer"
            return "chatfull"

        def _flush() -> None:
            nonlocal lo, hi
            if lo is not None:
                assert current_id is not None
                assert hi is not None
                runs.append(ReplayRun(current_id, lo, hi))
                lo = hi = None

        def _open_new_legacy(first_id: int) -> None:
            nonlocal current_id, legacy_n, lo
            legacy_n += 1
            next_id = f"legacy-{legacy_n:04d}"
            if next_id in seen_run_ids:
                raise ReprojectSourceError(
                    f"raw log run {next_id!r} is not contiguous — refusing to replay"
                )
            seen_run_ids.add(next_id)
            current_id = next_id
            lo = first_id

        def _resolve_pending() -> None:
            nonlocal hi
            if not pending:
                return
            # Split the pending cluster into one sub-cluster per collect
            # pass: walk it accumulating a sub-cluster, and when a row's
            # opening role has already been seen in the CURRENT sub-cluster,
            # that role is starting over — close the sub-cluster and start a
            # new one at that row (see the docstring above).
            sub_clusters: list[list[sqlite3.Row]] = []
            sub: list[sqlite3.Row] = []
            seen_roles: set[str] = set()
            for row in pending:
                role = _opening_role(row)
                if role in seen_roles:
                    sub_clusters.append(sub)
                    sub, seen_roles = [], set()
                sub.append(row)
                seen_roles.add(role)
            sub_clusters.append(sub)

            for cluster in sub_clusters:
                # No open segment to fold noise into (the very start of the
                # log) — the cluster must open segment 1 regardless of
                # whether it happens to contain a self marker.
                genuine = current_id is None or any(_is_self_marker(r) for r in cluster)
                if genuine:
                    _flush()
                    _open_new_legacy(cluster[0]["id"])
                hi = cluster[-1]["id"]
            pending.clear()

        for row in rows:
            if row["run_id"] is not None:
                _resolve_pending()
                stamped_id: str = row["run_id"]
                if stamped_id != current_id:
                    _flush()
                    if stamped_id in seen_run_ids:
                        raise ReprojectSourceError(
                            f"raw log run {stamped_id!r} is not contiguous — "
                            "refusing to replay"
                        )
                    seen_run_ids.add(stamped_id)
                    current_id = stamped_id
                    lo = row["id"]
                hi = row["id"]
                continue
            if _is_opening(row):
                pending.append(row)
                continue
            _resolve_pending()
            if current_id is None:
                _open_new_legacy(row["id"])
            hi = row["id"]
        _resolve_pending()
        _flush()
        return runs

    def index(self, run: ReplayRun) -> RunIndex:
        """The run's in-memory raw index, built by ONE walk and reused until
        another run is asked for (#75)."""
        key = (run.lo, run.hi)
        if self._index is None or self._index[0] != key:
            self._index = None  # free the previous run before building the next
            self._index = (key, RunIndex.build(self.conn, run))
        return self._index[1]

    def payloads(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        """`observed_at, payload_json` by rowid. A missing id is a corrupt
        source (the index just saw it), never silently skipped."""
        wanted = list(ids)
        out: dict[int, sqlite3.Row] = {}
        for i in range(0, len(wanted), _LOOKUP_CHUNK):
            chunk = wanted[i : i + _LOOKUP_CHUNK]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                f"SELECT id, observed_at, payload_json FROM raw_records WHERE id IN ({marks})",
                chunk,
            ):
                out[row["id"]] = row
        missing = [i for i in wanted if i not in out]
        if missing:
            raise ReprojectSourceError(
                f"raw record(s) {missing[:5]} vanished from the source during replay"
            )
        return out

    def resolve_targets(self, run: ReplayRun) -> list[str]:
        """Every distinct `target` a `resolve()` was recorded against WITHIN
        `run`, in first-seen (capture) order — `reproject` re-runs a full
        collect per target per historical run (ADR-0005)."""
        seen: dict[str, None] = {}
        for e in self.index(run).entries(("resolvedpeer",)):
            target = _as_sqlite_json_value(e.ctx.get("target"))
            if target is not None:
                seen.setdefault(target)
        return list(seen)

    def resolve_catalogue(self) -> list[ResolveRecord]:
        """Every `(run, raw target)` pair with the channel id it resolved to,
        from ONE pass over the `ResolvedPeer` rows (no per-run index builds, so
        validating a `--include/--exclude-target` costs a query, not a
        replay). Pairs are keyed and ordered exactly as `resolve_targets` does
        (first-seen order, `_as_sqlite_json_value` on the context target); a
        pair resolved twice in one run keeps its latest resolution, as
        `RawReplayGateway.resolve` serves it."""
        runs = self.runs()
        los = [r.lo for r in runs]
        kind_sql, kind_params = _kind_clause(("resolvedpeer",))
        pairs: dict[tuple[str, str], ResolveRecord] = {}
        for row in self.conn.execute(
            "SELECT id, context_json, payload_json FROM raw_records "
            f"WHERE {kind_sql} ORDER BY id",
            kind_params,
        ):
            i = bisect.bisect_right(los, row["id"]) - 1
            if i < 0 or row["id"] > runs[i].hi:
                continue  # in no run: never replayed, never catalogued
            run = runs[i]
            try:
                ctx = json.loads(row["context_json"]) if row["context_json"] else {}
            except ValueError:
                raise ReprojectSourceError(
                    f"raw record id={row['id']} in run {run.run_id} has malformed context_json"
                ) from None
            target = _as_sqlite_json_value(ctx.get("target") if isinstance(ctx, dict) else None)
            if target is None:
                continue
            channel_id, username = _resolved_channel(json.loads(row["payload_json"]))
            pairs[(run.run_id, str(target))] = ResolveRecord(
                run.run_id, str(target), channel_id, username
            )
        return list(pairs.values())

    def linked_group_map(self) -> dict[int, int]:
        """`{channel id: linked discussion group id}` from every `ChatFull`
        (latest wins) — so an unknown-target error can say which ids are a
        parent's linked group (#70)."""
        kind_sql, kind_params = _kind_clause(("chatfull",))
        linked: dict[int, int] = {}
        for row in self.conn.execute(
            "SELECT json_extract(context_json, '$.channel_id') AS cid, "
            "json_extract(payload_json, '$.full_chat.linked_chat_id') AS gid "
            f"FROM raw_records WHERE {kind_sql} ORDER BY id",
            kind_params,
        ):
            if isinstance(row["cid"], int) and isinstance(row["gid"], int) and row["gid"]:
                linked[row["cid"]] = row["gid"]
        return linked

    def linked_group_ids(self, run: ReplayRun) -> set[int]:
        chatfulls = self.index(run).entries(("chatfull",))
        linked: set[int] = set()
        for row in self.payloads(e.id for e in chatfulls).values():
            full_chat = json.loads(row["payload_json"]).get("full_chat")
            group = (
                _as_sqlite_json_value(full_chat.get("linked_chat_id"))
                if isinstance(full_chat, dict)
                else None
            )
            if group:
                linked.add(group)
        return linked

    def has_history_evidence(self, run: ReplayRun) -> bool:
        """Whether `run` left any trace of a `history` phase: a message, or the
        `getChannelDifference` page `catch_up` always records. A run without
        one (`--phases channel`, a fetch-media segment) must not replay
        `history`, which would append a synthetic difference raw the source
        never had."""
        index = self.index(run)
        return bool(index.entries(_MESSAGE_KINDS)) or bool(
            index.entries(("channeldifference",), "contains")
        )

    def has_kind(self, run: ReplayRun, *kinds: str) -> bool:
        return bool(self.index(run).entries(kinds))

    def _markers(self, run: ReplayRun, kind: str) -> list[RunMarker]:
        entries = self.index(run).entries((kind,), "exact")
        rows = self.payloads(e.id for e in entries)
        markers: list[RunMarker] = []
        for e in entries:
            row = rows[e.id]
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                raise ReprojectSourceError(
                    f"raw record id={e.id} in run {run.run_id} ({kind}) is not a JSON object"
                )
            markers.append(RunMarker(row["observed_at"], row["payload_json"], payload, e.tier))
        return markers

    def context_markers(self, run: ReplayRun) -> list[RunMarker]:
        """The run's `ChannelContextReused` markers (a fetch-media segment that
        reused an already-resolved channel; payload `{channel_id, source_run_id}`)."""
        return self._markers(run, "channelcontextreused")

    def media_selection(self, run: ReplayRun) -> RunMarker | None:
        """The run's `MediaSelection` record (payload `{msg_ids}`), if its media
        phase was scoped to specific messages."""
        found = self._markers(run, "mediaselection")
        return found[-1] if found else None

    def resolved_access_hash(self, run_id: str, channel_id: int) -> int:
        """The `access_hash` the `ResolvedPeer` of run `run_id` recorded for
        `channel_id`. Raises `ReprojectSourceError` if that run is not in the
        log or never resolved the channel - a marker pointing nowhere is a
        corrupt source, never guessed around."""
        run = next((r for r in self.runs() if r.run_id == run_id), None)
        if run is None:
            raise ReprojectSourceError(
                f"a ChannelContextReused marker names source run {run_id!r}, "
                "which is not in the raw log"
            )
        entries = self.index(run).entries(("resolvedpeer",))
        for row in self.payloads(e.id for e in entries).values():
            for chat in json.loads(row["payload_json"]).get("chats") or []:
                if isinstance(chat, dict) and chat.get("id") == channel_id:
                    access_hash = chat.get("access_hash")
                    return access_hash if isinstance(access_hash, int) else 0
        raise ReprojectSourceError(
            f"source run {run_id!r} never resolved channel {channel_id}, but a "
            "ChannelContextReused marker says its context was reused"
        )

    def has_context_channel(self, run: ReplayRun, channel_ids: set[int]) -> bool:
        groups = self.index(run).ctx_groups("channel_id")
        return any(cid in groups for cid in channel_ids)

    def has_context_value(self, run: ReplayRun, key: str, value: object) -> bool:
        """Whether any raw record in `run` carries `value` under context
        `key` (e.g. `method` = `users.getUsers`) — for phase detection of
        kinds that are NOT distinctive on their own (a `User` record is also
        the self marker)."""
        if value is None or isinstance(value, dict | list):
            return False
        return value in self.index(run).ctx_groups(key)


# Read size when streaming a stored media file into the sink: reproject
# re-verifies every file's sha on the way through without holding a whole
# file in memory.
_CHUNK = 1 << 20


class RawReplayGateway:
    """`Gateway` served from a raw log, scoped to ONE historical `ReplayRun`
    (ADR-0005). Every lookup is answered from the run's in-memory `RunIndex`
    (#75), so within one run each call site has at most one matching record —
    the latest, same as a live RPC has one now — and a lookup never scans the
    run. Never touches the network — there is no client, no session, no
    `Budget` anywhere in this class."""

    # Tells collectors this is a replay: they must not write to the source
    # profile (e.g. the media collector uses file-less hashing sinks).
    replay = True

    def __init__(self, source: ReplaySource, clock: ReplayClock, run: ReplayRun) -> None:
        self._src = source
        self._clock = clock
        self._run = run
        # get_channel_difference is inherently sequential (a pts catch-up
        # loop); a per-channel cursor over the stored pages models that.
        self._diff_cursor: dict[int, int] = {}

    @property
    def _index(self) -> RunIndex:
        # Cache hit after the first call of the run (`ReplaySource.index`).
        return self._src.index(self._run)

    def _latest(
        self,
        kinds: tuple[str, ...],
        fields: tuple[str, ...],
        values: tuple[object, ...],
    ) -> RawEntry | None:
        hits = self._index.lookup(kinds, fields, values)
        return hits[-1] if hits else None

    def _row(self, entry: RawEntry) -> sqlite3.Row:
        return self._src.payloads([entry.id])[entry.id]

    def _serve(self, entry: RawEntry) -> dict:
        row = self._row(entry)
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return payload

    async def resolve(self, target_value: str) -> dict:
        for entry in reversed(self._index.entries(("resolvedpeer",))):
            raw_target = entry.ctx.get("target")
            if raw_target and parse_target(raw_target).value == target_value:
                return self._serve(entry)
        raise SkipAndRecord(f"replay: no ResolvedPeer recorded for {target_value!r}")

    async def get_full_channel(self, input_channel: dict) -> dict:
        entry = self._latest(("chatfull",), ("channel_id",), (input_channel["channel_id"],))
        if entry is None:
            raise SkipAndRecord(
                f"replay: no ChatFull recorded for channel {input_channel['channel_id']}"
            )
        return self._serve(entry)

    async def get_self(self) -> dict:
        entry = self._latest(("user",), ("@tier",), ("self",))
        if entry is None:
            raise SkipAndRecord("replay: no self User recorded")
        return self._serve(entry)

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        # Reconstructs the original paging (spec §3): id DESC below the
        # cursor. Secondary order id ASC (capture order) so an edited
        # message's revisions replay oldest-first. MessageEmpty is excluded —
        # getHistory never yielded one; they came from the probe.
        hits = self._index.lookup(
            ("message", "messageservice"), ("channel_id",), (input_channel["channel_id"],)
        )
        # SQL semantics preserved: a cursor excludes records with no numeric
        # id (`NULL < x` is not true); no cursor keeps them, sorted last.
        if offset_id != 0:
            hits = [e for e in hits if e.payload_id is not None and e.payload_id < offset_id]
        ordered = sorted(
            hits,
            key=lambda e: (1, 0, e.id) if e.payload_id is None else (0, -e.payload_id, e.id),
        )
        # Never split one msg_id's records across pages: the collector's next
        # cursor is `min(page ids)` and the next page takes strictly-below,
        # so a split id's tail records would be unreachable forever.
        page = ordered[:limit]
        while len(ordered) > len(page) and ordered[len(page)].payload_id == page[-1].payload_id:
            page.append(ordered[len(page)])
        rows = self._src.payloads(e.id for e in page)
        self._clock.begin_batch()
        for entry in page:
            row = rows[entry.id]
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            yield json.loads(row["payload_json"])

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        channel_id = input_channel["channel_id"]
        self._clock.begin_batch()
        found = {
            i: e
            for i in ids
            if (e := self._latest(_MESSAGE_KINDS, ("channel_id", "@payload_id"), (channel_id, i)))
        }
        rows = self._src.payloads(e.id for e in found.values())
        out: list[dict] = []
        for i in ids:
            entry = found.get(i)
            if entry is None:
                # D4.1: a placeholder, NOT a synthetic messageEmpty — that
                # would fabricate deletion evidence (mark_deleted evidence=
                # 'empty') for ids the original run never observed as
                # deleted (a gap the probe found alive, or a range only
                # reachable via replayed catch-up). The collector skips any
                # non-messageEmpty shape, so this projects nothing — exactly
                # the original source's state.
                out.append({"_": "ReplayUnknownMessage", "id": i})
                continue
            row = rows[entry.id]
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            out.append(json.loads(row["payload_json"]))
        return out

    def _nested_match(self, channel_id: object, message: dict) -> sqlite3.Row | None:
        """The latest stored message record of `channel_id` whose payload text
        is exactly `dumps(message)` (how the original run stored it)."""
        index = self._index
        mid = message.get("id")
        if isinstance(mid, int) and not isinstance(mid, bool):
            hits = index.lookup(_MESSAGE_KINDS, ("channel_id", "@payload_id"), (channel_id, mid))
        else:  # no plain integer id to key on: compare the channel's whole set
            hits = index.lookup(_MESSAGE_KINDS, ("channel_id",), (channel_id,))
        if not hits:
            return None
        text = dumps(message)
        rows = self._src.payloads(e.id for e in hits)
        for entry in reversed(hits):
            if rows[entry.id]["payload_json"] == text:
                return rows[entry.id]
        return None

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        del limit
        channel_id = input_channel["channel_id"]
        idx = self._diff_cursor.get(channel_id, 0)
        # Substring, not prefix/suffix: the stored kind is one of
        # `updates.channelDifference`/`...Empty`/`...TooLong` — namespaced
        # AND suffixed, so the suffix rule doesn't cover it; "contains" does.
        pages = self._index.lookup(
            ("channeldifference",), ("channel_id",), (channel_id,), mode="contains"
        )
        entry = pages[idx] if idx < len(pages) else None
        self._diff_cursor[channel_id] = idx + 1

        if entry is None:
            # D4.4: exhausted the stored pages — a synthetic final EMPTY
            # diff, not SkipAndRecord. A mid-catch_up SkipAndRecord would
            # mark the whole history phase skipped and discard the backfill
            # counts already applied this run.
            in_channel = self._index.ctx_groups("channel_id").get(channel_id, [])
            stamp = max((e.observed_at for e in in_channel), default=None)
            synthetic = {"_": "updates.channelDifferenceEmpty", "final": True, "pts": pts}
            self._clock.begin_batch()
            # `stamp` is None when the channel has no records in the run —
            # behaviour unchanged from the SQL MAX() this replaced.
            self._clock.serve(stamp, synthetic)  # type: ignore[arg-type]
            return synthetic

        row = self._row(entry)
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        # Also register every nested message on the clock, keyed by its OWN
        # raw record (each is also individually stored by history.py's
        # _observe_message) — so `_observe_message` gets a per-message stamp
        # rather than falling back to the envelope's.
        nested = [
            *payload.get("new_messages", []),
            *payload.get("messages", []),
            *(u["message"] for u in payload.get("other_updates", [])
              if isinstance(u.get("message"), dict)),
        ]
        for m in nested:
            m_row = self._nested_match(channel_id, m)
            if m_row is not None:
                self._clock.serve_json(m_row["observed_at"], m_row["payload_json"])
        return payload

    async def get_authorizations(self) -> dict:
        raise SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")

    async def get_password_state(self) -> dict:
        raise SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")

    async def get_privacy(self, key: str) -> dict:
        # `profiles`/`participants` record the account's own posture once per
        # run (spec §4.3, `posture.py`) — that IS a recorded observation
        # (unlike `doctor`'s own reads, which are never recorded).
        entry = self._latest(("account.privacyrules", "privacyrules"), ("key",), (key,))
        if entry is None:
            raise SkipAndRecord(
                "replay: privacy posture not recorded for this run; reproject never runs doctor"
            )
        return self._serve(entry)

    def _resolve_payload_file(self, stored: str, sha: str) -> Path:
        """Resolve a `MediaDownload`/`AvatarDownload` payload's location under the
        source profile dir (ADR-0007). Payloads written before #62 hold absolute or
        cwd-relative paths; they are normalised by their sha, never trusted as
        paths. An unusable value is a recorded skip, not a reproject abort."""
        try:
            key = normalize_legacy_location(stored, sha)
        except ValueError as exc:
            raise SkipAndRecord(f"replay: unusable media location for sha {sha}") from exc
        return resolve_key_under(self._src.profile_root, key)

    async def download_media(
        self, input_channel: dict, message: dict, sink: MediaSink
    ) -> bool:
        entry = self._latest(
            ("mediadownload",), ("channel_id", "msg_id"),
            (input_channel["channel_id"], message["id"]),
        )
        if entry is None:
            return False
        row = self._row(entry)
        payload = json.loads(row["payload_json"])
        sha = payload["sha256"]
        path = self._resolve_payload_file(payload["path"], sha)
        # A small local disk stat/read on an offline, single-user CLI tool —
        # not worth a trio/anyio dependency for.
        if not path.exists():  # noqa: ASYNC240
            raise SkipAndRecord(f"replay: media file missing for sha {sha}")
        sink.reset()
        stream_file_into(path, sink, chunk_size=_CHUNK)
        # The collector trusts the streamed fingerprint (it names the row and,
        # when copying into another profile, the file): a rotted or swapped
        # file must be a recorded skip, never a row filed under the wrong sha.
        if sink.sha256 != sha:
            raise SkipAndRecord(
                f"replay: media file for sha {sha[:12]} does not match its receipt"
            )
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return True

    async def get_channel_recommendations(self, input_channel: dict) -> dict:
        entry = self._latest(
            ("chats", "chatsslice"), ("channel_id",), (input_channel["channel_id"],)
        )
        if entry is None:
            raise SkipAndRecord(
                "replay: no channel recommendations recorded for channel "
                f"{input_channel['channel_id']}"
            )
        return self._serve(entry)

    async def check_chat_invite(self, hash_: str) -> dict:
        entry = self._latest(
            ("chatinvite", "chatinvitealready", "chatinvitepeek"), ("hash",), (hash_,)
        )
        if entry is None:
            raise SkipAndRecord(f"replay: no ChatInvite recorded for hash {hash_!r}")
        return self._serve(entry)

    async def join_channel(self, input_channel: dict) -> dict:
        # D4.3: reproject always runs with allow_join=True so a source whose
        # original run used --join still replays its discussion sweep — but
        # nothing is actually joined. No network, no session, no real write
        # anywhere in this class.
        del input_channel
        return {"_": "Updates", "updates": []}

    async def get_sponsored_messages(self, input_channel: dict) -> dict:
        hits = self._index.lookup(
            ("sponsoredmessage",), ("channel_id",), (input_channel["channel_id"],)
        )
        self._clock.begin_batch()
        if not hits:
            # D4.2: the original collector never stores the envelope, only
            # each SponsoredMessage individually — empty-and-skipped
            # originals are indistinguishable, so both project nothing.
            return {"_": "sponsoredMessagesEmpty"}
        rows = self._src.payloads(e.id for e in hits)
        messages = []
        for entry in hits:
            row = rows[entry.id]
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            messages.append(json.loads(row["payload_json"]))
        return {"_": "SponsoredMessages", "messages": messages}

    # Person-layer replay methods (`participants`/`profiles`, spec §10),
    # served by the raw kinds/contexts those two collectors record (plan D1).

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        del limit, hash_
        entry = self._latest(
            ("channels.channelparticipants", "channels.channelparticipantsnotmodified"),
            ("channel_id", "filter", "offset"),
            (input_channel["channel_id"], filter.get("_"), offset),
        )
        if entry is None:
            raise SkipAndRecord(
                f"replay: no {filter.get('_')} page at offset {offset} recorded for "
                f"channel {input_channel['channel_id']}"
            )
        return self._serve(entry)

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        entry = self._latest(
            ("channels.channelparticipant", "usernotparticipant"),
            ("channel_id", "user_id"),
            (input_channel["channel_id"], participant["user_id"]),
        )
        if entry is None:
            raise SkipAndRecord(
                f"replay: no getParticipant answer recorded for user {participant['user_id']}"
            )
        payload = self._serve(entry)
        # The definitive negative was stored as a synthetic record (plan D4);
        # served so the clock has its stamp, then returned as the None it was.
        return None if (payload.get("_") or "").lower() == "usernotparticipant" else payload

    async def get_users(self, refs: list[dict]) -> list[dict]:
        self._clock.begin_batch()
        found = {
            ref["user_id"]: e
            for ref in refs
            if (e := self._latest(
                ("user", "userempty"), ("method", "user_id"),
                ("users.getUsers", ref["user_id"]),
            ))
        }
        rows = self._src.payloads(e.id for e in found.values())
        out: list[dict] = []
        for ref in refs:
            entry = found.get(ref["user_id"])
            if entry is None:
                # D4.1's analogue: a placeholder the collector ignores — never
                # a synthetic UserEmpty, which would fabricate a "deleted
                # account" observation the original run never made.
                out.append({"_": REPLAY_UNKNOWN_USER_KIND, "id": ref["user_id"]})
                continue
            row = rows[entry.id]
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            out.append(json.loads(row["payload_json"]))
        return out

    async def get_full_user(self, ref: dict) -> dict:
        entry = self._latest(("users.userfull",), ("user_id",), (ref["user_id"],))
        if entry is None:
            raise SkipAndRecord(f"replay: no UserFull recorded for user {ref['user_id']}")
        return self._serve(entry)

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        del offset, max_id, limit
        entry = self._latest(
            ("photos.photos", "photos.photosslice"), ("user_id",), (ref["user_id"],)
        )
        if entry is None:
            raise SkipAndRecord(f"replay: no photo history recorded for user {ref['user_id']}")
        return self._serve(entry)

    async def download_user_photo(self, photo: dict) -> bytes | None:
        entry = self._latest(("avatardownload",), ("photo_id",), (photo["id"],))
        if entry is None:
            return None
        row = self._row(entry)
        payload = json.loads(row["payload_json"])
        sha = payload["sha256"]
        path = self._resolve_payload_file(payload["path"], sha)
        if not path.exists():  # noqa: ASYNC240 — same rationale as download_media
            raise SkipAndRecord(f"replay: avatar file missing for sha {sha}")
        data = path.read_bytes()
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return data

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        del limit
        entry = self._latest(
            ("messages.messagereactionslist",),
            ("channel_id", "msg_id", "offset"),
            (input_channel["channel_id"], msg_id, offset or ""),
        )
        if entry is None:
            raise SkipAndRecord(
                f"replay: no reaction list recorded for message {msg_id} at offset {offset!r}"
            )
        return self._serve(entry)


class RawReplayWebClient:
    """Serve stored `tme_page`/`wayback_cdx` captures as `httpx.Response`s,
    scoped to ONE historical `ReplayRun` (ADR-0005).

    Keyed by exact URL — the web collector re-derives the same URL sequence
    from the same parsed posts, so replay requests exactly the recorded set.
    Repeat captures of one URL WITHIN a run serve in capture order (a
    reproject re-instantiates this client per run, so the cursor never
    crosses a run boundary). An unrecorded URL is a definitive empty 404: the
    page loop must stop cleanly there, exactly where the original run
    stopped.
    """

    def __init__(self, source: ReplaySource, clock: ReplayClock, run: ReplayRun) -> None:
        self._src = source
        self._clock = clock
        self._run = run
        self._served: dict[str, int] = {}  # url -> raw id already served

    def get(self, url: str) -> httpx.Response:
        captures = self._src.index(self._run).lookup(
            _WEB_KINDS, ("@url",), (url,), mode="exact"
        )
        after = self._served.get(url, self._run.lo - 1)
        entry = next((e for e in captures if e.id > after), None)
        if entry is None:
            return httpx.Response(404, text="")
        self._served[url] = entry.id
        row = self._src.payloads([entry.id])[entry.id]
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return httpx.Response(payload["status_code"], text=payload["text"])
