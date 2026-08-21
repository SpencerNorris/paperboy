"""The `history` collector: newest->oldest backfill with gap-based deletion
tombstones, plus `pts`-based catch-up (`catch_up`) applying edits and
deletions delivered via `updates.getChannelDifference` (ADR-0004).

Resumability has two independent layers: a per-run `offset_id` cursor in
`sync_state('history', ...)` lets a killed/Ctrl-C'd backfill continue paging
from where it stopped, while `sync_ranges` records which numeric message-id
spans have been fully swept *and* gap-probed — the two are deliberately not
conflated (see the note on `_probe_gaps`). `catch_up` uses a third, separate
cursor: `sync_state('channel', ...)`'s `pts`, seeded by the `channel`
collector from `channelFull.pts`.
"""

from __future__ import annotations

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import msg_uri, peer_ref_uri, utc_now_iso
from paperboy.store.edges import add_edge
from paperboy.store.messages import mark_deleted, upsert_message
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import add_range, get_state, set_state
from paperboy.targets import Target

_HISTORY_PAGE_SIZE = 100
_GET_MESSAGES_CHUNK = 200
_CHANNEL_DIFFERENCE_LIMIT = 100


def _latest_revision_hash(ctx: CollectContext, uri: str) -> str | None:
    row = ctx.store.conn.execute(
        "SELECT content_hash FROM message_revisions WHERE message_uri=? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (uri,),
    ).fetchone()
    return row["content_hash"] if row else None


