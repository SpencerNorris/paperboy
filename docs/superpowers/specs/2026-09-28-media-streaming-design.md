# Stream media downloads to disk; guard size and free space (#64, #53)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** #64 (streaming);
closes the remaining half of #53 (free-disk floor; `--media-max-mb` already
shipped in #57).
**Order:** 3 of 5 in the sequential chain (after #69 and #62; read
`2026-09-28-media-storage-overview.md` first, including the live smoke
protocol).
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
  the sink in chunks (no `read_bytes()` of the whole file). In replay that
  sink is **hash-only** (no backing file) — see §2.3.
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
3. After a successful download: `key = media_key(sink.sha256, ext)` and
   `dest = resolve_media_key(settings, profile, key)` (both from #62, already
   merged); if `dest` exists, delete the temp file and take the dedup path
   (custody only); otherwise `os.replace(temp, dest)`. Then write rows as #62
   left them (the key is the stored value), with `size = sink.size`.
4. On any exception after the sink was created: delete the temp file
   (`try/finally`), then let the exception propagate as today.
5. At phase start, sweep `.incoming/*.part` older than 1 hour (a crashed
   previous run), logging how many and how many bytes were removed.

### 2.3 Replay (`reproject`) leaves the source untouched

*Amended 2026-09-29 after the first #64 run escalated: the design above
routed replay through the live download path (disk floor, `.incoming`, temp
file + rename), and each review round found another way that leaked into a
rebuild.*

Replay rebuilds rows from files that already exist; it downloads nothing.
So in replay (`ctx.gateway.replay is True`):

- **Reads only from the source.** Replay writes the output database and
  nothing else. It creates no `.incoming/` directory, no temp file and no
  rename anywhere under the source profile, and must work when the source
  profile directory is read-only.
- **Hash-only sink.** The stored file is streamed (chunked, bounded memory)
  into a `MediaSink` with no backing file; its `sha256`/`size` confirm the
  bytes match the receipt. Nothing is copied.
- **No free-disk floor and no `.incoming` sweep** — both guard downloads,
  and replay makes none. A rebuild must not depend on how much space the
  host has.
- **Stored file present** → write the `media`/`custody_log` rows pointing at
  the existing key. **Stored file missing** (under the recorded key or a
  legacy suffix) → `skipped` with a WARNING; never fabricate a row.
- **Writing media into a *different* output profile** (#70's
  `--out-profile`) is not replay-into-source and is specified by #70, not
  here. Until #70 lands, replay's only output is the database.

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
- **Replay writes nothing into the source (§2.3):** reproject a fixture whose
  source profile directory is made read-only (`chmod`, restored in teardown);
  it completes, media rows are rebuilt, and a before/after snapshot of every
  path + size + mtime under the source profile is identical — no `.incoming`.
- **Replay ignores the disk floor:** `shutil.disk_usage` monkeypatched to 0
  free → reproject still rebuilds the media rows.
- **Replay with a missing stored file** → `skipped` counted, WARNING logged,
  no row.

## 4. Definition of done (smoke on real data)

Live, under the overview's smoke protocol (scratch data dir, no `--unsafe`,
≤ 3 GB): one video of 1–2.5 GB and one photo, from a channel already in the
store, not yet downloaded, with a memory measurement:

```
PAPERBOY_DATA_DIR=<scratch> /usr/bin/time -l uv run paperboy collect @<channel> \
    --phases channel,media --media-msgs <video-id>,<photo-id> --max-rpc 60
```
Paste (redacted per protocol §6): the collect table, `maximum resident set
size` from `time -l` (must be far below the file size), `shasum -a 256` of
the stored file matching its `media.sha256`, and `ls media/.incoming` empty.
Then re-run the same command and show `duplicates` counted with no new
download (that re-run is the second of the feature's ≤ 5 live invocations).

### 4.1 Replay smoke on a small real-data fixture (offline, required)

A full-store replay is not a usable smoke on this hardware (#75), so build a
small fixture that finishes in minutes:

1. `.backup` the scratch store (never the real one) to
   `<scratch>/replay-src/default/paperboy.sqlite`.
2. Trim it to **one run**: the smallest run (`ReplaySource.runs()`) whose raw
   rows include ≥ 1 `MediaDownload`, by deleting every `raw_records` row
   outside that run's rowid range; `VACUUM`. Paste the run's row count and
   `MediaDownload` count.
3. `<scratch>/replay-src/default/media/` is a real directory containing
   **per-file symlinks** to exactly the stored files that run references
   (never a symlink to the whole media dir, and never write into the real
   one). Then `chmod -R a-w <scratch>/replay-src/default` (if the agent
   sandbox refuses `chmod`, say so; the before/after digest below is then
   the proof).
4. `PAPERBOY_DATA_DIR=<scratch>/replay-src paperboy reproject --profile
   default --phases channel,history,media --out <scratch>/replay-out.sqlite`
   (**`history` is required**: the media phase selects its rows from the
   rebuilt `messages`).

Paste (redacted): the reproject table with **source and output columns
labelled** — output `media` must equal the run's `MediaDownload` count minus
any reported missing files, and must be > 0 (a zero is a failed smoke, not a
pass); a before/after `find <src> -exec stat` digest of the fixture profile
proving it unchanged; no `.incoming` anywhere under it.

## 5. Out of scope

GCS (#63 — it will add a second sink target), key format (#62), avatars,
parallel downloads.
