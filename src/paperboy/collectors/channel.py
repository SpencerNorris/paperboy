"""The `channel` collector: resolve a target, fetch full channel metadata,
project it, and prime `ctx` for `history` (and later Phase 2 collectors).
"""

from __future__ import annotations

from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import channel_uri, utc_now_iso
from paperboy.store.channels import upsert_channel
from paperboy.store.edges import add_edge
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import set_state
from paperboy.targets import Target


def _pick_channel(chats: list[dict]) -> dict:
    for chat in chats:
        if chat.get("_", "").startswith("channel"):
            return chat
    raise ValueError("resolve() returned no channel-typed chat for a channel-like target")


class ChannelCollector:
    name = "channel"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        observed_at = utc_now_iso()
        peer_uris: set[str] = set()

        resolved = await ctx.gateway.resolve(ctx.target.value)
        resolve_raw_id = ctx.store.add_raw(
            resolved.get("_", "resolvedPeer"), resolved, ctx.tier, {"target": ctx.target.raw}
        )
        chan = _pick_channel(resolved.get("chats", []))
        input_channel = {"channel_id": chan["id"], "access_hash": chan["access_hash"]}

        full = await ctx.gateway.get_full_channel(input_channel)
        full_raw_id = ctx.store.add_raw(
            full.get("_", "messages.chatFull"), full, ctx.tier, {"channel_id": chan["id"]}
        )
        full_chat = full["full_chat"]
        # Prefer the richer `chat` object returned alongside getFullChannel
        # (may carry admin_rights/creator not present on the resolve() one).
        full_chats = full.get("chats", [])
        chan_for_channel = _pick_channel(full_chats) if full_chats else chan

        channel_id = chan_for_channel["id"]
        channel_uri_ = upsert_channel(
            ctx.store, full_chat, chan_for_channel, full_raw_id, observed_at
        )

        set_state(ctx.store, "channel", str(channel_id), {"pts": full_chat["pts"]})

        linked_chat_id = full_chat.get("linked_chat_id") or None
        if linked_chat_id:
            add_edge(
                ctx.store, channel_uri_, "linked_group", channel_uri(linked_chat_id),
                observed_at, ctx.tier, full_raw_id,
                {"field": "linked_chat_id"},
            )

        for source_raw_id, payload in ((resolve_raw_id, resolved), (full_raw_id, full)):
            for obj in (*payload.get("chats", []), *payload.get("users", [])):
                uri = upsert_peer(
                    ctx.store, obj, source_raw_id, observed_at,
                    seen_in_chat=None, seen_in_msg=None,
                )
                peer_uris.add(uri)

        if chan_for_channel.get("creator"):
            ctx.tier = "self"
        elif chan_for_channel.get("admin_rights"):
            ctx.tier = "admin"

        self_user = await ctx.gateway.get_self()
        self_raw_id = ctx.store.add_raw(self_user.get("_", "user"), self_user, "self", None)
        self_uri = upsert_peer(
            ctx.store, self_user, self_raw_id, observed_at, seen_in_chat=None, seen_in_msg=None
        )
        set_state(ctx.store, "account", "self", {"uri": self_uri, "id": self_user.get("id")})

        ctx.input_channel = input_channel
        ctx.channel_id = channel_id

        return CollectResult(name=self.name, counts={"channels": 1, "peers": len(peer_uris)})
