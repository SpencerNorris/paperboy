# Implementation plan — #64 (+#53) stream media downloads; size guard; free-disk floor

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-28-media-streaming.md` in your first commit.)

Branch: cut `feat/media-streaming` from `origin/dev/media-storage` (a68a6ee). Per task: `uv run pytest -q` (point `TMPDIR`/`--basetemp` at a volume with free space), `uv run ruff check`, `uv run pyright` green before committing. Commits end with `Co-Authored-By:` only. Never commit a machine path, channel name/id or message text (public repo). No ADR is required (spec: bytes travel differently, they live in the same place).

Spec: `docs/superpowers/specs/2026-09-28-media-streaming-design.md`; read the overview spec first (smoke protocol). Code touched: new `src/paperboy/media_sink.py`; `gateway.py` (Protocol + `FakeGateway` + `TelethonGateway`), `replay.py`, `collectors/media.py`, `config.py`, `cli.py`; tests per task; docs in Task 8.

Facts checked against the installed Telethon 1.44.0 (`telethon/client/downloads.py`): `_download_file` (527-601) does `f = file` for any non-str/non-bytes value, calls `f.write(chunk)` per chunk (570; awaits if the result is awaitable), `f.flush()` if present (580), calls `f.tell()` only when a `progress_callback` is given (575), and closes `f` only for `str`/in-memory (600-601) — never our sink. `_get_proper_filename` returns a stream object untouched (1045-1047). `_download_photo`/`_download_document` return the `file` object (882, 945) or `None` for a non-Photo/Document, empty size, etc. (851, 856, 914, 927). Cached/stripped photo sizes are written straight into `f` (823-843). `_CdnRedirect` is raised by the first `upload.getFile` result (line 94), before any chunk, and `_download_file` re-enters with the same `file` — so a redirect never leaves partial bytes ahead of the real stream.

## Ambiguities / contradictions and recommended resolutions

1. **`MediaSizeExceeded` must survive `Budget.call`.** `errors.classify` (`errors.py:150-170`) re-raises anything it does not know (`raise exc`), but turns any `OSError` into RETRY (168-169) and `Budget.call` (`budget.py:261-282`) then re-invokes the factory 3 times. Resolution: `class MediaSizeExceeded(Exception)` in `media_sink.py` (never an `OSError` subclass), attributes `declared`, `received`. Pin with a test through a real `Budget` (Task 3): the fake client is invoked exactly once.
2. **Disk errors inside the sink.** A full disk / EIO in `sink.write` is an `OSError` → RETRY → three full re-downloads of the same file, then `PhaseStop` (`budget.py:274-276`). Not transient. Resolution (addition to spec §2.1): the sink wraps `OSError` from its own file operations into `MediaSinkWriteError(Exception)` (message carries `errno`/`strerror`, not the path); the collector converts it to `PhaseStop(..., counts=counts)`. Telethon's own network `OSError`s are untouched (they are raised outside `sink.write`).
3. **Spec §2.2 step 3 conflates two existing branches.** `media.py:252-260` (sha already in `media` → custody only, `duplicates`) and `media.py:275-289` (file on disk but no row — replay idempotency / legacy suffix via `find_existing_key` → write the rows, skip the write). Resolution: keep both. After streaming: `_lookup_by_sha` hit → unlink temp, custody row, `duplicates += 1`; else if `path.exists()` or `find_existing_key(...)` → unlink temp and insert `media`/`custody_log`/raw as today; else `os.replace(temp, path)` then the rows.
4. **Declared size for photos is an upper bound, not an exact size.** Telethon picks `thumbs[-1]` after sorting `photo.sizes + video_sizes` (`_get_thumb` 780-812; a `VideoSize` sorts after every photo size), while `_recorded_size` (`media.py:74-92`) ignores `video_sizes`, so an animated photo would now trip the guard and be skipped (a regression: today it downloads). Resolution: add each `video_sizes[*].size` to the candidates (one-line change, also correct for `--media-max-mb`), pass the max as the sink `limit` for photos and documents, and apply the post-download equality check (`sink.size != declared` → `size_mismatch`) **only for documents** (`document.size` is exact; Telethon passes it as `file_size` at 940). A short document is a truncated transfer and must never be stored under a sha name.
5. **"File-reference retry (existing test)" does not exist** — `git grep FileReferenceExpired -- tests` finds nothing. Resolution: write it (Task 3), fake-client style as `tests/test_gateway_telethon.py:20-38`.
6. **Reproject.** `reproject.py:173-213` runs `MediaCollector` with the real `settings` + the source `profile`, so `resolve_media_key` points at the **source** profile's media dir: on replay the sink writes a temp copy of every stored file into `<source>/media/.incoming/` and the collector discards it (dest exists, branch 3). Cost: one extra write+delete per file per replayed run (146 GB archive → 146 GB transient writes), and `.incoming/` is created under the source profile dir. Accept for this feature (single code path, and reproject re-verifies every file's sha on the way through) and document it in `docs/features/reproject.md`; note the alternative (a hash-only sink mode for replay) as a follow-up issue, not code. `test_reproject_never_rewrites_media_files` (`tests/test_reproject.py:502-510`) monkeypatches `Path.write_bytes`, which the sink no longer uses — rework it (Task 6).
7. **ruff `ASYNC230`/`ASYNC240`** flag `open()` and `Path` methods lexically inside `async def` (verified with ruff 0.16.4 on a probe). Resolution: every blocking file operation lives in a sync helper (the sink's methods, `_prepare_media_root`, `_sweep_incoming`, `_check_disk_floor`, `_finalize`, `_stream_file_into`) called from the async code — no `noqa`.
8. **Memory test fixture.** A 200 MB `bytes` fixture is itself 200 MB of test RAM. Resolution: `FakeGateway`'s `media` value may also be a zero-arg callable returning an iterator of chunks (Task 2); the memory test builds chunks lazily.
9. **#68 needs to recognise the floor stop** ("a `PhaseStop` from the free-disk floor ends the command", list-fetch spec §2.4). Resolution: `class DiskFloorStop(PhaseStop)` in `collectors/media.py`; recipes' `except PhaseStop` (`recipes.py:174`) still catches it.
10. **`media/` is now created at phase start** (`shutil.disk_usage` needs an existing path; `.incoming/` must be on the destination filesystem). Today it is created lazily on first write; a media phase that downloads nothing will leave an empty `media/.incoming/`. Harmless; say so in the feature doc.
11. **`--max-flood-sleep`.** The spec's DoD command omits it; the overview protocol and the handoff require `--max-flood-sleep 60`. Use it.

## Task 0 — plan file

Commit this file as `docs/superpowers/plans/2026-09-28-media-streaming.md`. Commit: `docs(plan): #64 media streaming implementation plan`.

