# Delta plan: `fetch-media` by channel id (#68, spec §9 amendment)
(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-10-01-media-list-fetch-by-id.md`.)

Resume `feat/fetch-media-list` (ac20097) per the run rules. Authoritative: spec
`docs/superpowers/specs/2026-09-28-media-list-fetch-design.md` **§9**, the #84 spec
`2026-09-30-collect-by-id-design.md`, #68's root-cause comment; background: the previous plan
`2026-09-29-media-list-fetch.md`. `file:line` cites ac20097 unless marked `(dev)`. The reviewers' rule:
**every input that shapes a projection is in `raw_records`, and live and replay run the same
collectors**. In anything committed: `@<channel>`, `<id>`, `<list>`, `<scratch>/68b`, `<data-dir>`.

## 1. Merge `origin/dev/media-storage` (d5a0883) into the branch — first commit

The branch base (6b9c6ee) already contains #75 and #70; the only new code on dev is **#84** (+ the two
spec amendments). `git merge-tree` predicts four content conflicts; everything else auto-merges:

| File | Resolution |
|---|---|
| `src/paperboy/gateway.py` (`FakeGateway.resolve`, ~:251) | Take **dev** whole: `resolve` raising a fixture `BaseException`, the `replay` property, `channel_access_receipt`. Delete the branch's `resolve_by_target` (it only served per-handle segments; §3 removes every use). |
| `CLAUDE.md` status paragraph | Keep dev's #84 sentence (now merged, PR #86) and rewrite the #68 sentence for the by-id design (§4). |
| `README.md` command table | Keep **both**: dev's `status`/`export` rows ("handle or channel id") and the `fetch-media` row, reworded (§4). |
| `docs/data-model.md` paperboy-authored kinds | Keep dev's `ChannelAccess` paragraph; append the #68 kinds with the new payload (§4). |

Auto-merged but **semantically stale** (fix in the tasks below, not in the merge commit): `reproject.py`
(`detect_phases` :226-261 and the marker branch :375-434 still key on `ResolvedPeer`), `replay.py`
(`resolved_access_hash` :692 reads `ResolvedPeer` only — an id-started run has none), `recipes.py`
(`MediaSelection` :175-186 has no `channel_id`), `fetch_media.py`, `media_list.py`, `cli.py`.
After the merge: gates run; expect `tests/test_fetch_media.py` to fail on `resolve_by_target` — that is
the next task's failing test, do not patch the fake back. Commit: `merge: dev/media-storage (#84) into
feat/fetch-media-list`.

## 2. Gap analysis against §9 (what exists at ac20097 → what changes)

| §9 | Branch today | Change |
|---|---|---|
| 9.1 id target, no wrapper | `fetch_media.py:82-107` `_ExpectChannel`; `:154-158` `parse_target(f"@{seg.username}")` + `collectors=[_ExpectChannel(..), MediaCollector(..)]`; `Segment.username` `media_list.py:269,293`; `plan_segments(store, ..)` reads `channels` `:280-283` | Target `parse_target(str(seg.channel_id))`; collectors `[ChannelCollector(), MediaCollector(outcomes=..)]`; **delete** `_ExpectChannel`, `Segment.username`, the store lookup (`plan_segments(classified)`). |
| 9.1 reuse recorded | `ChannelContextReused` marker `recipes.py:154-166`, replayed `reproject.py:375-434` | Keep. Replay context's hash must come from the source run's `ChatFull` (§3 T6). |
| 9.2 selection names channel | `recipes.py:175-186` `{"msg_ids"}` written before any phase | `{"channel_id": ctx.channel_id, "msg_ids"}` written **immediately before the `media` collector runs**, only when `ctx.channel_id` is set (§3 T3 explains the replay-clock consequence). |
| 9.3 replay what executed | `detect_phases` `reproject.py:259`: `mediadownload OR media_selection is not None` | `mediadownload OR (selection AND channel established for selection.channel_id)` (§3 T5). |
| 9.4 no `unresolvable` | `media_list.py:45,241-246`; `cli.py:485-490` WARNING; `fetch-media.md:49,60-62,143`; `tests/test_media_list.py:156,165,175` | Delete all. |
| 9.4 `--exclude-target` / `excluded` | none | New flag + offline classification + linked-group follow (§3 T2). |
| 9.4 per-channel dry-run counts | `_print_plan` `cli.py:365-392` (outcome table + segment table) | Add a third table: channel `<id>` × outcome. |
| 9.4 `no_access` | channel `skip` → `dead_channels`, rows stay `not_attempted` `fetch_media.py:179-185` | Rows of that segment **and the channel's later segments** → `no_access`; WARNING once with `str(stop_exc)`; continue. |
| 9.5 no fetch-only collector | `test_handle_resolving_to_another_channel_fetches_nothing` `tests/test_fetch_media.py:202` | Replace with the by-id tests (§3 T4). |

**Code to delete** (grep each at the end; zero hits): `_ExpectChannel`; `Segment.username`;
`resolve_by_target` (gateway + test fixtures); `unresolvable` everywhere (code, tests, docs,
`OFFLINE_OUTCOMES`); the `cli.py:485-490` WARNING loop; `media_selection(run) is not None` as a phase
trigger; `resolved_access_hash`'s `ResolvedPeer` scan (replaced, T6). The `t.me/<username>/<id>` list
form stays (resolved offline via `channels.username`).

## 3. Ordered tasks (each: failing test → smallest change → gates → commit, one trailer only)

**T1 Segments by id, driver without the guard.** Test `tests/test_fetch_media.py`: extend
`seed_channel` (in `tests/test_media_list.py:111`) with `access_hash=None`; when given, also
`upsert_peer` a full (non-`min`) `peers` row (pattern: `tests/test_collector_channel.py:231 _seed_peer`
(dev)). Fixture `_gateway`: drop `resolve_by_target`, set `"resolve": AssertionError("no handle lookup")`.
`test_end_to_end_two_channels_two_tiers`: assert `"resolve" not in gw.calls`, one `full_channel_inputs`
entry per channel (later segments reuse), two `ChannelAccess` raws `via=saved_key`, markers as before. New
`test_stored_handle_now_elsewhere_still_fetches_by_id`: channel 10's `channels.username` points at a
handle whose `resolve` fixture answers channel 6; with a saved key the files land and `resolve` is never
called. New `test_segment_without_any_route_is_no_access_and_the_run_continues`: channel 10 has no
`peers` row and no username → its rows `no_access`, channel 20 `downloaded`, `summary.complete`, exit
path 0, exactly one WARNING naming channel 10 and the route-4 message. New
`test_live_collector_list_is_the_standard_one`: monkeypatch `fetch_media.collect_channel_with_context`
to capture `collectors`; assert `[type(c) for c in collectors] == [ChannelCollector, MediaCollector]`.
Code: `fetch_media.py` (`dead_channels: dict[int, str]`; `_run_segments` marks `no_access`; module
docstring), `media_list.py` (`Segment`, `plan_segments(classified)`), callers in `cli.py`.
Commit: `feat(fetch-media): address each segment by channel id through Step A; drop the handle guard (#68)`.

**T2 `excluded` and `--exclude-target`.** Test `tests/test_media_list.py`:
`test_exclude_target_marks_rows_excluded_offline` (`classify_rows(st, rows, excluded_ids=frozenset({10}))`
→ every channel-10 row `excluded`, including a row that would otherwise be `already_stored` or
`not_in_store`; username-form rows resolve first, then exclude). `test_excluded_channel_ids_follows_the_
linked_group`: seed `edges (tg:channel:10, 'linked_group', tg:channel:77)` (`store.edges.add_edge`; the
`channel` collector writes it, `collectors/channel.py:382` (dev)); `excluded_channel_ids(st, ["@chan_a"])
== {10, 77}`; `["-10010"]`, `["10"]` and `["t.me/c/10"]` give the same; an unknown spec raises
`MediaListError` naming it; a non-channel kind (invite link) raises. `tests/test_cli_fetch_media.py`
`test_dry_run_prints_per_channel_counts_and_excludes`: two channels, `--exclude-target 10` → table has one
row per channel id with outcome columns; `excluded` count right; `build_gateway`/`build_secrets` still
monkeypatched to raise (dry run makes no network call). Code: `store/channels.py` gains
`find_channel_id(store, target)` (move the body of `cli.py:84 _find_channel_id` (dev) there, keep the cli
name as a one-line delegate); `media_list.py`: `excluded_channel_ids(store, specs)` (parse with
`targets.parse_target`, accept `USERNAME`/`PEER_ID`, follow `edges.predicate='linked_group'` via
`ids.parse_uri`), `classify_rows(.., excluded_ids=)` with `excluded` right after `duplicate_row`;
`OFFLINE_OUTCOMES` = `duplicate_row, excluded, not_in_store, deleted, no_media, already_stored, pending`;
`cli.py`: repeatable `--exclude-target` (help: "same forms as `reproject`; a linked group follows its
parent"), unknown → red line, exit 1, before the store is touched further; `_print_plan` third table.
Commit: `feat(fetch-media): --exclude-target marks rows excluded offline; per-channel dry-run counts (#68)`.

**T3 `MediaSelection` names the channel.** Why the write moves: `ReplayClock.begin_batch`
(`clock.py:65` (dev)) is called by every served replay response (`replay.py:718` (dev)) and clears the
stamp registry, so today a paperboy-authored record only keeps its stored `observed_at` if
`for_payload` runs before the first gateway call — which is why both markers are written up front.
`collect --media-msgs @handle` cannot name its channel up front (§9.2 "writes the channel it resolved"),
so: add `ReplayClock.pin_json(observed_at, payload_json)` — a second registry consulted first by
`for_payload` and **not** cleared by `begin_batch` (docstring: paperboy-authored records are not
gateway responses). Tests: `tests/test_clock.py` (find it with `git grep ReplayClock tests`)
`test_pinned_stamp_survives_begin_batch`. `tests/test_recipe.py`
`test_media_selection_names_the_established_channel`: stub channel collector sets `ctx.channel_id=5`,
stub media; one `MediaSelection` raw with `{"channel_id": 5, "msg_ids": [..]}` written **after** the
channel phase's raws (compare rowids) and before media's; `test_media_selection_absent_when_channel_
not_established`: stub channel raises `SkipAndRecord` → no `MediaSelection`, media result `phase_stop`.
Code: `recipes.py` — remove :175-186; inside the loop, before `_run_one` when `collector.name ==
"media"`, `settings.media_msgs is not None` and `ctx.channel_id is not None`, write the selection with
`observed_at=ctx.clock.for_payload(selection)`. `reproject.py:419-421,453`: `clock.pin_json` for the
marker and the selection (replace `serve_json`). `ReplaySource.media_selection` docstring: payload
`{channel_id, msg_ids}`, legacy `{msg_ids}`. Commit: `feat(recipes): MediaSelection records the
established channel; pinned stamps for paperboy-authored records (#68)`.

**T4 Driver parity tests (§9.5).** `tests/test_reproject_fetch_media.py`: `build_source` fetch gateway
without `resolve_by_target` (runs 1-2's `upsert_peer` saved the keys); add a refused channel 30: messages,
**no** `peers` row, a `channels.username` whose `resolve` answers channel 6 (route 3 verification fails →
`granted:false` receipt). Tests:
`test_reproject_matches_live_for_normal_refused_and_zero_download_segments` (round trip; `custody_log`
and `media` sets equal; no custody row for channel 30; the zero-download case = a segment whose only
row is a repost of an already-stored key — `MediaSelection` present, no `MediaDownload`, still equal);
`test_legacy_msg_ids_only_selection_replays_unchanged` (rewrite one source selection to `{"msg_ids"}`
with `json_remove`; round trip still holds); `test_no_fetch_media_only_collector_in_replay` (grep-free:
assert `_replay_one`'s collector types equal the live list's for `channel`/`media`). Commit:
`test(reproject): fetch-media parity for normal, refused and zero-download segments (#68)`.

**T5 `detect_phases` on executed access.** Test (same file)
`test_detect_phases_media_requires_established_channel`: delete the `MediaDownload` raws of a granted
segment → still `["channel","media"]`; a refused segment (no `ChatFull` for its id) → `["channel"]`;
a run with a selection but whose `ChatFull` is for another id → `["channel"]`. Code: `ReplaySource.
channel_established(run, channel_id: int | None) -> bool` = a `chatfull` entry whose context
`channel_id == N` (any `chatfull` when `N is None`, the legacy shape); a granted `ChannelAccess` for N
always precedes that `ChatFull` (`collectors/channel.py:322,346` (dev)), so this is the "access granted"
test of §9.3 for stamped and legacy runs alike. Rewrite `detect_phases:259` and its docstring ("replay
what ran, never what was intended"). Replace `test_detect_phases_media_from_selection_marker_without_
download_raw`. Commit: `fix(reproject): replay media only where the live run established the channel (#68)`.

**T6 Marker replay from `ChatFull`.** Test: a source whose first segment took route 1 (no
`ResolvedPeer` in the run) followed by a marker segment → round trip (T4's source already is one; add
the assertion that run 3 has zero `resolvedpeer` entries). Code: `replay.py:692 resolved_access_hash`
→ read the source run's `chatfull` with context `channel_id == N`, `pick_channel(payload["chats"], N)
["access_hash"]` (0 placeholder stays); error text "never established channel N". Commit:
`fix(replay): reused-context replay reads the hash from the source run's ChatFull (#68)`.

**T7 Deletions + docs** (§4), then `git grep -n '_ExpectChannel\|unresolvable\|resolve_by_target'`
must be empty. Commit: `docs(fetch-media): by-id design, --exclude-target, no_access, replay rule (#68)`.

**T8 Gates + DoD smokes** (§5) → `docs(fetch-media): DoD gates and smoke transcripts (by-id) (#68)`.

## 4. Docs (DoD; a missing one blocks)

- `docs/features/fetch-media.md`: Input (`--exclude-target`); Outcomes (drop `unresolvable`, add
  `excluded`, `no_access`, order); "Segments and Step A" (id target, routes via #84, reuse marker, no
  guard, why identity holds); Stop policy (`no_access` continues; channel `phase_stop` still ends);
  Replay (selection payload, pinned stamps, §9.3 rule, marker replay from `ChatFull`); Deviations;
  Known limitations (route-4-nothing-tried runs skipped by `reproject`, media/custody unaffected; the
  history-evidence gap, see decisions); DoD transcripts replaced (the old ones ran on the superseded design).
- `README.md`: `fetch-media` row (flags incl. `--exclude-target`, outcomes list), "Where your data
  lives" (report), Documentation list. `CLAUDE.md`: Commands + status line ("#68 on
  `feat/fetch-media-list`, by-id amendment, PR pending → dev/media-storage").
- `docs/how-it-works.md` §6: step 2 (labels: excluded, not in store, …; no "cannot look up by name"),
  step 4 (reached by id via the §2 routes; one note per reuse), step 5 (replay follows the notes **and**
  only replays a pull where access was granted). `docs/data-model.md`: `ChannelContextReused
  {channel_id, source_run_id}`; `MediaSelection {channel_id, msg_ids}` (legacy `{msg_ids}`), written
  just before the media phase. `docs/features/reproject.md` "Replaying fetch-media runs": T5/T6 rules.
  ADR-0005 consequences: one sentence. `docs/features/collect-channel.md`: `fetch-media` calls Step A.

## 5. Definition of done — smokes (§9.6; paste verbatim, redacted; unredacted files stay in `<scratch>/68b`)

Setup: `sqlite3 <data-dir>/default/paperboy.sqlite ".backup '<scratch>/68b/default/paperboy.sqlite'"`,
`PAPERBOY_DATA_DIR=<scratch>/68b`, fresh `live-calls.log`. The real store is **not** yet split (#70's
swap is pending #81), so it still holds the excluded investigation: every command below carries
`--exclude-target @<channel>` (its linked group follows). Never print that handle or id.

**Offline.** (1) Gates, final lines verbatim. (2) `fetch-media tests/fixtures/media_list/synthetic.csv
--dry-run`: three tables. (3) Full `<list>` dry run on the copy, with the exclusion: expect 1,719 rows;
`excluded` = 40 (linked group) + the parent's rows; `already_stored` ≈ 118; `not_in_store`/`no_media`/
`deleted`/`duplicate_row` 0; the rest `pending`; per-channel table with ids as `<id>`; the segment plan.
No network by construction — cite the code: `cli.py` returns after `_print_plan` when `dry_run`, before
`_run_fetch` (`build_secrets`/`build_gateway`/doctor); `excluded_channel_ids`/`classify_rows` are store
queries; no `pacing:` log line; `test_dry_run_builds_no_gateway_and_no_secrets` asserts it. Optionally
repeat without the exclusion: the 40 formerly-`unresolvable` rows now read `pending` (reachable by id).

**Row selection** (offline, never named in committed text): rows from `<list>` with
`already_downloaded=0`, `deleted=False`, 1 photo + 2 videos with `size_mb ≤ 50`, from **2 different
channels** that are neither the excluded parent nor its linked group, each with a saved key in the copy
(`peers` row `is_min=0 AND access_hash IS NOT NULL`, so route 1 and zero resolve calls), whose dry run
reads 3 `pending`. Header + 3 rows → `<scratch>/68b/list-smoke.csv`; dry-run it first.

**Live** (protocol before each call: STOP flag, counter `< 5` then append, VPN check pasted).
Invocation 1: `PAPERBOY_REQUIRE_PROXY=false paperboy fetch-media <scratch>/68b/list-smoke.csv --profile
default --exclude-target @<channel> --max-rpc 60 --max-flood-sleep 60 --report <scratch>/68b/report-1.csv`.
Expected RPCs ≈ doctor (~6) + 2 channels × 2 (`get_self`, `get_full_channel`; **no** `resolve`) + 3 × 2
per file ≈ 16. Paste: result table (3 `downloaded`), the report with `tg:msg:<id>/<id>`, the two
`channel access: id=<id> via=saved_key granted=True` lines, raw kind counts (`ChannelAccess` 2,
`MediaSelection` = segments, `ChannelContextReused` 0 or 1 — say how the rows segmented, `MediaDownload`
3), `MediaSelection` rows with `json_extract(payload_json,'$.channel_id') IS NULL` = 0, `.incoming` empty,
`shasum -a 256` of one file = its `media.sha256`. Invocation 2: same with `--report
<scratch>/68b/report-2.csv` → 3 `already_stored`, no `pacing:` line, exit 0 (counts toward the cap).

**Reproject equality** (offline): `paperboy reproject --profile default --include-target <id>
--include-target <id>` (the two smoke channels; the ~29 min full-store run, #75, if time allows). Older
files of those channels are absent from the scratch media dir, so their replayed rows are `SkipAndRecord`
"media file missing" (expected; say so). Source vs `paperboy.reprojected.sqlite`: `SELECT message_uri,
sha256, path FROM media WHERE message_uri IN (<3 uris>)` equal; `custody_log` count for those uris equal;
raw kind counts for the fetch runs (`WHERE run_id IN (...)`) equal; the `decision=included` log lines.
A flag/cap/doctor/VPN block → PENDING/STOPPED per the rules; gates pass on offline evidence.

## 6. Edge cases and failure modes

- Route 4 with nothing tried writes no `ChannelAccess` (`collect-channel.md:128` (dev)): that segment's
  run holds only the self `User` raw and `reproject` skips it with its "no resolve records" WARNING —
  #84 behaviour, test (T4) and document it.
- A refused channel's later segments are `no_access` without retrying (routes come from the store, which
  does not change within the command). A `PhaseStop` in `channel` (a flood on route 3's `resolveUsername`,
  one per keyless channel per command, paced by #69) still ends the command — the persisted-cooldown
  rationale is unchanged.
- `getFullChannel` answering for another id raises `ValueError` (`channel.py:356` (dev)), which escapes
  `collect_channel`; the driver's `finally` still writes the report, then the command fails loudly.
- `--exclude-target` naming a channel the store never saw → exit 1 (as `reproject`); a linked group named
  directly needs its own `channels` row (the `discussion` collector writes one).
- `--phases media` without `channel`, or a refused channel: no selection, media `phase_stop`, replay
  identical. Legacy `{msg_ids}` selections exist only in test stores (the real store has none).
- `pin_json` keys on canonical JSON: keep `msg_ids` as `sorted(set(...))` or the replayed selection
  silently loses its stamp. SIGKILL mid-segment, unwritable report, BOM/CRLF, duplicates: as before.

## Orchestrator decisions (2026-10-01, binding)

1. **Later segments of a `no_access` channel** are marked `no_access` without retrying, since the routes come from the store and can't change within one command. Approved. Say so in the feature doc.
2. **Report:** add a `reason` column to the per-row report CSV (empty unless the outcome has one: `no_access`, `excluded`, and any skip with a reason). Keep the reason in the WARNING log and console summary too.
3. **History-evidence gap:** orthogonal to fetch-media (it never runs `history`). Don't fix it here. Document it in the plan, and the orchestrator files it as an issue.
4. **Reproject smoke:** the `--include-target` scoped run is required; run the full-store reproject if time allows, in the background with its PID recorded, and stop it before you finish.
5. **`ReplayClock.pin_json`:** approved, with its own unit tests in the clock tests and a docstring explaining why paperboy-authored receipts written mid-run need it.
