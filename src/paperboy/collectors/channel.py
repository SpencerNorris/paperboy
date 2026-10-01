"""The `channel` collector: get access to a target channel (by id or handle),
fetch full channel metadata, project it, and prime `ctx` for `history` (and
later Phase 2 collectors).
"""

from __future__ import annotations

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import channel_uri, user_uri
from paperboy.store.channels import upsert_channel
from paperboy.store.edges import add_edge
from paperboy.store.peers import input_channel_ref, stored_channel_username, upsert_peer
from paperboy.store.sync import set_state
from paperboy.targets import Target, TargetKind

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


def _refusal_message(receipt: dict) -> str:
    return (
        f"the stored handle for channel {receipt['channel_id']} now belongs to channel "
        f"{receipt['resolved_channel_id']}, not {receipt['channel_id']}; refusing to "
        "collect under the wrong id"
    )


class ChannelCollector:
    """Step A (get access: an `input_channel` plus a `ChannelAccess` receipt of
    how) then Step B (everything from `getFullChannel` on, keyed by id).

    Step A routes for an id target, first that works (#84, spec §2.2): 1 saved
    full key, 2 from-message reference, 3 stored handle verified to resolve to
    the same id, 4 nothing worked -> `SkipAndRecord`. A handle target is route 3
    without the verification. The receipt is appended BEFORE `getFullChannel`
    so replay can serve Step A from raw instead of re-deriving it from the
    output store's `peers`."""

    name = "channel"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def _resolve_and_record(
        self, ctx: CollectContext, handle: str
    ) -> tuple[dict, int, str]:
        """`contacts.resolveUsername` + its `ResolvedPeer` raw record."""
        resolved = await ctx.gateway.resolve(handle)
        t_resolved = ctx.clock.for_payload(resolved)
        context = {"target": ctx.target.raw}
        if ctx.target.kind is TargetKind.PEER_ID:
            # An id target resolved through its stored handle (route 3): record
            # the handle so replay can match this record by it (the target alone
            # is just the id).
            context["handle"] = handle
        raw_id = ctx.store.add_raw(
            resolved.get("_", "ResolvedPeer"), resolved, ctx.tier, context,
            observed_at=t_resolved,
        )
        return resolved, raw_id, t_resolved

    async def _step_a(
        self, ctx: CollectContext
    ) -> tuple[dict, str | None, tuple[dict, int, str] | None, dict | None]:
        """Returns `(receipt, receipt stamp | None, resolve (payload, raw id,
        stamp) | None, chan | None)`.

        `chan` is the resolve-side channel object (handle route only). The
        receipt stamp is set only for a replay-served receipt: it must be read
        before a following served `resolve` re-batches the clock. Does not write
        the receipt; raises `SkipAndRecord` for route 4 (no receipt)."""
        raw = ctx.target.raw
        served = await ctx.gateway.channel_access_receipt(raw)
        if served is not None:
            # Replay: take the route and input_channel from the recorded receipt.
            # A handle route also re-records its ResolvedPeer, served from raw.
            t_receipt = ctx.clock.for_payload(served)
            if served.get("via") != "handle":
                return served, t_receipt, None, None
            resolve = await self._resolve_and_record(ctx, served["handle"])
            chan = pick_channel(
                resolve[0].get("chats", []), _resolved_channel_id(resolve[0])
            ) if served["granted"] else None
            return served, t_receipt, resolve, chan

        channel_id: int | None = None
        if ctx.target.kind is TargetKind.PEER_ID:
            channel_id = int(ctx.target.value)
            ref = input_channel_ref(ctx.store, channel_id)
            if ref is not None:
                receipt = {
                    "channel_id": channel_id, "requested": raw, "via": ref.via,
                    "granted": True, "input_channel": ref.input_channel,
                    "key_source_raw_id": ref.key_source_raw_id,
                }
                return receipt, None, None, None
            handle = stored_channel_username(ctx.store, channel_id)
            if handle is None:
                raise SkipAndRecord(
                    f"cannot get access to channel {channel_id}: no saved full key, "
                    "no message in this store referencing it through a known chat, "
                    "and no known handle. Collect a channel that forwards it first, "
                    "or supply its handle or invite link"
                )
        else:
            handle = ctx.target.value

        resolve = await self._resolve_and_record(ctx, handle)
        resolved, resolve_raw_id, _ = resolve
        resolved_id = _resolved_channel_id(resolved)
        chan = pick_channel(resolved.get("chats", []), resolved_id)
        receipt = {
            "channel_id": channel_id if channel_id is not None else resolved_id,
            "requested": raw, "via": "handle", "handle": handle,
            "key_source_raw_id": resolve_raw_id,
        }
        if channel_id is not None and resolved_id != channel_id:
            receipt |= {"granted": False, "resolved_channel_id": resolved_id, "input_channel": None}
        else:
            receipt |= {
                "granted": True,
                "input_channel": {"channel_id": chan["id"], "access_hash": chan["access_hash"]},
            }
        return receipt, None, resolve, chan

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

        receipt, t_receipt, resolve, chan = await self._step_a(ctx)
        requested_id = receipt["channel_id"]
        ctx.store.add_raw(
            "ChannelAccess", receipt, ctx.tier,
            {"target": ctx.target.raw, "channel_id": requested_id},
            observed_at=t_receipt or ctx.clock.for_payload(receipt),
        )
        # Channels are named by id only in logs (never the handle).
        ctx.log.info(
            "channel access: id=%s via=%s granted=%s",
            requested_id, receipt["via"], receipt["granted"],
        )
        if not receipt["granted"]:
            raise SkipAndRecord(_refusal_message(receipt))

        full = await ctx.gateway.get_full_channel(receipt["input_channel"])
        t_full = ctx.clock.for_payload(full)
        full_raw_id = ctx.store.add_raw(
            full.get("_", "ChatFull"), full, ctx.tier, {"channel_id": requested_id},
            observed_at=t_full,
        )
        full_chat = full["full_chat"]
        # getFullChannel(input_channel) must answer for the channel we asked
        # about, whichever route got us access: if it answered for another,
        # one run would address one channel and store under another — fail
        # loudly rather than split identity across the collect. This is also
        # the verification point for routes 1 and 2 (spec §2.4).
        if full_chat["id"] != requested_id:
            raise ValueError(
                f"getFullChannel for {requested_id} answered with full_chat for "
                f"{full_chat['id']} — refusing to split channel identity"
            )
        # Prefer the richer `chat` object returned alongside getFullChannel
        # (may carry admin_rights/creator not present on the resolve() one).
        full_chats = full.get("chats", [])
        if full_chats:
            chan_for_channel = pick_channel(full_chats, full_chat["id"])
        elif chan is not None:
            chan_for_channel = chan
        else:
            raise SkipAndRecord(
                f"getFullChannel for {requested_id} returned no channel object to project"
            )

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

        sources = [(full_raw_id, full, t_full)]
        if resolve is not None:
            resolved, resolve_raw_id, t_resolved = resolve
            sources.insert(0, (resolve_raw_id, resolved, t_resolved))
        for source_raw_id, payload, t in sources:
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

        # From here on the run addresses the channel by its FULL key, whichever
        # route got us in (a from-message start is a saved key next time).
        ctx.input_channel = {
            "channel_id": channel_id, "access_hash": chan_for_channel["access_hash"],
        }
        ctx.channel_id = channel_id

        return CollectResult(name=self.name, counts={"channels": 1, "peers": len(peer_uris)})