## Task 1 — `media_sink.py`

Tests first, new `tests/test_media_sink.py` (`tmp_path / "x.part"`):
- `test_write_appends_hashes_and_counts`: three chunks → `size == total`, `sha256 == hashlib.sha256(all).hexdigest()`, file bytes equal.
- `test_reset_truncates_and_restarts_hash`: write `b"garbage"`, `reset()`, write `b"good"` → `size == 4`, sha of `b"good"`, file holds exactly `b"good"`.
- `test_limit_raises_before_exceeding`: `limit=10`; `write(b"x"*10)` ok; `write(b"y")` → `MediaSizeExceeded` with `declared == 10`, `received == 11`; file size on disk still 10.
- `test_no_limit_when_none`: 1 MB through `limit=None`.
- `test_write_oserror_is_wrapped`: monkeypatch the sink's file object's `write` to raise `OSError(errno.ENOSPC, "No space left")` → `MediaSinkWriteError`, `not isinstance(exc, OSError)`, message contains `ENOSPC`/`No space left`.
- `test_context_manager_closes_file`: after `with`, the handle is closed (`sink.closed`), `size`/`sha256` still readable.
- `test_stream_file_into_copies_in_chunks`: `stream_file_into(src_path, sink, chunk_size=4)` on 10 bytes → sink sha equals, and a recording sink subclass saw 3 writes.

