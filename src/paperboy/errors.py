"""RPC error → `Disposition` classification (spec §8).

Telethon error classes are imported lazily inside `classify` so nothing that
merely wants to reason about dispositions (e.g. a unit test) needs Telethon
importable. `classify` duck-types on Telethon's `FloodWaitError.seconds`
attribute for `FakeFlood`/`FakePeerFlood` (thin test doubles below) as well
as the real thing.

`classify` has no per-method scope — every skip class it recognizes applies
to EVERY RPC in the codebase, since `Budget.call` classifies purely on the
exception type. That is safe only for errors whose meaning does not depend
on which method raised them (e.g. `CHAT_ADMIN_REQUIRED`). `CHANNEL_INVALID`
is not one of those: on `users.getFullUser`/`users.getUsers` (an
`inputUserFromMessage` whose provenance went stale) it means "skip this one
user"; on `channels.getFullChannel`/`updates.getChannelDifference` (the
collection target itself) it means the whole run is broken and must surface,
not be silently skipped. That case is therefore handled locally — the two
`TelethonGateway` methods that can legitimately see a stale-provenance
`CHANNEL_INVALID` catch it themselves and raise `SkipAndRecord`, and
`CHANNEL_INVALID` is deliberately absent from `_skip_error_classes` below.
"""

from __future__ import annotations

from enum import Enum


class Disposition(Enum):
    RETRY = "retry"
    SKIP = "skip"
    PHASE_STOP = "phase_stop"
    HARD_STOP = "hard_stop"


DEFAULT_FLOOD_SLEEP_THRESHOLD = 60


class FakeFlood(Exception):
    """Test double mirroring `telethon.errors.FloodWaitError`'s `.seconds`."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class FakePeerFlood(Exception):
    """Test double mirroring `telethon.errors.PeerFloodError` (hard stop, no payload)."""


def _skip_error_classes() -> tuple[type[Exception], ...]:
    from telethon.errors import (
        BroadcastForbiddenError,
        ChannelPrivateError,
        ChannelsTooMuchError,
        ChatAdminRequiredError,
        ChatNotModifiedError,
        InviteHashEmptyError,
        InviteHashExpiredError,
        InviteHashInvalidError,
        InviteRequestSentError,
        MsgIdInvalidError,
        PremiumAccountRequiredError,
        UserBannedInChannelError,
        UserChannelsTooMuchError,
        UserIdInvalidError,
        UsersTooMuchError,
    )

    return (
        ChatAdminRequiredError,
        ChannelPrivateError,
        MsgIdInvalidError,
        BroadcastForbiddenError,
        PremiumAccountRequiredError,
        # `graph`'s getChannelRecommendations/getSponsoredMessages/
        # checkChatInvite each land here in the documented "nothing to
        # return" case (no recommendations, ads disabled, invite already
        # known unchanged) — skip that one RPC, the phase continues.
        ChatNotModifiedError,
        # A t.me/+<hash> invite link in a message can be dead by the time we
        # preview it (checkChatInvite) — expired / invalid / empty. That is one
        # dead link, not a run-ending failure: skip that preview, continue.
        InviteHashExpiredError,
        InviteHashInvalidError,
        InviteHashEmptyError,
        # A `--join` (issue #20) can be refused by a real gated group in several
        # ways, all meaning "this one group cannot be joined": an approval-gated
        # group answers INVITE_REQUEST_SENT; the account is in too many channels
        # (CHANNELS_TOO_MUCH / its user-scoped sibling); the group is at its
        # member cap (USERS_TOO_MUCH); the account is banned from it
        # (USER_BANNED_IN_CHANNEL). Each is a clean per-join skip — the
        # discussion phase reports "joining failed" — not a run-ending crash.
        InviteRequestSentError,
        ChannelsTooMuchError,
        UserChannelsTooMuchError,
        UsersTooMuchError,
        UserBannedInChannelError,
        # `users.getFullUser`/`users.getUsers` on an `inputUserFromMessage`
        # whose provenance went stale (message deleted, hash rotated) can
        # answer USER_ID_INVALID: skip that one user, the profiles sweep
        # continues (person layer, spec §5 case 2). USER_ID_INVALID has no
        # other caller in this codebase (only user-input RPCs can raise it),
        # so classifying it globally is safe. CHANNEL_INVALID is deliberately
        # NOT here — see the module docstring's per-method-scoping note: it
        # is caught locally by the two gateway methods that need it, because
        # `classify` has no per-method scope and CHANNEL_INVALID from
        # `channels.getFullChannel`/`updates.getChannelDifference` on the
        # collection target itself must still surface as a real failure.
        UserIdInvalidError,
    )


def _hard_stop_error_classes() -> tuple[type[Exception], ...]:
    from telethon.errors import (
        AuthKeyDuplicatedError,
        FrozenMethodInvalidError,
        PeerFloodError,
        SessionRevokedError,
    )

    return (
        PeerFloodError,
        FrozenMethodInvalidError,
        AuthKeyDuplicatedError,
        SessionRevokedError,
        FakePeerFlood,
    )


def classify(exc: BaseException, threshold: int = DEFAULT_FLOOD_SLEEP_THRESHOLD) -> Disposition:
    """Map one RPC exception to a `Disposition` per spec §8.

    Order matters: a flood-wait check comes first (it duck-types on
    `.seconds`, which only `FloodWaitError`/`FakeFlood` carry), then the
    explicit skip/hard-stop class lists, then transient network errors
    (retryable), and finally — since "no exception is swallowed" — anything
    unrecognized is re-raised as itself rather than mapped to a made-up
    disposition.
    """
    from telethon.errors import FloodWaitError

    if isinstance(exc, FloodWaitError | FakeFlood):
        return Disposition.RETRY if exc.seconds <= threshold else Disposition.PHASE_STOP
    if isinstance(exc, _skip_error_classes()):
        return Disposition.SKIP
    if isinstance(exc, _hard_stop_error_classes()):
        return Disposition.HARD_STOP
    if isinstance(exc, ConnectionError | TimeoutError | OSError):
        return Disposition.RETRY
    raise exc
