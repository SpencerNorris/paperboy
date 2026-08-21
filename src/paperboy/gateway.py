"""The `Gateway` seam (ADR-0001): every Telegram-shaped operation a collector needs.

`Gateway` is a `typing.Protocol` so collectors depend on a shape, not on
Telethon. Every method returns plain dicts (`TLObject.to_dict()`), never
Telethon types — that's what makes `FakeGateway` (fixture replay, no
network) a drop-in test double for `TelethonGateway` (the real thing, every
call routed through `Budget.call`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from telethon import TelegramClient
    from telethon.tl.types import InputChannel, InputPeerChannel

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


class FakeGateway:
    """Replays recorded fixture dicts — no network, no `Budget` involved.

    `fixtures` keys: `resolve`, `full_channel`, `self`, `history` (a flat
    list, newest-first), `get_messages` (a `{id: message_dict}` lookup),
    `channel_difference`, `authorizations`, `password_state`, `privacy` (a
    `{key: rules_dict}` lookup keyed by `"phone"`/`"lastseen"`/`"photo"`),
    `media` (a `{msg_id: bytes}` lookup for `download_media`).
    """

    def __init__(self, fixtures: dict) -> None:
        self._fx = fixtures
        # Test introspection: every msg id `download_media` was actually
        # asked to fetch, in call order — lets a dedup test assert a
        # duplicate never reaches the gateway at all, not just that its
        # result was discarded.
        self.download_media_calls: list[int] = []

    async def resolve(self, target_value: str) -> dict:
        del target_value
        return self._fx["resolve"]

    async def get_full_channel(self, input_channel: dict) -> dict:
        del input_channel
        return self._fx["full_channel"]

    async def get_self(self) -> dict:
        return self._fx["self"]

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        del input_channel
        history = self._fx.get("history", [])
        # Mirrors real getHistory semantics: offset_id=0 means "from the
        # top"; otherwise only ids strictly below it (paging older).
        page = [m for m in history if offset_id == 0 or m["id"] < offset_id]
        for m in page[:limit]:
            yield m

    async def get_messages(self, input_channel: dict, ids: list[int]) -> list[dict]:
        del input_channel
        table: dict[int, dict] = self._fx.get("get_messages", {})
        return [table.get(i, {"_": "MessageEmpty", "id": i}) for i in ids]

    async def get_channel_difference(self, input_channel: dict, pts: int, limit: int) -> dict:
        del input_channel, pts, limit
        return self._fx["channel_difference"]

    async def get_authorizations(self) -> dict:
        return self._fx["authorizations"]

    async def get_password_state(self) -> dict:
        return self._fx["password_state"]

    async def get_privacy(self, key: str) -> dict:
        return self._fx["privacy"][key]

    async def download_media(self, input_channel: dict, message: dict) -> bytes | None:
        del input_channel
        self.download_media_calls.append(message["id"])
        table: dict[int, bytes] = self._fx.get("media", {})
        return table.get(message["id"])


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
