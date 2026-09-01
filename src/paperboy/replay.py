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

import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import httpx

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.store.db import dumps
from paperboy.targets import parse_target


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


class ReplaySource:
    """Read-only access to a source DB's raw log + its content-addressed media."""

    def __init__(self, conn: sqlite3.Connection, media_root: Path) -> None:
        self.conn = conn
        self.media_root = media_root
        # A real archive captured before this feature existed (ADR-0005) has
        # only pre-0003 migrations applied — `raw_records` has no `run_id`
        # column at all yet, not merely NULL values in it. `ReplaySource` is
        # strictly read-only and never applies migrations to the source, so
        # `runs()` must fall back to treating the whole log as legacy rather
        # than querying a column that may not exist.
        self._has_run_id = any(
            r[1] == "run_id" for r in conn.execute("PRAGMA table_info(raw_records)")
        )

    @classmethod
    def open(cls, db_path: Path, media_root: Path) -> Self:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return cls(conn, media_root)

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

    def resolve_targets(self, run: ReplayRun) -> list[str]:
        """Every distinct `target` a `resolve()` was recorded against WITHIN
        `run`, in first-seen (capture) order — `reproject` re-runs a full
        collect per target per historical run (ADR-0005)."""
        kind_sql, kind_params = _kind_clause(("resolvedpeer",))
        rows = self.conn.execute(
            "SELECT json_extract(context_json, '$.target') AS target FROM raw_records "
            f"WHERE {kind_sql} AND target IS NOT NULL AND id BETWEEN ? AND ? ORDER BY id",
            (*kind_params, run.lo, run.hi),
        ).fetchall()
        seen: dict[str, None] = {}
        for r in rows:
            seen.setdefault(r["target"])
        return list(seen)

    def linked_group_ids(self, run: ReplayRun) -> set[int]:
        kind_sql, kind_params = _kind_clause(("chatfull",))
        rows = self.conn.execute(
            "SELECT json_extract(payload_json, '$.full_chat.linked_chat_id') AS g "
            f"FROM raw_records WHERE {kind_sql} AND id BETWEEN ? AND ?",
            (*kind_params, run.lo, run.hi),
        ).fetchall()
        return {r["g"] for r in rows if r["g"]}

    def has_kind(self, run: ReplayRun, *kinds: str) -> bool:
        kind_sql, kind_params = _kind_clause(kinds)
        return self.conn.execute(
            f"SELECT 1 FROM raw_records WHERE {kind_sql} AND id BETWEEN ? AND ? LIMIT 1",
            (*kind_params, run.lo, run.hi),
        ).fetchone() is not None

    def has_context_channel(self, run: ReplayRun, channel_ids: set[int]) -> bool:
        return any(
            self.conn.execute(
                "SELECT 1 FROM raw_records "
                "WHERE json_extract(context_json, '$.channel_id') = ? "
                "AND id BETWEEN ? AND ? LIMIT 1",
                (cid, run.lo, run.hi),
            ).fetchone() is not None
            for cid in channel_ids
        )