class HistoryCollector:
    name = "history"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.input_channel is None or ctx.channel_id is None:
            # The `channel` phase didn't complete (e.g. it raised `PhaseStop`
            # on a FLOOD_WAIT during resolve()/getFullChannel before setting
            # these) — a handled disposition the recipe layer records and
            # continues past, never a bare `AssertionError` crash.
            raise PhaseStop(
                "history skipped: channel context not established "
                "(channel phase did not complete)"
            )
        channel_id = ctx.channel_id
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}

        resume = get_state(ctx.store, "history", str(channel_id)) or {}
        cursor: int = resume.get("offset_id", 0)

        ids_seen: set[int] = set()
        min_id: int | None = None
        max_id: int | None = None

        while True:
            page = [
                m
                async for m in ctx.gateway.iter_history(
                    ctx.input_channel, offset_id=cursor, limit=_HISTORY_PAGE_SIZE
                )
            ]
            if not page:
                break

            for m in page:
                self._observe_message(ctx, channel_id, m, counts)
                mid = m["id"]
                ids_seen.add(mid)
                min_id = mid if min_id is None else min(min_id, mid)
                max_id = mid if max_id is None else max(max_id, mid)

            cursor = min(m["id"] for m in page)
            set_state(ctx.store, "history", str(channel_id), {"offset_id": cursor})

        if min_id is not None and max_id is not None:
            await self._probe_gaps(ctx, channel_id, min_id, max_id, ids_seen, counts)
            add_range(ctx.store, channel_id, min_id, max_id)

        return CollectResult(name=self.name, counts=counts)

    def _observe_message(
        self, ctx: CollectContext, channel_id: int, m: dict, counts: dict[str, int]
    ) -> None:
        observed_at = utc_now_iso()
        raw_id = ctx.store.add_raw(
            m.get("_", "Message"), m, ctx.tier, {"channel_id": channel_id}
        )
        uri = msg_uri(channel_id, m["id"])
        before = _latest_revision_hash(ctx, uri)
        upsert_message(ctx.store, channel_id, m, raw_id, observed_at, ctx.tier)
        after = _latest_revision_hash(ctx, uri)
        counts["messages"] += 1
        if after != before:
            counts["revisions"] += 1

        from_id = m.get("from_id")
        if from_id and from_id.get("_", "").lower() == "peeruser":
            # We only have the bare peer reference here (no username/name) —
            # record it as `min`, honestly reflecting how little we know; a
            # Phase 2 `profiles` collector fills in the rest.
            stub = {"_": "User", "id": from_id["user_id"], "min": True}
            upsert_peer(
                ctx.store, stub, raw_id, observed_at,
                seen_in_chat=channel_id, seen_in_msg=m["id"],
            )

        fwd_from = m.get("fwd_from")
        object_uri = peer_ref_uri(fwd_from.get("from_id")) if fwd_from else None
        if object_uri:
            add_edge(
                ctx.store, uri, "forwarded_from", object_uri, observed_at, ctx.tier, raw_id,
                {"fwd_from": fwd_from},
            )
            counts["edges"] += 1

    async def _probe_gaps(
        self,
        ctx: CollectContext,
        channel_id: int,
        min_id: int,
        max_id: int,
        ids_seen: set[int],
        counts: dict[str, int],
    ) -> None:
        """Probe every id in `[min_id, max_id]` that `iter_history` never
        returned. This is *this sweep's* candidate-gap set — the numeric
        complement of what we actually saw, not `sync.missing_ids()` (which
        answers a different question: what isn't yet covered by any
        previously verified-complete range at all, useful across separate
        runs, not within one).
        """
        assert ctx.input_channel is not None
        candidates = sorted(set(range(min_id, max_id + 1)) - ids_seen)
        for start in range(0, len(candidates), _GET_MESSAGES_CHUNK):
            chunk = candidates[start : start + _GET_MESSAGES_CHUNK]
            results = await ctx.gateway.get_messages(ctx.input_channel, chunk)
            for r in results:
                if r.get("_", "").lower() != "messageempty":
                    continue
                observed_at = utc_now_iso()
                ctx.store.add_raw("MessageEmpty", r, ctx.tier, {"channel_id": channel_id})
                mark_deleted(ctx.store, channel_id, r["id"], "empty", observed_at)
                counts["tombstones"] += 1

    async def catch_up(self, ctx: CollectContext) -> CollectResult:
        """Apply everything since the last stored `pts` via `getChannelDifference`.

        `new_messages` are upserted; `other_updates` are applied by kind —
        `updateEditChannelMessage` appends a revision (via `upsert_message`'s
        own content-hash check), `updateDeleteChannelMessages` tombstones
        with `evidence="update"` (spec §7's highest-confidence deletion
        evidence). A `channelDifferenceTooLong` re-seeds `pts` from the
        payload's `dialog` and returns with `stopped="resynced"` — a full
        gap probe for this channel is the caller's job (a future `watch`
        loop iteration or a plain re-run of `history`).
        """
        if ctx.input_channel is None or ctx.channel_id is None:
            raise PhaseStop(
                "history skipped: channel context not established "
                "(channel phase did not complete)"
            )
        channel_id = ctx.channel_id
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}

        state = get_state(ctx.store, "channel", str(channel_id)) or {}
        pts = state.get("pts", 0)

        diff = await ctx.gateway.get_channel_difference(
            ctx.input_channel, pts, _CHANNEL_DIFFERENCE_LIMIT
        )
        ctx.store.add_raw(
            diff.get("_", "ChannelDifference"), diff, ctx.tier, {"channel_id": channel_id}
        )

        if diff.get("_", "").lower() == "channeldifferencetoolong":
            resynced_pts = diff.get("dialog", {}).get("pts", pts)
            set_state(ctx.store, "channel", str(channel_id), {"pts": resynced_pts})
            return CollectResult(name=self.name, counts=counts, stopped="resynced")

        for m in diff.get("new_messages", []):
            self._observe_message(ctx, channel_id, m, counts)

        for update in diff.get("other_updates", []):
            kind = update.get("_", "").lower()
            if kind == "updateeditchannelmessage":
                self._observe_message(ctx, channel_id, update["message"], counts)
            elif kind == "updatedeletechannelmessages":
                for mid in update.get("messages", []):
                    mark_deleted(ctx.store, channel_id, mid, "update", utc_now_iso())
                    counts["tombstones"] += 1

        set_state(ctx.store, "channel", str(channel_id), {"pts": diff.get("pts", pts)})
        return CollectResult(name=self.name, counts=counts)
