# Plan: `fetch-from-list` — fetch the listed posts, then their media (#91)

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-10-06-fetch-from-list.md` in your first commit.)

Branch `feat/fetch-from-list`, cut from `origin/dev/gcs-pull` (7e6de9d). PR target `dev/gcs-pull`.
Spec (authoritative): `docs/superpowers/specs/2026-10-06-fetch-from-list-design.md`. Also binding: the
overview spec's live smoke protocol and docs DoD, the run rules you were handed. Base code wins over
this prose; the spec wins over both for behaviour. TDD throughout: failing test, then code, then commit.

## 1. Ambiguities and resolutions (file:line on the base branch)

1. **Where `posts` lives.** A new collector `src/paperboy/collectors/posts.py::PostsCollector`
   (`name = "posts"`), run by the segment recipe between `channel` and `media`
   (`fetch_media.py:146-150` passes an explicit collector list; add it there). Not in
   `recipes._default_collectors` (`recipes.py:53`): `collect` never runs it. It is in reproject's
   explicit list (`reproject.py:553-559`, after `ChannelCollector()` — list order is execution
   order, `recipes.py:180`). The ids it walks come from a new `Settings.post_msgs: list[int] | None`
   (`config.py:167` pattern), set by the driver; `media_msgs` keeps its meaning (the ids the media
   phase walks) so `MediaSelection` (`recipes.py:184-203`) is unchanged in shape and timing.
2. **Replay detection.** Each returned object is appended with context
   `{"method": "channels.getMessages", "channel_id": cid}` (`observe_message`, below). Replay:
   `ReplaySource.has_history_evidence` (`replay.py:669-678`) must ignore message-kind entries whose
   `ctx.get("method") == "channels.getMessages"` — otherwise a segment replays `history`
   (`reproject.py:263`). Add `ReplaySource.fetched_post_ids(run) -> list[int]`: payload ids of
   message-kind entries with that method, sorted (via `RunIndex.lookup(_MESSAGE_KINDS, ("method",),
   ("channels.getMessages",))`, `replay.py:265`). `detect_phases` (`reproject.py:250-292`): marker
   runs (`:258-261`) return `["posts"]` if `fetched_post_ids` is non-empty, plus `"media"` iff
   `source.media_selection(run)` is not None (a pre-#91 marker run has a selection and no posts
   evidence → `["media"]`, as today); non-marker runs append `"posts"` right after `"channel"` on the
   same evidence. `reproject()` sets `replay_settings.post_msgs = fetched_post_ids(run) or None`
   next to `media_msgs` (`reproject.py:393-400`). `RawReplayGateway.get_messages` (`replay.py:923`)
   already serves the latest record per id and the `ReplayUnknownMessage` placeholder (D4.1); the
   collector skips any kind outside `message|messageservice|messageempty`.
3. **Shared projection.** Hoist `HistoryCollector._observe_message` (`history.py:154-209`) to a
   module function `observe_message(ctx, channel_id, m, counts, *, context: dict | None = None)`;
   `context` defaults to `{"channel_id": channel_id}`. The method becomes a one-line delegate, so
   `discussion.py:167` (`HistoryCollector().collect(...)`) and every history test are untouched.
   Proof: `git diff --stat origin/dev/gcs-pull -- tests/test_collector_history.py
   tests/test_history_catchup.py tests/test_collector_discussion.py` is empty and they pass.
4. **`MessageEmpty` → tombstone.** `observe_message` already routes it to
   `mark_deleted(..., "empty", ...)` (`history.py:167-175`, `store/messages.py:140-166`: a tombstone
   row always, `deleted_at` set when the message row exists, never a blank row). Posts outcome
   `deleted_upstream`. `MediaCollector.collect` excludes `deleted_at IS NOT NULL`
   (`media.py:281`), so no media attempt follows — no special case needed.
5. **Media eligibility after posts.** `MediaCollector` queries `messages` at the start of its own
   phase (`media.py:306-310`), after `posts` has upserted the rows, so freshly fetched posts are
   eligible in the same run. `media_msgs` = the segment's ids whose file this run's store does NOT
   already hold (`Segment.media_ids`), because `_record_custody` (`media.py:668`) is not idempotent:
   walking an already-held row would add a `duplicate` custody row per re-run. `post_msgs` = every
   id in the segment (`Segment.msg_ids`), so already-stored posts are re-fetched (spec §2.3).
6. **Classification (`media_list.py:263-324`).** Offline outcomes shrink to `duplicate_row`,
   `excluded`, `unknown_channel` (a `t.me/<username>/<id>` row whose username the store never saw:
   there is no id to address, and list rows are never resolved by handle — keep that rule), and
   `pending`. `not_in_store`, `deleted`, `no_media`, `already_stored` stop being offline outcomes;
   `ClassifiedRow` gains `in_store: bool`, `media_held: bool` (this run's store holds the file —
   the existing `_held_by_store` logic, `:252-260`) and keeps `stored`. Deleted-in-store rows are
   pending too (one rule: every addressable row is fetched).
7. **Final outcomes (`_run_segments`).** `PostsCollector(outcomes=dict)` fills uri →
   `fetched | deleted_upstream` (same caller-owned-dict pattern as `MediaCollector`,
   `media.py:217-231`). Per row, precedence: media outcome if the media collector noted one
   (`downloaded|duplicate|too_large|size_mismatch|unavailable|skipped`) > `already_stored`
   (`media_held`) > `deleted_upstream` > `post_only` (`--no-media`) > `no_media` (fetched, and
   `messages.media_kind` is not downloadable or NULL) > `not_attempted`. `no_access` as today.
   `RowResult.post: str = "skipped"` becomes `fetched`/`deleted_upstream` from the posts dict.
8. **Report.** `REPORT_COLUMNS = ("line_no", "uri", "outcome", "post", "sha256", "key", "reason")`
   (`fetch_media.py:60`); default name `fetch-from-list-<stamp>.csv` (`cli.py:574`).
9. **Dry run (`cli.py:399-444`).** Outcome table over the four offline outcomes; per-channel table
   columns `pending`, `in_store`, `not_yet_collected`, `media_stored`, `excluded`, `duplicate_row`,
   `total`; segment plan unchanged plus a `posts calls` column (`ceil(len(msg_ids)/100)`). Still no
   gateway/keychain (`test_cli_fetch_media.py:36`); a bucket store still HEADs (`:214`).
10. **`--no-media`.** Driver phases `["channel", "posts"]` and collectors without
    `MediaCollector`; no `MediaSelection` is written (recipe condition is media-only), so replay
    detects `posts` alone (item 2). `--media-max-mb`/`--media-min-free-gb` are accepted and unused.
11. **Stop policy.** Unchanged shape (`fetch_media.py:163-186`): a `posts` `phase_stop` ends the
    command (`"posts phase_stop (<Exc>)"`); a `posts` `skip` (e.g. `ChannelPrivateError` from
    `getMessages`, a SKIP disposition in `errors.py`) marks the segment and the channel's later
    segments `no_access`, like a channel skip. Batches of ≤ 100 (`_GET_MESSAGES_BATCH = 100`, not
    history's 200); `PhaseStop` mid-way re-raises with the counts so far (`history.py:290-296`).
12. **Rename blast radius.** `src/paperboy/fetch_media.py` → `fetch_from_list.py` (function
    `fetch_from_list`, `FetchSummary`, `RowResult`, `initial_results`, `write_report`); CLI command
    `fetch-from-list` (`cli.py:479`), table titles (`:406,424,436,448`), log prefixes; tests
    `test_fetch_media.py` → `test_fetch_from_list.py`, `test_cli_fetch_media.py` →
    `test_cli_fetch_from_list.py`, `test_reproject_fetch_media.py` → `test_reproject_fetch_from_list.py`
    (`git mv`), `test_cli.py:42`; docs `docs/features/fetch-media.md` → `fetch-from-list.md`
    (`git mv`, then rewrite) and every `fetch-media` mention in README, CLAUDE.md,
    `docs/how-it-works.md` §6, `docs/features/{reproject,media-stores,collect-channel,pacing}.md`,
    `docs/data-model.md:66`, `docs/adr/0005-run-structure.md:134`, `docs/adr/0008-media-stores.md:93,102`,
    code comments in `collectors/base.py:63`, `media_store.py:3`, `replay.py:672,701`,
    `reproject.py:259,418,423,546`. `media_list.py` keeps its name and docstring says so.
    `progress.phase_status` (`progress.py:38`) gets a `posts` line (messages count).
13. **Existing fixtures.** `FakeGateway.get_messages` answers `MessageEmpty` for any id missing
    from `fx["get_messages"]` (`gateway.py:309-313`). Every existing fetch test therefore needs a
    `get_messages` table or all posts tombstone and nothing downloads: give `_gateway()` in the
    driver tests a default table built from the seeded messages (`seed_msg` payloads,
    `test_media_list.py:131`), and `build_source` in the reproject tests one from `_history(...)`.
    `test_detect_phases_marker_run_is_media_only` becomes `["channel","posts","media"]` /
    `["posts","media"]`; `test_no_fetch_media_only_collector_in_replay` and
    `test_live_collector_list_is_the_standard_one` list `PostsCollector` as standard.

## 2. Ordered tasks (each: failing test → code → commit; suite, ruff, pyright green before commit)

T0. Branch + this plan. `docs: fetch-from-list plan (#91)`.
T1. **Shared projection.** `tests/test_collector_history.py` stays untouched; new
   `tests/test_collector_posts.py::test_observe_message_context_overrides_channel_only` asserts the
   raw row's `context_json` is `{"channel_id":5,"method":"channels.getMessages"}` and a message
   row exists. Code: hoist `observe_message` in `history.py`. `refactor(history): module-level
   observe_message shared with posts (#91)`.
T2. **PostsCollector.** Tests in `tests/test_collector_posts.py`: (a) `test_batches_of_100`: 250
   ids → `gw.calls.count("get_messages") == 3`; (b) `test_new_post_projected_with_peer_and_edge`:
   message with `from_id`/`fwd_from` → messages, peers, edges rows, outcome `fetched`;
   (c) `test_refetch_edit_adds_revision_and_metric`: seed via `seed_msg`, fixture returns edited
   text + `views` → `message_revisions` count 2, `message_metrics` 1, counts `revisions == 1`;
   (d) `test_message_empty_is_tombstone_deleted_upstream`: tombstone evidence `empty`, outcome
   `deleted_upstream`, no blank row for an unseen id; (e) `test_replay_unknown_placeholder_projects_nothing`;
   (f) `test_phase_stop_without_context`; (g) `test_phase_stop_mid_batch_carries_counts` (fixture
   raising `PhaseStop` on the second call: `FakeGateway` has no exception support for
   `get_messages` — add it, mirroring `get_channel_difference`'s list convention `gateway.py:321`).
   Code: `collectors/posts.py`, `Settings.post_msgs`, `progress.py`. `feat(posts): PostsCollector
   fetches listed ids by channels.getMessages (#91)`.
T3. **Classification + segments.** `tests/test_media_list.py`: rewrite
   `test_classify_covers_every_offline_outcome` for the four outcomes and the `in_store`/
   `media_held` flags; `test_already_stored_is_per_store` asserts `media_held` per store;
   new `test_segment_media_ids_exclude_held_rows`; `test_unknown_username_is_unknown_channel`.
   Code: `media_list.py` (`OFFLINE_OUTCOMES`, `ClassifiedRow`, `classify_rows`, `Segment.media_ids`).
   `feat(media-list): every addressable row is pending; in_store/media_held flags (#91)`.
T4. **Rename + driver.** `git mv` module and tests; update imports. `tests/test_fetch_from_list.py`:
   keep the existing 14 tests green with the `get_messages` default (item 13); add
   `test_post_absent_from_store_is_fetched_projected_and_downloaded` (list row for an unseeded id,
   fixture has the message + bytes → report `outcome=downloaded, post=fetched`, `messages` row,
   peer row); `test_already_stored_row_is_refetched_but_not_redownloaded` (second run: outcome
   `already_stored`, `post=fetched`, `get_messages` called, `download_media_calls == []`, custody
   count unchanged); `test_deleted_upstream_has_no_media_attempt`; `test_no_media_flag_gives_post_only`
   (no `MediaSelection`, no `media` run_event); `test_no_media_outcome_for_text_post`;
   `test_posts_phase_stop_ends_the_command`; `test_posts_skip_marks_channel_no_access`;
   `test_report_has_post_column`. Code: `fetch_from_list.py` (`_run_segments` builds
   `post_msgs`/`media_msgs`, phases per `--no-media`, outcome precedence, `post` column).
   `feat(fetch-from-list): rename fetch-media; posts step before media (#91)`.
T5. **CLI.** `tests/test_cli_fetch_from_list.py`: `test_help_has_fetch_from_list_and_no_fetch_media`,
   default report name `fetch-from-list-`, `test_dry_run_counts_not_yet_collected_per_channel`
   (zero gateway builds, `not_yet_collected 1` for an unseeded id), `--no-media` passes through;
   `test_cli.py:42` updated. Code: `cli.py` (command, `--no-media`, tables, titles).
   `feat(cli): fetch-from-list command, --no-media, dry-run post counts (#91)`.
T6. **Replay.** `tests/test_replay_gateway.py` (or `test_replay_index.py`):
   `test_history_evidence_ignores_getmessages_receipts`, `test_fetched_post_ids`. `tests/test_reproject_fetch_from_list.py`:
   update `test_detect_phases_marker_run_is_media_only` → `..._is_posts_and_media`; new
   `test_marker_run_without_posts_evidence_is_media_only` (hand-built legacy source: add a
   `ChannelContextReused` + `MediaSelection` run with no `getMessages` records);
   `test_no_media_run_detects_posts_only`; `test_reproject_replays_posts_runs_to_identical_messages_revisions_and_tombstones`
   (`assert_round_trip` from `tests/test_reproject.py` over the extended source: an edited post and
   a `MessageEmpty` one). Code: `replay.py` (`has_history_evidence`, `fetched_post_ids`),
   `reproject.py` (`detect_phases`, `post_msgs`, collector list). `feat(reproject): detect and
   replay the posts phase (#91)`.
T7. **Docs** (§3) and DoD report. `docs(fetch-from-list): feature doc, README, CLAUDE.md,
   how-it-works, reproject, data-model (#91)`; then `docs(fetch-from-list): DoD gates and smoke
   transcripts (#91)`.

## 3. Docs (docs DoD — every one listed under "## Docs updated" in the report)

- `docs/features/fetch-from-list.md` (renamed): purpose, synopsis with `--no-media`, input,
  outcomes table (offline: 4; live: `fetched`/`deleted_upstream` post column; media outcomes +
  `already_stored`, `no_media`, `post_only`, `no_access`, `not_attempted`), segment =
  channel → posts → media, RPC cost ⌈ids/100⌉, stop policy incl. posts, report columns, dry-run
  tables, replay (posts evidence rule, `post_msgs` from receipts, no new raw kinds), deviations,
  known limitations (`unknown_channel`; media walks only non-held rows), DoD gates + transcripts.
- README (commands row, documentation list, report filename in the layout tree); CLAUDE.md
  (commands line; status paragraph: #91 on `feat/fetch-from-list` → `dev/gcs-pull`);
  `docs/how-it-works.md` §6 (steps 2-6 reworded: posts are fetched, then media; `MediaSelection`
  still only before media); `docs/features/reproject.md` "Replaying fetch-media runs" → rename +
  phase rule for `posts`; `docs/features/media-stores.md`, `collect-channel.md`, `pacing.md`,
  `docs/data-model.md:66` (rename; posts receipts' context), ADR-0005/0008 mentions (rename only —
  no new ADR, spec §"ADR"). PR body: the code-atlas note (collector list gained `posts`).

## 4. DoD smoke (spec §4), in order

Offline first: full suite (`--basetemp` per rules), ruff, pyright; the parity test of T6; a
`--dry-run` of the smoke list against the scratch store (no Telegram, no keychain).

**Scratch store.** `.backup` the real `default/paperboy.sqlite` into `<scratch>/default/`
(overview protocol §1; never `cp`). Do not symlink media: the point is a real download.

**Build the 3-row list** (never the operator's list; never the two private ids in the rules
file — filter them out of every query with `channel_id NOT IN (...)`; never write them into the
list, the plan, commits or the report). From the scratch sqlite, read-only:
1. Channels: `SELECT id FROM channels WHERE id NOT IN (<private ids>)`; pick two, A and B, that
   have a saved key (`peers` row with `access_hash`, Step A route 1).
2. Row 1 (in store, no media): from A, `SELECT msg_id FROM messages WHERE channel_id=A AND
   media_kind IS NULL AND deleted_at IS NULL AND is_service=0 ORDER BY msg_id DESC LIMIT 1`.
3. Row 2 (in store, small photo, never downloaded): from A, a `MessageMediaPhoto` row whose
   `media_json` largest `photo.sizes[].size` < 200000 and whose uri has no `media` and no
   `custody_log` row (so the local run downloads it; a custody row would classify it held).
4. Row 3 (not in store): from B, `max(msg_id)+1` (B's newest stored post should be days old so
   the id exists upstream). `deleted_upstream` or `no_media` is an acceptable observed result;
   report it honestly, do not re-pick to get a cleaner run.
Write it as `uri` lines `tg:msg:<id>/<msg_id>` under `<scratch>`; redact as `@<channel>`/`<id>`.

**Live (≤ 4 invocations; counter, STOP flag and VPN check before each; `PAPERBOY_REQUIRE_PROXY=false
--profile default --max-rpc 60 --max-flood-sleep 60 --media-max-mb 1`; ≤ 3 files, photos < 200 KB):**
1. Local store: `fetch-from-list <list> --report <scratch>/r1.csv`. Expect: `get_messages`
   ×2 (one per channel), rows `no_media`/`downloaded`/(`downloaded|no_media|deleted_upstream`),
   `post=fetched` ×3 (or `deleted_upstream`), new `messages` row for row 3, `message_metrics`
   rows for all fetched, `run_events` phases `channel,posts,media` per segment, sha of the file
   under `<scratch>/default/media/` equals `media.sha256`.
2. Bucket store, 2-row list (rows 1-2 only, so ≤ 3 files overall): `--media-store
   gs://<bucket>/<prefix>` with `<prefix>` = `paperboy/smoke-<YYYYMMDD>-91` (fresh; never an
   existing prefix; ADC only). Expect: row 2 `downloaded` again (per-store), custody row names the
   bucket, object exists (`gcloud storage ls`, read-only), `MediaStore` marker in the run.
3. Re-run 1 (local, 3-row list): posts re-fetched (`post=fetched`, `get_messages` again, new
   `message_metrics` rows, no new revisions unless edited), media `already_stored` for row 2,
   `download_media` not called, custody count unchanged.
4. Spare — only if 1-3 need a documented repeat; otherwise unused.
Then offline: `reproject --profile default` with `PAPERBOY_DATA_DIR=<scratch>` and the bucket in
`PAPERBOY_MEDIA_STORE_BUCKETS` (read-only GETs): source-vs-output table over `REPROJECT_TABLES`;
`run_events`/phases show `posts` replayed; messages/revisions/tombstones/metrics counts equal.
Transcripts verbatim and redacted in the report; unredacted copies stay under `<scratch>`.

## 5. Edge cases and failure modes

- A list id above the channel's newest post: `MessageEmpty` → tombstone `empty` + `deleted_upstream`
  (that is what Telegram says; document it as "deleted or never existed").
- A post edited to add media after a `no_media` run: the re-fetch revises it and media follows.
- `getMessages` on a channel the account can no longer read → SKIP disposition → `no_access`.
- `ReplayUnknownMessage` on replay projects nothing (D4.1); never a synthetic tombstone.
- Posts stop after batch 1 of 3: fetched rows keep `post=fetched`, unreached rows
  `not_attempted`, report written, exit 1, re-run resumes.
- Hard stop in posts: `hard_stop` ends the command as today.
- A row whose message is deleted upstream AND whose file is held: `outcome=already_stored`,
  `post=deleted_upstream` (both facts in the report).
- Dry run never builds a gateway; a bucket dry run still HEADs.
- Replay of a pre-#91 segment run is unchanged (`["media"]`); a #91 `--no-media` run replays
  `["posts"]` with no `MediaSelection`, no media run_event.
- Linked-group rows and `--exclude-target` behave as today (offline, before anything).

## Orchestrator decisions (2026-10-06, binding)

1. **Handle rows for channels the store has never seen:** resolve them live by handle, through #84's handle route (`ChannelAccess via: handle`, recorded as a receipt), instead of a dead-end outcome. The operator wants lookup by id OR handle. `--dry-run` (offline) reports them as `needs_resolve`. Test both, and note it in the spec's §2.3 in your docs commit.
2. **Tombstoned-in-store rows** are re-fetched like every other addressable row (one rule). Telegram's answer decides between `deleted_upstream` and a live post.
3. **Live smoke:** the bucket run uses the 2-row sub-list so that ≤ 3 files are retained across the local and bucket runs. `--media-max-mb 1` on every live call.
4. **Dry runs** make no Telegram calls and don't count against the live-call cap. A bucket-store dry run makes HEAD requests only.
