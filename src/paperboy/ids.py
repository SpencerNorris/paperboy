"""URI-style entity ids and UTC time helpers.

Entity ids are URI strings of the shape ``tg:<kind>:<id>`` (or
``tg:msg:<channel_id>/<msg_id>`` for messages) so that projections, edges,
and exports can all reference entities by one stable, human-legible key
regardless of storage backend. All timestamps in the store are ISO-8601 UTC
text produced by :func:`to_iso` / :func:`utc_now_iso`.
"""

from __future__ import annotations

from datetime import UTC, datetime

_SCHEME = "tg"
_KINDS = {"channel", "chat", "user", "msg"}


def channel_uri(id: int) -> str:
    return f"{_SCHEME}:channel:{id}"


def chat_uri(id: int) -> str:
    return f"{_SCHEME}:chat:{id}"


def user_uri(id: int) -> str:
    return f"{_SCHEME}:user:{id}"


def msg_uri(channel_id: int, msg_id: int) -> str:
    return f"{_SCHEME}:msg:{channel_id}/{msg_id}"


def username_uri(name: str) -> str:
    """A pseudo-URI for a bare `@name`/`t.me/name` reference that hasn't been
    resolved to a numeric peer id (the `graph` collector's mention scan does
    not spend an RPC resolving these — see its module docstring). Not one of
    `parse_uri`'s numeric-id kinds — construct-only, never parsed back."""
    return f"{_SCHEME}:username:{name}"


def invite_uri(hash_: str) -> str:
    """A pseudo-URI for a `t.me/+<hash>`/`t.me/joinchat/<hash>` invite link,
    keyed by the hash itself rather than a numeric id (unjoined invite
    previews carry no chat id — see the `graph` collector). Not one of
    `parse_uri`'s numeric-id kinds — construct-only, never parsed back."""
    return f"{_SCHEME}:invite:{hash_}"


def primary_username(obj: dict) -> str | None:
    """Extract the canonical username from a TL `Channel`/`Chat`/`User` dict.

    Accounts/channels enrolled in Telegram's multi-username feature (extra
    handles bought via Fragment, or just claimed) report the legacy
    singular `username` field as null and list every handle in `usernames`
    instead (`[{"username": ..., "editable": ..., "active": ...}, ...]`).
    `editable: True` marks the account's own chosen primary handle;
    verified against a live multi-username channel (Task 17 DoD smoke).
    """
    legacy = obj.get("username")
    if legacy:
        return legacy
    usernames = obj.get("usernames") or []
    for entry in usernames:
        if entry.get("editable"):
            return entry.get("username")
    for entry in usernames:
        if entry.get("active"):
            return entry.get("username")
    return None


def peer_ref_uri(peer: dict | None) -> str | None:
    """Convert a TL `Peer*`/`InputPeer*` reference dict (e.g. a message's
    `from_id` or a forward header's `from_id`) to a paperboy URI, or None if
    absent/unrecognized. Shared by the message projection and the history
    collector's forward-edge extraction so both agree on one mapping.

    Matched case-insensitively: Telethon's `to_dict()` uses the PascalCase
    class name (`"PeerUser"`, ...), not the lowercase TL constructor name.
    """
    if not peer:
        return None
    kind = peer.get("_", "").lower()
    if kind in ("peeruser", "inputpeeruser"):
        return user_uri(peer["user_id"])
    if kind in ("peerchannel", "inputpeerchannel"):
        return channel_uri(peer["channel_id"])
    if kind in ("peerchat", "inputpeerchat"):
        return chat_uri(peer["chat_id"])
    return None


def parse_uri(uri: str) -> tuple[str, tuple[int, ...]]:
    """Parse a ``tg:<kind>:<ids>`` URI back into (kind, ids).

    Raises ``ValueError`` for anything that isn't a well-formed paperboy URI.
    """
    parts = uri.split(":")
    if len(parts) != 3 or parts[0] != _SCHEME:
        raise ValueError(f"malformed paperboy URI: {uri!r}")
    _, kind, rest = parts
    if kind not in _KINDS:
        raise ValueError(f"unknown URI kind: {kind!r}")
    try:
        if kind == "msg":
            channel_id_s, msg_id_s = rest.split("/")
            ids = (int(channel_id_s), int(msg_id_s))
        else:
            ids = (int(rest),)
    except ValueError as e:
        raise ValueError(f"malformed paperboy URI: {uri!r}") from e
    return kind, ids


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice `text` by UTF-16 code-unit offset/length, the unit Telegram's
    `MessageEntity.offset`/`length` are always expressed in (not Python
    `str` index, which counts codepoints) — matters once `text` contains any
    character outside the Basic Multilingual Plane (most emoji), which
    Python encodes as one codepoint but UTF-16 as a surrogate pair.
    `errors="ignore"` guards a slice that lands mid-pair rather than raising.
    """
    encoded = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    return encoded[start:end].decode("utf-16-le", errors="ignore")


_TME_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


def parse_tme_link(url: str) -> tuple[str, str] | None:
    """Parse a `t.me`/`telegram.me`/`telegram.dog` URL into `("username", name)`
    or `("invite", hash)`, or `None` if `url` isn't a recognized Telegram link.

    Handles `t.me/name` (and its `t.me/name/123` message-link / `t.me/s/name`
    preview variants, both reduced to the bare username — resolving the
    specific message is out of scope for this pass), `t.me/+hash`, and the
    older `t.me/joinchat/hash` invite form. Not exhaustive: reserved paths
    Telegram serves off `t.me` for other purposes (`t.me/share/...`,
    `t.me/addstickers/...`, `t.me/proxy?...`, ...) are not denylisted, so
    (username, "share") etc. is possible for those, a known limitation left
    as a follow-up rather than a hand-maintained keyword list.
    """
    from urllib.parse import urlsplit

    candidate = url if "://" in url else f"//{url}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in _TME_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    first = segments[0]
    if first.startswith("+") and len(first) > 1:
        return ("invite", first[1:])
    if first == "joinchat" and len(segments) > 1:
        return ("invite", segments[1])
    if first == "s" and len(segments) > 1:
        first = segments[1]
    if not first or first.startswith("+"):
        return None
    return ("username", first)


def to_iso(dt: datetime | int) -> str:
    """Normalize a timezone-aware datetime or an epoch-seconds int to UTC ISO-8601 text."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            raise ValueError("to_iso requires a timezone-aware datetime")
        return dt.astimezone(UTC).isoformat()
    return datetime.fromtimestamp(dt, tz=UTC).isoformat()


def utc_now_iso() -> str:
    return to_iso(datetime.now(UTC))
