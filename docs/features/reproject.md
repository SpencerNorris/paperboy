# Feature: `reproject`

**Status:** shipped, including the ADR-0005 run-structure revision (below).
**Spec:** `docs/superpowers/specs/2026-08-25-reproject-design.md`.
**Plan:** `docs/superpowers/plans/2026-08-26-reproject.md`.
**ADR:** `docs/adr/0005-run-structure.md`.

## Purpose

`paperboy reproject` rebuilds every normalized projection (`channels`,
`peers`, `messages` + revisions/tombstones, `edges`, `media`, `web_snapshots`,
...) from an already-captured archive's `raw_records` — **offline, zero
network, zero credentials**. It realizes the raw-first thesis (raw is the
system of record; normalized tables are a projection that can be rebuilt from
it) as an actual command: apply every collector/projection fix shipped since
the archive was captured, without re-scraping Telegram.

## How it works

A `RawReplayGateway` (plus `RawReplayWebClient` for the `web` collector's
plain-HTTP vector) implements the same `Gateway`/`WebGetter` seams the real
collectors already depend on, but serves every response from a source DB's
`raw_records` instead of the network. `recipes.collect_channel` then runs
**unchanged** against the replay pair into a fresh target `Store` — same
collector code path as a live collect, so the reprojection is provably
identical to one (`tests/test_reproject.py::test_round_trip_identity`).

**The clock seam.** Projections stamp `observed_at` at write time; a live
collect wants "now", but a faithful reproject wants the *original* observation
time, not reproject-time. `paperboy.clock.Clock` (`LiveClock`/`ReplayClock`)
is threaded through every collector as `ctx.clock.for_payload(payload)`,
looked up by the payload's own canonical JSON — payload-keyed, not
call-order-keyed, because `history` consumes a whole page before projecting
each message and `catch_up` projects messages nested inside a
`ChannelDifference` envelope. `ReplayClock` is fed by the replay gateway as it
serves each raw record.

**Phase auto-detection.** `detect_phases(source, run)` infers which phases
ONE historical run executed from which raw *kinds* it left behind (a run
that never did `graph` has no `ChatsSlice`/`ChatInvite*`/`SponsoredMessage`
raws for that run, so reproject doesn't invent `graph`-only projections it
never had) — overridable with `--phases`. Detection is per-run (ADR-0005),
not source-wide: a source whose early runs never did `discussion` and whose
later runs did gets exactly that phase history back.

## Run structure (ADR-0005)

The design above models replay as ONE undifferentiated pass over the whole
raw log. That is wrong for any archive built from more than one `collect`
invocation (the ordinary shape — see `docs/adr/0005-run-structure.md` for
the full diagnosis): per-call-site `_latest()` lookups silently collapse a
multi-run archive's time series (`channel_snapshots`, `web_snapshots`,
per-run `custody_log` entries, ...) down to whichever run happened to write
last.

**`raw_records.run_id`** (migration `0003_run_id.sql`) records which collect
pass produced each raw record. `Store.begin_run()` mints an opaque id at the
start of every `collect_channel` call; a source built before this migration
has `NULL` run_id throughout, and `ReplaySource.runs()` infers pass
boundaries for those rows from the OPENING CLUSTER every pass writes once —
`resolve()`'s `ResolvedPeer`, `getFullChannel()`'s `ChatFull`, and the self
`User` record — in whatever order the collector version that captured them
used (current code writes self first; older archives predate that and wrote
resolve/full before self). `reproject` then replays **once per run**:
`RawReplayGateway`/`RawReplayWebClient` are constructed per run, every query
scoped to that run's own `raw_records` rowid range, and each run's raw
`run_id` (or inferred `legacy-NNNN` label) is stamped onto the target store
via `collect_channel(run_id=...)` — so a reprojected DB carries the same
pass structure as its source and is itself faithfully re-reprojectable.

`HistoryCollector`'s live-collection incremental-vs-full-sweep bookkeeping
(`sync_state` scopes `history`/`history_sweep`) is reset before every
replayed run: under LIVE collection that state legitimately persists across
re-runs (Telegram's history only grows forward, so "already fully swept" is
permanent), but under per-run REPLAY a run's own raw window naturally
running dry is a scope artifact, not a Telegram-side fact — left uncleared,
that flag wrongly short-circuits a later run's OWN, entirely-older raw
messages. See `src/paperboy/reproject.py::_reset_incremental_backfill_state`.

