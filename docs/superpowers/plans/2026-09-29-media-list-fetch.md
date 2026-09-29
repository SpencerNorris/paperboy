# Plan: `fetch-media LIST` — download media for an ordered cross-channel list (#68)

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-29-media-list-fetch.md` in your first commit.)

Branch `feat/fetch-media-list`, cut from `origin/dev/media-storage` (6b9c6ee). Spec:
`docs/superpowers/specs/2026-09-28-media-list-fetch-design.md` (authoritative for behaviour and DoD);
overview spec (live smoke protocol, docs DoD); `docs/how-it-works.md` §2–§6; `docs/features/pacing.md`,
`media-streaming.md`; ADR-0003/0005/0007. The code on the branch wins over this prose. `file:line` cites
the base. The operator's list is `<list>`; channels are `@<channel>`, ids `<id>` — never anything else in
committed text. TDD: every task starts with a failing test, then the smallest change that passes.

## 0. What the branch already gives you (read before coding)

- `collect_channel` (`src/paperboy/recipes.py:108-207`): builds ONE `CollectContext` (`:141`), runs the
  active collectors in order; `SkipAndRecord`/`PhaseStop` are recorded and the run continues (`:162-188`),
  `HardStop` is recorded and the loop `break`s (`:189-196`) — it is NOT re-raised, the caller reads
  `CollectResult.stopped`. The `channel` phase sets `ctx.input_channel/channel_id/tier` (`collectors/
  channel.py:158-164`) and costs 3 RPCs: `get_self`, `resolve` (`contacts.resolveUsername`, 5 s base ×
  factor), `get_full_channel`. `collectors=` overrides the active list (`:116`, tests do this).
- `MediaCollector.collect` (`collectors/media.py:223-442`): requires context (`:224-230`), walks
  `messages WHERE channel_id=? AND media_kind IS NOT NULL AND deleted_at IS NULL ORDER BY msg_id`
  (`:255-285`), filters by `settings.media_msgs` in Python (`:287-306`), dedups by `_content_key`
  (`:122-136`, per-channel index `:490-511`) then by sha (`:513`), raises `DiskFloorStop(PhaseStop)`
  (`:74-77`, `:340-349`) and `PhaseStop` on sink errors (`:357-363`); per file: `channels.getMessages`
  + `upload.getFile` (`gateway.py:749-822`). Outcome vocabulary = the `counts` keys (`:232-235`).
- `Budget` (`budget.py:101-308`): every attempt counts toward `max_rpc_per_run` (`:217-223` → `HardStop`);
  `_pace` sleeps a persisted flood cooldown unconditionally (`:204-215`) — relevant to §2.8.
- `CollectResult` (`collectors/base.py:47-51`), `Store.begin_run/run_id/add_raw` (`store/db.py:113-158`).
- Replay: `ReplaySource.runs()` (`replay.py:370`), `resolve_targets(run)` (`:559`), `resolve_catalogue()`
  (`:570`, gives `(run_id, raw_target, channel_id, username)`), `has_kind` (`:636`); `RawReplayGateway`
  serves `resolve`/`get_full_channel`/`get_self` from the run's own index (`:703-722`); a run with no
  message raws replays `history` cleanly (empty page + synthetic final diff, `:813-825`). `reproject()`
  skips a run with no `ResolvedPeer` with a WARNING (`reproject.py:357-370`); `detect_phases` (`:225`).
  `ReplayClock.serve_json/for_payload` (`clock.py:74-86`) — a payload gets its stored stamp only if the
  same JSON was served first.
- CLI patterns: `collect` (`cli.py:154-348`; doctor preflight `:334-348`), `FakeGateway` fixture keys
  (`gateway.py:167-231`; `resolve` ignores its argument `:233-236`; `full_channel_by_id` exists).
  `targets.parse_target` rejects `t.me/c/<id>/<mid>` (`targets.py:55`) — the list parser needs its own regexes.

## 1. Measurements (read-only probe of `<list>` and the real store, 2026-09-29; counts only)

- `<list>`: 1,719 rows, 13 columns (`uri`, `priority`, `kind`, `size_mb`, `already_downloaded`,
  `deleted`, `review_status`, … ; only `uri` and `priority` matter). Every `uri` is `tg:msg:N/N`;
  0 duplicates. Priority: P1 446 / P2 228 / P3 298 / PH 747. Kind: video 972 / photo 747.
  `already_downloaded`: 0 → 1,620, 1 → 99. `deleted`: all False. Declared size: sum ≈ 162 GB,
  max ≈ 2.42 GB, median ≈ 6.9 MB. 11 channels (rows per channel: 800, 245, 202, 194, 80, 64, 40, 40,
  28, 24, 2).
- Store: all 1,719 URIs have a `messages` row (0 `not_in_store`), all have a downloadable
  `media_kind`, 0 tombstoned. 10 of the 11 channels have a `channels.username`; the 11th (40 rows) is the
  username-less linked group of the excluded investigation → `unresolvable`. `already_stored` by content
  key over the whole store: 118 rows (98 have a custody row; the catalogue's own flag says 99 — reposts
  share content ids). 0 intra-list content-key duplicates. Smoke candidates (≤ 50 MB, flag 0, resolvable,
  key not stored): 346 videos, 677 photos — plenty from ≥ 2 channels.
- The store is at migration 0004; opening the `.backup` copy applies 0005/0006 (expected, logged).

## 2. Ambiguities and resolutions

1. **Format detection & parsing** (`src/paperboy/media_list.py`, new). The first non-blank, non-`#`
   line decides: if its comma-split, stripped, lower-cased fields contain `uri` → CSV (`csv.DictReader`,
   `newline=""`, `encoding="utf-8-sig"`, `line_no = reader.line_num`); else plain lines (`line_no` =
   physical line). Accepted `uri` forms (own regexes): `tg:msg:<cid>/<mid>`, `https?://t.me/c/<cid>/<mid>`,
   `https?://t.me/<username>/<mid>` (also without scheme; `telegram.me` not needed). `ListRow(line_no,
   uri, channel_id: int | None, username: str | None, msg_id, priority: str | None)`; `priority` is the
   stripped column value, `""`/absent → `None`, never interpreted. Every malformed row (bad uri, empty
   uri, CSV row shorter than the header) is collected and `MediaListError` lists ALL `line_no`s; the CLI
   prints them and exits 1 before touching the store. Dedup at parse time by `(channel_id or
   username.lower(), msg_id)`: later occurrences become `duplicate_row` (kept in the row list for the
   report, never fetched).
