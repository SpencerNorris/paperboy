"""The `discussion` collector: the linked group's comments, and the people in them.

For a broadcast channel a non-admin account can enumerate nothing about
subscribers — not the member list, not even the admin list
(`docs/research/sources/mtproto-participants-users.md` §1.3, sourced to TDLib).
The linked discussion group is therefore *the* person vector, and reading it
never requires joining unless the group sets `join_to_send`.

The subtle part is mapping a comment back to the post it hangs off.
`reply_to_top_id` is **not** the channel post id: it is the id of the group's
auto-forwarded mirror of that post. The mirror carries the real post id in
`fwd_from.channel_post`, so the chain is

    comment.reply_to_top_id -> mirror.id -> mirror.fwd_from.channel_post -> post

and a forwarded message only counts as a mirror if its origin is *our* channel —
members forward third-party posts into groups all the time, and treating one of
those as a mirror silently attributes comments to the wrong post. A comment that
does not resolve is stored and counted, never guessed at.
"""

from __future__ import annotations

import json

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.history import HistoryCollector
from paperboy.ids import channel_uri, msg_uri, peer_ref_uri, utc_now_iso
from paperboy.store.edges import add_edge
from paperboy.store.repliers import backfill_recent_repliers
from paperboy.targets import Target

_COMMENTED_ON = "commented_on"
_REPLIED_TO = "replied_to"


