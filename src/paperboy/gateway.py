"""The `Gateway` seam (ADR-0001): every Telegram-shaped operation a collector needs.

`Gateway` is a `typing.Protocol` so collectors depend on a shape, not on
Telethon. Every method returns plain dicts (`TLObject.to_dict()`), never
Telethon types — that's what makes `FakeGateway` (fixture replay, no
network) a drop-in test double for `TelethonGateway` (the real thing, every
call routed through `Budget.call`).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from telethon import TelegramClient
    from telethon.tl.types import (
        InputChannel,
        InputPeerChannel,
        TypeChannelParticipantsFilter,
        TypeInputPeer,
        TypeInputUser,
    )

    from paperboy.budget import Budget


class Gateway(Protocol):
    async def resolve(self, target_value: str) -> dict:
        """`contacts.resolveUsername` — returns `{"chats": [...], "users": [...]}`."""
        ...

    async def get_full_channel(self, input_channel: dict) -> dict:
        """`channels.getFullChannel` — `{"full_chat": {...}, "chats": [...], "users": [...]}`."""
        ...

    def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        """One page of `messages.getHistory`, newest-first, as an async stream of message dicts."""
        ...

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        """`channels.getMessages` — missing ids come back as `messageEmpty` dicts."""
        ...

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        """`updates.getChannelDifference` — the `pts`-based incremental sync primitive."""
        ...

    async def get_self(self) -> dict:
        """The collecting account's own user dict, `about` merged in from `getFullUser`."""
        ...

    async def get_authorizations(self) -> dict:
        """`account.getAuthorizations` — `{"authorizations": [...]}`, used by `doctor` for
        session age (the entry with `current: True`)."""
        ...

    async def get_password_state(self) -> dict:
        """`account.getPassword` — includes `has_password` (2FA presence), used by `doctor`."""
        ...

    async def get_privacy(self, key: str) -> dict:
        """`account.getPrivacy` for one key (`phone`/`lastseen`/`photo`) — `{"rules": [...]}`."""
        ...

    async def download_media(self, input_channel: dict, message: dict) -> bytes | None:
        """Download one message's media (`upload.getFile`, via Telethon's own
        `download_media` helper) as raw bytes — `None` if the media is gone/
        unavailable server-side. `message` need only carry enough to
        re-resolve the live message (its `id`); read-only, never mutates
        anything on Telegram's side."""
        ...

    async def get_channel_recommendations(self, input_channel: dict) -> dict:
        """`channels.getChannelRecommendations` — a `Chats`/`ChatsSlice` dict
        (the latter's `count` is the true total, even though at most ~10 `chats` come
        back). May raise `SkipAndRecord` (e.g. `CHAT_NOT_MODIFIED`, `PREMIUM_ACCOUNT_REQUIRED`)."""
        ...

    async def check_chat_invite(self, hash_: str) -> dict:
        """`messages.checkChatInvite` — never joins. Returns a `ChatInvite` dict
        (unjoined preview: title/photo/participants_count, no chat id) or a
        `ChatInviteAlready`/`ChatInvitePeek` dict (a real `Chat`, if already known)."""
        ...

    async def join_channel(self, input_channel: dict) -> dict:
        """`channels.joinChannel` — the one WRITE paperboy makes, and only under
        an explicit `--join` (issue #20). Returns the `Updates` dict on success.
        May raise `SkipAndRecord` (a documented refusal like `INVITE_REQUEST_SENT`
        for an approval-gated group, `CHANNELS_TOO_MUCH`, `CHANNEL_PRIVATE`) or
        `HardStop` on `PEER_FLOOD`. NEVER call this outside the `--join` path."""
        ...

    async def get_sponsored_messages(self, input_channel: dict) -> dict:
        """`messages.getSponsoredMessages` — a `SponsoredMessages` or
        `SponsoredMessagesEmpty` dict. May raise `SkipAndRecord` (e.g.
        `CHAT_ADMIN_REQUIRED`, `PREMIUM_ACCOUNT_REQUIRED` on some accounts)."""
        ...

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        """`channels.getParticipants` — a `channels.ChannelParticipants` dict
        (`count` is the TRUE total even when paging is server-capped, spec
        §6.3) or `channels.ChannelParticipantsNotModified`. `filter` is a
        `channelParticipants*` dict (`FILTER_RECENT`/`FILTER_ADMINS`/
        `FILTER_BOTS`, or a `channelParticipantsMentions` dict). Page size
        is Telegram's 200. May raise `SkipAndRecord` (`CHAT_ADMIN_REQUIRED`,
        `CHANNEL_PRIVATE` — a walled roster)."""
        ...

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        """`channels.getParticipant` — the per-user membership oracle. Returns
        a `channels.ChannelParticipant` dict (`participant` + `users`), or
        `None` for a definitive `USER_NOT_PARTICIPANT` (a RESULT, not a
        failure — plan D4). `participant` is an input-user ref dict (see
        `_input_user`). May raise `SkipAndRecord` (`CHAT_ADMIN_REQUIRED` —
        e.g. an arbitrary user on a broadcast channel, spec §13)."""
        ...

    async def get_users(self, refs: list[dict]) -> list[dict]:
        """`users.getUsers` — batched triage; one `User`/`UserEmpty` dict per
        ref, in order. `refs` are input-user ref dicts; a single stale
        `from_msg` provenance fails the WHOLE vector (`MSG_ID_INVALID` →
        `SkipAndRecord`), which is why callers bisect (plan D13)."""
        ...

    async def get_full_user(self, ref: dict) -> dict:
        """`users.getFullUser` for an arbitrary input-user ref — a
        `users.UserFull` dict: parse BOTH `full_user` and the `users` vector
        (disjoint data, research Part 2 §1). May raise `SkipAndRecord`
        (`USER_ID_INVALID`, `CHANNEL_INVALID`, `MSG_ID_INVALID`)."""
        ...

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        """`photos.getUserPhotos` (limit ≤ 100) — a `photos.Photos` /
        `photos.PhotosSlice` dict: the target's own dated avatar history,
        newest first; personal/fallback photos are never included."""
        ...

    async def download_user_photo(self, photo: dict) -> bytes | None:
        """Download one `Photo` from `get_user_photos` (largest size) as raw
        bytes via `upload.getFile`; `None` if gone. Raises `SkipAndRecord`
        when its `file_reference` has expired. Read-only."""
        ...

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        """`messages.getMessageReactionsList` — `{count, reactions: [{peer_id,
        date, reaction}], users, chats, next_offset}`. GROUPS ONLY: a broadcast
        answers `BROADCAST_FORBIDDEN` (`SkipAndRecord`) and must never be
        asked (guardrail)."""
        ...