2. **Username rows** resolve offline in `classify_rows` via `channels.username` (case-insensitive);
   no match → `not_in_store`. After resolution dedup again by `(channel_id, msg_id)` → `duplicate_row`.
3. **Offline classification order** (per row, first match wins; `classify_rows(store, rows)`):
   `duplicate_row` → `not_in_store` (no `messages` row) → `deleted` (`deleted_at` set — the collector
   never selects these, `media.py:255`; spec extension, honest rather than `no_media`) → `no_media`
   (`media_kind` not in `_DOWNLOADABLE_KINDS`) → `unresolvable` (no `channels` row or NULL username) →
   `already_stored` (content key in a GLOBAL `{key: (sha, path)}` index from `media JOIN messages`, OR
   `media.message_uri = uri`, OR a `custody_log.source_message_uri = uri` row) → `pending`. Make
   `_content_key`/`_recorded_size` public (`content_key`, `recorded_size`, keep the old names as aliases)
   for the classifier and the GB columns. Spec order is kept deliberately: the DoD expects the 40
   linked-group rows as `unresolvable`.
4. **Segmenting**: pending rows grouped by `(priority, channel_id)` in first-appearance order of the
   pair (`plan_segments(rows) -> list[Segment(priority, channel_id, username, rows)]`). Row order
   inside a segment is list order; `media_msgs` is a set so the collector fetches by `msg_id ASC`
   (`media.py:283`) — document that within-segment order is id order, list order decides segments.
