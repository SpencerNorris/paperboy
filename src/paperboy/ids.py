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


def to_iso(dt: datetime | int) -> str:
    """Normalize a timezone-aware datetime or an epoch-seconds int to UTC ISO-8601 text."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            raise ValueError("to_iso requires a timezone-aware datetime")
        return dt.astimezone(UTC).isoformat()
    return datetime.fromtimestamp(dt, tz=UTC).isoformat()


def utc_now_iso() -> str:
    return to_iso(datetime.now(UTC))