class FakeGateway:
    """Replays recorded fixture dicts — no network, no `Budget` involved.

    `fixtures` keys: `resolve`, `full_channel`, `self`, `history` (a flat
    list, newest-first), `get_messages` (a `{id: message_dict}` lookup),
    `channel_difference`, `authorizations`, `password_state`, `privacy` (a
    `{key: rules_dict}` lookup keyed by `"phone"`/`"lastseen"`/`"photo"`;
    missing key → `SkipAndRecord`, was `KeyError`),
    `media` (a `{msg_id: bytes}` lookup for `download_media`).
    `channel_recommendations`, `sponsored_messages`, `chat_invite` (a
    `{hash: dict}` lookup). Any of these three graph-collector fixture
    values may instead be a `BaseException` instance (e.g. a `SkipAndRecord`)
    to simulate `Budget.call`'s classification without going through it —
    `FakeGateway` never touches `Budget`.

    Person-layer fixture keys (spec §4–§8): `full_channel_by_id` (a
    `{channel_id: dict | BaseException}` lookup — lets a test answer the
    LINKED GROUP's `ChatFull` differently from the target's; missing key
    falls back to `full_channel`), `participants` (a `{channel_id:
    {filter_name: list[dict | BaseException] | BaseException}}` lookup —
    pages consumed in call order per `(channel_id, filter)`; past the end →
    an empty page), `participant` (a `{channel_id: {user_id: dict | None |
    BaseException}}` lookup — missing → `None`), `users` (a `{user_id: dict
    | BaseException}` lookup — missing → `UserEmpty`; any exception value
    fails the whole batch), `full_user` (a `{user_id: dict | BaseException}`
    lookup — missing → `SkipAndRecord`), `user_photos` (a `{user_id: dict |
    BaseException}` lookup — missing → empty `Photos`), `avatar` (a
    `{photo_id: bytes | None | BaseException}` lookup — missing → `None`),
    `reactions` (a `{channel_id: {msg_id: dict | list[dict] |
    BaseException}}` lookup — missing → empty list dict; a list is consumed
    per `offset` call order).
    """

    def __init__(self, fixtures: dict) -> None:
        self._fx = fixtures
        # Test introspection: every msg id `download_media` was actually
        # asked to fetch, in call order — lets a dedup test assert a
        # duplicate never reaches the gateway at all, not just that its
        # result was discarded.
        self.download_media_calls: list[int] = []
        # Every protocol method invoked, in call order. Lets a test assert that
        # a code path made NO RPC at all, or exactly one — an assertion that is
        # otherwise unfalsifiable, because a fake that records nothing looks
        # identical whether it was called or not.
        self.calls: list[str] = []
        # Every `input_channel` `iter_history` was pointed at. The only way to
        # catch a sweep silently aimed at the wrong channel or built with a
        # stale access hash.
        self.history_targets: list[dict] = []
        # Every `pts` value `get_channel_difference` was called with. A cursor
        # corrupted in the store (issue #22: `None` persisted by a TooLong
        # resync) only becomes observable at the call boundary — the real
        # gateway would crash on it inside Telethon, past every handler.
        self.channel_difference_pts: list[int | None] = []
        # Person-layer introspection (same rationale as `calls` above).
        self.participants_calls: list[tuple[int, str, int]] = []
        self.participant_calls: list[tuple[int, int]] = []
        self.users_calls: list[list[int]] = []
        self.full_user_calls: list[int] = []
        self.user_photos_calls: list[int] = []
        self.avatar_calls: list[int] = []
        self.reactions_calls: list[tuple[int, int, str | None]] = []

    async def resolve(self, target_value: str) -> dict:
        self.calls.append("resolve")
        del target_value
        return self._fx["resolve"]

    async def get_full_channel(self, input_channel: dict) -> dict:
        self.calls.append("get_full_channel")
        # `full_channel_by_id` lets a test answer the LINKED GROUP's ChatFull
        # (participants preflight) differently from the target's.
        by_id: dict[int, object] = self._fx.get("full_channel_by_id", {})
        value = by_id.get(input_channel["channel_id"], self._fx.get("full_channel"))
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise KeyError("full_channel")
        return cast(dict, value)

    async def get_self(self) -> dict:
        self.calls.append("get_self")
        return self._fx["self"]

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        self.calls.append("iter_history")
        self.history_targets.append(dict(input_channel))
        del input_channel
        history = self._fx.get("history", [])
        # Mirrors real getHistory semantics: offset_id=0 means "from the
        # top"; otherwise only ids strictly below it (paging older).
        page = [m for m in history if offset_id == 0 or m["id"] < offset_id]
        for m in page[:limit]:
            yield m

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        self.calls.append("get_messages")
        del input_channel
        table: dict[int, dict] = self._fx.get("get_messages", {})
        return [table.get(i, {"_": "MessageEmpty", "id": i}) for i in ids]

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        self.calls.append("get_channel_difference")
        idx = len(self.channel_difference_pts)
        self.channel_difference_pts.append(pts)
        del input_channel, limit
        fx = self._fx["channel_difference"]
        # A list models a multi-page catch-up (getChannelDifference is called
        # until the server sets `final`); the pages are consumed in order and
        # the last repeats if the collector over-reads. A bare dict is the
        # single-page case and is returned on every call, as before. A page that
        # is a BaseException is raised, modelling a flood (PhaseStop) mid-loop —
        # the same exception-fixture convention as download_media/check_chat_invite.
        page = fx[min(idx, len(fx) - 1)] if isinstance(fx, list) else fx
        if isinstance(page, BaseException):
            raise page
        return page

    async def get_authorizations(self) -> dict:
        self.calls.append("get_authorizations")
        return self._fx["authorizations"]

    async def get_password_state(self) -> dict:
        self.calls.append("get_password_state")
        return self._fx["password_state"]

    async def get_privacy(self, key: str) -> dict:
        self.calls.append("get_privacy")
        table = self._fx.get("privacy") or {}
        if key not in table:
            from paperboy.budget import SkipAndRecord

            raise SkipAndRecord(f"fake: no privacy fixture for {key!r}")
        return table[key]

    @staticmethod
    def _raise_if_exc(value: object) -> None:
        if isinstance(value, BaseException):
            raise value

    async def download_media(self, input_channel: dict, message: dict) -> bytes | None:
        self.calls.append("download_media")
        del input_channel
        self.download_media_calls.append(message["id"])
        value = self._fx.get("media", {}).get(message["id"])
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_channel_recommendations(self, input_channel: dict) -> dict:
        self.calls.append("get_channel_recommendations")
        del input_channel
        return self._fx_or_raise(
            "channel_recommendations", {"_": "messages.chats", "chats": []}
        )

    async def check_chat_invite(self, hash_: str) -> dict:
        self.calls.append("check_chat_invite")
        table: dict[str, dict] = self._fx.get("chat_invite", {})
        value = table.get(hash_)
        if value is None:
            return {"_": "chatInvite", "title": "", "participants_count": 0}
        if isinstance(value, BaseException):
            raise value
        return value

    async def join_channel(self, input_channel: dict) -> dict:
        self.calls.append("join_channel")
        del input_channel
        value = self._fx.get("join", {"_": "updates", "updates": []})
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_sponsored_messages(self, input_channel: dict) -> dict:
        self.calls.append("get_sponsored_messages")
        del input_channel
        return self._fx_or_raise(
            "sponsored_messages", {"_": "messages.sponsoredMessagesEmpty"}
        )

    def _fx_or_raise(self, key: str, default: dict | None = None) -> dict:
        # Missing fixture => a benign, correctly-shaped empty (a channel may
        # genuinely have no recommendations/sponsored messages), unless the test
        # configured a BaseException to simulate Budget.call's classification.
        if key not in self._fx:
            return {} if default is None else default
        value = self._fx[key]
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        self.calls.append("get_participants")
        del limit, hash_
        cid = input_channel["channel_id"]
        name = filter.get("_", "")
        prior = sum(1 for c in self.participants_calls if c[0] == cid and c[1] == name)
        self.participants_calls.append((cid, name, offset))
        pages = self._fx.get("participants", {}).get(cid, {}).get(name)
        self._raise_if_exc(pages)
        empty = {
            "_": "ChannelParticipants", "count": 0, "participants": [], "chats": [], "users": [],
        }
        if not pages:
            return empty
        if prior >= len(pages):
            # Past the recorded pages: an empty page ends the collector's loop
            # (never repeat the last page — that would spin forever).
            return {**empty, "count": pages[-1].get("count", 0)}
        page = pages[prior]
        self._raise_if_exc(page)
        return page

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        self.calls.append("get_participant")
        cid, uid = input_channel["channel_id"], participant["user_id"]
        self.participant_calls.append((cid, uid))
        value = self._fx.get("participant", {}).get(cid, {}).get(uid)
        self._raise_if_exc(value)
        return value

    async def get_users(self, refs: list[dict]) -> list[dict]:
        self.calls.append("get_users")
        ids = [r["user_id"] for r in refs]
        self.users_calls.append(ids)
        table = self._fx.get("users", {})
        for uid in ids:
            self._raise_if_exc(table.get(uid))  # one bad ref fails the vector
        return [table.get(uid, {"_": "UserEmpty", "id": uid}) for uid in ids]

    async def get_full_user(self, ref: dict) -> dict:
        self.calls.append("get_full_user")
        self.full_user_calls.append(ref["user_id"])
        value = self._fx.get("full_user", {}).get(ref["user_id"])
        self._raise_if_exc(value)
        if value is None:
            from paperboy.budget import SkipAndRecord

            raise SkipAndRecord(f"fake: no full_user fixture for {ref['user_id']}")
        return value

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        self.calls.append("get_user_photos")
        del offset, max_id, limit
        self.user_photos_calls.append(ref["user_id"])
        value = self._fx.get("user_photos", {}).get(ref["user_id"])
        self._raise_if_exc(value)
        return value if value is not None else {"_": "Photos", "photos": [], "users": []}

    async def download_user_photo(self, photo: dict) -> bytes | None:
        self.calls.append("download_user_photo")
        self.avatar_calls.append(photo["id"])
        value = self._fx.get("avatar", {}).get(photo["id"])
        self._raise_if_exc(value)
        return value

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        self.calls.append("get_message_reactions_list")
        del limit
        cid = input_channel["channel_id"]
        prior = sum(1 for c in self.reactions_calls if c[0] == cid and c[1] == msg_id)
        self.reactions_calls.append((cid, msg_id, offset))
        value = self._fx.get("reactions", {}).get(cid, {}).get(msg_id)
        self._raise_if_exc(value)
        empty = {
            "_": "MessageReactionsList", "count": 0, "reactions": [], "chats": [], "users": [],
            "next_offset": None,
        }
        if value is None:
            return empty
        if isinstance(value, list):
            page = value[prior] if prior < len(value) else empty
            self._raise_if_exc(page)
            return page
        return value