5. **One resolve per channel per run** (recipe change): add `ChannelContext(input_channel: dict,
   channel_id: int, tier: str, source_run_id: str)` (frozen dataclass, `collectors/base.py`) and
   `collect_channel(..., channel_context: ChannelContext | None = None)`. With a context: preset
   `ctx.input_channel/channel_id/tier`, drop `channel` from the active list, and before the first phase
   `store.add_raw("ChannelContextReused", {"channel_id": cid, "source_run_id": rid}, tier,
   {"channel_id": cid}, observed_at=ctx.clock.for_payload(payload))` (no access hash). The driver needs
   the context back, so refactor the body into `collect_channel_with_context(...) ->
   tuple[list[CollectResult], ChannelContext | None]` (context = None when `channel` did not complete)
   and keep `collect_channel` as a thin wrapper with its exact signature. Cache per `channel_id` in the
   driver only (never persisted).
6. **Per-row outcomes**: `MediaCollector(outcomes: dict[str, str] | None = None)` — a caller-owned dict
   the collector fills for every row it considers (`downloaded`, `duplicate`, `too_large`,
   `size_mismatch`, `unavailable`, `skipped`; `skipped_kind` → `skipped`). Deviation from the spec's
   `CollectResult.outcomes`: a `DiskFloorStop`/`HardStop` discards the result, and §3.4 needs outcomes on
   every stop; a live dict survives all of them without plumbing exceptions. Do not add both.
7. **Stop kinds**: `collect_channel` maps `DiskFloorStop` to `stopped="phase_stop"` and the type is
   lost. Add `CollectResult.stop_exc: BaseException | None = None` set on the `PhaseStop`/`HardStop`
   paths (`recipes.py:174-196`); check `git grep "CollectResult(" tests` for equality assertions first.
8. **Stop policy** in the driver (`src/paperboy/fetch_media.py`, new): `channel` phase `skip`/
   `phase_stop` → that segment and every later segment of that channel `not_attempted`, continue with
   other channels, WARNING once. `media` phase `phase_stop` of ANY kind (floor, flood over the ceiling,
   sink error, repeated failures) → end the command: `Budget._pace` would sleep the persisted cooldown
   on the very next call (`budget.py:213-215`), and sink/disk errors are not channel-specific. `hard_stop`
   anywhere → end. Unreached rows `not_attempted`; exit 1; the report is always written (`try/finally`).
9. **Budget & pacing**: one `build_gateway` (one `Budget`) for the whole command; doctor preflight
   runs through it (`cli.py:338-345`). Nothing new: resolveUsername 10 s effective, per-file 2 s.
   Add `--max-rpc`, `--pacing-factor`, `--max-flood-sleep` to `fetch-media` (the smoke protocol needs
   `--max-rpc`); fix `pacing.md:19`'s parenthetical.
