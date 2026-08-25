"""RPC error → `Disposition` classification (spec §8).

Telethon error classes are imported lazily inside `classify` so nothing that
merely wants to reason about dispositions (e.g. a unit test) needs Telethon
importable. `classify` duck-types on Telethon's `FloodWaitError.seconds`
attribute for `FakeFlood`/`FakePeerFlood` (thin test doubles below) as well
as the real thing.
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
        UserChannelsTooMuchError,
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
        # A `--join` (issue #20) can be refused by a real gated group: an
        # approval-gated group answers INVITE_REQUEST_SENT, and a saturated
        # account CHANNELS_TOO_MUCH. That is a clean per-join skip — the
        # discussion phase reports "joining failed" — not a run-ending crash.
        InviteRequestSentError,
        ChannelsTooMuchError,
        UserChannelsTooMuchError,
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