def _input_channel(input_channel: dict) -> InputChannel:
    from telethon.tl.types import InputChannel as _InputChannel

    return _InputChannel(
        channel_id=input_channel["channel_id"], access_hash=input_channel["access_hash"]
    )


def _input_peer_channel(input_channel: dict) -> InputPeerChannel:
    from telethon.tl.types import InputPeerChannel as _InputPeerChannel

    return _InputPeerChannel(
        channel_id=input_channel["channel_id"], access_hash=input_channel["access_hash"]
    )


FILTER_RECENT = {"_": "channelParticipantsRecent"}
FILTER_ADMINS = {"_": "channelParticipantsAdmins"}
FILTER_BOTS = {"_": "channelParticipantsBots"}


def _input_user(ref: dict) -> TypeInputUser:
    """Spec §5's `_input_user` builder (gateway side). Case 1: a real
    `access_hash` → `InputUser`. Case 2: `from_msg` provenance →
    `InputUserFromMessage` (the server re-derives the hash from the message
    the user was seen in). Case 3 never reaches here — `store.peers
    .input_user_ref` returns `None` for it and callers skip."""
    from telethon.tl.types import InputUser, InputUserFromMessage

    from_msg = ref.get("from_msg")
    if from_msg is not None:
        return InputUserFromMessage(
            peer=_input_peer_channel(from_msg), msg_id=from_msg["msg_id"], user_id=ref["user_id"]
        )
    return InputUser(user_id=ref["user_id"], access_hash=ref["access_hash"])


