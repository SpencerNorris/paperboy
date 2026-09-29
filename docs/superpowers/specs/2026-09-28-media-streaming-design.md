# Stream media downloads to disk; guard size and free space (#64, #53)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** #64 (streaming);
closes the remaining half of #53 (free-disk floor; `--media-max-mb` already
shipped in #57).
**Batch:** 1 (parallel with #62 — read the "Seams" section of
`2026-09-28-media-storage-overview.md` first).
**No ADR needed:** storage layout and key format are unchanged here; this is
how bytes travel, not where they live.

## 1. Problem

`TelethonGateway.download_media` passes `file=bytes`, so Telethon returns the
whole file in memory; `MediaCollector` then hashes it and `write_bytes` it.
Peak RAM is ≥ the file size (plus copies). The pending pull contains files up
to 2.4 GB; the VM has 7.7 GiB RAM, the Mac 8.6 GB. There is also no
free-disk check: an unscoped run can fill the volume.

## 2. Design

### 2.1 Gateway: stream into a sink

Change the Protocol method (`gateway.py`, the `Gateway` Protocol):

```python
async def download_media(self, input_channel: dict, message: dict, sink: MediaSink) -> bool:
    """Stream one message's media into `sink`. False if unavailable server-side."""
```

- `MediaSink` (new, `src/paperboy/media_sink.py`) is a small file-like
  writer: `write(chunk: bytes) -> int`, `reset() -> None`, and read-only
  properties `size`, `sha256` (hex, valid once writing is done). It writes to
  a temp file and updates a `hashlib.sha256` incrementally.
- `TelethonGateway`: pass the sink as Telethon's `file=` argument
  (`client.download_media(msg, file=sink)` — Telethon accepts any object with
  `write`; verify against the installed 1.44 and pin the behaviour in a
  test with a fake client). Keep `cryptg` (already a dependency) for speed.
- **Every attempt starts clean.** `Budget.call` re-invokes its factory after
  a RETRY-class error (flood wait under threshold, `ConnectionError`,
  `TimeoutError`, `OSError` — see `budget.py`), and `download_media` itself
  re-fetches and retries once on `FileReferenceExpiredError`. The factory
  must call `sink.reset()` first (truncate the temp file, fresh hasher),
  otherwise a retried download appends to a partial one and hashes garbage.
  Test this explicitly.
- Declared-size guard: the caller passes the recorded size (from
  `_recorded_size`) to the sink as `limit`; `write` raises
  `MediaSizeExceeded` once `size > limit` (only when a size is recorded).
  The collector converts it to a per-file skip (`counts["size_mismatch"]`),
  logging declared vs received — never a partial file under a final name.
- `FakeGateway` writes its fixture bytes into the sink (in a few chunks, so
  chunking is exercised); `RawReplayGateway` streams the stored file into
  the sink in chunks (no `read_bytes()` of the whole file).
- `download_user_photo` (avatars, < ~1 MB) keeps returning bytes — out of
  scope; note it in the docstring.

### 2.2 Collector: temp file, then atomic rename

In `MediaCollector.collect`:

1. Before each download: **free-disk floor** — `shutil.disk_usage(media_root)`;
   if `free - (declared size or 0) < settings.media_min_free_gb * 10**9`,
   raise `PhaseStop` with both numbers in the message (the run stops cleanly
   and resumes next time). New setting `media_min_free_gb: float = 5.0`
   (`PAPERBOY_MEDIA_MIN_FREE_GB`, `--media-min-free-gb`), `ge=0`.
2. Temp files live in `media_root / ".incoming" / f"{uuid4().hex}.part"` —
   same filesystem as the destination, so the rename is atomic.
3. After a successful download: compute the destination from the sink's sha
   exactly as today (`media_root / sha[:2] / f"{sha}{ext}"` — see Seams);
   if it exists, delete the temp file and take the dedup path (custody only);
   otherwise `os.replace(temp, dest)`. Then write rows exactly as today, with
   `size = sink.size`.
4. On any exception after the sink was created: delete the temp file
   (`try/finally`), then let the exception propagate as today.
5. At phase start, sweep `.incoming/*.part` older than 1 hour (a crashed
   previous run), logging how many and how many bytes were removed.

Seams (batch 1): keep today's destination expression and `str(path)` stored
value — #62 replaces both with keys at integration.

## 3. Tests (write first, see them fail)

- **Bounded memory:** a fake gateway streams 200 MB in 1 MB chunks;
  `tracemalloc` peak during `collect` stays under ~20 MB.
- **Retry resets the sink:** a fake client that writes half the bytes then
  raises `ConnectionError` once; the stored file's sha equals the full
  content's sha.
- **File-reference retry** (existing test) still passes with the sink.
- **Interruption:** exception mid-stream → no file under `media/`, no
  `media`/`custody_log` row, no leftover `.part`.
- **Oversize stream:** declared 10 MB, stream 11 MB → skipped, counted, no
  file, no row.
- **Disk floor:** monkeypatched `shutil.disk_usage` below the floor →
  `PhaseStop` before any bytes are requested (assert the gateway was not
  called).
- **Dedup unchanged:** identical bytes under a second document id → one file,
  two custody rows.
- **Sweep:** a stale `.part` is removed at phase start; a fresh one is not.
- `reproject` parity suite unchanged (replay streams from the stored file).

## 4. Definition of done (smoke on real data)

Against live Telegram (operator's account, proxy on), one large known file
and one photo from the pending list, with a memory measurement:

```
/usr/bin/time -l uv run paperboy collect @<channel> --phases channel,media \
    --media-msgs <id-of-a-≥1 GB-video>,<id-of-a-photo> --unsafe
```
Paste: the collect table, `maximum resident set size` from `time -l` (must
be far below the file size), `shasum -a 256` of the stored file matching its
`media.sha256`, and `ls media/.incoming` empty. Then re-run the same command
and show `duplicates` counted with no new download.

## 5. Out of scope

GCS (#63 — it will add a second sink target), key format (#62), avatars,
parallel downloads.
