# Plan: split a mixed profile with `reproject --include-target/--exclude-target/--out-profile` (#70)

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-29-profile-split.md` in your first commit.)

Branch `feat/profile-split`, cut from `origin/dev/media-storage` (5c22389). Spec:
`docs/superpowers/specs/2026-09-28-profile-split-design.md` (authoritative for behaviour and DoD);
overview spec (smoke protocol); `docs/how-it-works.md` §4–5; `docs/features/reproject.md`; ADR-0005,
ADR-0007. The code on the branch wins over this prose. The investigation being split is written
`@<target>`, its new profile `<target-profile>` (the rules file names them). `file:line` cites the base.

## 0. What the branch already gives you (read before coding)

- `reproject()` loops `for run in runs: for raw_target in source.resolve_targets(run):` →
  `collect_channel(..., profile=profile, run_id=run.run_id)` (`src/paperboy/reproject.py:157-234`);
  `profile` only reaches `CollectContext.profile`, used by `media` alone (`recipes.py:119-129`).
- `ReplaySource` (`replay.py:259-570`): `runs()`, per-run `RunIndex` (#75), `resolve_targets(run)`
  (:530), `linked_group_ids(run)` (:541), `profile_root` = the SOURCE profile dir (:262; the spec says
  `media_root`). `RawReplayGateway.replay = True` (:590); `download_media` streams the stored file into
  the collector's sink, chunked, and never compares the streamed sha with the receipt's (:794-815).
- `MediaCollector.collect` (`collectors/media.py:210-426`) keys everything on
  `replay = gateway.replay is True` (:228): no `.incoming`/sweep (:229-234), no disk floor (:324),
  no temp file (:335), and a stored-file-absent skip (:384-393). Otherwise the live path: temp in
  `<media>/.incoming/<uuid>.part` → `_finalize` = `os.replace` (:116-119, :395), floor check on
  `media_root` (:324-333), dedup by content id / sha / `path.exists()` / `find_existing_key`.
- CLI `reproject` (`cli.py:432-504`): `build_reproject` (`app.py:147-166`: source exists, refuses an
  existing `out_path`, opens source THEN `Store.open`, which mkdirs the parent, `store/db.py:67`); log
  beside the output (`cli.py:459`); on `ReprojectError` the half-made output is unlinked (:471-474).
- Resolved channel id of a `ResolvedPeer` = `payload.peer.channel_id` (`collectors/channel.py:59-75`;
  non-channel → `SkipAndRecord`, #34). Username helper: `ids.primary_username` (`ids.py:50`).

## 1. Measurements on the real store (immutable `.backup`, 2026-09-29)

65 runs (6 legacy + 59 stamped). `@<target>` (its id `T`) owns exactly runs `legacy-0001..0006`
(raw ids 1–6258, 6,258 rows; 6,213 rows carry `T` or its linked group `L` in `context_json`;
zero rows for `T`/`L` in any stamped run). `legacy-0002` also holds one foreign `ResolvedPeer` that
resolved to a **user** (PeerUser, no channel id) — the "stray intrusion" ADR-0005 describes.
Six stamped runs are single-row strays with no resolve record (already warned, dropped by both).
Messages: `T` 543 + `L` 4,953 = **5,496** of 59,050. Media rows for `T`+`L`: **451**, 720,382,102 B;
449 files present on disk (720,150,296 B), **2 missing** (the known 451→449). Custody rows 607
(reproject reproduces 599, #36). No sha is shared with any other channel (0 crossover). Real media
dir: **754 files, 34,153,238,030 B** (`du -sh` = 32G). All 755 source `media.path` values are still
legacy-form (0006 not yet applied to the live store) — replay normalises them by sha (ADR-0007).
Baseline unfiltered reproject (#75 DoD, `docs/features/reproject.md` "Definition of done — offline
transcript"): ~29 min wall, raw 65,827→57,110 with `--phases channel,history,media`.

## 2. Ambiguities and resolutions

1. **Filter unit = (run, raw_target), not run.** The loop already iterates raw targets per run
   (`reproject.py:197`); a run can hold a second, foreign resolve (measured above). Decision per
   raw target by its resolved channel id; a raw target with no channel id (`peer` not a
   PeerChannel) matches nothing: kept under `--exclude-target` (it replays to the same recorded
   `channel: skip` an unfiltered reproject produces), dropped under `--include-target`. Invariant:
   every (run, raw_target) replays in exactly one of the two outputs. Log one INFO per pair:
   `reproject: run=<run_id> target=<raw> channel_id=<id|none> decision=included|excluded` and one
   summary `reproject: targets replayed=N skipped=M runs_touched=K filter=include|exclude ids=[...]`.
2. **Linked group follows its parent** because `discussion` runs inside the parent's
   `collect_channel`; no separate matching. Naming the linked group's id as `T` is therefore an
   unknown target (error), and the error lists it as `linked group of @x`.
3. **Target matching** (`targets.parse_target`, `targets.py:59-61`): `USERNAME` → case-insensitive
   match against the username of the resolved channel (`primary_username` of the `chats[]` entry whose
   `id == peer.channel_id`) OR against `parse_target(raw_target).value`; `PEER_ID` → the bare
   channel id as stored in `channels.id` (a `-100…` bot-API form is rejected by the unknown-target
   error, which prints the ids). Other target kinds → error. Result: a set of channel ids; empty →
   `ReprojectError("unknown target …; this source contains: @a (id) [+ linked group id], …;
   N resolve(s) to non-channel peers")`.
4. **Catalogue without double index builds.** Do NOT build every `RunIndex` twice. Add
   `ReplaySource.resolve_catalogue() -> list[ResolveRecord(run_id, raw_target, channel_id | None,
   username | None)]` from ONE query over `raw_records` restricted with `_kind_clause(("resolvedpeer",))`
   (`replay.py:56-71`), rows assigned to runs by rowid range (`runs()`; bisect on `lo`). `raw_target`
   uses the same `_as_sqlite_json_value(ctx["target"])` semantics as `resolve_targets` (:536) so
   the two agree.
5. **Unknown target writes nothing.** `build_reproject` creates the out store before `reproject()`
   runs, so validate before it: split `app.py:147-166` into `open_replay_source(settings, profile)`
   (source-exists check + `ReplaySource.open` + `ReplaySourceError→ConfigError`) and
   `build_reproject(settings, profile, out_path, *, check=None)` which opens the source, calls
   `check(source)` (the filter validation, may raise `ReprojectError`), then refuses/creates the out
   store. Same single composition path; the guardrail test still holds.
6. **`--out-profile P` copies media** (spec §2; #64 §2.3 delegated this here). Reuse the LIVE write
   path with the replay gateway as the byte source: `MediaCollector(copy_on_replay: bool = False)`;
   in `collect` compute `writes = (not replay) or self._copy_on_replay` and use `writes` at
   `media.py:230` (prepare `.incoming` under the OUTPUT profile's `media/`, sweep), `:324` (disk floor
   on the OUTPUT `media_root`, `settings.media_min_free_gb`), `:335` (temp `.part`), `:395`
   (`_finalize` = atomic `os.replace` in the destination). Dedup unchanged: content id, output
   `media.sha256`, `path.exists()` under the output profile, `find_existing_key(output profile dir)`.
   The hash-only branch (:384-393) stays for `writes=False`. `reproject()` gains `out_profile: str |
   None`; passes `profile=out_profile or profile` to `collect_channel` and
   `MediaCollector(copy_on_replay=out_profile is not None)`; asserts
   `profile_dir(settings, out_profile).resolve() != source.profile_root.resolve()` (ReprojectError).
   The gateway keeps `replay = True`; nothing is ever written under `source.profile_root`.
7. **Sha verified on every replayed stream** (no-shed fix): in `RawReplayGateway.download_media`,
   after `stream_file_into`, `if sink.sha256 != sha: raise SkipAndRecord("replay: media file for sha
   <sha> does not match its receipt")` → the collector's `_stream_one` turns it into `skipped` with a
   WARNING (`media.py:453-457`). Today a corrupt file surfaces as a misleading "not found"; in copy
   mode it would be copied under the wrong name. Applies to both modes.
8. **`--out-profile` name rules:** non-empty, no path separator, not `.`/`..`, `!= --profile`;
   mutually exclusive with `--out`; refuse if `<data_dir>/P/paperboy.sqlite` exists (clear message,
   before `build_reproject`'s generic one). Output `<data_dir>/P/paperboy.sqlite`, log
   `<data_dir>/P/paperboy.sqlite.log` (the existing `cli.py:459` rule; a later live collect writes
   `paperboy.log` — different file, document it). `include` and `exclude` are mutually exclusive;
   all four errors print red and `Exit(1)` like `cli.py:452-454`.
9. **Leak vs shared entity.** Leak checks are the spec's three (messages by `channel_id`, raw rows
   whose context names `T`/`L`, media via `message_uri`) plus `channels`, `custody_log`, `participants`.
   `peers`/`edges`/`users` rows that mention `T`/`L` because ANOTHER channel forwarded or mentioned
   them are legitimate and are reported, not treated as leaks.
10. **No schema change** → `docs/data-model.md` untouched (say so in the DoD). No new ADR (spec).
11. Unreferenced-media listing lives in `src/paperboy/media_audit.py` (testable) with
    `scripts/unreferenced_media.py` as a thin CLI wrapper. It opens the DB with `mode=ro` (never
    `Store.open`, which migrates); referenced = `media.sha256 ∪ custody_log.sha256` (keyed by sha
    because legacy `path` values exist); walks `media/` skipping `.incoming`; prints `key  size`
    lines sorted, a total line, exit 0; deletes nothing.

## 3. Tasks (TDD: failing test → code → commit; `uv run pytest -q --basetemp=…`, ruff, pyright green each)

Fixture helper first: `tests/test_profile_split.py::seed_two_target_source(tmp_path) -> Path`:
two `collect_channel` calls into one `Store` (two runs): run 1 `@alpha` (channel 5, history + one
media file, `FakeGateway` `media={msg_id: bytes}`), run 2 `@beta` (channel 6, `linked_chat_id=77`
with the group `Channel` in `full_channel.chats` as in `tests/test_integration_discussion.py:57-97`,
phases `channel,history,discussion,media`, its own media bytes). `FakeGateway.resolve` ignores its
argument (`gateway.py:233-236`), so each run gets its own fixture dict. Return the DB path; tests set
`PAPERBOY_DATA_DIR` and use `CliRunner` (`tests/test_reproject.py:57-70` pattern; never `async def`
around `runner.invoke`).

1. **Catalogue.** Test `tests/test_replay_gateway.py::test_resolve_catalogue_maps_targets_to_channel_ids`:
   seeded source + one hand-inserted `ResolvedPeer` with `peer={"_":"PeerUser","user_id":9}` in run 2
   → records `[(run1,"@alpha",5,"alpha"),(run2,"@beta",6,"beta"),(run2,"@stray",None,None)]`; a
   private channel (no username) → `username=None`. Code: `ResolveRecord` + `resolve_catalogue` in
   `replay.py`. Commit `feat(replay): resolve catalogue of (run, target, channel id)`.
2. **Selector.** `src/paperboy/reproject.py`: `TargetFilter(mode: "include"|"exclude", ids:
   frozenset[int]) ` with `replays(channel_id: int | None) -> bool`, and
   `resolve_target_filter(source, include: list[str], exclude: list[str]) -> TargetFilter | None`
   (resolution 3/4). Tests `tests/test_profile_split.py::test_target_spellings_resolve_to_one_id`
   (`@beta`, `beta`, `BETA`, `"6"` → `{6}`), `::test_unknown_target_lists_available_targets`
   (message contains `@alpha (5)`, `@beta (6)`, `linked group 77`, and "non-channel" when a stray
   exists), `::test_linked_group_id_is_not_a_target`. Commit `feat(reproject): target filter by
   resolved channel id`.
3. **Filtered replay + logs.** `reproject(..., target_filter=None, out_profile=None)`. Tests:
   `test_exclude_target_output_has_no_rows_for_the_channel_or_its_linked_group` (zero rows in
   messages/channels/media/custody_log/participants for 6 and 77; zero `raw_records` with
   `json_extract(context_json,'$.channel_id') IN (6,77)` or a resolve context naming beta; alpha's
   table dumps equal an unfiltered reproject's alpha dumps), `test_include_target_output_has_only_that_channel`
   (messages `channel_id ∉ {6,77}` = 0; 77 present), `test_include_and_exclude_partition_the_source`
   (parse both logs beside `--out`: the set of `(run, target)` pairs marked `included` in one and
   `excluded` in the other is the full catalogue, each pair once; `raw_records` clean + split =
   unfiltered), `test_reproject_warns_and_keeps_a_non_channel_target_under_exclude`. Commit
   `feat(reproject): --include-target/--exclude-target replay filter`.
4. **CLI flags + validation** (`cli.py`): `--include-target`/`--exclude-target` (`list[str]`,
   repeatable), `--out-profile`; resolution 5 split of `build_reproject`; resolution 8 checks. Tests:
   `test_cli_include_and_exclude_are_mutually_exclusive`, `test_cli_out_and_out_profile_are_mutually_exclusive`,
   `test_cli_out_profile_rejects_bad_names_and_the_source_profile` (parametrize `""`, `a/b`, `..`,
   `default`), `test_cli_out_profile_refuses_existing_store`, `test_cli_unknown_target_writes_nothing`
   (exit 1, `--out` path absent AND `<data_dir>/P/` absent for `--out-profile`),
   `test_cli_reproject_without_filters_is_unchanged` (existing suite green is the assertion). Commit
   `feat(cli): reproject --include-target/--exclude-target/--out-profile`.
5. **Sha verification in replay** (resolution 7). Tests
   `tests/test_replay_gateway.py::test_download_media_rejects_a_file_whose_sha_differs_from_the_receipt`
   (hash-only sink → `SkipAndRecord` naming "does not match") and
   `tests/test_reproject.py::test_reproject_skips_a_corrupt_stored_file_with_a_warning` (corrupt one
   stored byte → `media` count −1, WARNING line in the log beside `--out`, source dir unchanged).
   Commit `fix(replay): verify the streamed media sha against its receipt`.
6. **Copy into `--out-profile`** (resolution 6). Tests in `tests/test_profile_split.py`:
   `test_out_profile_copies_only_that_targets_media_and_leaves_the_source_unchanged` (size+sha
   snapshot of the source `media/` before == after, cf. `tests/test_reproject.py:549-556`; the
   output `media/` holds exactly beta's files under their keys, bytes equal, `.incoming` exists and is
   empty, every output `media.path` resolves under `profile_dir(settings, P)`),
   `test_out_profile_copy_is_atomic_temp_then_rename` (monkeypatch `os.replace` to record calls: one
   per copied file, src under `<P>/media/.incoming/`, dst the key path; no `.part` left),
   `test_out_profile_copy_applies_the_disk_floor_to_the_destination` (monkeypatch `shutil.disk_usage`
   to a tiny `free` only when called with the output media root → media phase `stopped` mentions
   the floor, no partial file, DB still written; the plain-replay test
   `test_reproject_ignores_the_live_free_disk_floor` still passes),
   `test_out_profile_copy_dedups_a_file_referenced_twice` (two messages, same bytes → one file, two
   custody rows), `test_out_profile_skips_a_missing_or_corrupt_source_file` (no file, no row,
   WARNING). Commit `feat(media): copy replayed media into a different output profile (#70)`.
7. **Unreferenced media audit** (resolution 11). `tests/test_media_audit.py`: referenced by media
   only / custody only / both → not listed; an extra file → listed with its size; `.incoming/*.part`
   ignored; legacy-form `path` rows still count as referenced (keyed by sha); the DB is opened
   read-only (mtime/size unchanged, no `-wal` growth). Script test: `runpy`/`subprocess` on
   `scripts/unreferenced_media.py --profile p` with `PAPERBOY_DATA_DIR`, exit 0, output lines.
   Commit `feat(scripts): unreferenced_media.py lists (never deletes) orphaned media`.
8. **Docs** (all in one commit, `docs: profile split (#70)`): `docs/features/reproject.md` — CLI
   section (new flags, `--out-profile` layout and log name), a "Splitting a mixed profile (#70)"
   section with the operator procedure (spec §3 verbatim in substance: back up, the two commands,
   verify, swap, `scripts/unreferenced_media.py --profile default` "list, don't delete", re-upload;
   the agent never performs the swap), the residuals paragraph (#36/#37/#38/#39/#50/#74 are
   inherited and measured), and the DoD transcript (§4). `README.md` — command table row (flags),
   "Where your data lives" (`paperboy.split.sqlite` is an operator-chosen `--out`; a new profile's
   `paperboy.sqlite.log`), `scripts/` mention. `CLAUDE.md` — commands line and the "In progress on
   `dev/media-storage`" status. `docs/how-it-works.md` §4 (replay verifies each file's fingerprint
   against the receipt; a copy into another profile is the one case replay writes files, and only
   there) and §5 (exact flags, `.incoming` in the new profile, the audit script). `docs/features/
   media-streaming.md` — one line under the replay section pointing at #70 for `--out-profile`.
   `docs/data-model.md` unchanged (no schema change) — state it in the DoD.

## 4. DoD smoke — OFFLINE, zero live calls (spec §5; rules: `PAPERBOY_DATA_DIR=<scratch>`)

`S=<scratch>/70` (rules file); `R=<real data dir>/default` (read-only). No live command runs here: leave `live-calls.log` untouched and say so in the report.

1. Source: `mkdir -p $S/default && sqlite3 "file:$R/paperboy.sqlite?mode=ro" ".backup '$S/default/paperboy.sqlite'"`.
2. Real media dir BEFORE: `find $R/media -type f ! -path '*/.incoming/*' | wc -l` and
   `find $R/media -type f ! -path '*/.incoming/*' -exec stat -f %z {} + | awk '{s+=$1} END {print NR, s}'`
   (expect 754 and 34153238030). Paste.
3. Per-file symlink farm (never a link to the whole dir):
   `cd $R/media && find . -type f ! -path './.incoming/*' | while read f; do mkdir -p "$S/default/media/$(dirname "$f")"; ln -s "$R/media/$f" "$S/default/media/$f"; done`;
   `find $S/default/media -type l | wc -l` (754).
4. Split 1 (background; record PID, start/end `date`, `/usr/bin/time -l`):
   `PAPERBOY_DATA_DIR=$S nohup /usr/bin/time -l uv run paperboy reproject --profile default --exclude-target @<target> --out $S/default/paperboy.split.sqlite > $S/split-exclude.out 2>&1 &`
5. Split 2 (may run concurrently — both read the source `mode=ro`):
   `PAPERBOY_DATA_DIR=$S nohup /usr/bin/time -l uv run paperboy reproject --profile default --include-target @<target> --out-profile <target-profile> > $S/split-include.out 2>&1 &`
   Expect ≈30 min for split 1 (it hashes ~31.4 GB), minutes for split 2 (copies 0.72 GB).
6. Verify (paste, redacted; `T`,`L` = the ids from the catalogue error message or the log):
   - clean: `SELECT count(*) FROM messages WHERE channel_id IN (T,L)`; `… FROM raw_records WHERE
     json_extract(context_json,'$.channel_id') IN (T,L)`; `… FROM raw_records WHERE lower(kind)
     LIKE '%resolvedpeer' AND lower(json_extract(context_json,'$.target')) LIKE '%<target>%'`;
     `… FROM media m JOIN messages s ON m.message_uri=s.uri WHERE s.channel_id IN (T,L)`; same for
     `custody_log.source_message_uri`; `channels WHERE id IN (T,L)`; `participants` for `T`/`L` —
     all **0**. Report (not a leak) `peers`/`edges`/`users` rows mentioning `tg:channel:T|L`.
   - split-out: `messages WHERE channel_id NOT IN (T,L)` = 0; messages 543 + 4,953; media 449
     (2 missing → `skipped` in the log); custody 599 (#36); raw rows ≤ 6,258 with the 2 stray-user
     rows absent (kept in clean instead — explain).
   - Per-table counts for `REPROJECT_TABLES`: source, clean, split-out, and `source − split-out`
     beside clean; explain each difference (shared users/peers/edges; #36 custody; #37 sync
     bookkeeping; #38/#39 peer lineage; #50 person-layer bookkeeping; #74 `web_snapshots`; the
     six zero-target stray runs dropped by both; the unfiltered baseline in `reproject.md`).
   - Media copied: `find $S/<target-profile>/media -type f ! -path '*/.incoming/*' | wc -l` (449) and
     total bytes (720150296); `.incoming` empty; no `*.part`. Sha spot check of 5 files:
     `shasum -a 256 <file>` == the filename's sha == `media.sha256` for that `path`.
   - `paperboy status --profile <target-profile>`; for the clean output copy it to
     `$S/clean/paperboy.sqlite` and run `status --profile clean` (status has no `--db`).
   - Real media dir AFTER: repeat step 2 — identical numbers. Source profile: no new files
     (`find $S/default -newer $S/default/paperboy.sqlite -type f` shows only the outputs' logs).
7. Stop-and-mark rule: any non-zero exit, a differing real-media count/size, a leak query ≠ 0, or a
   copied-file sha mismatch → stop, do not retry for a clean run, keep the transcripts, mark the
   smoke PENDING/FAILED in the DoD with the pasted output, and report. Never run the swap (§3 step 4)
   on the real profile; never delete anything the audit script lists.

## 5. Edge cases and failure modes (each is either a test above or a documented behaviour)

- Both flags / neither; repeated flags union (`--include-target @x --include-target 123`); `@x` in
  one run and `x` in another → one id, both runs; a run with two channel targets, one named → only
  that pair replays; non-channel resolve never selectable (listed in the error, kept under exclude);
  zero-target stray runs: existing warning, dropped by both (partition holds over resolved pairs).
- `--out-profile` equal to the source, path-like, or an existing store → refused, nothing written.
- Destination disk floor crossed mid-copy → `DiskFloorStop`, phase ends cleanly, no `.part` left; a
  rerun needs the output moved aside. Corrupt/missing source file → `skipped` + WARNING, no row, no file.
- SIGINT mid-split → half-made DB and possibly copied files under `<P>/media/` (the CLI unlinks the
  DB only on exceptions): document "delete `<data_dir>/P/` and rerun"; stale `.part` (> 1 h) is swept
  on the next copy (`media.py:88-103`).
- Legacy `media.path` values in the source (the real one): payloads normalised by sha
  (`replay.py:783-792`), outputs carry keys (ADR-0007). `--phases` still applies per run; a run
  without `media` copies nothing (log says so).

## Orchestrator decisions (2026-09-29, binding)

1. **Filter granularity:** resolve per (run, raw_target) pair as planned. BUT a raw_target that resolves to no channel id (it resolved to a user, or didn't resolve) inherits the run's channel target(s). If every channel target in that run is included/excluded, the stray pair follows the same way. So the stray user resolve in `@<target>`'s run goes with `@<target>`: out of the clean output, into `<target-profile>`. A run with mixed channel targets keeps the stray pair in both outputs, with a WARNING naming the run id. Test both cases, and document it in reproject.md.
2. **Sha verification in `download_media`:** yes, in both replay modes (mismatch → skipped + WARNING naming the sha prefix). Update the parity suite only if a fixture relies on the old "not found" wording, and say so.
3. **Split `build_reproject`** (`open_replay_source` + a `check=` hook) so an unknown target errors before any output exists: accepted.
4. **Smoke:** run the two split commands **sequentially**, never concurrently.
5. **Unreferenced-media listing:** `src/paperboy/media_audit.py` plus a thin `scripts/unreferenced_media.py` wrapper. Accepted.