class DiscussionCollector:
    name = "discussion"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None:
            raise PhaseStop(
                "discussion skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "messages": 0, "revisions": 0, "tombstones": 0, "edges": 0,
            "backfilled_peers": 0, "unmapped": 0,
        }

        # Zero-RPC harvest first. It reads only the store, so it still yields
        # peers when the group turns out to be absent or unreadable — which is
        # why it must not sit behind preflight.
        counts["backfilled_peers"] = backfill_recent_repliers(
            ctx.store, ctx.channel_id, ctx.tier
        )

        target = self._linked_group(ctx)
        if isinstance(target, str):
            return CollectResult(name=self.name, counts=counts, stopped=target)
        group_id, input_channel = target

        # Gap-probing is off: on a churn-heavy group of tens of thousands of
        # messages it is a second pass the size of the sweep, and it yields
        # only the weak `evidence='gap'` tier.
        sweep = await HistoryCollector().collect(
            ctx, channel_id=group_id, input_channel=input_channel,
            probe_gaps=False, page_budget=ctx.settings.discussion_page_budget,
        )
        for key in ("messages", "revisions", "tombstones", "edges"):
            counts[key] += sweep.counts.get(key, 0)

        self._write_thread_edges(ctx, group_id, counts)
        return CollectResult(name=self.name, counts=counts)

    def _linked_group(self, ctx: CollectContext) -> tuple[int, dict] | str:
        """`(group_id, input_channel)`, or a `stopped` reason string.

        Every failure here is a clean skip, never an exception: a channel with
        no discussion group, or one that gates reading behind membership, is a
        normal thing to encounter.

        The reasons are deliberately lexically disjoint — no reason contains
        another's distinguishing word — because tests assert on them, and an
        overlapping pair lets a test pass on the wrong branch.
        """
        row = ctx.store.conn.execute(
            "SELECT linked_chat_id FROM channels WHERE id=?", (ctx.channel_id,)
        ).fetchone()
        group_id = row["linked_chat_id"] if row else None
        if not group_id:
            # `0` is as meaningful as NULL here and must not be treated as a
            # channel id — falsy, not `is None`.
            return "no linked discussion group"

        peer = ctx.store.conn.execute(
            "SELECT access_hash, flags_json FROM peers WHERE uri=?",
            (channel_uri(group_id),),
        ).fetchone()
        if peer is None or not peer["access_hash"]:
            # A stored `0` is not a usable hash — it yields CHANNEL_INVALID
            # against live Telegram, a phase error rather than a clean skip.
            return f"discussion group {group_id}: no access hash known"

        flags = json.loads(peer["flags_json"]) if peer["flags_json"] else {}
        if flags.get("join_to_send"):
            # Reading is open to anyone *unless* this flag is set. Honouring it
            # is what keeps collection passive; `--join` is the (unimplemented)
            # escape hatch, tracked in issue #20.
            return (
                f"discussion group {group_id}: join_to_send is set, so reading it "
                "requires membership"
            )

        return group_id, {"channel_id": group_id, "access_hash": peer["access_hash"]}

    def _write_thread_edges(
        self, ctx: CollectContext, group_id: int, counts: dict[str, int]
    ) -> None:
        """Emit `commented_on` (person → channel post) and `replied_to`
        (comment → parent) from the rows the sweep just stored.

        Runs over stored rows rather than the live page stream so that a comment
        paged in before its mirror — or in an entirely earlier run — still maps.
        """
        # `collect()` has already rejected a None channel_id; restating it
        # here is what carries that guarantee across the method boundary.
        assert ctx.channel_id is not None
        channel_id = ctx.channel_id
        mirrors = self._mirror_map(ctx, group_id)
        rows = ctx.store.conn.execute(
            "SELECT uri, msg_id, from_uri, reply_to_msg_id, reply_to_top_id, source_raw_id "
            "FROM messages WHERE channel_id=? "
            "AND (reply_to_msg_id IS NOT NULL OR reply_to_top_id IS NOT NULL)",
            (group_id,),
        ).fetchall()

        for row in rows:
            observed_at = utc_now_iso()
            if row["reply_to_msg_id"]:
                # Every reply gets this, including a direct reply to a thread
                # root: `replied_to` is comment → parent, with no restriction to
                # nested replies.
                self._add_edge_once(
                    ctx, row["uri"], _REPLIED_TO, msg_uri(group_id, row["reply_to_msg_id"]),
                    observed_at, row["source_raw_id"], {"source": "discussion"}, counts,
                )

            top_id = row["reply_to_top_id"]
            if top_id is None:
                # Ordinary in-group chatter replying to another message. It was
                # never a comment-thread post, so it is not a mapping candidate
                # and must not inflate `unmapped` — on a real group these
                # dominate, and counting them would bury the signal.
                continue

            post_id = mirrors.get(top_id)
            if post_id is None or not row["from_uri"]:
                # No mirror for this thread root, or no resolvable author.
                # Counted once and reported, never attributed to a guess.
                counts["unmapped"] += 1
                continue

            self._add_edge_once(
                ctx, row["from_uri"], _COMMENTED_ON, msg_uri(channel_id, post_id),
                observed_at, row["source_raw_id"], {"comment_uri": row["uri"]}, counts,
            )

    def _mirror_map(self, ctx: CollectContext, group_id: int) -> dict[int, int]:
        """`group_msg_id -> channel_post_id` for the group's auto-forwarded
        copies of our channel's posts.

        Rebuilt from stored rows on every run, so it survives resumption. The
        origin check is what makes it a *mirror* rather than any old forward.
        """
        rows = ctx.store.conn.execute(
            "SELECT msg_id, fwd_json FROM messages "
            "WHERE channel_id=? AND fwd_json IS NOT NULL",
            (group_id,),
        ).fetchall()
        ours = channel_uri(ctx.channel_id) if ctx.channel_id is not None else None
        mirrors: dict[int, int] = {}
        for row in rows:
            fwd = json.loads(row["fwd_json"])
            post = fwd.get("channel_post")
            if post is not None and peer_ref_uri(fwd.get("from_id")) == ours:
                mirrors[row["msg_id"]] = post
        return mirrors

    def _add_edge_once(
        self,
        ctx: CollectContext,
        subject: str,
        predicate: str,
        object_: str,
        observed_at: str,
        source_raw_id: int | None,
        evidence: dict | None,
        counts: dict[str, int],
    ) -> None:
        """`add_edge`, skipped when the identical triple is already stored.

        These two predicates are structural facts — "X commented on Y" — not
        observations that vary over time like `message_metrics`. This method
        re-scans every stored group row on every run, so an unguarded insert
        would append a fresh row with a new `observed_at` and the previous run's
        `source_raw_id` for evidence this run never gathered. The guard lives
        here rather than in `add_edge` because `channel`, `history` and `graph`
        all still depend on that function's append-only semantics.
        """
        exists = ctx.store.conn.execute(
            "SELECT 1 FROM edges WHERE subject_uri=? AND predicate=? AND object_uri=? LIMIT 1",
            (subject, predicate, object_),
        ).fetchone()
        if exists is not None:
            return
        add_edge(ctx.store, subject, predicate, object_, observed_at, ctx.tier,
                 source_raw_id, evidence)
        counts["edges"] += 1
