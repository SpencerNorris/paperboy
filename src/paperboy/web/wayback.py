"""Parse a Wayback Machine CDX `output=json` response (spec §2.8, §6 `web`).

`GET https://web.archive.org/cdx/search/cdx?url=t.me/s/<name>*&output=json`
returns a JSON array whose *first* row is the field-name header and every
row after it is a same-shaped array of string values — not a JSON array of
objects — e.g.::

    [
      ["urlkey","timestamp","original","mimetype","statuscode","digest","length"],
      ["...", "20190101000000", "http://t.me/s/telegram", "text/html", "200", "AB12", "12345"]
    ]

An index with zero snapshots is `[]` (no header row at all).
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_cdx_rows(payload: list[list[str]]) -> list[dict[str, str]]:
    """Zip the header row onto every following row; `[]`/header-only -> `[]`."""
    if len(payload) < 2:
        return []
    header, *rows = payload
    return [dict(zip(header, row, strict=True)) for row in rows]


def cdx_timestamp_to_iso(timestamp: str) -> str | None:
    """Wayback's 14-digit `YYYYMMDDHHMMSS` (always UTC) -> ISO-8601 UTC text.

    Returns `None` rather than raising on a malformed/short timestamp — a
    CDX row this shattered on is still stored (raw + content_hash are
    enough provenance); it just gets no parsed `timestamp` column value.
    """
    try:
        return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None
