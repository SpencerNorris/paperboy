# Feature: `reproject`

**Status:** shipped. **Spec:** `docs/superpowers/specs/2026-08-25-reproject-design.md`.
**Plan:** `docs/superpowers/plans/2026-08-26-reproject.md`.

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

**Phase auto-detection.** `detect_phases(source)` infers which phases the
original run(s) executed from which raw *kinds* are present (a source that
never ran `graph` has no `ChatsSlice`/`ChatInvite*`/`SponsoredMessage` raws,
so reproject doesn't invent `graph`-only projections it never had) —
overridable with `--phases`. Detection is currently **source-wide, not
per-target** — see Known limitations.

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

- **Phase detection is source-wide, not per-target.** If one source DB
  contains raws from more than one resolved target — e.g. two historical
  `collect` runs against slightly different spellings of the same channel's
  handle — every target replays the *union* of phases seen anywhere in the
  source, and (if those targets resolve to the same underlying channel)
  redundantly re-projects it once per target-spelling. This does not produce
  *incorrect* rows — every row is a faithful replay of a real historical
  observation — but it inflates append-only time-series tables
  (`web_snapshots`, `custody_log`, `message_metrics`, `message_tombstones`)
  beyond the source's own counts. Found on the real archive (below); tracked
  as [#35](https://github.com/SpencerNorris/paperboy/issues/35).
- **A target that no longer resolves to a channel fails cleanly per-target,
  not silently.** `channel.collect` raises a bare `ValueError` when a
  resolved peer isn't a channel — the same crash a live `collect` against
  that target would hit today. Reproject's multi-target loop catches it,
  logs it, records it as a failed `target` phase in that target's own
  results table, and continues with the other targets rather than losing
  every already-committed target's projections to one bad historical entry.
  The underlying `channel.py` crash-instead-of-skip behavior is itself
  tracked as [#34](https://github.com/SpencerNorris/paperboy/issues/34)
  (orthogonal to reproject — it's pre-existing live-collect behavior).
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
