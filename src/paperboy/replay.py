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
from pathlib import Path
from typing import Self

import httpx

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.store.db import dumps
from paperboy.targets import parse_target


class ReplaySource:
    """Read-only access to a source DB's raw log + its content-addressed media."""

    def __init__(self, conn: sqlite3.Connection, media_root: Path) -> None:
        self.conn = conn
        self.media_root = media_root

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

    def resolve_targets(self) -> list[str]:
        """Every distinct `target` a `resolve()` was recorded against, in
        first-seen (capture) order — `reproject` re-runs a full collect per
        target."""
        rows = self.conn.execute(
            "SELECT json_extract(context_json, '$.target') AS target FROM raw_records "
            "WHERE lower(kind) = 'resolvedpeer' AND target IS NOT NULL ORDER BY id"
        ).fetchall()
        seen: dict[str, None] = {}
        for r in rows:
            seen.setdefault(r["target"])
        return list(seen)

    def linked_group_ids(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT json_extract(payload_json, '$.full_chat.linked_chat_id') AS g "
            "FROM raw_records WHERE lower(kind) = 'chatfull'"
        ).fetchall()
        return {r["g"] for r in rows if r["g"]}

    def has_kind(self, *kinds: str) -> bool:
        qmarks = ",".join("?" * len(kinds))
        return self.conn.execute(
            f"SELECT 1 FROM raw_records WHERE lower(kind) IN ({qmarks}) LIMIT 1",
            kinds,
        ).fetchone() is not None

    def has_context_channel(self, channel_ids: set[int]) -> bool:
        return any(
            self.conn.execute(
                "SELECT 1 FROM raw_records "
                "WHERE json_extract(context_json, '$.channel_id') = ? LIMIT 1",
                (cid,),
            ).fetchone() is not None
            for cid in channel_ids
        )


class RawReplayGateway:
    """`Gateway` served from a raw log. Never touches the network — there is
    no client, no session, no `Budget` anywhere in this class."""

    def __init__(self, source: ReplaySource, clock: ReplayClock) -> None:
        self._src = source
        self._clock = clock
        # get_channel_difference is inherently sequential (a pts catch-up
        # loop); a per-channel cursor over the stored pages models that.
        self._diff_cursor: dict[int, int] = {}

    def _latest(
        self, kinds: tuple[str, ...], where: str, params: tuple
    ) -> sqlite3.Row | None:
        qmarks = ",".join("?" * len(kinds))
        return self._src.conn.execute(
            f"SELECT observed_at, payload_json FROM raw_records "
            f"WHERE lower(kind) IN ({qmarks}) AND {where} ORDER BY id DESC LIMIT 1",
            (*kinds, *params),
        ).fetchone()

    def _serve(self, row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return payload

    async def resolve(self, target_value: str) -> dict:
        rows = self._src.conn.execute(
            "SELECT observed_at, payload_json, "
            "json_extract(context_json, '$.target') AS target "
            "FROM raw_records WHERE lower(kind) = 'resolvedpeer' ORDER BY id DESC"
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
        rows = self._src.conn.execute(
            "SELECT id, observed_at, payload_json, "
            "CAST(json_extract(payload_json, '$.id') AS INTEGER) AS msg_id "
            "FROM raw_records "
            "WHERE lower(kind) IN ('message', 'messageservice') "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "AND (? = 0 OR CAST(json_extract(payload_json, '$.id') AS INTEGER) < ?) "
            "ORDER BY msg_id DESC, id ASC",
            (input_channel["channel_id"], offset_id, offset_id),
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
        row = self._src.conn.execute(
            "SELECT observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) LIKE 'channeldifference%' "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "ORDER BY id ASC LIMIT 1 OFFSET ?",
            (channel_id, idx),
        ).fetchone()
        self._diff_cursor[channel_id] = idx + 1

        if row is None:
            # D4.4: exhausted the stored pages — a synthetic final EMPTY
            # diff, not SkipAndRecord. A mid-catch_up SkipAndRecord would
            # mark the whole history phase skipped and discard the backfill
            # counts already applied this run.
            stamp = self._src.conn.execute(
                "SELECT MAX(observed_at) AS t FROM raw_records "
                "WHERE json_extract(context_json, '$.channel_id') = ?",
                (channel_id,),
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
        rows = self._src.conn.execute(
            "SELECT observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) = 'sponsoredmessage' "
            "AND json_extract(context_json, '$.channel_id') = ? ORDER BY id ASC",
            (input_channel["channel_id"],),
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


class RawReplayWebClient:
    """Serve stored `tme_page`/`wayback_cdx` captures as `httpx.Response`s.

    Keyed by exact URL — the web collector re-derives the same URL sequence
    from the same parsed posts, so replay requests exactly the recorded set.
    Repeat captures of one URL (multi-run sources) serve in capture order.
    An unrecorded URL is a definitive empty 404: the page loop must stop
    cleanly there, exactly where the original run stopped.
    """

    def __init__(self, source: ReplaySource, clock: ReplayClock) -> None:
        self._src = source
        self._clock = clock
        self._served: dict[str, int] = {}  # url -> raw id already served

    def get(self, url: str) -> httpx.Response:
        row = self._src.conn.execute(
            "SELECT id, observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) IN ('tme_page', 'wayback_cdx') "
            "AND json_extract(payload_json, '$.url') = ? AND id > ? "
            "ORDER BY id ASC LIMIT 1",
            (url, self._served.get(url, 0)),
        ).fetchone()
        if row is None:
            return httpx.Response(404, text="")
        self._served[url] = row["id"]
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return httpx.Response(payload["status_code"], text=payload["text"])
