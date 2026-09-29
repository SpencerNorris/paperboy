"""The `media` collector (Phase 2, opt-in): download every stored message's
media, content-address it under
`<data_dir>/<profile>/media/<sha256[:2]>/<sha256><ext>`, and record chain of
custody. The stored location is the profile-relative key
`media/<sha256[:2]>/<sha256><ext>` (ADR-0007), never a run-dependent path.
Mirrors `channel`/`history`'s shape (spec §6); unlike `history` it does not
itself talk to `messages.getHistory` — it walks messages already
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

Streaming (#64): each download is streamed through a `MediaSink` into
`<media>/.incoming/<uuid>.part` (bounded memory, sha256 computed
incrementally), then atomically renamed to its content-addressed name — a
crash never leaves a partial file under a final name. Stale `.part` files
(> 1 h) from a dead run are swept at phase start. Before each download a
free-disk floor (`--media-min-free-gb`, #53) is checked against
`free - declared size`; crossing it stops the phase cleanly.

EXIF/metadata extraction (`media.exif_json`) is out of scope for this pass —
it needs a dependency decision (Pillow/exifread/...) not yet made — left
NULL; tracked as a follow-up.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.config import profile_dir
from paperboy.media_keys import (
    find_existing_key,
    is_valid_ext,
    media_dir,
    media_key,
    resolve_media_key,
)
from paperboy.media_sink import MediaSink, MediaSinkWriteError, MediaSizeExceeded
from paperboy.store.db import dumps

if TYPE_CHECKING:
    from paperboy.targets import Target

# Telethon's `to_dict()` uses the PascalCase TL class name ("MessageMediaPhoto",
# "MessageMediaDocument"), not the lowercase constructor name — matched
# case-insensitively like every other `_`-discriminator check in this repo
# (see `channel.py`/`ids.py`). Every other media kind (webpage/geo/contact/
# poll/venue/...) has nothing to download and is left alone.
_DOWNLOADABLE_KINDS = {"messagemediaphoto": "photo", "messagemediadocument": "document"}


_INCOMING_DIR = ".incoming"
_STALE_PART_SECONDS = 3600


class DiskFloorStop(PhaseStop):
    """Free disk (minus the next file's declared size) is below the configured
    floor. Raised before any RPC for that file; a `PhaseStop`, so recipes end
    the phase cleanly, and a distinct type so #68 can end the whole command."""


def _prepare_media_root(root: Path) -> Path:
    """Create `root` and `root/.incoming` (same filesystem as the final
    location, so the finishing rename is atomic); return the incoming dir."""
    incoming = root / _INCOMING_DIR
    incoming.mkdir(parents=True, exist_ok=True)
    return incoming


def _sweep_incoming(
    incoming: Path, *, now: float, max_age: int = _STALE_PART_SECONDS
) -> tuple[int, int]:
    """Delete `*.part` files older than `max_age` seconds (left by a crashed
    run); return `(files removed, bytes removed)`."""
    removed = freed = 0
    for part in incoming.glob("*.part"):
        try:
            st = part.stat()
            if st.st_mtime < now - max_age:
                part.unlink()
                removed += 1
                freed += st.st_size
        except FileNotFoundError:
            continue
    return removed, freed


def _free_bytes(root: Path) -> int:
    """Free bytes on the volume holding `root`."""
    return shutil.disk_usage(root).free


def _unlink_quiet(path: Path) -> None:
    """Remove `path` if it is still there (it is gone after a successful rename)."""
    path.unlink(missing_ok=True)


def _finalize(temp: Path, dest: Path) -> None:
    """Atomically move the finished temp file to its content-addressed name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp, dest)


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


def _recorded_size(media: dict) -> int | None:
    """The byte size Telegram recorded for this media, if the stored dict has
    one — `document.size`, or for a photo its largest size variant (the one
    `download_media` fetches): the max over each `PhotoSize.size`, each
    `PhotoSizeProgressive.sizes` entry and each animated-photo `VideoSize.size`
    (Telethon sorts video sizes after every photo size, so they can be the
    variant it fetches). For a photo this is an upper bound, not an exact
    size. `None` when no size is recorded, so a `--media-max-mb` cap never
    skips a file it can't prove is too big.
    """
    kind = (media.get("_") or "").lower()
    if kind == "messagemediadocument":
        size = (media.get("document") or {}).get("size")
        return size if isinstance(size, int) else None
    if kind == "messagemediaphoto":
        candidates: list[int] = []
        for variant in (media.get("photo") or {}).get("sizes") or []:
            if isinstance(variant.get("size"), int):
                candidates.append(variant["size"])
            candidates.extend(v for v in variant.get("sizes") or [] if isinstance(v, int))
        for variant in (media.get("photo") or {}).get("video_sizes") or []:
            if isinstance(variant.get("size"), int):
                candidates.append(variant["size"])
        return max(candidates) if candidates else None
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
        if is_valid_ext(suffix):
            return suffix
        # A hostile/odd DocumentAttributeFilename must never abort the phase
        # (media_key would raise): ignore the suffix and fall through to the
        # MIME guess (ADR-0007).
        logging.getLogger("paperboy.media").warning(
            "media: unusable filename suffix (len %d); ignoring it", len(suffix)
        )
    if mime_type and (guessed := mimetypes.guess_extension(mime_type)):
        return guessed
    return ""


class MediaCollector:
    """Downloads (live) or re-derives (replay) each message's media.

    `copy_on_replay` (#70): a replay normally only re-hashes files already in
    the source profile and writes nothing. When the reproject targets a
    DIFFERENT output profile (`--out-profile`), the replay gateway becomes the
    byte source for the live write path instead: the file is streamed into the
    output profile's `.incoming/`, then atomically renamed to its content-
    addressed key, with the same disk floor and dedup as a live download.
    """

    name = "media"

    def __init__(self, *, copy_on_replay: bool = False) -> None:
        self._copy_on_replay = copy_on_replay

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
            "skipped_kind": 0, "skipped": 0, "size_mismatch": 0,
        }

        media_root = media_dir(ctx.settings, ctx.profile)
        # A replay (reproject) only re-derives rows from files already in the
        # source profile: it must never write there (the source may be
        # read-only), so it gets hash-and-count-only sinks and no `.incoming`
        # - unless it copies into a different output profile (`writes`), where
        # `media_root` is the OUTPUT profile's and the live write path applies.
        replay = getattr(ctx.gateway, "replay", False) is True
        writes = not replay or self._copy_on_replay
        incoming: Path | None = None
        if writes:
            incoming = _prepare_media_root(media_root)
            swept, swept_bytes = _sweep_incoming(incoming, now=time.time())
            if swept:
                ctx.log.info("media: swept %d stale part file(s), %d bytes", swept, swept_bytes)
        floor_bytes = int(ctx.settings.media_min_free_gb * 10**9)

        content_index = self._load_content_index(ctx, channel_id)

        base_where = "channel_id=? AND media_kind IS NOT NULL AND deleted_at IS NULL"
        params: tuple = (channel_id,)
        where = base_where
        counts["out_of_window"] = 0
        counts["not_selected"] = 0
        counts["too_large"] = 0
        since = ctx.settings.media_since
        if since is not None:
            # `--media-since` (issue #52). `messages.date` is stored as
            # `to_iso()` text (`YYYY-MM-DDTHH:MM:SS+00:00`) and `parse_since`
            # yields a whole-second UTC cutoff of the same shape, so a string
            # comparison orders correctly. A NULL date can't be placed in the
            # window and is excluded — counted as out_of_window, not dropped.
            cutoff = since.isoformat()
            where = f"{base_where} AND date >= ?"
            params = (channel_id, cutoff)
            counts["out_of_window"] = ctx.store.conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {base_where} "
                "AND (date IS NULL OR date < ?)",
                (channel_id, cutoff),
            ).fetchone()[0]
            ctx.log.info(
                "media: window since %s — %d media message(s) before it skipped",
                cutoff, counts["out_of_window"],
            )

        rows = ctx.store.conn.execute(
            "SELECT uri, msg_id, media_kind, media_json, first_seen FROM messages "
            f"WHERE {where} ORDER BY msg_id",
            params,
        ).fetchall()

        selected = ctx.settings.media_msgs
        if selected is not None:
            # `--media-msgs` (issue #55). Filtered in Python, not as a SQL
            # `IN (...)`, so an arbitrarily long id list never hits SQLite's
            # bound-parameter limit. Ids with no stored media simply match
            # nothing; they are logged so a typo'd id is visible.
            wanted = set(selected)
            in_window = len(rows)
            rows = [r for r in rows if r["msg_id"] in wanted]
            counts["not_selected"] = in_window - len(rows)
            missing = sorted(wanted - {r["msg_id"] for r in rows})
            ctx.log.info(
                "media: %d selected message(s) have media to fetch; %d other(s) not selected",
                len(rows), counts["not_selected"],
            )
            if missing:
                ctx.log.warning(
                    "media: %d selected id(s) have no stored, in-window media: %s",
                    len(missing), missing,
                )
        max_bytes = (
            ctx.settings.media_max_mb * 1_000_000
            if ctx.settings.media_max_mb is not None else None
        )

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

            size = _recorded_size(media)
            if max_bytes is not None and size is not None and size > max_bytes:
                # `--media-max-mb` (issue #53): decided from the size Telegram
                # recorded, before a single byte is fetched.
                ctx.log.info(
                    "media: skipping msg %s: %.1f MB exceeds --media-max-mb %d",
                    row["msg_id"], size / 1e6, ctx.settings.media_max_mb,
                )
                counts["too_large"] += 1
                continue

            if floor_bytes and writes:
                free = _free_bytes(media_root)
                if free - (size or 0) < floor_bytes:
                    raise DiskFloorStop(
                        f"media: free disk {free / 1e9:.2f} GB minus declared "
                        f"{(size or 0) / 1e6:.1f} MB is below the "
                        f"{ctx.settings.media_min_free_gb:g} GB floor "
                        "(--media-min-free-gb)",
                        counts=counts,
                    )

            temp = incoming / f"{uuid.uuid4().hex}.part" if incoming is not None else None
            try:
                try:
                    outcome = await self._stream_one(
                        ctx, row["msg_id"], media, kind, size, temp
                    )
                except MediaSinkWriteError as exc:
                    # A full disk / EIO is not transient: stop the phase rather
                    # than re-download (the sink error is deliberately not an OSError).
                    raise PhaseStop(
                        f"media: cannot write to the media directory: {exc}",
                        counts=counts,
                    ) from exc
                if isinstance(outcome, str):
                    counts[outcome] += 1
                    continue
                sha, received = outcome
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
                loc = media_key(sha, ext)
                path = resolve_media_key(ctx.settings, ctx.profile, loc)
                # Content-addressed: an existing path is already the right bytes.
                # Guards replay idempotency (spec §4 — reproject never re-writes a
                # media file) and spares a live re-run a redundant write too.
                if not path.exists():
                    # The same bytes may already sit under a different extension -
                    # a pre-#62 file whose legacy suffix this version would not
                    # re-derive. Reuse that location instead of writing a second
                    # copy (and keep the reprojected row faithful to the source).
                    existing_key = find_existing_key(profile_dir(ctx.settings, ctx.profile), sha)
                    if existing_key is not None:
                        loc = existing_key
                    elif temp is None:
                        # Replay with the stored file absent under both names:
                        # there are no bytes to place and the source must not be
                        # written to. Skip (recorded), never fabricate a row.
                        ctx.log.warning(
                            "media: replay skipping msg %s: stored file for sha %s not found",
                            row["msg_id"], sha,
                        )
                        counts["skipped"] += 1
                        continue
                    else:
                        _finalize(temp, path)
            finally:
                # A no-op after a successful rename; otherwise discards the
                # partial/duplicate temp so nothing lingers under `.incoming/`.
                if temp is not None:
                    _unlink_quiet(temp)

            raw_payload = {
                "sha256": sha, "kind": kind, "size": received, "mime_type": mime_type,
                "file_name": file_name, "path": loc, "message_uri": row["uri"],
            }
            downloaded_at = ctx.clock.for_payload(raw_payload)
            ctx.store.conn.execute(
                "INSERT INTO media (sha256, message_uri, kind, mime_type, size, file_name, "
                "attributes_json, path, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha, row["uri"], kind, mime_type, received, file_name,
                    dumps(attributes) if attributes is not None else None,
                    loc, downloaded_at,
                ),
            )
            self._record_custody(ctx, loc, sha, row["uri"], downloaded_at)
            ctx.store.add_raw(
                "MediaDownload", raw_payload, ctx.tier,
                {"channel_id": channel_id, "msg_id": row["msg_id"]},
                observed_at=downloaded_at,
            )
            counts["downloaded"] += 1
            if key is not None:
                content_index[key] = (sha, loc)

        return CollectResult(name=self.name, counts=counts)

    async def _stream_one(
        self,
        ctx: CollectContext,
        msg_id: int,
        media: dict,
        kind: str,
        declared: int | None,
        temp: Path | None,
    ) -> str | tuple[str, int]:
        """Stream one message's media into `temp`. Returns the `counts` key of
        a non-download outcome (`skipped`/`unavailable`/`size_mismatch`), or
        `(sha256, received bytes)` of a complete file. A `MediaSinkWriteError`
        (local disk failure) propagates to the caller.

        `declared` caps the stream (a longer one raises `MediaSizeExceeded`
        mid-transfer, never after buffering). Only a *document* has an exact
        declared size, so only a short document is a mismatch; a photo's
        declared size is an upper bound (see `_recorded_size`).
        """
        assert ctx.input_channel is not None  # guarded at the top of `collect`
        try:
            with MediaSink(temp, limit=declared) as sink:
                available = await ctx.gateway.download_media(
                    ctx.input_channel, {"id": msg_id, "media": media}, sink
                )
        except SkipAndRecord as exc:
            # A per-file skip (e.g. file_reference expired twice) must not
            # abort the whole media phase — skip this one file and continue.
            ctx.log.warning("media: skipping msg %s: %s", msg_id, exc)
            return "skipped"
        except MediaSizeExceeded as exc:
            ctx.log.warning(
                "media: msg %s streamed %d bytes, more than the declared %d; discarded",
                msg_id, exc.received, exc.declared,
            )
            return "size_mismatch"
        if not available:
            return "unavailable"
        if kind == "document" and declared is not None and sink.size != declared:
            ctx.log.warning(
                "media: msg %s streamed %d bytes, declared %d; discarded",
                msg_id, sink.size, declared,
            )
            return "size_mismatch"
        return sink.sha256, sink.size

    def _load_content_index(
        self, ctx: CollectContext, channel_id: int
    ) -> dict[tuple[str, int], tuple[str, str]]:
        """`content_key -> (sha256, media key)` (ADR-0007: relative to the profile
        dir) for every file already downloaded for this channel — seeded from
        persisted state, so dedup works across separate `collect` runs, not
        just within one.
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
        self, ctx: CollectContext, key: str, sha: str, message_uri: str, recorded_at: str
    ) -> None:
        ctx.store.conn.execute(
            "INSERT INTO custody_log (path, sha256, recorded_at, source_message_uri) "
            "VALUES (?, ?, ?, ?)",
            (key, sha, recorded_at, message_uri),
        )