Code — `src/paperboy/media_sink.py`: module docstring (why a sink: Telethon accepts any object with `write`, spec §2.1; bounded memory; every attempt starts clean). `class MediaSizeExceeded(Exception)` (`declared: int`, `received: int`), `class MediaSinkWriteError(Exception)`. `class MediaSink`: `__init__(self, path: Path, *, limit: int | None = None)` opens `path` with `open(path, "wb")` (parent must exist), `hashlib.sha256()`, `_size = 0`; `write(chunk) -> int` (limit check first — raise before writing the chunk; then write, update hasher, return `len(chunk)`; `OSError` from the write → `MediaSinkWriteError`); `reset()` (`seek(0)`, `truncate()`, fresh hasher, `_size = 0`; `OSError` wrapped likewise); `flush()`; `close()`; `closed` property; `__enter__`/`__exit__`; read-only `size`, `sha256` (hexdigest — valid once writing is done). Do not implement `tell()` (Telethon only needs it for a progress callback we never pass; say so in a comment). Module function `stream_file_into(path: Path, sink: MediaSink, *, chunk_size: int = 1 << 20) -> None` = `open(path, "rb")` + `shutil.copyfileobj(fh, sink, chunk_size)` (used by replay and by tests).
Commit: `feat(media): MediaSink — incremental sha256 into a temp file with a size limit (#64)`

## Task 2 — `Gateway` Protocol + `FakeGateway`

Tests first (`tests/test_gateway_fake.py:65-69`, `137`): `download_media(ic, {"id": 7}, sink)` returns `True` and `sink.sha256`/file bytes equal the fixture; id 8 → `False` and the sink is untouched (`size == 0`); a `BaseException` fixture value is raised; `download_media_calls == [7, 8]`. New `test_fake_download_media_writes_in_several_chunks`: a 30-byte fixture arrives as ≥ 3 `write` calls (recording sink subclass). New `test_fake_download_media_accepts_chunk_factory`: value `lambda: iter([b"a", b"b"])` → sha of `b"ab"`.
Code — `gateway.py`: Protocol `download_media(self, input_channel: dict, message: dict, sink: MediaSink) -> bool` (docstring: streams into `sink`, `False` if unavailable server-side; may raise `SkipAndRecord`, `MediaSizeExceeded`; every attempt begins with `sink.reset()`); import `MediaSink` under `TYPE_CHECKING` or directly (no cycle: `media_sink` imports nothing from paperboy). `download_user_photo` docstring: "still returns bytes — avatars are < ~1 MB; streaming is out of scope (#64)". `FakeGateway.download_media`: record the call; value `None` → `False`; exception → raise; callable → iterate its chunks into `sink.write`; `bytes` → write in 3 slices (`n = max(1, len // 3)`); return `True`. Update the class docstring's `media` entry.
Commit: `feat(gateway): download_media streams into a MediaSink; FakeGateway chunks fixtures (#64)`

## Task 3 — `TelethonGateway.download_media` through a real `Budget`