def _input_peer_user(ref: dict) -> TypeInputPeer:
    from telethon.tl.types import InputPeerUser, InputPeerUserFromMessage

    from_msg = ref.get("from_msg")
    if from_msg is not None:
        return InputPeerUserFromMessage(
            peer=_input_peer_channel(from_msg), msg_id=from_msg["msg_id"], user_id=ref["user_id"]
        )
    return InputPeerUser(user_id=ref["user_id"], access_hash=ref["access_hash"])


def _participants_filter(filter: dict) -> TypeChannelParticipantsFilter:
    from telethon.tl.types import (
        ChannelParticipantsAdmins,
        ChannelParticipantsBots,
        ChannelParticipantsMentions,
        ChannelParticipantsRecent,
    )

    kind = (filter.get("_") or "").lower()
    if kind == "channelparticipantsrecent":
        return ChannelParticipantsRecent()
    if kind == "channelparticipantsadmins":
        return ChannelParticipantsAdmins()
    if kind == "channelparticipantsbots":
        return ChannelParticipantsBots()
    if kind == "channelparticipantsmentions":
        return ChannelParticipantsMentions(q=filter.get("q"), top_msg_id=filter.get("top_msg_id"))
    raise ValueError(f"unsupported participants filter: {filter.get('_')!r}")


