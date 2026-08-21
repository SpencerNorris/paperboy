"""Parse a user-supplied string into a typed :class:`Target`.

Accepted shapes: ``@name``, bare ``name``, ``t.me/name``, ``t.me/+hash``,
``t.me/joinchat/hash``, ``t.me/name/123`` (a message link), numeric peer ids,
``+phone`` numbers, and ``#hashtag``. v1 recipes only act on channel-like
targets (``is_channel_like``); the others parse cleanly so future recipes
(phone lookup, hashtag search) can consume them, but attempting to collect
against them today raises :class:`UnsupportedTarget` at the recipe layer, not
here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TargetKind(Enum):
    USERNAME = "username"
    INVITE = "invite"
    MSG_LINK = "msg_link"
    PEER_ID = "peer_id"
    PHONE = "phone"
    HASHTAG = "hashtag"


class UnsupportedTarget(Exception):
    """Raised when the input string does not match any known target shape."""


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    raw: str
    value: str
    msg_id: int | None = None

    @property
    def is_channel_like(self) -> bool:
        return self.kind in (
            TargetKind.USERNAME,
            TargetKind.INVITE,
            TargetKind.MSG_LINK,
            TargetKind.PEER_ID,
        )


_SCHEME_RE = re.compile(r"^(https?://)?(www\.)?t\.me/", re.IGNORECASE)
# Invite hashes (`t.me/+AbCdEf...`) share the `+` prefix with E.164 phone
# numbers (`+15551234567`); the negative lookahead excludes all-digit bodies
# so a bare phone number falls through to `_PHONE_RE` instead.
_INVITE_PLUS_RE = re.compile(r"^\+(?!\d+$)([A-Za-z0-9_-]+)$")
_INVITE_JOINCHAT_RE = re.compile(r"^joinchat/([A-Za-z0-9_-]+)$")
_MSG_LINK_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,31})/(\d+)$")
# Telegram enforces its own (longer) minimum public-username length; this
# module only recognizes the *shape* of a username-like token — an invalid
# or nonexistent one still fails, later, at resolve() time.
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{0,31})$")
_PHONE_RE = re.compile(r"^\+\d{7,15}$")
_PEER_ID_RE = re.compile(r"^-?\d+$")
_HASHTAG_RE = re.compile(r"^#(\w+)$")


def parse_target(text: str) -> Target:
    raw = text
    stripped = text.strip()
    if not stripped:
        raise UnsupportedTarget(f"empty target: {text!r}")

    # Normalize away scheme + host so `t.me/...` and `https://t.me/...` share
    # one code path.
    body = _SCHEME_RE.sub("", stripped)

    if m := _INVITE_PLUS_RE.match(body):
        return Target(TargetKind.INVITE, raw, m.group(1))
    if m := _INVITE_JOINCHAT_RE.match(body):
        return Target(TargetKind.INVITE, raw, m.group(1))
    if m := _MSG_LINK_RE.match(body):
        return Target(TargetKind.MSG_LINK, raw, m.group(1), msg_id=int(m.group(2)))
    if m := _USERNAME_RE.match(body):
        return Target(TargetKind.USERNAME, raw, m.group(1))
    if _PHONE_RE.match(body):
        return Target(TargetKind.PHONE, raw, body)
    if m := _HASHTAG_RE.match(body):
        return Target(TargetKind.HASHTAG, raw, m.group(1))
    if _PEER_ID_RE.match(body):
        return Target(TargetKind.PEER_ID, raw, body)

    raise UnsupportedTarget(f"unrecognized target: {text!r}")