Tests first, new `tests/test_gateway_telethon_media.py`, modelled on `tests/test_gateway_telethon.py:20-38`: `_FakeClient` with `__call__(request)` → `SimpleNamespace(messages=[object()])` (counts `get_messages_calls`) and `async def download_media(self, msg, file=None)` driven by a scripted list of behaviours (each: write these chunks, then optionally raise). `Budget(settings, store, sleeper=lambda s: None)` so backoffs never block; `settings = load_settings("default", {"flood_sleep_threshold": 3600})`.
- `test_transient_error_retries_with_a_fresh_sink`: attempt 1 writes half then raises `ConnectionError`; attempt 2 writes all → returns `True`, `sink.sha256 == sha(full)`, `sink.size == len(full)`, temp file bytes == full, `download_calls == 2`.
- `test_file_reference_expiry_refetches_and_resets`: attempt 1 writes 3 bytes then raises `FileReferenceExpiredError(None)`; attempt 2 succeeds → full sha, `get_messages_calls == 2`.
- `test_file_reference_expiry_twice_is_skip_and_record`: both attempts raise → `SkipAndRecord`; sink `size == 0` after the last reset... (assert `size <= 3`, and the message mentions the msg id).
- `test_size_exceeded_is_not_retried`: `MediaSink(limit=10)`, client writes 11 bytes → `pytest.raises(MediaSizeExceeded)`, `download_calls == 1`.
- `test_message_gone_returns_false`: `__call__` returns `messages=[]` → `False`, no download call.
- `test_non_downloadable_media_returns_false`: client `download_media` returns `None` without writing → `False`.
Code — `gateway.py:726-792`: signature `(self, input_channel, message, sink: MediaSink) -> bool`; `_download(tl_message) -> bool`: 
```python
def _attempt() -> Awaitable[object]:
    sink.reset()  # every Budget attempt starts clean (budget.py:245-246)
    return self.client.download_media(tl_message, file=cast(Any, sink))
return await self.budget.call("upload.getFile", _attempt) is not None
```
Keep the fetch/expiry structure; update the docstring (`file=sink`, the reset invariant, why `cast(Any, ...)` — `hints.FileLike` has no protocol case).
Commit: `feat(gateway): TelethonGateway streams downloads into the sink; fresh sink per attempt (#64)`

## Task 4 — settings + CLI: `media_min_free_gb`

