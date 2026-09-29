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
  through a hash-and-count-only `MediaSink(None)` (re-verifying its sha).
  Replay never writes into the source profile: no `.incoming/`, no sweep, no
  temp copy, so a read-only source works and needs no spare disk. The
  collector finds the destination present (or the same bytes under a legacy
  name) and records rows only; if neither exists the message is counted
  `skipped` with a warning. Replay never consults the free-disk floor (the
  collector gates it on `gateway.replay`). The
  sweep therefore runs only in live collect; concurrent live runs on one
  profile are unsupported (one session per auth key).
- **Durability.** `MediaSink.close()` fsyncs before the rename, so an OS crash
  cannot leave a partial file under a final name; a failed close during
  exception unwinding is logged, never allowed to mask the original error.

## Definition of done

See the smoke transcript below (redacted; the unredacted transcripts stay in
the operator's scratch directory).

### Live smoke (4 of the 5 permitted invocations)

Runs 1 and 2 ran at commit 95c98d8; run 3 ran at `b0a00f8`, the last commit
that touches `src/` (later commits are docs only). Runs 1-2 are still valid
for the live sink path: after them, the only change to `gateway.py` /
`media_sink.py` is the `path=None` (replay) branch, which live sinks never take.

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

Invocation 3, at `b0a00f8` (its VPN check output was not saved in the
transcript file; invocation 4 below repeats the check and keeps it), a second ~1.0 GB video (`<id>`, same channel, no custody row before),
`--media-msgs <id>` only. Transcript: `smoke-64-run3.txt`.

```
media · downloaded=1 duplicates=0 unavailable=0 skipped_kind=0 skipped=0 size_mismatch=0 out_of_window=0 not_selected=7792 too_large=0 · 656s
     663.09 real        65.04 user         9.91 sys
       77381632  maximum resident set size
       96600568  peak memory footprint
sqlite> SELECT sha256, size, path FROM media ORDER BY downloaded_at DESC LIMIT 1;
086f3fb4...23d4b2|1005068556|media/08/086f3fb4...23d4b2.mp4
$ shasum -a 256 <that file>   -> 086f3fb4...23d4b2 (equals media.sha256)
$ stat -f %z <that file>      -> 1005068556          (equals media.size)
$ ls -la <scratch>/default/media/.incoming -> total 0 (empty)
```

Peak RSS 77.4 MB for a 1,005,068,556-byte file (7.7% of the file size) on the
final code.

Invocation 4, at the final commit `36b8fab` (dedup re-run of invocation 3's
command; HEAD sha and VPN check are the first lines of `smoke-64-run4.txt`):

```
HEAD: 36b8fab...
149.154.167.51 -> utun4
91.108.56.130 -> utun4
media · downloaded=0 duplicates=1 unavailable=0 ... size_mismatch=0 ...
       14.61 real ; 103219200 maximum resident set size
$ ls -la <scratch>/default/media/.incoming -> total 0 (empty)
```

Feature totals: 3 files, about 2.0 GB downloaded; 4 of 5 live invocations used.

### Replay smoke (spec 4.1): reproject reads the media, and writes nothing

Fixture: a `.backup` of the real store trimmed (`DELETE FROM raw_records WHERE
id > 990`, then `VACUUM`) to its first historical run, `legacy-0001`: 990 raw
rows, 152 `MediaDownload` records. The fixture stays in WAL mode and is
sidecar-free. Its `media/` is a symlink farm (one link per stored file, 150
present, 2 missing) into the read-only real data dir, then every non-link
entry is `chmod a-w`. Run: `reproject --profile default --phases
channel,history,media --out <scratch>/replay-out.sqlite` (12.9 s, peak RSS
63.6 MB). Unredacted transcript: `smoke-64-replay.txt`; log
`replay-out.log`; digests `replay-before.txt`/`replay-after.txt`.

A narrowed phase set with no `history` selects zero rows (the trap an earlier
attempt fell into), so the phases above are the full set the run needs.

```
WARNING  source DB could not be opened plain read-only (read-only directory,
         WAL sidecars absent); opened with immutable=1 - it must not be written
         concurrently
history  messages=543 revisions=543 tombstones=258 edges=238
WARNING  media: skipping msg <id>: replay: media file missing for sha <sha>   (x2)
media    downloaded=150 duplicates=0 unavailable=302 skipped_kind=27 skipped=2
         size_mismatch=0 out_of_window=0 not_selected=0 too_large=0
```

Row counts, columns labelled. **source** is the untrimmed backup's projection
tables (the fixture's `raw_records` is the trimmed 990); **reprojected** is the
output, and it is the pass criterion:

| table | source (untrimmed backup) | reprojected (output) |
|---|---|---|
| raw_records | 990 (trimmed) | 955 |
| messages | 59050 | 543 |
| media | 757 | **150** (152 MediaDownload - 2 missing files) |
| custody_log | 1148 | **150** |

Source-untouched proof (pasted commands and output):

```
$ diff replay-before.txt replay-after.txt          # size, mtime, mode of all 272 entries
(no output)
$ find replay-fx -name .incoming -o -name '*.log' -o -name '*-shm' -o -name '*-wal' | wc -l
0
symlink targets (size+mtime+mode md5) before == after
cross-check: output media rows 150, distinct 150, not matching a MediaDownload payload 0; custody_log 150
```