## CLI

```
paperboy reproject [--profile P] [--out PATH] [--phases a,b,c]
```

- `--profile` (default `default`): selects `<data_dir>/<profile>/paperboy.sqlite`
  as the source. The source is opened `mode=ro` and never mutated.
- `--out` (default `<data_dir>/<profile>/paperboy.reprojected.sqlite`):
  refuses to overwrite an existing file — move it aside or pass a fresh path.
- `--phases`: comma-separated override of the auto-detected phase set.

On success: a per-target, per-phase counts table (mirroring `collect`'s own
output), then a row-count diff — source vs. reprojected — per table, so the
operator can eyeball the correction before swapping files.

## Guardrails (spec §2, §8)

- **Zero network.** `build_reproject` (the composition root) constructs only
  `ReplaySource`/`RawReplayGateway`/`RawReplayWebClient` — never
  `TelethonGateway`, a real `WebClient`, a Telethon session, or `Budget`.
  Asserted in test by monkeypatching all three real constructors to raise.
- **Zero credentials.** No keychain access anywhere on this path (asserted
  by monkeypatching `keyring.get_password` to raise).
- **No media re-download or re-write.** `download_media` reads bytes back
  from the source profile's content-addressed store; a live collect's own
  write-if-not-exists guard (added as part of this feature) makes the
  guarantee free to verify by monkeypatching `Path.write_bytes` to raise.

## Design deviations from the spec (D4, plan §"Locked design decisions")

The approved spec's method-by-method table (§3) needed five refinements,
forced by the round-trip identity test being the correctness contract:

1. **`get_messages`** for an id with no raw record returns a
   `{"_": "ReplayUnknownMessage", "id": i}` placeholder, not a synthetic
   `messageEmpty` — a fabricated `messageEmpty` would manufacture deletion
   evidence for ids the original run never actually observed as deleted.
2. **`get_sponsored_messages`** reconstructs the `SponsoredMessages` envelope
   from the individually-stored `SponsoredMessage` records (the original
   collector never stores the envelope itself).
3. **`join_channel`** is a synthetic no-op returning success; reproject always
   runs with `allow_join=True` so a source whose original run used `--join`
   still replays its discussion sweep, but nothing is ever actually joined —
   no session exists to join with.
4. **`get_channel_difference`** past the last stored page serves a synthetic
   final `updates.channelDifferenceEmpty`, not `SkipAndRecord` — a
   mid-catch_up `SkipAndRecord` would mark the whole `history` phase skipped
   and discard the backfill counts already applied in that run.
5. Phase-set reproduction is by raw-*kind* detection (`detect_phases`), not
   per-method `SkipAndRecord` alone.

A sixth, found while wiring the replay gateway against fixtures rather than a
real archive: Telethon's `to_dict()` prefixes some RPC-result **envelope**
types with their TL namespace (`contacts.resolvedPeer` for `ResolvedPeer`,
`messages.chatFull` for `ChatFull`, `updates.channelDifference*` for
`ChannelDifference*`) but not others (`Message`, `ChatInvite*`,
`SponsoredMessage`, `MediaDownload`, ...) — collectors record
`payload.get("_", ...)` verbatim, so every kind lookup in `replay.py` matches
a bare kind *or* any `<namespace>.<kind>` suffix (`_kind_clause`), not an
exact string.

## Round-trip equality contract (D5)

The correctness contract (`tests/test_reproject.py::test_round_trip_identity`,
run with the real wall clock — no freezing): collect into DB1, reproject
DB1's raw into DB2, then every projection table is equal **as a set of
distinct rows**, modulo autoincrement primary keys and `source_raw_id` (whose
*content* round-trips transitively via the `raw_records` comparison, which is
itself part of the contract). Set, not multiset, comparison: a message
delivered by both `getHistory` and `getChannelDifference` is legitimately
served twice on replay, producing byte-identical duplicate raw/metrics rows —
that duplication is real signal (two independent observations), not a bug.
`run_events`/`sync_state`/`sync_ranges`/`flood_log`/`schema_migrations` are
excluded (operational/bookkeeping, not projections of raw content).

## Known limitations

Two limitations from the first (single-run) implementation are resolved by
the ADR-0005 run-structure revision:

- ~~Phase detection is source-wide, not per-target~~ — **resolved.** Phase
  detection and target resolution are now per-run (`detect_phases(source,
  run)`, `resolve_targets(run)`): two historical `collect` runs against
  different spellings of the same channel's handle each replay only within
  their own run(s), so there is no more redundant re-projection of one
  channel under two target strings. Was
  [#35](https://github.com/SpencerNorris/paperboy/issues/35).
- ~~A target that no longer resolves to a channel fails cleanly per-target,
  not silently~~ — **resolved.** `channel.py`'s `_resolved_channel_id` now
  raises `SkipAndRecord` for a non-channel resolution (issue
  [#34](https://github.com/SpencerNorris/paperboy/issues/34)), so it is a
  normal per-run `channel: skip` phase result — reproject's multi-target
  `try/except Exception` fallback (still present, for genuinely unexpected
  failures) is no longer what handles this case.

One residual, narrowed but not closed by this revision (and narrowed again
by the segmentation fix below — see "Bug found and fixed by a later
real-archive re-run"):

- **A historical run whose media phase downloaded nothing NEW leaves no raw
  trace, so its duplicate-custody observations are not reproduced.**
  `detect_phases`'s `media` inclusion is raw-*kind*-based per run (spec
  D4.5): a run that only re-encountered media already downloaded by an
  earlier run writes no `MediaDownload` raw record for it, so reproject
  correctly infers "media didn't run" for that specific run and skips the
  phase there — which also means that run's `custody_log` entry (a real,
  historical observation: "this run saw this file again, attached to this
  message") never gets replayed. On the real archive, after the segmentation
  fix below, this narrows `custody_log` below the source's count (607→599)
  — every row that IS present is correct and complete; nothing beyond a
  media file's OWN raw trace round-trips. (The first cut of this revision,
  before the segmentation fix, measured a much larger gap — 607→443 — but
  that number conflated this residual with the distinct segmentation bug
  below; it did not correctly isolate what #36 alone accounts for.)
  Deliberately not attempted in this revision (fixing it properly means
  detecting "ran but found nothing new" as distinct from "didn't run" from
  raw kinds alone, without fabricating custody observations for runs that
  genuinely never touched media) — tracked as
  [#36](https://github.com/SpencerNorris/paperboy/issues/36). (`media`
  itself narrows too, 451→449, but that 2-file gap is unrelated to #36 — see
  the DoD smoke transcript below: two files are genuinely missing from this
  profile's local media directory on disk, so replay's own content-addressed
  lookup correctly reports them unavailable rather than fabricating bytes.)
- Deferred (spec §9, deliberately out of scope): an in-place `--force` mode,
  a `--verify`-only mode, incremental reproject of a single phase into an
  existing DB.

## Definition-of-Done smoke transcript (2026-08-26, profile `default`, real archive)

Read-only throughout: the source `<data_dir>/default/paperboy.sqlite` (the
real, live-collected archive currently tracked in this repo's `data/`) was
never opened for writing; `--out` pointed at a scratch path outside the
profile directory so the existing archive was never at risk. No network, no
keychain — confirmed by the guardrail tests, and consistent with this
transcript's total runtime (under a minute for ~6,500 raw records / ~5,500
messages, which would be impossible against live Telegram's pacing).

```
$ PAPERBOY_DATA_DIR=.../data uv run paperboy reproject --profile default --out /tmp/reprojected.sqlite
▶ channel
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=543 revisions=543 tombstones=258 edges=238 · 2s
▶ discussion
✓ discussion · messages=4953 revisions=4953 tombstones=0 edges=2067 backfilled_peers=31 unmapped=359 · 6s
▶ graph
  graph invite preview skipped for LS-77p_pVyRiZDRk: replay: no ChatInvite recorded for hash 'LS-77p_pVyRiZDRk'
✓ graph · edges=120 peers=21 raw=5 skipped=1 · 0s
▶ web
  web: wayback CDX returned HTTP 429 for national_resistance_movement — reporting failure, not zero
✓ web · tme_posts=362 deleted_recovered=0 wayback_failed=429 · 0s
▶ media
  media: skipping msg 413: replay: media file missing for sha b006ec25...
  media: skipping msg 414: replay: media file missing for sha 295dbde0...
✓ media · downloaded=449 duplicates=0 unavailable=3 skipped_kind=27 skipped=2 · 33s

  reproject @atom8388 -> /tmp/reprojected.sqlite
  ERROR reproject: target '@atom8388' failed: target resolved to a non-channel peer (PeerUser)
┌────────┬────────┬───────────────────────────────────────────────────────┐
│ phase  │ counts │ stopped                                                │
├────────┼────────┼───────────────────────────────────────────────────────┤
│ target │ {}     │ error: target resolved to a non-channel peer (PeerUser)│
└────────┴────────┴───────────────────────────────────────────────────────┘

  [national_resistance_movement, no leading @: full re-run, same shape as above]

     row counts — source vs reprojected
┌────────────────────┬────────┬─────────────┐
│ table               │ source │ reprojected │
├────────────────────┼────────┼─────────────┤
│ raw_records         │ 6258   │ 6489        │
│ channels            │ 1      │ 1           │
│ channel_snapshots   │ 6      │ 2           │
│ peers               │ 158    │ 160         │
│ messages            │ 5496   │ 5496        │
│ message_revisions   │ 5496   │ 5496        │
│ message_metrics     │ 4020   │ 4186        │
│ message_tombstones  │ 258    │ 268         │
│ edges               │ 2503   │ 2630        │
│ media               │ 451    │ 449         │
│ custody_log         │ 607    │ 898         │
│ web_snapshots       │ 362    │ 724         │
└────────────────────┴────────┴─────────────┘
```

Verifying the spec §9 payoff directly (source vs. reprojected):

```
$ sqlite3 data/default/paperboy.sqlite \
  "SELECT count(*) FROM json_each((SELECT flags_json FROM channels));"
10
$ sqlite3 /tmp/reprojected.sqlite \
  "SELECT count(*) FROM json_each((SELECT flags_json FROM channels));"
48

$ sqlite3 data/default/paperboy.sqlite \
  "SELECT count(*) FROM peers WHERE kind='user' AND id IN
   (SELECT json_extract(value_json,'\$.id') FROM sync_state WHERE scope='account');"
1
$ sqlite3 /tmp/reprojected.sqlite \
  "SELECT count(*) FROM peers WHERE uri IN
   (SELECT json_extract(value_json,'\$.uri') FROM sync_state WHERE scope='account');"
0
```

The source archive's `channels.flags_json` (written by pre-#20-fix code) has
10 boolean flags; the reprojected copy has the full 48-flag set the current
`_channel_flags` projection captures. The source still has one self peer row
(pre-#12 fix); the reprojected copy has none — `is_self` is applied from raw
alone, over data the live account never re-observed since.

`messages`/`message_revisions` match the source **exactly** (5496/5496) even
though the archive's raw log turned out to contain two historical resolves of
the same channel (see Known limitations) — `upsert_message`'s
`ON CONFLICT DO UPDATE` on a URI primary key deduplicated correctly across
the redundant replay. `media` is one lower than the source (449 vs. 451): two
of the archive's `MediaDownload` raw records point at sha256 hashes whose
bytes are no longer present on disk under `data/default/media/` — a
pre-existing gap in the archive's file store, not a reproject defect;
`RawReplayGateway.download_media` correctly raises `SkipAndRecord` for each
and `MediaCollector` skips them cleanly (`skipped=2` above) rather than
fabricating a `media` row for bytes that don't exist.

### Bug found and fixed by this smoke test (no-shed)

**`reproject` crashed the entire multi-target run on one bad historical
target.** The real archive's raw log turned out to contain a `ResolvedPeer`
record for `@atom8388` (evidently an accidental `collect` invocation, at some
point, against something that wasn't a channel) whose `peer` is a `PeerUser`.
`channel.collect`'s `_resolved_channel_id` correctly raises `ValueError` for
this (pre-existing behavior — the same crash would hit a live `collect`
against that target too) — but `reproject`'s per-target loop had no
try/except around `collect_channel`, so this one bad historical entry took
down the *entire* reproject run, discarding the other targets'
already-committed-to-`out_store` projections along with it (visible in the
first smoke attempt, which crashed with an unhandled traceback right after
the primary channel's `media` phase completed).

Fixed in `reproject.py`: the per-target `collect_channel` call is wrapped in
a `try/except Exception`, which logs the failure and records it as a failed
`target` phase in that target's own results (`stopped="error: ..."`) rather
than aborting the run — `collect_channel` was designed for exactly one
target per invocation and has no notion of "this target, among several,
turned out bad"; that safety net belongs at the multi-target orchestration
layer reproject itself adds. Pinned by
`tests/test_one_bad_historical_target_does_not_abort_other_targets` (a
hand-seeded two-target source, one good and one PeerUser-resolving) before
the fix, confirmed against the real archive after it (the second transcript
above). The underlying `channel.py` crash-instead-of-skip behavior is
tracked separately as [#34](https://github.com/SpencerNorris/paperboy/issues/34)
— orthogonal to reproject, since it's a live-collect defect this smoke test
happened to surface, not something reproject's replay introduced.

---

## ADR-0005 revision — multi-run real-archive smoke (2026-08-26)

The transcript above pins the FIRST implementation, which modeled replay as
one undifferentiated pass over the whole raw log (`_latest()` per call
site). Re-running it against the same real `default` archive after that
implementation landed is what surfaced #35 and #36's evidence and the
`channel_snapshots`/`web_snapshots`/`message_metrics` collapse the ADR-0005
run-structure redesign exists to fix (`docs/adr/0005-run-structure.md`).
This section pins the run-scoped replay instead: `reproject` now replays
**once per historical run**, with `ReplaySource.runs()` segmenting this
archive's raw log (captured entirely before migration `0003_run_id`, so
every boundary is inferred, not stamped) into 7 legacy runs.

Same guardrails as before: source opened read-only throughout, `--out`
pointed outside the profile directory, no network/keychain access (asserted
by the guardrail tests; consistent with total runtime for ~6,500 raw
records / ~5,500 messages, which real Telegram pacing could not match).

```
$ PAPERBOY_DATA_DIR=.../data uv run paperboy reproject --profile default --out /tmp/reprojected.sqlite
▶ channel
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=543 revisions=543 tombstones=258 edges=238 · 1s
▶ graph
  graph invite preview skipped for LS-77p_pVyRiZDRk: replay: no ChatInvite recorded for hash 'LS-77p_pVyRiZDRk'
✓ graph · edges=120 peers=21 raw=5 skipped=1 · 0s
▶ web
  web: wayback CDX returned HTTP 429 for national_resistance_movement — reporting failure, not zero
✓ web · tme_posts=362 deleted_recovered=0 wayback_failed=429 · 0s
▶ media
  media: skipping msg 413: replay: media file missing for sha b006ec25...
  media: skipping msg 414: replay: media file missing for sha 295dbde0...
✓ media · downloaded=150 duplicates=0 unavailable=302 skipped_kind=27 skipped=2 · 7s
▶ channel                                          [run 2, same target spelling]
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=0 revisions=0 tombstones=0 edges=0 · 0s
▶ media
✓ media · downloaded=143 duplicates=150 unavailable=161 skipped_kind=27 skipped=0 · 7s

  reproject @national_resistance_movement -> /tmp/reprojected.sqlite
  [2-row table: run 1's channel/history/graph/web/media, run 2's channel/history/media]

▶ channel                                          [run 3: @atom8388]
⏹ channel · skip · 0s
  phase channel skipped: replay: no self User recorded
▶ history
⏹ history · phase_stop · 0s
▶ media
⏹ media · phase_stop · 0s

  reproject @atom8388 -> /tmp/reprojected.sqlite
  [clean per-run skip — NOT an error row this time; #34's fix (below)]

▶ channel                                          [runs 4-7: 'national_resistance_movement', no leading @]
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=0 revisions=0 tombstones=0 edges=0 · 0s
▶ discussion
✓ discussion · messages=300 revisions=300 tombstones=0 edges=256 backfilled_peers=31 unmapped=6 · 2s
[... 3 more channel/history/discussion blocks: 300, 1200, 3153 discussion messages ...]

  reproject national_resistance_movement -> /tmp/reprojected.sqlite
  [4-row table, one per run]

     row counts — source vs reprojected
┌────────────────────┬────────┬─────────────┐
│ table               │ source │ reprojected │
├────────────────────┼────────┼─────────────┤
│ raw_records         │ 6258   │ 6104        │
│ channels            │ 1      │ 1           │
│ channel_snapshots   │ 6      │ 6           │
│ peers               │ 158    │ 160         │
│ messages            │ 5496   │ 5496        │
│ message_revisions   │ 5496   │ 5496        │
│ message_metrics     │ 4020   │ 4020        │
│ message_tombstones  │ 258    │ 258         │
│ edges               │ 2503   │ 2522        │
│ media               │ 451    │ 293         │
│ custody_log         │ 607    │ 443         │
│ web_snapshots       │ 362    │ 362         │
└────────────────────┴────────┴─────────────┘
```

**Segmentation.** `run_events` (a manual cross-check only, per ADR-0005 —
it's a projection-side table, not raw) shows 6 `channel: complete` events;
`ReplaySource.runs()` found 7 legacy segments. The extra one is `@atom8388`
(run 3) — its channel phase never completed live (`ChannelPrivateError` or
similar; the archive's `ResolvedPeer` for it is the only trace), so it never
wrote a `channel: complete` event, but it IS its own genuine historical
collect pass and correctly gets its own run.

**This 6-vs-7 mismatch turned out to be a real bug, not a benign
discrepancy** — see "A third bug found and fixed by a later real-archive
re-run" below, which also supersedes this transcript's `media`/`custody_log`/
`raw_records` numbers.

**The ADR's headline payoff, confirmed exactly:** `channel_snapshots` (6→6),
`web_snapshots` (362→362), and `message_metrics` (4020→4020) all now
round-trip **exactly** — these are the three tables that collapsed under
the single-pass design (6→2, 362→724, 4020→4186 in the first transcript
above). `messages`/`message_revisions` also match exactly (5496/5496).

**Round-1's payoffs still hold:** `channels.flags_json` still corrects from
the source's 10 pre-#20-fix flags to the full 48-flag set current code
captures; the source's one pre-#12 self peer row still projects to zero in
the reprojected copy (same verification queries as the first transcript,
against `/tmp/reprojected.sqlite` from this run — unchanged, omitted here).

**`@atom8388` now shows as a clean per-run skip, not an error row** — #34's
fix (`_resolved_channel_id` raises `SkipAndRecord`, not a bare `ValueError`)
landed in this revision (Task R4) and is exercised directly by the real
archive: no ERROR line, no `stopped="error: ..."` row, just an ordinary
`channel: skip` result like a live `collect` against a private/deleted
target would produce.

**`peers`/`edges` are up slightly** (158→160, 2503→2522) — the same kind of
"current code corrects old code's projections" delta round 1 documented, not
a defect; every extra row is a legitimate current-projection fact the
original archive's older collector code didn't capture.

**`media`/`custody_log`/`raw_records` were attributed to the #36 residual**
(see Known limitations) at the time: 293/443/6104 vs. the source's
451/607/6258 — **this attribution was wrong.** The 6-vs-7 segmentation
mismatch noted above is the dominant cause; #36 alone accounts for a much
smaller gap. See below.

### Two bugs found and fixed by this smoke test (no-shed)

Both were caught by re-running this exact command against the real archive
BEFORE committing the run-structure implementation — the round-trip test
battery's synthetic fixtures did not happen to exercise either shape.

**1. Legacy run segmentation orphaned a real archive's pre-invariant leading
rows, dropping whole runs.** `ReplaySource.runs()` infers legacy (pre-`0003_
run_id`) run boundaries from the `tier='self'` marker every collect pass
writes first — but THIS archive's earliest collector code wrote
`resolve()`/`getFullChannel()` BEFORE `self`, and that ordering recurred at
EVERY historical pass boundary, not just the first. The initial fix (cut a
boundary at every self marker except the very first) still left every run's
own leading `ResolvedPeer`/`ChatFull` attached to the PRECEDING run instead
of the one they actually opened — silently dropping the archive's single
largest run (3154 of 6258 raw records, discovered as `raw_records` /
`messages` undercounting by roughly that amount on the first re-run).
Fixed by anchoring a new legacy segment on the first row of the whole
OPENING CLUSTER (`ResolvedPeer`/`ChatFull`/self, in whatever order) seen
after at least one substantive row, so all three land together in the run
they belong to regardless of order. Pinned by
`tests/test_replay_gateway.py::test_runs_absorbs_resolve_before_self_at_every_boundary`.

**2. `HistoryCollector`'s incremental-sync state leaked across replayed
runs, silently dropping a later run's own older messages.** `iter_history`
is scoped to one run's raw window (by design, ADR-0005) — so it naturally
runs out the moment that window is exhausted, and `history.py` reads that as
"reached the real end of the channel's history," persisting
`backfill_complete=True`. That is correct for a LIVE gateway (Telegram's
history only grows forward). Left uncleared across REPLAYED runs, it wrongly
switches the NEXT run to incremental-only (ids above the previous run's
high-water mark) — and a real multi-session backward backfill (this
archive's discussion group: msg-id bands 26955–34977, then 4525–26954, then
3220–4524, then 1–3219, each a separate historical run extending further
into the past) is exactly the shape that trips it, since every subsequent
run's messages are entirely BELOW the high-water mark. Fixed by clearing the
`history`/`history_sweep` `sync_state` scopes before every replayed run —
free under replay (no network, no rate limit to economize on), and safe
because idempotent projection upserts make any resulting re-processing of
already-seen messages harmless. Pinned by
`tests/test_reproject.py::test_reproject_does_not_truncate_a_backward_multi_run_backfill`.

### A third bug found and fixed by a later real-archive re-run (2026-08-26)

The two bugs above were caught before the ADR-0005 revision first landed.
Re-running the exact same command against the real archive during a later
validation pass surfaced a third, distinct legacy-segmentation defect that
the round-trip test battery's synthetic fixtures — and the first re-run
above — did not happen to exercise, because it needs a *foreign* row with no
relationship to either run on either side of it, which no hand-built
fixture had included.

**Legacy run segmentation cut an unconditional new-run boundary on ANY
opening-kind row seen after a substantive row — including a lone, foreign
one with no self marker anywhere near it — silently discarding everything
after it up to the next genuine opening cluster.** Nothing in this codebase
stops two `collect` invocations from writing to the same profile
concurrently (no file lock exists anywhere in `src/paperboy/`), and the real
archive turned out to contain exactly that: a single stray `ResolvedPeer`
for `@atom8388`, landing mid-run inside what was otherwise one continuous
156-row `MediaDownload` loop for `national_resistance_movement` (msg ids
ascending 585→801 straight through it). The opening-cluster rule (bug #1,
above) correctly keeps a GENUINE new pass's `ResolvedPeer`/`ChatFull`/self
trio together — but it committed to a new-run boundary the instant it saw
ANY opening-kind row after a substantive one, with no check for whether a
self marker ever showed up to confirm the cluster was a genuine new pass.
The synthetic segment this cut then had no self record at all, so replay
failed immediately at `get_self()` (`channel: skip · no self User
recorded`) with `history`/`media` both `phase_stop` — silently dropping all
157 rows in that segment (156 of them genuine historical `MediaDownload`
observations) with no error and no warning. This is exactly the mismatch
the previous transcript's "Segmentation" note flagged (`run_events`: 6
`channel: complete` events; `runs()`: 7 legacy segments) without yet
diagnosing it — the 7th "run" was this orphaned fragment, not `@atom8388`'s
own genuine pass as first assumed.

Fixed by not committing to a legacy boundary until the candidate opening
cluster is known to be COMPLETE (bounded by the next substantive row, a
stamped row, or the log's end) and confirmed to contain its own self
marker; a cluster with none is folded into whichever run is already open
instead of orphaning everything after it. `ReplaySource.runs()`'s docstring
and the inline comments in `src/paperboy/replay.py` describe the buffering
algorithm this required. Pinned by
`tests/test_replay_gateway.py::test_runs_does_not_split_a_run_on_a_foreign_single_row_intrusion`
(the minimal synthetic repro) and
`test_runs_still_cuts_a_genuine_boundary_after_a_foreign_intrusion` (a
genuine boundary elsewhere in the same log must still cut). Also added:
`test_runs_raises_on_a_genuinely_interleaved_stamped_run_id`, closing a
pre-existing gap — ADR-0005's stamped-run contiguity guarantee
(`ReprojectSourceError`) had no test coverage at all before this pass.

Re-running the smoke command after the fix:

```
$ PAPERBOY_DATA_DIR=.../data uv run paperboy reproject --profile default --out /tmp/reprojected.sqlite
▶ channel
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=543 revisions=543 tombstones=258 edges=238 · 0s
▶ graph
  graph invite preview skipped for LS-77p_pVyRiZDRk: replay: no ChatInvite recorded for hash 'LS-77p_pVyRiZDRk'
✓ graph · edges=120 peers=21 raw=5 skipped=1 · 0s
▶ web
  web: wayback CDX returned HTTP 429 for national_resistance_movement — reporting failure, not zero
✓ web · tme_posts=362 deleted_recovered=0 wayback_failed=429 · 0s
▶ media
  media: skipping msg 413: replay: media file missing for sha b006ec25...
  media: skipping msg 414: replay: media file missing for sha 295dbde0...
✓ media · downloaded=150 duplicates=0 unavailable=302 skipped_kind=27 skipped=2 · 5s
▶ channel                                          [run 2 — now the MERGED legacy-0002: old legacy-0002 + old legacy-0003 (the orphan)]
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=0 revisions=0 tombstones=0 edges=0 · 0s
▶ media
✓ media · downloaded=299 duplicates=150 unavailable=5 skipped_kind=27 skipped=0 · 10s

  reproject @national_resistance_movement -> /tmp/reprojected.sqlite
  [2-row table, as before]

▶ channel                                          [@atom8388's stray resolve, now correctly a SECOND target within run 2 rather than its own orphaned run]
⏹ channel · skip · 0s
  phase channel skipped: target resolved to a non-channel peer (PeerUser)
▶ history
⏹ history · phase_stop · 0s
▶ media
⏹ media · phase_stop · 0s

  reproject @atom8388 -> /tmp/reprojected.sqlite
  [clean per-run skip, same shape as before — but now correctly attributed to run 2, not a phantom run 3]

▶ channel                                          [runs 3-6 (was 4-7): 'national_resistance_movement', no leading @]
✓ channel · channels=1 peers=2 · 0s
▶ history
✓ history · messages=0 revisions=0 tombstones=0 edges=0 · 0s
▶ discussion
✓ discussion · messages=300 revisions=300 tombstones=0 edges=256 backfilled_peers=31 unmapped=6 · 0s
[... 3 more channel/history/discussion blocks: 300, 1200, 3153 discussion messages — unchanged from before ...]

  reproject national_resistance_movement -> /tmp/reprojected.sqlite
  [4-row table, one per run]

     row counts — source vs reprojected
┌────────────────────┬────────┬─────────────┐
│ table               │ source │ reprojected │
├────────────────────┼────────┼─────────────┤
│ raw_records         │ 6258   │ 6262        │
│ channels            │ 1      │ 1           │
│ channel_snapshots   │ 6      │ 6           │
│ peers               │ 158    │ 160         │
│ messages            │ 5496   │ 5496        │
│ message_revisions   │ 5496   │ 5496        │
│ message_metrics     │ 4020   │ 4020        │
│ message_tombstones  │ 258    │ 258         │
│ edges               │ 2503   │ 2522        │
│ media               │ 451    │ 449         │
│ custody_log         │ 607    │ 599         │
│ web_snapshots       │ 362    │ 362         │
└────────────────────┴────────┴─────────────┘
```

**`ReplaySource.runs()` now finds exactly 6 legacy segments, matching
`run_events`'s 6 `channel: complete` rows exactly** — the mismatch the
previous transcript flagged and didn't yet explain is gone. `@atom8388`'s
stray resolve now correctly appears as a SECOND target discovered within
the merged run 2 (`resolve_targets` returns every distinct target seen
within a run, in capture order) rather than manufacturing a phantom run of
its own — still a clean `channel: skip` (#34's fix), just now attributed to
the run that actually recorded it.

**`raw_records` recovers from 6104 to 6262** — slightly ABOVE the source's
6258, which is expected and correct: the previously-orphaned 156
`MediaDownload` rows are now replayed as part of run 2, and a few messages
legitimately re-served via both `getHistory` and `getChannelDifference`
within that run produce byte-identical duplicate raw rows (real signal, per
the round-trip contract's set-comparison note above — the identity contract
counts these DEduplicated as a set, so they don't cost `test_round_trip_
identity` anything). **`media` recovers from 293 to 449** (2 below the
source's 451 — the pre-existing 2-file local-disk gap round 1 already
documented, now the ONLY remaining `media` gap). **`custody_log` recovers
from 443 to 599** (8 below the source's 607) — this residual 8-row gap is
what #36 alone actually accounts for; the doc's "Known limitations" section
above is updated to the corrected number. `messages`/`message_revisions`/
`message_metrics`/`message_tombstones`/`web_snapshots`/`channel_snapshots`
were already exact before this fix and remain exact after it — this bug
never touched those tables' correctness, only which run `media`/`custody_
log`/`raw_records` rows got attributed to (or dropped from) via the wrong
segment boundary.