Tests first: `tests/test_config.py` — `test_media_min_free_gb_default_env_and_bounds(monkeypatch)`: default `5.0`; `PAPERBOY_MEDIA_MIN_FREE_GB=1.5` → `1.5`; override `0` accepted; `-1` → `ValidationError`. `tests/test_cli.py:396-398` parametrize add `("--media-min-free-gb", "-1")`; new `test_collect_media_min_free_gb_reaches_settings` in the style of the `--media-max-mb` override test (monkeypatch the recipe/gateway as neighbouring tests do; assert `settings.media_min_free_gb == 0.5`).
Code — `config.py` after `media_max_mb` (148): `media_min_free_gb: float = Field(default=5.0, ge=0)` with a comment (#53: floor of free space on the media volume, checked before each download against `free - declared`; `0` disables). `cli.py`: `media_min_free_gb: float = typer.Option(None, "--media-min-free-gb", min=0.0, help="With --media: stop the media phase when free disk on the media volume, minus the next file's declared size, would fall below this many GB (default 5).")`; override at 242-243 style.
Commit: `feat(cli): --media-min-free-gb / PAPERBOY_MEDIA_MIN_FREE_GB (#53)`

## Task 5 — the collector

Tests first (`tests/test_collector_media.py`; helpers 17-74 unchanged):
- Existing tests keep passing unchanged except their expectations already hold (files/rows identical).
- `test_bounded_memory_for_a_large_stream`: fixture `1: lambda: (bytes([i % 251]) * (1 << 20) for i in range(200))`; `tracemalloc.start()` before `collect`, `tracemalloc.get_traced_memory()[1] < 20 * 1024 * 1024`; stored file size `200 << 20`; `media.size` equal; sha equals a streamed `hashlib` computation in the test. (Mark it with a comment: ~1–2 s.)
- `test_interrupted_stream_leaves_nothing`: fixture callable yields one chunk then raises `RuntimeError("boom")` → `pytest.raises(RuntimeError)`; no file under `tmp_path/"p"/"media"` except the empty `.incoming` dir; `media`/`custody_log` empty; no `MediaDownload` raw.
- `test_oversized_stream_is_size_mismatch`: `_sized_doc(1, 10_000_000)` with a fixture of `11_000_000` bytes (callable, 1 MB chunks) → `counts["size_mismatch"] == 1`, `downloaded == 0`, no file, no rows, `.incoming` empty, log line has both numbers.
- `test_short_document_is_size_mismatch`: declared `100`, stream `60` bytes → same assertions.
- `test_photo_size_is_only_an_upper_bound`: photo with `sizes=[{"size": 50}]`, `video_sizes=[{"_": "VideoSize", "size": 500}]`, stream 400 bytes → downloaded (no mismatch). Also extend `test_media_max_mb_uses_largest_photo_size` with a `video_sizes` entry that is the largest.
- `test_disk_floor_stops_before_any_request(monkeypatch)`: `monkeypatch.setattr(shutil, "disk_usage", lambda p: os.statvfs_result-like (total=10**12, used=0, free=2 * 10**9))` (use `shutil._ntuple_diskusage` or a `SimpleNamespace(free=...)`) with default floor 5 GB → `pytest.raises(DiskFloorStop)` (a `PhaseStop`), `gw.download_media_calls == []`, message contains both numbers; `exc.counts` carries a duplicate counted earlier in the same run (seed a dup first). Companion: floor `0` → downloads.
- `test_disk_floor_subtracts_declared_size`: free 5.5 GB, floor 5 GB, declared 1 GB → stop; declared 100 MB → proceeds.
- `test_dedup_by_sha_after_streaming_keeps_one_file`: extend `test_duplicate_sha_across_distinct_content_ids_is_safety_netted` (203-221) with: exactly one file under `media/`, `.incoming` empty.
- `test_stale_incoming_part_is_swept_fresh_is_kept(caplog)`: pre-create `media/.incoming/old.part` (mtime `now - 7200` via `os.utime`) and `new.part`; after `collect`, `old` gone, `new` present; INFO log "swept 1 stale part file(s), N bytes".
- `test_sink_write_error_is_phase_stop`: monkeypatch `MediaSink.write` to raise `MediaSinkWriteError("ENOSPC")` → `PhaseStop` (not `DiskFloorStop`), temp removed.
- `test_reproject_parity` suite (`tests/test_reproject_parity.py`) must pass unchanged — the golden's `media.size` equals `sink.size`.
Code — `collectors/media.py`:
- Imports: `os`, `shutil`, `time`, `uuid`; `from paperboy.media_keys import media_dir`; `from paperboy.media_sink import MediaSink, MediaSizeExceeded, MediaSinkWriteError`.
- `class DiskFloorStop(PhaseStop)` (docstring: raised before any RPC; #68 ends the command on it).
- Sync helpers (module level, each with a docstring): `_prepare_media_root(root) -> Path` (mkdir root and `root/".incoming"`, returns incoming); `_sweep_incoming(incoming, *, now, max_age=3600) -> tuple[int, int]` (unlink `*.part` with `st_mtime < now - max_age`; returns count, bytes); `_check_disk_floor(root, declared, floor_bytes) -> None` (raises `DiskFloorStop` — the caller attaches `counts`: raise inside `collect` instead, passing `counts=counts`); `_unlink_quiet(path)`; `_finalize(temp, dest)` (`dest.parent.mkdir(parents=True, exist_ok=True)`; `os.replace(temp, dest)`).
- `_recorded_size`: add `video_sizes` candidates (ambiguity 4).
- In `collect`: after the context guard, `media_root = media_dir(ctx.settings, ctx.profile)`, `incoming = _prepare_media_root(media_root)`, sweep + INFO log; `floor_bytes = int(ctx.settings.media_min_free_gb * 10**9)`; `counts["size_mismatch"] = 0`.
- Per row, after the `too_large` check: floor check → `raise DiskFloorStop(f"media: free disk {free/1e9:.2f} GB minus declared {declared/1e6:.1f} MB is below the {gb} GB floor (--media-min-free-gb)", counts=counts)`. Then `temp = incoming / f"{uuid.uuid4().hex}.part"`; `try:` `with MediaSink(temp, limit=declared) as sink:` call the gateway; `except SkipAndRecord` → `skipped`; `except MediaSizeExceeded as exc` → log `declared`/`received`, `size_mismatch`; `except MediaSinkWriteError as exc` → `raise PhaseStop(f"media: cannot write to the media directory: {exc}", counts=counts) from exc`; `False` → `unavailable`; document with `sink.size != declared` → `size_mismatch`; then `sha, received = sink.sha256, sink.size`; dedup/finalize per ambiguity 3 with `size=received` in the row, payload and `INSERT`; `finally: _unlink_quiet(temp)`. Extract the streaming part into `async def _stream_one(self, ctx, msg_id, media, declared, temp) -> str | tuple[str, int]` (outcome key, or `(sha, size)`) so `collect` stays readable — the `except` clauses live there.
- Update the module docstring (temp file → atomic rename; `.incoming/`; floor) and the `Gateway` call site comment.
Commit: `feat(media): stream to .incoming temp file, atomic rename, size guard, free-disk floor (#64, #53)`

## Task 6 — replay gateway + reproject guard

Tests first: `tests/test_replay_gateway.py:160-166` and `452-470`: pass a `MediaSink(tmp_path / "t.part")`; assert `True`/`False` and `sink.sha256 == sha256(b"file contents")`; the legacy-path test asserts the same via the sink. New `test_download_media_streams_in_chunks`: seed a 5 KB file, `stream_file_into` chunk size is 1 MB by default — instead monkeypatch `paperboy.replay._CHUNK` (a module constant, default `1 << 20`) to `1024` and assert ≥ 5 writes. `tests/test_reproject.py:502-510`: replace the `write_bytes` monkeypatch with (a) `monkeypatch.setattr(os, "replace", raising)`, (b) snapshot `{path: (size, sha)}` of every file under `<data>/default/media/` (excluding `.incoming`) before and assert identical after, (c) `.incoming/` empty after.
Code — `replay.py:527-546`: `download_media(self, input_channel, message, sink) -> bool`; after resolving `path`: `if not path.exists(): raise SkipAndRecord(...)` (keep the noqa), `stream_file_into(path, sink, chunk_size=_CHUNK)`, serve the clock, `return True`. `download_user_photo` unchanged.
Commit: `feat(replay): stream stored media into the sink; reproject guard asserts no final-name writes (#64)`

## Task 7 — DoD live smoke (≤ 5 invocations; this feature uses 2)

Scratch data dir `<scratch>` (named in the handoff, outside the repo; never committed). Setup, offline:
```
mkdir -p <scratch>/default/media
sqlite3 <real>/default/paperboy.sqlite ".backup '<scratch>/default/paperboy.sqlite'"
```
Pick the two files with SQL on the **scratch** copy (a video 1–2.5 GB and a photo, same channel, neither downloaded before — no custody row for the message):
```
sqlite3 <scratch>/default/paperboy.sqlite "SELECT c.username, m.msg_id, ROUND(json_extract(m.media_json,'\$.document.size')/1e9,2) AS gb FROM messages m JOIN channels c ON c.id=m.channel_id WHERE lower(m.media_kind)='messagemediadocument' AND m.deleted_at IS NULL AND json_extract(m.media_json,'\$.document.mime_type') LIKE 'video/%' AND gb BETWEEN 1.0 AND 2.5 AND NOT EXISTS (SELECT 1 FROM custody_log l WHERE l.source_message_uri=m.uri) ORDER BY gb LIMIT 5;"
sqlite3 <scratch>/default/paperboy.sqlite "SELECT m.msg_id FROM messages m WHERE m.channel_id=<that channel id> AND lower(m.media_kind)='messagemediaphoto' AND m.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM custody_log l WHERE l.source_message_uri=m.uri) ORDER BY m.msg_id DESC LIMIT 1;"
```
Choose the smallest qualifying video (keeps the run short and well under the 3 GB cap). Record the choice only in `<scratch>/smoke-64.txt`.
Before **each** live command: the VPN check from the overview §3 (both addresses via `utun*`/`ipsec*`/`ppp*`, else stop). Then (invocation 1; `.venv/bin/paperboy` rather than `uv run` so `time -l` measures the Python process itself):
```
PAPERBOY_DATA_DIR=<scratch> PAPERBOY_REQUIRE_PROXY=false /usr/bin/time -l .venv/bin/paperboy collect @<channel> --profile default --phases channel,media --media-msgs <video-id>,<photo-id> --max-rpc 60 --max-flood-sleep 60 2>&1 | tee <scratch>/smoke-64-run1.txt
```
Evidence to paste (redacted per overview §6): the collect table (`downloaded: 2`), `maximum resident set size` (must be far below the video size), then:
```
sqlite3 <scratch>/default/paperboy.sqlite "SELECT sha256, size, path FROM media ORDER BY downloaded_at DESC LIMIT 2;"
shasum -a 256 <scratch>/default/media/<xx>/<sha><ext>      # equals media.sha256; size equals media.size
ls -la <scratch>/default/media/.incoming                  # empty
```
Invocation 2: the same command again → table shows `duplicates: 2`, `downloaded: 0`; `find <scratch>/default/media -type f -newer <scratch>/smoke-64-run1.txt` prints nothing. Any stop condition (overview §5) → record on #64, no further live calls. If `doctor` blocks for a non-proxy reason, record the smoke as pending with its output.

## Task 8 — docs

- New `docs/features/media-streaming.md` in the `docs/features/pacing.md` shape: Purpose, Inputs (`--media-min-free-gb`/env, `--media-max-mb` recap), Behaviour (sink, `.incoming/<uuid>.part`, reset-per-attempt, size guard incl. the photo upper-bound rule, floor formula, sweep, dedup order, reproject transient-write note), DoD transcript (redacted).
- `README.md:141` add `--media-min-free-gb`; env table add `MEDIA_MIN_FREE_GB | 5.0`; the data-dir tree add `media/.incoming/  # in-flight downloads, swept after 1 h`.
- `docs/features/reproject.md:127-133`: replace the `write_bytes` sentence with the new guard and the transient temp-copy cost.
- `CLAUDE.md` status paragraph: one sentence (#64 shipped on `feat/media-streaming`: streamed downloads, `size_mismatch`, free-disk floor).
- `docs/data-model.md:123-136`: `media.size` = bytes received (equals the declared size for documents).
Commit: `docs: media streaming feature doc, README flags, reproject note (#64)`

## Edge cases and failure modes (each must be covered by a test above)

- Crash/`KeyboardInterrupt` mid-stream: temp under `.incoming/`, never a final name; next run's sweep removes it after 1 h (a fresh `.part` from a run that died seconds ago is kept for an hour — acceptable, bounded).
- Floor hit mid-stream is impossible by construction only if `declared` is known: with `declared=None` the floor is checked against `free` alone; a disk-full `OSError` then surfaces as `MediaSinkWriteError` → `PhaseStop` (no 3× re-download).
- Received > declared: raised before the over-limit chunk is written; counted `size_mismatch`; no rows. Received < declared (document): `size_mismatch`. Photo: upper bound only.
- Retry re-invokes the factory → `sink.reset()` (flood wait under the ceiling, transient network error, file-reference re-fetch) — never appends to a partial file.
- Dedup by sha after streaming: temp unlinked, custody row only; identical bytes under two document ids → one file, two custody rows; a file already on disk with a legacy suffix → rows only.
- `os.replace` is atomic on the same filesystem; the sink is closed before the rename; `_unlink_quiet` tolerates the temp being gone after a successful rename.
- Replay: streams the stored file through the sink (sha re-verified), transient temp copy discarded; missing file → `SkipAndRecord` as before.
- `Budget` still counts one download as one RPC however many `upload.getFile` chunks Telethon issues (pre-existing; unchanged).