10. **`--dry-run`**: parse → classify → segment → print two tables (outcome | rows | declared GB;
    segment # | priority | channel `<id>` | rows | declared GB) → exit 0. Verified offline by the code
    path: no `build_secrets`, no `build_gateway`, no doctor (test asserts both via monkeypatch). Prints
    channel ids only (logs/consoles reference targets by id). No report file on a dry run.
11. **Zero pending rows** (e.g. the DoD re-run): print tables, write the report, exit 0 — WITHOUT
    building a gateway (test: `build_gateway` not called). Same path as dry-run plus the report.
12. **Report** (`--report`, default `<profile_dir>/fetch-media-<YYYYmmddTHHMMSSZ>.csv`): exactly
    `line_no,uri,outcome,sha256,key` for every parsed row, in list order (`uri` normalised to `tg:msg:`).
    `sha256`/`key` filled for `downloaded`/`duplicate`/`already_stored` from `media.message_uri` else the
    latest `custody_log` row for the uri; blank otherwise.
13. **Exit code**: 0 iff no row is `not_attempted`; `unresolvable`/`no_media`/… are final outcomes.
14. **Replay of a marker run** (`reproject.py`): `ReplaySource.context_markers(run)` → the run's
    `channelcontextreused` entries (channel_id, source_run_id, tier, observed_at, payload_json).
    For a run with no resolve records but markers: find the source run's `ResolveRecord` with that
    `channel_id` (`resolve_catalogue()`), read `access_hash` via `pick_channel(payload["chats"],
    channel_id)` from that `ResolvedPeer` (new `ReplaySource.resolved_channel(src_run, channel_id)`),
    `clock.serve_json(observed_at, payload_json)` so the re-written marker keeps its stamp, then
    `collect_channel(..., phases=["media"], channel_context=ChannelContext(...), run_id=run.run_id)`.
    `detect_phases` returns `["media"]` for a marker run. `target_filter`: decide by
    `target_filter.replays(channel_id)`. Unknown `source_run_id` → `ReprojectSourceError`.
    A first (channel+media) segment run replays as today (`channel,history,media`; history is empty).
15. **Doctor**: as `collect` — once, unless `--unsafe`; a block exits 1 before any segment.
16. **Logging**: INFO per segment start/end (`fetch-media: segment i/n priority=… channel=<id> rows=…`
    and the counts), one WARNING per unresolvable channel, final INFO summary + report path. Ids only.

## 3. Tasks (each: failing test → code → green → `ruff`/`pyright` → commit)

1. **Parser.** `tests/test_media_list.py`: `test_csv_with_extra_columns_and_priority` (rows keep
   line_no/priority, extra columns ignored); `test_plain_lines_accept_all_three_forms_and_comments`;
   `test_malformed_rows_fail_listing_every_line` (`MediaListError` with `[3, 7]`, nothing returned);
   `test_duplicates_keep_first_and_flag_later` (`duplicate_row`). Code: `media_list.py` (`ListRow`,
   `MediaListError`, `parse_media_list`). Commit: `feat(media-list): parse CSV / plain-line media lists`.
2. **Classification + segments.** Same file, fixture store seeded with `upsert_message`/`upsert_channel`
   (see `tests/test_collector_media.py:52-56`): `test_classify_covers_every_offline_outcome` (one row per
   outcome incl. `deleted`, username row → resolved, key stored in ANOTHER channel → `already_stored`);
   `test_segments_group_by_priority_then_channel_in_first_appearance_order`. Code: `classify_rows`,
   `plan_segments`, public `content_key`/`recorded_size`. Commit: `feat(media-list): offline
   classification and segment plan`.
3. **Recipe context.** `tests/test_recipe.py`: `test_channel_context_skips_channel_phase_and_writes_
   marker` (collectors=[stub channel that raises if called, stub media]; one `ChannelContextReused` raw
   with the run's `run_id`, payload without `access_hash`); `test_collect_channel_with_context_returns_
   established_context`; `test_stop_exc_carries_the_exception_type` (stub raising `DiskFloorStop`).
   Code: `base.py` `ChannelContext`, `CollectResult.stop_exc`; `recipes.py` refactor. Commit:
   `feat(recipes): pre-resolved channel context with a replay marker (#68)`.
4. **Collector outcomes.** `tests/test_collector_media.py`: `test_outcomes_dict_records_every_row`
   (downloaded, duplicate, too_large, unavailable, skipped); `test_outcomes_survive_disk_floor_stop`
   (monkeypatch `_free_bytes` as in `test_collector_media_streaming.py:169-190`; rows before the stop
   present). Code: `MediaCollector(outcomes=)`. Commit: `feat(media): per-row outcome recording`.
5. **Driver.** `tests/test_fetch_media.py` (FakeGateway; extend it with `resolve_by_target: {username:
   dict}` falling back to `resolve`, mirroring `full_channel_by_id`; two channels with disjoint msg ids,
   two priorities, messages seeded directly): `test_end_to_end_two_channels_two_tiers` (files on disk,
   report rows per input row, segment order P1(ch A), P1(ch B), P2(ch A), exactly 2 `resolve` calls,
   second A-segment wrote a marker); `test_second_run_is_already_stored_with_no_gateway`;
   `test_hard_stop_marks_rest_not_attempted_and_exit_1` (media fixture value = `FakePeerFlood` → via a
   stub collector raising `HardStop`, or `max_rpc_per_run` tiny with a real Budget-less fake: use the stub);
   `test_disk_floor_ends_the_command`; `test_channel_phase_skip_continues_with_other_channels`;
   `test_report_written_on_stop`. Code: `fetch_media.py` (`FetchSummary`, `async fetch_media(gateway,
   store, settings, rows, log, *, profile, report_path) -> FetchSummary`, `write_report`). Commit:
   `feat(fetch-media): ordered cross-channel fetch driver with resume and report`.
6. **CLI.** `tests/test_cli_fetch_media.py`: `test_dry_run_builds_no_gateway_and_no_secrets` (monkeypatch
   both to raise; tables printed; exit 0); `test_malformed_list_exits_1_listing_lines`;
   `test_live_run_exit_codes_and_default_report_path`; `test_help_lists_fetch_media` (extend
   `tests/test_cli.py:39`). Code: `cli.py` `fetch_media` command (flags in spec §5 + `--max-rpc`,
   `--pacing-factor`, `--max-flood-sleep`). Commit: `feat(cli): paperboy fetch-media LIST`.
7. **Replay.** `tests/test_reproject_fetch_media.py`: build a store with run 1 = `channel,history`
   (FakeGateway) and run 2 = `fetch_media` over two segments of the same channel (second writes the
   marker); `test_reproject_replays_marker_runs_to_identical_media_and_custody` (`assert_round_trip`
   pattern, `tests/test_reproject.py:380-395`, plus the output has the marker raw under run 2's id);
   `test_marker_with_unknown_source_run_is_a_source_error`; `test_detect_phases_marker_run_is_media_only`.
   Code: `replay.py` `context_markers`/`resolved_channel`, `reproject.py` branch. Commit:
   `feat(reproject): replay media-only fetch segments from their context marker`.
8. **Docs** (§4) — commit `docs(fetch-media): feature doc, README, CLAUDE.md, how-it-works, ADR-0005`.
9. Full gates, DoD smokes (§5), paste transcripts into the feature doc; final commit `docs(fetch-media):
   DoD smoke transcripts`.

## 4. Docs (part of DoD; reviewers block on a missing one)

- New `docs/features/fetch-media.md`: purpose, inputs (both list formats, flags), classification
  outcomes table, segment/resolve-once behaviour and the marker, stop policy (§2.8), report format,
  exit codes, known limitations (linked-group rows, within-segment id order, ADR-0005 residual for a
  first segment that downloads nothing new), DoD transcripts (redacted).
- `README.md`: command-reference row for `fetch-media`; `Where your data lives` (report file);
  Documentation list entry. No new env var. `CLAUDE.md`: Commands paragraph + the "In progress on
  dev/media-storage" status line (#68 merged). `docs/how-it-works.md` §6: rewrite in plain language
  (list → offline classification → segments → one resolve per channel → marker → reproject replays it).
  `docs/features/pacing.md:19` parenthetical. `docs/adr/0005-run-structure.md` Consequences: the
  `ChannelContextReused` marker (closes the #36 residual for media-only segment runs). `data-model.md`:
  schema unchanged — say "no migration" in the feature doc.

## 5. Definition of done — smokes (paste output verbatim, redacted; keep unredacted files in `<scratch>/68/`)

Setup once: `sqlite3 <real>/default/paperboy.sqlite ".backup '<scratch>/68/default/paperboy.sqlite'"`;
`export PAPERBOY_DATA_DIR=<scratch>/68`. Never write to the real data dir.

OFFLINE (no network by construction; also assert it in tests):
1. `uv run pytest -q --basetemp=…`, `uv run ruff check`, `uv run pyright` — final lines verbatim.
2. `paperboy fetch-media <synthetic list> --dry-run` (commit it under `tests/fixtures/media_list/`) → both tables.
3. `paperboy fetch-media <list> --dry-run --profile default` over the operator's FULL list on the `.backup`
   copy: paste the outcome table and the segment-plan table with channel ids replaced by `<id>`.
   Expect: 1,719 rows; `unresolvable` 40; `already_stored` ≈ 118 (spec said ~99 — explain the content-key
   dedup in the doc); `pending` ≈ 1,560; `not_in_store`/`no_media`/`deleted`/`duplicate_row` 0;
   ≤ 4 × 10 segments. Confirm from the log that no `pacing:` line (gateway construction) appears.

LIVE (overview protocol; ≤ 5 invocations, plan uses 2; ≤ 3 files, ≤ 3 GB; channels already in the store):
- Row selection, done offline and never named in committed text: from `<list>` take rows with
  `already_downloaded=0`, `deleted=False`, whose channel column is a username (the one numeric channel
  value is the excluded linked group — never it), `kind` = 1 photo + 2 videos with `size_mb` ≤ 50 each,
  from 2 different channels; copy the header + those 3 rows to `<scratch>/68/list-smoke.csv`. Run
  `--dry-run` on it first: expect 3 `pending`, 2 channels, ≥ 2 segments, ≈ 0.1 GB declared.
- Before EACH live invocation: `test -e <scratch>/STOP-LIVE` (absent), counter `< 5` then append, VPN
  check (both DC addresses via `utun*`/`ipsec*`/`ppp*`; paste it).
- Invocation 1: `PAPERBOY_REQUIRE_PROXY=false paperboy fetch-media <scratch>/68/list-smoke.csv --profile
  default --max-rpc 60 --max-flood-sleep 60 --report <scratch>/68/report-1.csv`. Expected RPCs ≈ doctor
  (≈ 6) + 2 × 3 channel + 3 × 2 files ≈ 18. Paste: final table (3 `downloaded`), the report with URIs as
  `tg:msg:<id>/<id>`, `SELECT count(*) FROM raw_records WHERE kind='ChannelContextReused'` (0 or 1,
  depending on how the 3 rows segment — say which), `ls <scratch>/68/default/media/.incoming` (empty),
  `shasum -a 256` of one file equals its `media.sha256`.
- Invocation 2: the same command with `--report <scratch>/68/report-2.csv`: 3 `already_stored`, no
  `pacing:` log line (no gateway built), exit 0.
- Any FLOOD_WAIT > 60 s, PEER_FLOOD, FROZEN_METHOD_INVALID, auth error or failed VPN check → `touch
  <scratch>/STOP-LIVE`, write the reason, no retry, mark STOPPED; a flag/cap/doctor block → PENDING.
  Neither is a code failure; gates pass on offline evidence.

## 6. Edge cases and failure modes

- Empty/short CSV row → malformed (listed). BOM → `utf-8-sig`. CRLF → `csv` handles; plain mode
  `rstrip("\r\n")`.
- A renamed channel: link-form rows → `not_in_store`; `tg:msg:` rows → `channel` phase gets
  `UsernameNotOccupied` → `skip` → that channel's rows `not_attempted` (§2.8), exit 1.
- The resolved id differs from the segment's `channel_id` (`_resolved_channel_id`): treat as a
  channel-phase stop for that channel — never fetch under the wrong id.
- Content key stored only under another channel → `already_stored` offline (global index), although the
  collector's own index is per channel (`media.py:498-503`). `too_large`/`size_mismatch`/`unavailable`
  are final outcomes (exit 0).
- SIGKILL mid-segment: no report; media/custody rows persist, `.incoming` parts are swept next run
  (`media.py:88-103`); a re-run classifies finished rows `already_stored`.
- Exactly one marker per run (each segment is its own `collect_channel` → own `run_id`); assert it.
  Legacy NULL-`run_id` rows never hold a marker, and a marker is not an opening kind (`replay.py:429`).
- Report path unwritable → fail BEFORE any segment (open for writing up front, rewrite at the end).

## Orchestrator decisions (2026-09-29, binding)

1. **Stop policy:** any media-phase `PhaseStop` (floor, flood over the ceiling, sink error, repeated failures) ends the command after recording outcomes, not just `DiskFloorStop`. Document why (the persisted cooldown would defeat `--max-flood-sleep` on the next channel). `HardStop` ends it as before.
2. **Classification order:** keep the spec's order (`unresolvable` before `already_stored`).
3. **Outcomes:** a caller-owned `MediaCollector(outcomes=dict)`, as planned.
4. **`deleted`:** accepted as a sixth offline outcome.
5. **Live-call cap:** the no-network re-run counts against the 5-call cap (conservative).

## Implementation notes (added during the run)

Deviations found while implementing, all recorded in `docs/features/fetch-media.md`:

- Replay of a scoped media phase needs the ids: the recipe now writes a `MediaSelection` raw whenever `media_msgs` is set and `media` runs, and reproject walks only those ids (the plan's parity test failed with `duplicates=2` without it).
- `detect_phases` no longer infers `history` for a run with no message and no `getChannelDifference` raw; replaying it wrote a synthetic difference raw the source never had.
- The driver wraps the `channel` collector in a guard so a handle that resolves to a different channel id is skipped before `media` can fetch the segment ids from the wrong channel.