def _largest_photo_size(sizes: list[dict]) -> str:
    """The `thumb_size` type letter of the largest real size (`photoSize` /
    `photoSizeProgressive` carry `w`/`h`; stripped/cached thumbs do not).
    Telegram's largest avatar size is conventionally `x`, the fallback."""
    best: tuple[int, str] | None = None
    for size in sizes:
        w, h, type_ = size.get("w"), size.get("h"), size.get("type")
        if (
            isinstance(w, int) and isinstance(h, int) and isinstance(type_, str)
            and (best is None or w * h > best[0])
        ):
            best = (w * h, type_)
    return best[1] if best else "x"


def _file_reference(photo: dict) -> bytes:
    """`file_reference` arrives as `bytes` live (`to_dict()`) and as base64
    text from a stored/replayed record (`store.db.dumps`)."""
    ref = photo.get("file_reference") or b""
    return ref if isinstance(ref, bytes) else base64.b64decode(ref)


class TelethonGateway:
    """The real `Gateway`: builds raw `telethon.tl.functions.*` requests, every
    one routed through `Budget.call` — no collector or gateway consumer talks
    to Telethon directly (ADR-0003).

    Telethon ships no return-type stubs for `TelegramClient.__call__` (it's
    dynamically typed on the request instance), so each response is `cast`
    to the TL response shape that request actually returns — a typing aid
    only; every cast target is a real `TLObject` verified against the
    installed layer (ADR-0001), never a runtime behavior change.
    """

    def __init__(self, client: TelegramClient, budget: Budget) -> None:
        self.client = client
        self.budget = budget

    async def resolve(self, target_value: str) -> dict:
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        from telethon.tl.types.contacts import ResolvedPeer

        result = cast(
            ResolvedPeer,
            await self.budget.call(
                "contacts.resolveUsername",
                lambda: self.client(ResolveUsernameRequest(username=target_value)),
            ),
        )
        return result.to_dict()

    async def get_full_channel(self, input_channel: dict) -> dict:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types.messages import ChatFull

        channel = _input_channel(input_channel)
        result = cast(
            ChatFull,
            await self.budget.call(
                "channels.getFullChannel",
                lambda: self.client(GetFullChannelRequest(channel=channel)),
            ),
        )
        return result.to_dict()

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        from telethon.tl.functions.messages import GetHistoryRequest
        from telethon.tl.types.messages import Messages

        peer = _input_peer_channel(input_channel)
        result = cast(
            Messages,
            await self.budget.call(
                "messages.getHistory",
                lambda: self.client(
                    GetHistoryRequest(
                        peer=peer,
                        offset_id=offset_id,
                        offset_date=None,
                        add_offset=0,
                        limit=limit,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                ),
            ),
        )
        for m in result.messages:
            yield m.to_dict()

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        from telethon.tl.functions.channels import GetMessagesRequest
        from telethon.tl.types import InputMessageID, TypeInputMessage
        from telethon.tl.types.messages import Messages

        channel = _input_channel(input_channel)
        message_ids: list[TypeInputMessage] = [InputMessageID(id=i) for i in ids]
        result = cast(
            Messages,
            await self.budget.call(
                "channels.getMessages",
                lambda: self.client(GetMessagesRequest(channel=channel, id=message_ids)),
            ),
        )
        return [m.to_dict() for m in result.messages]

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        from telethon.tl.functions.updates import GetChannelDifferenceRequest
        from telethon.tl.tlobject import TLObject
        from telethon.tl.types import ChannelMessagesFilterEmpty

        channel = _input_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "updates.getChannelDifference",
                lambda: self.client(
                    GetChannelDifferenceRequest(
                        channel=channel,
                        filter=ChannelMessagesFilterEmpty(),
                        pts=pts,
                        limit=limit,
                        force=False,
                    )
                ),
            ),
        )
        return result.to_dict()

    async def get_self(self) -> dict:
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import InputUserSelf
        from telethon.tl.types.users import UserFull

        result = cast(
            UserFull,
            await self.budget.call(
                "users.getFullUser",
                lambda: self.client(GetFullUserRequest(id=InputUserSelf())),
            ),
        )
        # UserFull wraps the actual User under `.users[0]`; expose the user,
        # not the full-profile wrapper, matching `resolve`/history peers —
        # but merge in `about` (bio), which only `full_user` carries and
        # `doctor`'s minimal-profile check needs.
        user_dict = result.users[0].to_dict()
        user_dict["about"] = getattr(result.full_user, "about", None)
        return user_dict

    async def get_authorizations(self) -> dict:
        from telethon.tl.functions.account import GetAuthorizationsRequest
        from telethon.tl.types.account import Authorizations

        result = cast(
            Authorizations,
            await self.budget.call(
                "account.getAuthorizations", lambda: self.client(GetAuthorizationsRequest())
            ),
        )
        return result.to_dict()

    async def get_password_state(self) -> dict:
        from telethon.tl.functions.account import GetPasswordRequest
        from telethon.tl.types.account import Password

        result = cast(
            Password,
            await self.budget.call(
                "account.getPassword", lambda: self.client(GetPasswordRequest())
            ),
        )
        return result.to_dict()

    async def get_privacy(self, key: str) -> dict:
        from telethon.tl.functions.account import GetPrivacyRequest
        from telethon.tl.types import (
            InputPrivacyKeyPhoneNumber,
            InputPrivacyKeyProfilePhoto,
            InputPrivacyKeyStatusTimestamp,
        )
        from telethon.tl.types.account import PrivacyRules

        input_keys = {
            "phone": InputPrivacyKeyPhoneNumber,
            "lastseen": InputPrivacyKeyStatusTimestamp,
            "photo": InputPrivacyKeyProfilePhoto,
        }
        if key not in input_keys:
            raise ValueError(f"unknown privacy key: {key!r}")
        input_key = input_keys[key]()
        result = cast(
            PrivacyRules,
            await self.budget.call(
                f"account.getPrivacy:{key}",
                lambda: self.client(GetPrivacyRequest(key=input_key)),
            ),
        )
        return result.to_dict()

    async def download_media(self, input_channel: dict, message: dict) -> bytes | None:
        """Re-fetch the live message (`channels.getMessages`) and download its
        media in-memory (`file=bytes` tells Telethon to return bytes instead
        of writing to disk). A fresh fetch carries a fresh `file_reference`;
        if the download still races an expiry (`FileReferenceExpiredError`
        isn't in `errors.classify`'s tables, so `Budget.call` re-raises it
        verbatim rather than converting it), re-fetch once more and retry
        exactly once. A second consecutive expiry is converted to
        `SkipAndRecord` here — skip this one file, spec §8's "no exception
        is swallowed" honored by recording *why*, not by crashing the run.
        """
        from telethon.errors import FileReferenceExpiredError
        from telethon.tl.functions.channels import GetMessagesRequest
        from telethon.tl.types import InputMessageID, Message
        from telethon.tl.types.messages import Messages

        from paperboy.budget import SkipAndRecord

        channel = _input_channel(input_channel)
        msg_id = message["id"]

        async def _fetch_message() -> Message | None:
            result = cast(
                Messages,
                await self.budget.call(
                    "channels.getMessages",
                    lambda: self.client(
                        GetMessagesRequest(channel=channel, id=[InputMessageID(id=msg_id)])
                    ),
                ),
            )
            # `channels.getMessages` returning a non-`Message` (e.g.
            # `MessageEmpty`, for an id that's since been deleted) is
            # possible but not media-bearing; the `cast` here matches every
            # other Telethon-response cast in this module (ADR-0001) — a
            # typing aid verified against the installed layer, not a
            # runtime behavior change.
            return cast(Message, result.messages[0]) if result.messages else None

        async def _download(tl_message: Message) -> bytes | None:
            # `file=bytes` is Telethon's own documented idiom for "download
            # in-memory and return it as a bytestring" — untyped in its
            # stubs (`hints.FileLike` has no meta-type case for it), so the
            # `Any` cast is a stub gap, not a real type mismatch.
            return cast(
                bytes | None,
                await self.budget.call(
                    "upload.getFile",
                    lambda: self.client.download_media(tl_message, file=cast(Any, bytes)),
                ),
            )

        tl_message = await _fetch_message()
        if tl_message is None:
            return None
        try:
            return await _download(tl_message)
        except FileReferenceExpiredError:
            tl_message = await _fetch_message()
            if tl_message is None:
                return None
            try:
                return await _download(tl_message)
            except FileReferenceExpiredError as exc:
                raise SkipAndRecord(
                    f"media download skipped: file_reference expired twice for msg {msg_id}"
                ) from exc

    async def get_channel_recommendations(self, input_channel: dict) -> dict:
        from telethon.tl.functions.channels import GetChannelRecommendationsRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "channels.getChannelRecommendations",
                lambda: self.client(GetChannelRecommendationsRequest(channel=channel)),
            ),
        )
        return result.to_dict()

    async def check_chat_invite(self, hash_: str) -> dict:
        from telethon.tl.functions.messages import CheckChatInviteRequest
        from telethon.tl.tlobject import TLObject

        result = cast(
            TLObject,
            await self.budget.call(
                "messages.checkChatInvite",
                lambda: self.client(CheckChatInviteRequest(hash=hash_)),
            ),
        )
        return result.to_dict()

    async def get_sponsored_messages(self, input_channel: dict) -> dict:
        from telethon.tl.functions.messages import GetSponsoredMessagesRequest
        from telethon.tl.tlobject import TLObject

        peer = _input_peer_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "messages.getSponsoredMessages",
                lambda: self.client(GetSponsoredMessagesRequest(peer=peer)),
            ),
        )
        return result.to_dict()

    async def join_channel(self, input_channel: dict) -> dict:
        # The single WRITE path (issue #20). Routed through Budget.call like
        # every other RPC, so PEER_FLOOD becomes a HardStop and the pacing/cap
        # apply; the caller only reaches here under an explicit --join.
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "channels.joinChannel",
                lambda: self.client(JoinChannelRequest(channel=channel)),
            ),
        )
        return result.to_dict()

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        tl_filter = _participants_filter(filter)
        result = cast(
            TLObject,
            await self.budget.call(
                "channels.getParticipants",
                lambda: self.client(
                    GetParticipantsRequest(
                        channel=channel, filter=tl_filter, offset=offset, limit=limit, hash=hash_
                    )
                ),
            ),
        )
        return result.to_dict()

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        # USER_NOT_PARTICIPANT is a definitive ANSWER (spec §13), not a failure:
        # `errors.classify` deliberately leaves it unclassified so it reaches
        # here verbatim, and it becomes `None` for the collector.
        from telethon.errors import UserNotParticipantError
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        peer = _input_peer_user(participant)
        try:
            result = cast(
                TLObject,
                await self.budget.call(
                    "channels.getParticipant",
                    lambda: self.client(GetParticipantRequest(channel=channel, participant=peer)),
                ),
            )
        except UserNotParticipantError:
            return None
        return result.to_dict()

    async def get_users(self, refs: list[dict]) -> list[dict]:
        from telethon.tl.functions.users import GetUsersRequest
        from telethon.tl.tlobject import TLObject

        ids = [_input_user(r) for r in refs]
        result = cast(
            list[TLObject],
            await self.budget.call("users.getUsers", lambda: self.client(GetUsersRequest(id=ids))),
        )
        return [u.to_dict() for u in result]

    async def get_full_user(self, ref: dict) -> dict:
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types.users import UserFull

        user = _input_user(ref)
        result = cast(
            UserFull,
            await self.budget.call(
                "users.getFullUser", lambda: self.client(GetFullUserRequest(id=user))
            ),
        )
        return result.to_dict()

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        from telethon.tl.functions.photos import GetUserPhotosRequest
        from telethon.tl.tlobject import TLObject

        user = _input_user(ref)
        result = cast(
            TLObject,
            await self.budget.call(
                "photos.getUserPhotos",
                lambda: self.client(
                    GetUserPhotosRequest(user_id=user, offset=offset, max_id=max_id, limit=limit)
                ),
            ),
        )
        return result.to_dict()

    async def download_user_photo(self, photo: dict) -> bytes | None:
        from telethon.errors import FileReferenceExpiredError
        from telethon.tl.types import InputPhotoFileLocation

        from paperboy.budget import SkipAndRecord

        location = InputPhotoFileLocation(
            id=photo["id"], access_hash=photo["access_hash"],
            file_reference=_file_reference(photo),
            thumb_size=_largest_photo_size(photo.get("sizes") or []),
        )
        try:
            # `file=bytes` is Telethon's documented "return the bytes" idiom
            # (same `Any` cast as `download_media`, a stub gap). `dc_id` is
            # typed `int` in Telethon's source despite defaulting to `None`
            # (an optional DC hint) — the same kind of stub gap, cast here too.
            return cast(
                bytes | None,
                await self.budget.call(
                    "upload.getFile",
                    lambda: self.client.download_file(
                        location, file=cast(Any, bytes), dc_id=cast(Any, photo.get("dc_id"))
                    ),
                ),
            )
        except FileReferenceExpiredError as exc:
            # The reference came from THIS run's getUserPhotos; a second fetch
            # would rarely help within the same pass. Skip this one avatar.
            raise SkipAndRecord(
                f"avatar download skipped: file_reference expired for photo {photo['id']}"
            ) from exc

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        from telethon.tl.functions.messages import GetMessageReactionsListRequest
        from telethon.tl.tlobject import TLObject

        peer = _input_peer_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "messages.getMessageReactionsList",
                lambda: self.client(
                    GetMessageReactionsListRequest(peer=peer, id=msg_id, limit=limit, offset=offset)
                ),
            ),
        )
        return result.to_dict()
