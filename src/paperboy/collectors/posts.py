"""The `posts` collector (#91): fetch an explicit list of message ids by
`channels.getMessages` and project them like `history` does.

It runs only inside `fetch-from-list`, between `channel` and `media`, over the
ids in `Settings.post_msgs`; `collect` never runs it. Every object Telegram
answers is appended to the raw log first (context
`{"channel_id", "method": "channels.getMessages"}`) and then goes through the
shared `observe_message` projection, so a new post gets its message row, author
peer and forward edge, an edit appends a revision (and a counter change a
metric row), and a `MessageEmpty` answer becomes a tombstone - never a blank
row. The `method` tag is how `reproject` tells these receipts from history's
(`replay.ReplaySource.fetched_post_ids`).

Already-stored ids are fetched again on purpose: the point of the pass is that
edits and counters are current as of this run.
"""

from __future__ import annotations

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.history import observe_message
from paperboy.ids import msg_uri
from paperboy.targets import Target

GET_MESSAGES_BATCH = 100  # channels.getMessages accepts at most 100 ids per call
GET_MESSAGES_METHOD = "channels.getMessages"
# TL kinds that carry (or stand in for) a message. Anything else in an answer -
# notably the replay gateway's `ReplayUnknownMessage` placeholder - projects
# nothing and records no outcome.
_MESSAGE_KINDS = frozenset({"message", "messageservice", "messageempty"})


class PostsCollector:
    """Fetches `settings.post_msgs` from the established channel.

    `outcomes` is a caller-owned dict this collector fills, message uri ->
    `fetched | deleted_upstream`, as each object is projected - a live dict (not
    a `CollectResult` field) for the same reason `MediaCollector`'s is: a
    mid-phase `PhaseStop` discards the result yet the caller still needs the
    rows finished before it.
    """

    name = "posts"

    def __init__(self, *, outcomes: dict[str, str] | None = None) -> None:
        self._outcomes = outcomes

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.input_channel is None or ctx.channel_id is None:
            # Same guard as `history`/`media`: the `channel` phase did not complete.
            raise PhaseStop(
                "posts skipped: channel context not established "
                "(channel phase did not complete)"
            )
        channel_id = ctx.channel_id
        ids = sorted(set(ctx.settings.post_msgs or []))
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}
        context = {"channel_id": channel_id, "method": GET_MESSAGES_METHOD}

        for start in range(0, len(ids), GET_MESSAGES_BATCH):
            batch = ids[start : start + GET_MESSAGES_BATCH]
            try:
                answers = await ctx.gateway.get_messages(ctx.input_channel, batch)
            except PhaseStop as exc:
                # A flood raised by `Budget.call` carries no counts of its own;
                # attach the batches already projected so they are still reported.
                if not exc.counts:
                    raise PhaseStop(str(exc), counts=counts) from exc
                raise
            for m in answers:
                kind = m.get("_", "").lower()
                if kind not in _MESSAGE_KINDS:
                    continue
                observe_message(ctx, channel_id, m, counts, context=context)
                if self._outcomes is not None:
                    self._outcomes[msg_uri(channel_id, m["id"])] = (
                        "deleted_upstream" if kind == "messageempty" else "fetched"
                    )
            ctx.log.info(
                "posts: batch %d/%d (%d ids) done", start // GET_MESSAGES_BATCH + 1,
                -(-len(ids) // GET_MESSAGES_BATCH), len(batch),
            )
        return CollectResult(name=self.name, counts=counts)
