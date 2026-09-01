"""The `channel` collector: resolve a target, fetch full channel metadata,
project it, and prime `ctx` for `history` (and later Phase 2 collectors).
"""

from __future__ import annotations

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import channel_uri, user_uri
from paperboy.store.channels import upsert_channel
from paperboy.store.edges import add_edge
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import set_state
from paperboy.targets import Target

# Telegram returns the *full* `User` object for the collecting account, and it
# is the only peer object that ever carries `phone`. CLAUDE.md forbids
# persisting the collecting account's credentials, and `raw_records` is written
# verbatim before any projection — so the credential is stripped here, at the
# boundary where self first enters the store, rather than filtered downstream at
# export time where one missed code path leaks it into a Datasette instance.
_SELF_CREDENTIAL_FIELDS = frozenset({"phone"})


def _redact_self(user: dict) -> dict:
    """`user` minus the collecting account's credentials.

    Everything identifying the account (`id`, `self`, names) is kept: the raw
    record still has to say *which* account made the observation, or provenance
    breaks.
    """
    return {k: v for k, v in user.items() if k not in _SELF_CREDENTIAL_FIELDS}


def pick_channel(chats: list[dict], channel_id: int) -> dict:
    """The channel-typed chat in `chats` whose id is `channel_id`.

    Never pick by vector position: a linked discussion megagroup serialises as
    `Channel` too, and Telegram promises no ordering — a group listed first
    would silently misattribute the entire collect (issue #23). The
    authoritative id comes from `ResolvedPeer.peer` / `full_chat.id`.

    Public (Task 8): `participants` needs it too for the linked group's own
    preflight `ChatFull`.
    """
    for chat in chats:
        # Telethon's to_dict() uses the PascalCase class name ("Channel",
        # "ChannelForbidden"), not the lowercase TL constructor name.
        if chat.get("id") == channel_id and chat.get("_", "").lower().startswith("channel"):
            return chat
    raise ValueError(
        f"no channel-typed chat with id {channel_id} in the response's chats vector"
    )


_pick_channel = pick_channel  # back-compat alias


def _resolved_channel_id(resolved: dict) -> int:
    """The channel id `resolve()` actually resolved to, from its `peer` field.

    `contacts.ResolvedPeer` always carries `peer` in the wild; a channel-like
    target resolving to anything else (a user or a basic group — a username
    can legitimately be either, issue #34) means we have no authoritative
    channel identity here — skip cleanly rather than guess from the `chats`
    vector or crash the whole run.
    """
    peer = resolved.get("peer") or {}
    channel_id = peer.get("channel_id")
    if not isinstance(channel_id, int):
        raise SkipAndRecord(
            "target resolved to a non-channel peer "
            f"({peer.get('_') or 'no peer in response'})"
        )
    return channel_id


class ChannelCollector:
    name = "channel"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        peer_uris: set[str] = set()

        # Learn (and record) the collecting account FIRST, so `is_self` is
        # primed before any peer/message/edge is projected below — self must be
        # kept out of the store even when it rides along in a response's users
        # vector, not only the explicit get_me() (issue #12). The raw record is
        # kept (redacted) so provenance can still say which account observed;
        # the id lives in sync_state only, never as a peer row.
        self_user = _redact_self(await ctx.gateway.get_self())
        t_self = ctx.clock.for_payload(self_user)
        ctx.store.add_raw(
            self_user.get("_", "User"), self_user, "self", None, observed_at=t_self
        )
        self_uri = user_uri(self_user["id"])
        set_state(ctx.store, "account", "self", {"uri": self_uri, "id": self_user.get("id")})

        resolved = await ctx.gateway.resolve(ctx.target.value)
        t_resolved = ctx.clock.for_payload(resolved)
        resolve_raw_id = ctx.store.add_raw(
            resolved.get("_", "ResolvedPeer"), resolved, ctx.tier, {"target": ctx.target.raw},
            observed_at=t_resolved,
        )
        chan = pick_channel(resolved.get("chats", []), _resolved_channel_id(resolved))
        input_channel = {"channel_id": chan["id"], "access_hash": chan["access_hash"]}

        full = await ctx.gateway.get_full_channel(input_channel)
        t_full = ctx.clock.for_payload(full)
        full_raw_id = ctx.store.add_raw(
            full.get("_", "ChatFull"), full, ctx.tier, {"channel_id": chan["id"]},
            observed_at=t_full,
        )
        full_chat = full["full_chat"]
        # getFullChannel(input_channel) must answer for the channel we asked
        # about: input_channel (the access_hash history uses) is resolve-side,
        # while channel_id / pts below key off full_chat.id. If those ever
        # disagreed, one run would address one channel and store under another
        # — fail loudly rather than split identity across the collect.
        if full_chat["id"] != chan["id"]:
            raise ValueError(
                f"getFullChannel for {chan['id']} answered with full_chat for "
                f"{full_chat['id']} — refusing to split channel identity"
            )
        # Prefer the richer `chat` object returned alongside getFullChannel
        # (may carry admin_rights/creator not present on the resolve() one).
        full_chats = full.get("chats", [])
        chan_for_channel = pick_channel(full_chats, full_chat["id"]) if full_chats else chan

        channel_id = chan_for_channel["id"]
        channel_uri_ = upsert_channel(
            ctx.store, full_chat, chan_for_channel, full_raw_id, t_full
        )

        set_state(ctx.store, "channel", str(channel_id), {"pts": full_chat["pts"]})

        linked_chat_id = full_chat.get("linked_chat_id") or None
        if linked_chat_id:
            add_edge(
                ctx.store, channel_uri_, "linked_group", channel_uri(linked_chat_id),
                t_full, ctx.tier, full_raw_id,
                {"field": "linked_chat_id"},
            )

        for source_raw_id, payload, t in (
            (resolve_raw_id, resolved, t_resolved), (full_raw_id, full, t_full),
        ):
            for obj in (*payload.get("chats", []), *payload.get("users", [])):
                uri = upsert_peer(
                    ctx.store, obj, source_raw_id, t,
                    seen_in_chat=None, seen_in_msg=None,
                )
                if uri is not None:  # None => self, kept out of the store (#12)
                    peer_uris.add(uri)

        if chan_for_channel.get("creator"):
            ctx.tier = "self"
        elif chan_for_channel.get("admin_rights"):
            ctx.tier = "admin"

        ctx.input_channel = input_channel
        ctx.channel_id = channel_id

        return CollectResult(name=self.name, counts={"channels": 1, "peers": len(peer_uris)})
