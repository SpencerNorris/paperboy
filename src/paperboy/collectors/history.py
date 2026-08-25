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


def _author_stub(from_id: dict | None) -> dict | None:
    """A minimal `min` peer object for a message's `from_id`, or None.

    Matched case-insensitively: Telethon's `to_dict()` emits the PascalCase
    class name (`"PeerUser"`), not the lowercase TL constructor name.
    """
    if from_id is None:
        return None
    kind = from_id.get("_", "").lower()
    if kind == "peeruser":
        return {"_": "User", "id": from_id["user_id"], "min": True}
    if kind == "peerchannel":
        return {"_": "Channel", "id": from_id["channel_id"], "min": True}
    return None


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

    async def collect(
        self,
        ctx: CollectContext,
        *,
        channel_id: int | None = None,
        input_channel: dict | None = None,
        probe_gaps: bool = True,
        page_budget: int | None = None,
    ) -> CollectResult:
        channel_id = channel_id if channel_id is not None else ctx.channel_id
        input_channel = input_channel if input_channel is not None else ctx.input_channel
        if input_channel is None or channel_id is None:
            # The `channel` phase didn't complete (e.g. it raised `PhaseStop`
            # on a FLOOD_WAIT during resolve()/getFullChannel before setting
            # these) — a handled disposition the recipe layer records and
            # continues past, never a bare `AssertionError` crash.
            raise PhaseStop(
                "history skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}

        resume = get_state(ctx.store, "history", str(channel_id)) or {}
        sweep = get_state(ctx.store, "history_sweep", str(channel_id)) or {}
        committed_high: int = sweep.get("max_id_seen", 0)
        complete: bool = sweep.get("backfill_complete", False)
        resuming: bool = sweep.get("incremental_in_progress", False)

        # Backfill always resumes from its stored cursor. A *fresh* incremental
        # run starts at the newest message instead — but one that a page budget
        # interrupted must resume from where it stopped, or every id between
        # that stop and the previous high-water mark is fetched by nobody, ever.
        incremental = complete
        cursor: int = 0 if (incremental and not resuming) else resume.get("offset_id", 0)
        stop_at = committed_high if incremental else 0
        # The highest id seen so far by a sweep that has not finished yet. It
        # must survive across budget stops: the true maximum is observed on the
        # FIRST page of a catch-up, which is usually the run the budget kills.
        pending_high: int = sweep.get("pending_high", committed_high)
        pages = 0

        ids_seen: set[int] = set()
        min_id: int | None = None
        max_id: int | None = None

        while True:
            page = [
                m
                async for m in ctx.gateway.iter_history(
                    input_channel, offset_id=cursor, limit=_HISTORY_PAGE_SIZE
                )
            ]
            if not page:
                complete = True
                break

            for m in page:
                self._observe_message(ctx, channel_id, m, counts)
                mid = m["id"]
                ids_seen.add(mid)
                min_id = mid if min_id is None else min(min_id, mid)
                max_id = mid if max_id is None else max(max_id, mid)

            cursor = min(m["id"] for m in page)
            pending_high = max(pending_high, max(m["id"] for m in page))
            set_state(ctx.store, "history", str(channel_id), {"offset_id": cursor})
            # `max_id_seen` deliberately does NOT advance here. Promoting it
            # before the span below has been walked would make the next run's
            # stop test fire on its own first page, stranding everything in
            # between. It is committed only on a clean finish, below.
            set_state(ctx.store, "history_sweep", str(channel_id), {
                "max_id_seen": committed_high,
                "pending_high": pending_high,
                "backfill_complete": complete,
                "incremental_in_progress": incremental,
            })

            # Caught up beats out of budget: if we have paged back into known
            # territory the run is finished, budget or no budget.
            if incremental and cursor <= stop_at:
                break
            pages += 1
            if page_budget is not None and pages >= page_budget:
                raise PhaseStop(
                    f"page budget ({page_budget}) reached at offset_id={cursor}; "
                    "re-run to continue from the saved cursor",
                    counts=counts,
                )

        settled = max(committed_high, pending_high)
        set_state(ctx.store, "history_sweep", str(channel_id), {
            "max_id_seen": settled,
            "pending_high": settled,
            "backfill_complete": complete,
            "incremental_in_progress": False,
        })

        # `add_range` records a span as *verified-complete* — "every id was
        # either stored or probed" (store/sync.py). With probing off that is
        # untrue, so writing it would make `missing_ids()` report zero gaps for
        # this channel forever. Both go together or neither does.
        if probe_gaps and min_id is not None and max_id is not None:
            await self._probe_gaps(
                ctx, channel_id, input_channel, min_id, max_id, ids_seen, counts
            )
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

        # We only have the bare peer reference here (no username/name) — record
        # it as `min`, honestly reflecting how little we know; a Phase 2
        # `profiles` collector fills in the rest. Channel-typed authors count
        # too: in a linked discussion group an anonymous or channel-authored
        # comment arrives as `PeerChannel`, and those commenters are exactly
        # the people-discovery data the `discussion` phase exists to collect.
        stub = _author_stub(m.get("from_id"))
        if stub is not None:
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
        input_channel: dict,
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
        candidates = sorted(set(range(min_id, max_id + 1)) - ids_seen)
        for start in range(0, len(candidates), _GET_MESSAGES_CHUNK):
            chunk = candidates[start : start + _GET_MESSAGES_CHUNK]
            results = await ctx.gateway.get_messages(input_channel, chunk)
            for r in results:
                if r.get("_", "").lower() != "messageempty":
                    continue
                observed_at = utc_now_iso()
                ctx.store.add_raw("MessageEmpty", r, ctx.tier, {"channel_id": channel_id})
                mark_deleted(ctx.store, channel_id, r["id"], "empty", observed_at)
                counts["tombstones"] += 1

    async def catch_up(self, ctx: CollectContext) -> CollectResult:
        """Apply everything since the last stored `pts` via `getChannelDifference`.

        Loops the call until the server sets `final`, projecting each page and
        persisting its `pts` as it lands, so a backlog larger than one page is
        not truncated (issue #25) and an interruption resumes from the last
        stored cursor. `new_messages` are upserted; `other_updates` are applied
        by kind — `updateEditChannelMessage` appends a revision (via
        `upsert_message`'s own content-hash check), `updateDeleteChannelMessages`
        tombstones with `evidence="update"` (spec §7's highest-confidence
        deletion evidence).

        A `channelDifferenceTooLong` projects the recovery messages it carries,
        re-seeds `pts` from the payload's `dialog` (only when the dialog
        actually carries an int — issue #22), and returns `stopped="resynced"`
        — a full gap probe is then the caller's job. The loop stops early on a
        non-final page that fails to advance `pts` (a misbehaving server, not a
        backlog) and raises `PhaseStop` — carrying the counts applied so far —
        when `catchup_page_budget` pages have been pulled in one run.
        """
        if ctx.input_channel is None or ctx.channel_id is None:
            raise PhaseStop(
                "history skipped: channel context not established "
                "(channel phase did not complete)"
            )
        channel_id = ctx.channel_id
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}

        state = get_state(ctx.store, "channel", str(channel_id)) or {}
        # `or 0` and not a plain default: a pre-fix run could have persisted
        # {"pts": None} (issue #22), and forwarding None to the gateway dies
        # inside Telethon as a struct.error — outside the disposition system.
        pts = state.get("pts") or 0
        budget = ctx.settings.catchup_page_budget

        # getChannelDifference returns at most one page (`_CHANNEL_DIFFERENCE_LIMIT`
        # updates); the TL contract is to keep calling until the server sets
        # `final` (issue #25). A single call truncated any larger backlog and
        # reported success. Each page is projected and its pts persisted as it
        # lands, so an interruption resumes from the last stored cursor.
        pages = 0
        while True:
            diff = await ctx.gateway.get_channel_difference(
                ctx.input_channel, pts, _CHANNEL_DIFFERENCE_LIMIT
            )
            ctx.store.add_raw(
                diff.get("_", "ChannelDifference"), diff, ctx.tier, {"channel_id": channel_id}
            )

            if diff.get("_", "").lower() == "channeldifferencetoolong":
                # The resync response carries the newest messages as a recovery
                # payload — project them; they already reached raw_records above.
                for m in diff.get("messages", []):
                    self._observe_message(ctx, channel_id, m, counts)
                # `Dialog.pts` is flags.0?int and Telethon emits it present-with-
                # None when unset, so a `.get("pts", fallback)` never falls back
                # (issue #22). Never persist a non-int cursor: the next reader
                # would hand None to Telethon and crash outside the disposition
                # system. Keeping the old cursor just repeats the resync signal.
                resynced_pts = (diff.get("dialog") or {}).get("pts")
                if isinstance(resynced_pts, int):
                    set_state(ctx.store, "channel", str(channel_id), {"pts": resynced_pts})
                else:
                    ctx.log.warning(
                        "history: channelDifferenceTooLong for %s carried no dialog pts; "
                        "keeping the stored cursor (%s) — full re-sweep required",
                        channel_id, pts,
                    )
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

            new_pts = diff.get("pts", pts)
            if not isinstance(new_pts, int):
                new_pts = pts

            if diff.get("final"):
                set_state(ctx.store, "channel", str(channel_id), {"pts": new_pts})
                return CollectResult(name=self.name, counts=counts)

            # A non-final page that does not advance pts would loop forever — a
            # misbehaving server, not a real backlog. Stop rather than spin; the
            # pts-advance guard, not the budget below, is the loop's real bound.
            if new_pts <= pts:
                ctx.log.warning(
                    "history: getChannelDifference for %s returned a non-final page "
                    "without advancing pts (%s -> %s); stopping catch-up",
                    channel_id, pts, new_pts,
                )
                return CollectResult(name=self.name, counts=counts)

            set_state(ctx.store, "channel", str(channel_id), {"pts": new_pts})
            pts = new_pts
            pages += 1

            if pages >= budget:
                raise PhaseStop(
                    f"catch-up page budget ({budget}) reached at pts={pts}; "
                    "re-run to continue from the saved cursor",
                    counts=counts,
                )
