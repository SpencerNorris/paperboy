# Feature: streamed media downloads, size guard, free-disk floor

**Status:** implemented on `feat/media-streaming` (#64; also addresses #53's
free-disk floor). **Spec:**
`docs/superpowers/specs/2026-09-28-media-streaming-design.md`. **Plan:**
`docs/superpowers/plans/2026-09-28-media-streaming.md`. No ADR: bytes travel
differently, they live in the same place (ADR-0007 keys are unchanged).

## Purpose

Downloading a 2 GB video must not need 2 GB of RAM, a crash must never leave
a half-written file under a content-addressed name, and an unscoped run must
not fill the disk.

## Inputs

- `--media-min-free-gb G` / `PAPERBOY_MEDIA_MIN_FREE_GB` (default `5.0`,
  `>= 0`; `0` disables the floor). On `collect`, with `--media`.
- `--media-max-mb N` (unchanged, #57): skip files whose recorded size exceeds
  N MB before requesting them.

## Behaviour

- **Sink.** `MediaSink` (`src/paperboy/media_sink.py`) is a file-like writer:
  Telethon's `download_media(file=sink)` calls `sink.write(chunk)` per chunk.
  Each chunk goes to a temp file and into an incremental SHA-256, so memory
  is one chunk however large the file. `Gateway.download_media(ic, message,
  sink) -> bool` (`False` = unavailable server-side). Avatars still return
  bytes (< ~1 MB).
- **Temp file, atomic rename.** Downloads go to
  `media/.incoming/<uuid>.part` (same filesystem as the destination), then
  `os.replace` to `media/<sha[:2]>/<sha><ext>`. A crash or exception leaves
  at most a `.part`; `media` rows and files appear only for complete
  downloads. `media/` and `media/.incoming/` are created at phase start, so
  a media phase that downloads nothing leaves an empty `.incoming/`.
- **Every attempt starts clean.** `Budget.call` re-invokes its factory after
  a flood wait or transient error, and the gateway retries once on an
  expired file reference; each attempt calls `sink.reset()` first (truncate,
  fresh hash), so a retry never appends to a partial file.
- **Declared-size guard.** `_recorded_size` (document size, or the largest
  photo/`VideoSize` variant) becomes the sink's `limit`. A stream that
  exceeds it raises `MediaSizeExceeded` (not an `OSError`, so `Budget` does
  not retry) and is counted `size_mismatch`, logging declared vs received.
  For a *document* the declared size is exact, so a short stream is also
  `size_mismatch`; for a photo it is only an upper bound. Nothing is stored
  for a mismatch.
- **Free-disk floor (#53).** Before each download,
  `free - (declared or 0) < media_min_free_gb * 1e9` raises `DiskFloorStop`
  (a `PhaseStop`, so the run ends cleanly and resumes next time) before any
  request for that file; the message carries the free space, the declared
  size and the floor. Counts gathered so far ride on the exception.
- **Disk errors.** An `OSError` from the temp file (disk full, EIO) becomes
  `MediaSinkWriteError` and a `PhaseStop`: it is not transient, so no 3x
  re-download.
- **Dedup order.** After streaming: sha already in `media` -> temp deleted,
  custody row only (`duplicates`); else file already on disk (replay, or a
  legacy suffix) -> temp deleted, rows only; else rename, then rows.
- **Sweep.** At phase start `.incoming/*.part` older than 1 h (a dead run) are
  deleted and counted in an INFO log.
- **Reproject.** `RawReplayGateway.download_media` streams the stored file
  through the sink (re-verifying its sha); the collector finds the
  destination present and discards the temp copy. Cost: one transient extra
  write per replayed file (see `docs/features/reproject.md`). Replay forces
  `media_min_free_gb=0`: it downloads nothing, and ENOSPC on the temp copy
  already stops the phase, so a rebuild never depends on host free space.
- **Durability.** `MediaSink.close()` fsyncs before the rename, so an OS crash
  cannot leave a partial file under a final name; a failed close during
  exception unwinding is logged, never allowed to mask the original error.

## Definition of done

See the smoke transcript below (redacted; the unredacted transcripts stay in
the operator's scratch directory).

### Live smoke (2 of the 5 permitted invocations)

Scratch data dir (`<scratch>`), a snapshot of the real store made with
`sqlite3 ".backup"`; `PAPERBOY_REQUIRE_PROXY=false`, `--profile default`,
`--max-rpc 60 --max-flood-sleep 60`, no `--unsafe`/`--join`/`--profiles`.
Target: one ~1.0 GB video (`<id>`) and one photo (`<id>`) from `@<channel>`,
neither downloaded before. Unredacted transcripts: `smoke-64-run1.txt`,
`smoke-64-run2.txt` in the scratch dir.

VPN check before each invocation (both DC addresses via a tunnel):

```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```

Invocation 1:

```
/usr/bin/time -l paperboy collect @<channel> --profile default --phases channel,media \
    --media-msgs <id>,<id> --max-rpc 60 --max-flood-sleep 60
media · downloaded=2 duplicates=0 unavailable=0 skipped_kind=0 skipped=0 size_mismatch=0 out_of_window=0 not_selected=7791 too_large=0 · 837s
      847.13 real        29.81 user         5.41 sys
            81657856  maximum resident set size
            91734520  peak memory footprint
```

Peak RSS 81.7 MB for a 1,004,435,113-byte video (about 8% of the file size;
the pre-#64 path held the whole file plus copies).

```
sqlite> SELECT sha256, size, path FROM media ORDER BY downloaded_at DESC LIMIT 2;
452c8df5...b90bd|189730|media/45/452c8df5...b90bd.jpg
c7ec9656...075cd|1004435113|media/c7/c7ec9656...075cd.mp4

$ shasum -a 256 <scratch>/default/media/c7/c7ec9656...075cd.mp4
c7ec965613bfa6af40990c93d6524de916c8c1e039b366085d8e166da75607cd   (equals media.sha256)
$ stat -f %z <same file>
1004435113                                                          (equals media.size)
$ ls -la <scratch>/default/media/.incoming
total 0                                                             (empty)
```

Invocation 2 (same command, same VPN check):

```
media · downloaded=0 duplicates=2 unavailable=0 skipped_kind=0 skipped=0 size_mismatch=0 out_of_window=0 not_selected=7791 too_large=0 · 24s
$ find <scratch>/default/media -type f -newer smoke-64-run1.txt
(no output)
```

The second run made no `upload.getFile` request (only the channel-phase
RPCs), and `.incoming/` stayed empty.
