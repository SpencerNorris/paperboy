# Resume plan — #64 (+#53) stream media downloads: close the escalation

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-29-media-streaming-resume.md`.)

**Where you start:** `feat/media-streaming` at 7320b17 (9 commits over old `dev/media-storage` a68a6ee). Code gates were green there (736 passed, ruff, pyright). The run escalated on *evidence*, not code: the offline replay smoke never ran the media replay path (`--phases channel,media`, no `history` → 0 rows selected; "media 757" was the SOURCE column), the live smoke ran at 95c98d8 not HEAD, the VPN check was not in the transcript, and a comment in `reproject.py` went stale. Read `gh issue view 64 --comments` and the AMENDED spec `docs/superpowers/specs/2026-09-28-media-streaming-design.md` (§2.3, §3 last three tests, §4.1) after Step 1. Original plan: `docs/superpowers/plans/2026-09-28-media-streaming.md`. Same rules as before: tests first, `uv run pytest -q` (TMPDIR on a volume with space), `ruff`, `pyright` green per commit; `Co-Authored-By:` only; no machine paths / channel names / ids in anything committed.

## Step 1 — merge `origin/dev/media-storage` (a191e0f) into `feat/media-streaming`

`git fetch && git merge origin/dev/media-storage`. Verified with `git merge-tree`: **no textual conflicts** (dev added only docs: amended #64 spec, #75 spec, overview status, `docs/how-it-works.md`, CLAUDE.md "In progress" paragraph, README documentation list). Two semantic fix-ups in the same commit:
- `CLAUDE.md`: the branch's #64 sentence (after the person-layer paragraph) now sits above dev's "**In progress on `dev/media-storage`**" paragraph. Fold it into that paragraph ("#64 media streaming — on `feat/media-streaming`, PR pending: streamed downloads, `size_mismatch`, `--media-min-free-gb`, replay leaves the source untouched") so there is one status story.
- `README.md` "Documentation" list: add `docs/features/media-streaming.md` after `pacing.md`.
Commit: `merge: dev/media-storage (amended #64 spec, #75 spec, how-it-works) into feat/media-streaming`.

## Gap analysis — amended spec and escalation findings vs. 7320b17

| Requirement | At 7320b17 | Action |
|---|---|---|
| §2.3 hash-only sink, no `.incoming`, no sweep, no floor in replay | Done: `media_sink.py:58-76` (`path=None`), `collectors/media.py:225-235` (`replay` flag → no `_prepare_media_root`/sweep), `:324` (`if floor_bytes and not replay`), `:335` (`temp=None`), `replay.py:332-334` (`replay = True`), `:556-557` (`stream_file_into`) | none |
| §2.3 stored file missing → `skipped` + WARNING, no row | Code done: `collectors/media.py:383-392` | **no test** → T2b |
| §2.3 "must work when the source profile directory is read-only" | **Not met.** `cli.py:458` opens `<profile>/paperboy.log` for writing (`logging_setup.py:106-108` mkdir + `FileHandler`) → a read-only profile dir raises before replay starts. `test_reproject.py:533` only chmods `media/`, so it passed. Source DB itself is opened `mode=ro` only (`app.py:162` → `replay.py:89-92`), no migration — good | T1 |
| §3 replay-writes-nothing test (read-only profile, path+size+mtime digest, no `.incoming`) | Partial: `test_reproject.py:533-559` (media dirs only, paths only, default `--out` inside the profile) | T2a strengthens it |
| §3 replay-ignores-floor test | Done: `test_reproject.py:562-577` (patches `_free_bytes`) | T2c: patch `shutil.disk_usage` to *raise* — proves it is never consulted |
| §3 other tests (memory, retry reset, file-ref retry, interruption, oversize, floor, dedup, sweep, parity) | Done: `test_collector_media_streaming.py:66-268`, `test_gateway_telethon_media.py:62-121`, `test_media_sink.py`, parity suite | none |
| §4 live DoD at the final commit; VPN check in transcript | Runs 1–2 at 95c98d8 (transcripts `smoke-64-run1.txt`/`run2.txt`; VPN output not captured) | T6 |
| §4.1 small-fixture replay smoke | Never run (vacuous smoke) | T5 |
| Stale comment `reproject.py:178-183` ("temp copy of one stored file") | Stale | T3 |
| DoD "media 757" misread | Feature doc has no replay section yet; the claim lived in the DoD report | T4/T7: labelled source/output columns |

## T1 — reproject logs beside its output, never into the source profile

Test first (`tests/test_reproject.py`, next to `test_cli_reproject_custom_out_path` at :177): `test_reproject_logs_beside_the_output_not_into_the_source_profile` — `--out <tmp>/elsewhere/out.sqlite` → `<tmp>/elsewhere/out.log` exists with ≥1 JSON line; `<tmp>/default/paperboy.log` is absent (or byte-identical to before if the fixture made one). Companion assertion in the same test: default `--out` → `<tmp>/default/paperboy.reprojected.log`.
Code (`cli.py:455-458`): `configure_logging(out_path.with_suffix(".log"), console=True)` and reword the comment (the profile dir was only ever mkdir'd for the log; `build_reproject` has already validated the profile and created `out_path`'s parent). `pyright`: `Path.with_suffix` on `paperboy.reprojected.sqlite` → `paperboy.reprojected.log`.
Commit: `fix(reproject): log beside --out, not into the source profile (#64 §2.3)`.

## T2 — the three §3 replay tests

- **T2a** rewrite `test_reproject_works_against_a_read_only_source_media_dir` → `..._read_only_source_profile`: after `run_full_collect`, `sqlite3.connect(db).execute("PRAGMA journal_mode=DELETE")` and close (docstring: SQLite cannot open a WAL database in a directory where it cannot create `-shm` — wal.html "Read-only databases" — so a read-only fixture must be sidecar-free; verify this claim with a 5-line probe before relying on it and paste the probe in the commit body). Snapshot `{relpath: (size, st_mtime_ns)}` of **every** entry under `<tmp>/default` (files and dirs), `chmod` every dir `0o555` and file `0o444` (restore in `finally`), run `reproject --profile default --out <tmp>/out.sqlite`, assert exit 0, snapshot identical, no path containing `.incoming`, `media`/`custody_log` counts equal the source's.
- **T2b** `test_reproject_skips_a_missing_stored_file_with_a_warning(caplog)`: delete one stored file under `<tmp>/default/media` after the collect; reproject; assert `media` count == source − 1, `custody_log` likewise, one WARNING matching `replay skipping msg .* stored file for sha`, nothing created under `media/`.
- **T2c** in `test_reproject_ignores_the_live_free_disk_floor`: replace the `_free_bytes` patch with `monkeypatch.setattr(shutil, "disk_usage", lambda p: (_ for _ in ()).throw(AssertionError("replay consulted the disk floor")))`.
Commit: `test(reproject): read-only source profile digest, missing stored file, floor never consulted (#64 §3)`.

## T3 — `reproject.py:178-183`

Delete the `"media_min_free_gb": 0` override and its comment: the collector already skips the floor on `gateway.replay` (`media.py:324`), and T2c pins it — one mechanism, no stale prose. If the operator prefers to keep the override (decision 4), replace the comment with: "Replay never consults the floor (`MediaCollector` checks `gateway.replay`); this keeps the replay settings self-describing."
Commit: `refactor(reproject): drop the redundant free-disk override; the collector gates on gateway.replay (#64)`.

## T4 — docs (overview: docs are part of every feature's DoD)

- `docs/features/media-streaming.md`: Status → commits and PR; Reproject bullet → add "log beside `--out`", "a WAL source in a read-only directory needs its `-shm`/`-wal` present or `journal_mode=DELETE` (SQLite rule)"; DoD → state run 1–2 commit (95c98d8) and why sufficient (`git diff 95c98d8..HEAD -- src/paperboy/gateway.py src/paperboy/media_sink.py` touches only the `path=None` branch; live sinks always have a path), add run 3 (T6) with the VPN check pasted, add a **"Replay smoke (§4.1)"** section with the labelled table from T5.
- `docs/features/reproject.md`: log location; the read-only-source paragraph (journal-mode note); pointer to the §4.1 smoke.
- `README.md`: `reproject` row — "log written beside `--out`"; data-dir tree line for `paperboy.reprojected.log`.
- `CLAUDE.md`: already handled in Step 1 (keep one status paragraph).
- `docs/data-model.md`: no schema change (the `media.size` wording landed in 4f46b88) — nothing.
- `docs/how-it-works.md` §4: append "(and its log beside that database)"; §6 is correct as written.
Commit: `docs(media-streaming,reproject): replay contract, log location, §4.1 smoke (#64)`.

## T5 — §4.1 replay smoke on a small real-data fixture (offline; required)

`<scratch>` = the feature's scratch dir (holds `default/` = the `.backup` of the real store plus the two live-smoke files); `<data-dir>` = the real data dir (read only, symlink targets). Never write under `<data-dir>`.

1. `mkdir -p <scratch>/replay-src/default/media && sqlite3 <scratch>/default/paperboy.sqlite ".backup '<scratch>/replay-src/default/paperboy.sqlite'"`
2. Pick the run with the repo's own segmentation (a `--media-msgs`-only run has no history, so its media replay selects nothing — that is the vacuous-smoke trap again):
   ```
   uv run python - <<'EOF'
   from pathlib import Path; from paperboy.replay import ReplaySource
   p = Path("<scratch>/replay-src/default"); src = ReplaySource.open(p/"paperboy.sqlite", p); c = src.conn
   K = "(lower(kind)='message' OR lower(kind) LIKE '%.message')"
   for r in src.runs():
       md = c.execute("SELECT json_extract(context_json,'$.channel_id'), json_extract(context_json,'$.msg_id') FROM raw_records WHERE lower(kind)='mediadownload' AND id BETWEEN ? AND ?", (r.lo, r.hi)).fetchall()
       if not md: continue
       hist = set(c.execute(f"SELECT json_extract(context_json,'$.channel_id'), json_extract(payload_json,'$.id') FROM raw_records WHERE {K} AND id BETWEEN ? AND ?", (r.lo, r.hi)).fetchall())
       n = c.execute("SELECT count(*) FROM raw_records WHERE id BETWEEN ? AND ?", (r.lo, r.hi)).fetchone()[0]
       print(n, r.run_id, r.lo, r.hi, "mediadownloads", len(md), "covered_by_own_history", sum(t in hist for t in md))
   EOF
   ```
   Measured by the planner on the current scratch copy (67 runs, `runs()` 11.5 s): the smallest run whose own history covers its downloads is **`legacy-0001`, ids 1–990, 990 rows, 152 `MediaDownload` (152 distinct sha, ~244 MB, suffixes `.jpg/.MP4/.mp4/.MOV/.pdf`), 150 files present under `<data-dir>/default/media`, 2 missing** → expected output `media=150`, `skipped=2` (two WARNINGs), and the legacy upper-case suffixes exercise `find_existing_key`. Row 991 is the next run's `ResolvedPeer`, so the cut is clean. Re-run the snippet anyway and paste its line for the chosen run.
3. Trim (journal mode first — a WAL database cannot be opened read-only in an unwritable directory without its sidecars):
   `sqlite3 <scratch>/replay-src/default/paperboy.sqlite "PRAGMA journal_mode=DELETE; DELETE FROM raw_records WHERE id > 990; VACUUM;"` then paste `SELECT count(*), sum(lower(kind)='mediadownload') FROM raw_records;` (→ `990|152`) and `ReplaySource.open(...).runs()` (→ one run).
4. Symlink farm — resolve exactly what replay will open, one link per file:
   ```
   uv run python - <<'EOF'
   import json, sqlite3; from pathlib import Path
   from paperboy.media_keys import normalize_legacy_location, resolve_key_under
   fx, real = Path("<scratch>/replay-src/default"), Path("<data-dir>/default")
   c = sqlite3.connect(f"file:{fx/'paperboy.sqlite'}?mode=ro", uri=True); made = missing = 0
   for (pj,) in c.execute("SELECT payload_json FROM raw_records WHERE lower(kind)='mediadownload'"):
       p = json.loads(pj); key = normalize_legacy_location(p["path"], p["sha256"]); src = resolve_key_under(real, key)
       if not src.is_file(): missing += 1; print("missing", p["sha256"]); continue
       dst = resolve_key_under(fx, key); dst.parent.mkdir(parents=True, exist_ok=True); dst.symlink_to(src); made += 1
   print("links", made, "missing", missing)
   EOF
   ```
   Then `chmod -R a-w <scratch>/replay-src/default` (macOS `chmod -R` does not follow symlinks — confirm one target's mode with `stat -f %Sp` before/after; if the sandbox refuses `chmod`, say so — the digest is then the proof).
5. Digest before: `cd <scratch> && find replay-src/default -exec stat -f '%N %z %m %Sp' {} + | sort > replay-before.txt`.
6. Run (T1 puts the log at `<scratch>/replay-out.log`):
   `PAPERBOY_DATA_DIR=<scratch>/replay-src /usr/bin/time -l .venv/bin/paperboy reproject --profile default --phases channel,history,media --out <scratch>/replay-out.sqlite 2>&1 | tee <scratch>/smoke-64-replay.txt`
   Expect minutes, not hours: 481 media-bearing messages × a 990-row lookup range.
7. Digest after (same command → `replay-after.txt`); `diff replay-before.txt replay-after.txt` prints nothing; `find <scratch>/replay-src -name .incoming -o -name '*.log' -o -name '*-shm' -o -name '*-wal'` prints nothing.
8. Evidence to paste (redacted): the media phase line (`downloaded=150 … skipped=2`, plus `unavailable` = media messages with no download recorded), the "row counts — source vs reprojected" table with the columns **labelled** — the *source* column is the backup's untrimmed projection (e.g. `media 757`), the pass criterion is the *reprojected* column `media 150` (> 0, = 152 − 2 missing); `grep -c 'stored file for sha' smoke-64-replay.txt` → 2; and the cross-check (open both DBs `mode=ro` in Python): every output `media` row's `(sha256, size)` matches a fixture `MediaDownload` payload, and `custody_log` count == 150. Leave everything in `<scratch>` for the operator.

## T6 — live smoke at the FINAL commit (after T1–T5 are committed)

State so far: `<scratch>/live-calls.log` has 2 lines (run 1 08:45Z, run 2 09:00Z, both at 95c98d8); 2 files / 1.004 GB downloaded (one ~1.0 GB video, one 190 KB photo). Remaining: 3 invocations, 1 file, ~2 GB. Use **one** invocation: the same command with a third id — one *photo* (< 1 MB) from the same channel, not yet downloaded (query pattern in the original plan, Task 7, restricted to `messagemediaphoto`) → proves the live sink path at HEAD (`downloaded=1`) and dedup (`duplicates=2`) in one run; total stays 3 files / ~1.0 GB. Capture in one file, in this order: `git rev-parse HEAD`, the VPN route check (overview §3, both addresses via `utun*`), the command, its output, `sqlite3 … SELECT sha256,size,path FROM media ORDER BY downloaded_at DESC LIMIT 1`, `shasum -a 256` of that file, `ls -la <scratch>/default/media/.incoming` (empty). Append a third line to `live-calls.log`. Any stop condition (overview §5) → record on #64, no further live calls. If the operator declines a third file (decision 1), re-run the two-id command instead and report it as dedup-only.

## T7 — DoD report and PR

DoD report: code gates output pasted; T5 with the labelled table; T6 with HEAD sha and VPN lines; the explicit sentence "runs 1–2 ran at 95c98d8; run 3 at <HEAD>"; the "media 757" line corrected to "source 757 / reprojected 150 (fixture run)". Reviewer panel (adversarial + correctness), then push and open the PR against `dev/media-storage` (title `feat: stream media downloads; free-disk floor; replay leaves the source untouched (#64, #53)`), K=3 as before. Close nothing on #75 — leave the replay-lookup cost to its own spec.

## Orchestrator decisions (2026-09-29, binding)

1. Live run at the final commit: ONE new video of ~1-1.9 GB with `/usr/bin/time -l` (memory measurement on final code); totals within caps (<= 3 files, <= 3 GB, <= 5 live invocations). If no suitable video exists, record the measurement as PENDING.
2. Reproject log is written beside `--out` (`<out stem>.log`), never into the source profile.
3. WAL source in a read-only directory: open `mode=ro`; if SQLite refuses (cannot create `-shm`/`-wal`), fall back to `immutable=1` with a WARNING that the source must not be written concurrently. Test both paths.
4. Drop the redundant `media_min_free_gb: 0` override in reproject.py.
