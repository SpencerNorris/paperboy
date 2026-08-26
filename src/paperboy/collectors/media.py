"""The `media` collector (Phase 2, opt-in): download every stored message's
media, content-address it under
`<data_dir>/<profile>/media/<sha256[:2]>/<sha256><ext>`, and record chain of
custody. Mirrors `channel`/`history`'s shape (spec §6); unlike `history` it
does not itself talk to `messages.getHistory` — it walks messages already
projected into the store by an earlier `history` run.

Dedup is by SHA-256 (spec §6), but the actual bytes are the *expensive* thing
to compare, so this collector uses the Telegram-native `document.id`/
`photo.id` embedded in each message's stored `media_json` as a pre-download
proxy for content identity: a repost/forward carries the same document/photo
id as the original, so a duplicate is recognized *before* a single byte is
downloaded. A raw SHA-256 check right after every download is the safety net
for the (rare) case two distinct document/photo ids happen to hash to
identical bytes — either way, `media.sha256` is the primary key, so only the
first-seen message for a given file owns the `media` row; every later
occurrence (by content-id or by hash) still gets its own `custody_log` row.

EXIF/metadata extraction (`media.exif_json`) is out of scope for this pass —
it needs a dependency decision (Pillow/exifread/...) not yet made — left
NULL; tracked as a follow-up.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.config import profile_dir
from paperboy.store.db import dumps

if TYPE_CHECKING:
    from paperboy.targets import Target

# Telethon's `to_dict()` uses the PascalCase TL class name ("MessageMediaPhoto",
# "MessageMediaDocument"), not the lowercase constructor name — matched
# case-insensitively like every other `_`-discriminator check in this repo
# (see `channel.py`/`ids.py`). Every other media kind (webpage/geo/contact/
# poll/venue/...) has nothing to download and is left alone.
_DOWNLOADABLE_KINDS = {"messagemediaphoto": "photo", "messagemediadocument": "document"}


def _content_key(media: dict) -> tuple[str, int] | None:
    """A pre-download proxy for file identity: Telegram's own document/photo
    `id`, stable across every message carrying the same underlying file.
    `None` for a non-downloadable media kind or a malformed/id-less dict.
    """
    kind = (media.get("_") or "").lower()
    if kind == "messagemediaphoto":
        photo = media.get("photo") or {}
        pid = photo.get("id")
        return ("photo", pid) if pid is not None else None
    if kind == "messagemediadocument":
        doc = media.get("document") or {}
        did = doc.get("id")
        return ("document", did) if did is not None else None
    return None


def _document_attrs(media: dict) -> tuple[str | None, str | None, list | None]:
    """`(mime_type, file_name, attributes)` for a `messageMediaDocument`."""
    doc = media.get("document") or {}
    mime_type = doc.get("mime_type")
    attributes = doc.get("attributes") or []
    file_name = None
    for attr in attributes:
        if (attr.get("_") or "").lower() == "documentattributefilename":
            file_name = attr.get("file_name")
            break
    return mime_type, file_name, attributes


def _guess_ext(kind: str, mime_type: str | None, file_name: str | None) -> str:
    """Best-effort file extension for the content-addressed path.

    Telegram server-re-encodes photos as JPEG (spec §6), so `photo` is
    always `.jpg`. `document` prefers the original filename's own suffix,
    falls back to a `mimetypes` guess from the MIME type, and finally to no
    extension at all — the sha256-named file is still perfectly valid
    without one.
    """
    if kind == "photo":
        return ".jpg"
    if file_name and (suffix := Path(file_name).suffix):
        return suffix
    if mime_type and (guessed := mimetypes.guess_extension(mime_type)):
        return guessed
    return ""


class MediaCollector:
    name = "media"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.input_channel is None or ctx.channel_id is None:
            # Same guard as `history`/`catch_up`: the `channel` phase didn't
            # complete this run, so there's no access hash to download with.
            raise PhaseStop(
                "media skipped: channel context not established "
                "(channel phase did not complete)"
            )
        channel_id = ctx.channel_id
        counts = {
            "downloaded": 0, "duplicates": 0, "unavailable": 0,
            "skipped_kind": 0, "skipped": 0,
        }
        media_root = profile_dir(ctx.settings, ctx.profile) / "media"

        content_index = self._load_content_index(ctx, channel_id)

        rows = ctx.store.conn.execute(
            "SELECT uri, msg_id, media_kind, media_json, first_seen FROM messages "
            "WHERE channel_id=? AND media_kind IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY msg_id",
            (channel_id,),
        ).fetchall()

        for row in rows:
            media = json.loads(row["media_json"]) if row["media_json"] else {}
            kind = _DOWNLOADABLE_KINDS.get((row["media_kind"] or "").lower())
            if kind is None:
                counts["skipped_kind"] += 1
                continue

            key = _content_key(media)
            if key is not None and key in content_index:
                sha, path = content_index[key]
                # A dedup hit derives from the STORED message row, not a
                # fresh download (D3) — its own `first_seen` is the
                # observation, not "now".
                self._record_custody(ctx, path, sha, row["uri"], row["first_seen"])
                counts["duplicates"] += 1
                continue

            try:
                data = await ctx.gateway.download_media(
                    ctx.input_channel, {"id": row["msg_id"], "media": media}
                )
            except SkipAndRecord as exc:
                # A per-file skip (e.g. file_reference expired twice) must not
                # abort the whole media phase — skip this one file and continue.
                ctx.log.warning("media: skipping msg %s: %s", row["msg_id"], exc)
                counts["skipped"] += 1
                continue
            if data is None:
                counts["unavailable"] += 1
                continue

            sha = hashlib.sha256(data).hexdigest()
            existing = self._lookup_by_sha(ctx, sha)
            if existing is not None:
                # Safety-net dedup: two distinct document/photo ids hashed to
                # the same bytes (or `key` was None, e.g. a malformed dict).
                # Same D3 rationale as the content_index hit above.
                self._record_custody(ctx, existing, sha, row["uri"], row["first_seen"])
                counts["duplicates"] += 1
                if key is not None:
                    content_index[key] = (sha, existing)
                continue

            mime_type: str | None
            file_name: str | None
            attributes: list | None
            if kind == "photo":
                mime_type, file_name, attributes = "image/jpeg", None, None
            else:
                mime_type, file_name, attributes = _document_attrs(media)
            ext = _guess_ext(kind, mime_type, file_name)
            path = media_root / sha[:2] / f"{sha}{ext}"
            # Content-addressed: an existing path is already the right bytes.
            # Guards replay idempotency (spec §4 — reproject never re-writes a
            # media file) and spares a live re-run a redundant write too.
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            path_str = str(path)

            raw_payload = {
                "sha256": sha, "kind": kind, "size": len(data), "mime_type": mime_type,
                "file_name": file_name, "path": path_str, "message_uri": row["uri"],
            }
            downloaded_at = ctx.clock.for_payload(raw_payload)
            ctx.store.conn.execute(
                "INSERT INTO media (sha256, message_uri, kind, mime_type, size, file_name, "
                "attributes_json, path, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha, row["uri"], kind, mime_type, len(data), file_name,
                    dumps(attributes) if attributes is not None else None,
                    path_str, downloaded_at,
                ),
            )
            self._record_custody(ctx, path_str, sha, row["uri"], downloaded_at)
            ctx.store.add_raw(
                "MediaDownload", raw_payload, ctx.tier,
                {"channel_id": channel_id, "msg_id": row["msg_id"]},
                observed_at=downloaded_at,
            )
            counts["downloaded"] += 1
            if key is not None:
                content_index[key] = (sha, path_str)

        return CollectResult(name=self.name, counts=counts)

    def _load_content_index(
        self, ctx: CollectContext, channel_id: int
    ) -> dict[tuple[str, int], tuple[str, str]]:
        """`content_key -> (sha256, path)` for every file already downloaded
        for this channel — seeded from persisted state, so dedup works
        across separate `collect` runs, not just within one.
        """
        rows = ctx.store.conn.execute(
            "SELECT media.sha256 AS sha256, media.path AS path, "
            "messages.media_json AS media_json "
            "FROM media JOIN messages ON media.message_uri = messages.uri "
            "WHERE messages.channel_id = ?",
            (channel_id,),
        ).fetchall()
        index: dict[tuple[str, int], tuple[str, str]] = {}
        for r in rows:
            media = json.loads(r["media_json"]) if r["media_json"] else {}
            key = _content_key(media)
            if key is not None:
                index[key] = (r["sha256"], r["path"])
        return index

    def _lookup_by_sha(self, ctx: CollectContext, sha: str) -> str | None:
        row = ctx.store.conn.execute("SELECT path FROM media WHERE sha256=?", (sha,)).fetchone()
        return row["path"] if row else None

    def _record_custody(
        self, ctx: CollectContext, path: str, sha: str, message_uri: str, recorded_at: str
    ) -> None:
        ctx.store.conn.execute(
            "INSERT INTO custody_log (path, sha256, recorded_at, source_message_uri) "
            "VALUES (?, ?, ?, ?)",
            (path, sha, recorded_at, message_uri),
        )