class RawReplayGateway:
    """`Gateway` served from a raw log, scoped to ONE historical `ReplayRun`
    (ADR-0005) — every query below is additionally bounded to
    `id BETWEEN run.lo AND run.hi`, so within one run each call site has at
    most one matching record, same as a live RPC has one now. Never touches
    the network — there is no client, no session, no `Budget` anywhere in
    this class."""

    def __init__(self, source: ReplaySource, clock: ReplayClock, run: ReplayRun) -> None:
        self._src = source
        self._clock = clock
        self._run = run
        # get_channel_difference is inherently sequential (a pts catch-up
        # loop); a per-channel cursor over the stored pages models that.
        self._diff_cursor: dict[int, int] = {}

    def _latest(
        self, kinds: tuple[str, ...], where: str, params: tuple
    ) -> sqlite3.Row | None:
        kind_sql, kind_params = _kind_clause(kinds)
        return self._src.conn.execute(
            f"SELECT observed_at, payload_json FROM raw_records "
            f"WHERE {kind_sql} AND {where} AND id BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
            (*kind_params, *params, self._run.lo, self._run.hi),
        ).fetchone()

    def _serve(self, row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return payload

    async def resolve(self, target_value: str) -> dict:
        kind_sql, kind_params = _kind_clause(("resolvedpeer",))
        rows = self._src.conn.execute(
            "SELECT observed_at, payload_json, "
            "json_extract(context_json, '$.target') AS target "
            f"FROM raw_records WHERE {kind_sql} AND id BETWEEN ? AND ? ORDER BY id DESC",
            (*kind_params, self._run.lo, self._run.hi),
        ).fetchall()
        for row in rows:
            raw_target = row["target"]
            if raw_target and parse_target(raw_target).value == target_value:
                return self._serve(row)
        raise SkipAndRecord(f"replay: no ResolvedPeer recorded for {target_value!r}")

    async def get_full_channel(self, input_channel: dict) -> dict:
        row = self._latest(
            ("chatfull",),
            "json_extract(context_json, '$.channel_id') = ?",
            (input_channel["channel_id"],),
        )
        if row is None:
            raise SkipAndRecord(
                f"replay: no ChatFull recorded for channel {input_channel['channel_id']}"
            )
        return self._serve(row)

    async def get_self(self) -> dict:
        row = self._latest(("user",), "tier = 'self'", ())
        if row is None:
            raise SkipAndRecord("replay: no self User recorded")
        return self._serve(row)

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        # Reconstructs the original paging (spec §3): id DESC below the
        # cursor. Secondary order id ASC (capture order) so an edited
        # message's revisions replay oldest-first. MessageEmpty is excluded —
        # getHistory never yielded one; they came from the probe.
        kind_sql, kind_params = _kind_clause(("message", "messageservice"))
        rows = self._src.conn.execute(
            "SELECT id, observed_at, payload_json, "
            "CAST(json_extract(payload_json, '$.id') AS INTEGER) AS msg_id "
            "FROM raw_records "
            f"WHERE {kind_sql} "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "AND (? = 0 OR CAST(json_extract(payload_json, '$.id') AS INTEGER) < ?) "
            "AND id BETWEEN ? AND ? "
            "ORDER BY msg_id DESC, id ASC",
            (
                *kind_params, input_channel["channel_id"], offset_id, offset_id,
                self._run.lo, self._run.hi,
            ),
        ).fetchall()
        # Never split one msg_id's records across pages: the collector's next
        # cursor is `min(page ids)` and the next page takes strictly-below,
        # so a split id's tail records would be unreachable forever.
        page = list(rows[:limit])
        while len(rows) > len(page) and rows[len(page)]["msg_id"] == page[-1]["msg_id"]:
            page.append(rows[len(page)])
        self._clock.begin_batch()
        for row in page:
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            yield json.loads(row["payload_json"])

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        channel_id = input_channel["channel_id"]
        self._clock.begin_batch()
        out: list[dict] = []
        for i in ids:
            row = self._latest(
                ("message", "messageservice", "messageempty"),
                "json_extract(context_json, '$.channel_id') = ? "
                "AND CAST(json_extract(payload_json, '$.id') AS INTEGER) = ?",
                (channel_id, i),
            )
            if row is None:
                # D4.1: a placeholder, NOT a synthetic messageEmpty — that
                # would fabricate deletion evidence (mark_deleted evidence=
                # 'empty') for ids the original run never observed as
                # deleted (a gap the probe found alive, or a range only
                # reachable via replayed catch-up). The collector skips any
                # non-messageEmpty shape, so this projects nothing — exactly
                # the original source's state.
                out.append({"_": "ReplayUnknownMessage", "id": i})
                continue
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            out.append(json.loads(row["payload_json"]))
        return out

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        del limit
        channel_id = input_channel["channel_id"]
        idx = self._diff_cursor.get(channel_id, 0)
        # Substring, not prefix/suffix: the stored kind is one of
        # `updates.channelDifference`/`...Empty`/`...TooLong` — namespaced
        # AND suffixed, so `_kind_clause`'s suffix tolerance doesn't cover
        # it; a plain "contains" match does.
        row = self._src.conn.execute(
            "SELECT observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) LIKE '%channeldifference%' "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "AND id BETWEEN ? AND ? "
            "ORDER BY id ASC LIMIT 1 OFFSET ?",
            (channel_id, self._run.lo, self._run.hi, idx),
        ).fetchone()
        self._diff_cursor[channel_id] = idx + 1

        if row is None:
            # D4.4: exhausted the stored pages — a synthetic final EMPTY
            # diff, not SkipAndRecord. A mid-catch_up SkipAndRecord would
            # mark the whole history phase skipped and discard the backfill
            # counts already applied this run.
            stamp = self._src.conn.execute(
                "SELECT MAX(observed_at) AS t FROM raw_records "
                "WHERE json_extract(context_json, '$.channel_id') = ? "
                "AND id BETWEEN ? AND ?",
                (channel_id, self._run.lo, self._run.hi),
            ).fetchone()
            synthetic = {"_": "updates.channelDifferenceEmpty", "final": True, "pts": pts}
            self._clock.begin_batch()
            self._clock.serve(stamp["t"], synthetic)
            return synthetic

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
            m_row = self._latest(
                ("message", "messageservice", "messageempty"),
                "json_extract(context_json, '$.channel_id') = ? AND payload_json = ?",
                (channel_id, dumps(m)),
            )
            if m_row is not None:
                self._clock.serve_json(m_row["observed_at"], m_row["payload_json"])
        return payload

    async def get_authorizations(self) -> dict:
        raise SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")

    async def get_password_state(self) -> dict:
        raise SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")

    async def get_privacy(self, key: str) -> dict:
        del key
        raise SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")

    async def download_media(self, input_channel: dict, message: dict) -> bytes | None:
        row = self._latest(
            ("mediadownload",),
            "json_extract(context_json, '$.channel_id') = ? "
            "AND json_extract(context_json, '$.msg_id') = ?",
            (input_channel["channel_id"], message["id"]),
        )
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        sha = payload["sha256"]
        path = Path(payload["path"])
        # A small local disk stat/read on an offline, single-user CLI tool —
        # not worth a trio/anyio dependency for.
        if not path.exists():  # noqa: ASYNC240
            # A different profile than the one that captured it (spec §4):
            # re-derive the content-addressed location under THIS source's
            # media root rather than trusting the stored (possibly foreign)
            # absolute path.
            path = self._src.media_root / sha[:2] / (sha + Path(payload["path"]).suffix)
        if not path.exists():
            raise SkipAndRecord(f"replay: media file missing for sha {sha}")
        data = path.read_bytes()
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return data

    async def get_channel_recommendations(self, input_channel: dict) -> dict:
        row = self._latest(
            ("chats", "chatsslice"),
            "json_extract(context_json, '$.channel_id') = ?",
            (input_channel["channel_id"],),
        )
        if row is None:
            raise SkipAndRecord(
                "replay: no channel recommendations recorded for channel "
                f"{input_channel['channel_id']}"
            )
        return self._serve(row)

    async def check_chat_invite(self, hash_: str) -> dict:
        row = self._latest(
            ("chatinvite", "chatinvitealready", "chatinvitepeek"),
            "json_extract(context_json, '$.hash') = ?",
            (hash_,),
        )
        if row is None:
            raise SkipAndRecord(f"replay: no ChatInvite recorded for hash {hash_!r}")
        return self._serve(row)

    async def join_channel(self, input_channel: dict) -> dict:
        # D4.3: reproject always runs with allow_join=True so a source whose
        # original run used --join still replays its discussion sweep — but
        # nothing is actually joined. No network, no session, no real write
        # anywhere in this class.
        del input_channel
        return {"_": "Updates", "updates": []}

    async def get_sponsored_messages(self, input_channel: dict) -> dict:
        kind_sql, kind_params = _kind_clause(("sponsoredmessage",))
        rows = self._src.conn.execute(
            "SELECT observed_at, payload_json FROM raw_records "
            f"WHERE {kind_sql} "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "AND id BETWEEN ? AND ? ORDER BY id ASC",
            (*kind_params, input_channel["channel_id"], self._run.lo, self._run.hi),
        ).fetchall()
        self._clock.begin_batch()
        if not rows:
            # D4.2: the original collector never stores the envelope, only
            # each SponsoredMessage individually — empty-and-skipped
            # originals are indistinguishable, so both project nothing.
            return {"_": "sponsoredMessagesEmpty"}
        messages = []
        for row in rows:
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            messages.append(json.loads(row["payload_json"]))
        return {"_": "SponsoredMessages", "messages": messages}

    # Person-layer replay methods (`participants`/`profiles`, spec §10) —
    # deliberately UNIMPLEMENTED here. This leg (person-layer plan Tasks
    # 1-5) only grows the `Gateway` Protocol so `TelethonGateway`/
    # `FakeGateway` can serve these seven methods; the replay-side
    # implementation (7 replay methods + `get_privacy` serving + phase
    # detection, plan Task 10) is a later leg. `reproject.py`'s current
    # collector list never calls these — `ParticipantsCollector`/
    # `ProfilesCollector` are not wired into it until that same later leg —
    # so a stub that fails loudly if ever reached (rather than one that
    # silently returns an empty result) is the honest placeholder: it keeps
    # `RawReplayGateway` a structural `Gateway` for pyright without
    # pretending replay support exists yet.
    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        raise NotImplementedError("participants replay lands in a later person-layer leg (Task 10)")

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        raise NotImplementedError("participants replay lands in a later person-layer leg (Task 10)")

    async def get_users(self, refs: list[dict]) -> list[dict]:
        raise NotImplementedError("profiles replay lands in a later person-layer leg (Task 10)")

    async def get_full_user(self, ref: dict) -> dict:
        raise NotImplementedError("profiles replay lands in a later person-layer leg (Task 10)")

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        raise NotImplementedError("profiles replay lands in a later person-layer leg (Task 10)")

    async def download_user_photo(self, photo: dict) -> bytes | None:
        raise NotImplementedError("profiles replay lands in a later person-layer leg (Task 10)")

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        raise NotImplementedError("participants replay lands in a later person-layer leg (Task 10)")


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
        row = self._src.conn.execute(
            "SELECT id, observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) IN ('tme_page', 'wayback_cdx') "
            "AND json_extract(payload_json, '$.url') = ? AND id > ? AND id <= ? "
            "ORDER BY id ASC LIMIT 1",
            (url, self._served.get(url, self._run.lo - 1), self._run.hi),
        ).fetchone()
        if row is None:
            return httpx.Response(404, text="")
        self._served[url] = row["id"]
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return httpx.Response(payload["status_code"], text=payload["text"])
