# ADR-0005: Record run structure in the raw log (`raw_records.run_id`)

**Status:** accepted (2026-08-26); verified (2026-08-26) — real-archive
smoke re-run after implementation, `docs/features/reproject.md`'s "ADR-0005
revision" section. `channel_snapshots`/`web_snapshots`/`message_metrics` all
round-trip exactly (the headline defect this ADR fixes); two further bugs
surfaced by that smoke run and fixed before landing — see that section's
"Two bugs found and fixed by this smoke test."

## Context

The raw-first thesis (ADR-0002, CLAUDE.md) says normalized tables are a
projection that can be rebuilt from raw. Implementing `reproject` (#33)
proved that claim **false as stated for any multi-run archive**: the
projection is a function of the raw log *plus the run structure* — how many
collect passes produced the records and which record belongs to which pass —
and the run structure is nowhere in the raw log.
`raw_records(kind, observed_at, tier, context_json, payload_json)` cannot
distinguish "one run that observed the channel once" from "seven runs that
observed it seven times", yet the projections differ (snapshot time series,
`first_seen`/`last_seen` evolution, per-run web captures, custody entries).

The first implementation modeled replay as a single run (`_latest()` per call
site) and, when review rejected the resulting data loss, grew a parallel
"backfill older observations" projection path outside the collectors. Three
review rounds showed negative convergence — the shadow path kept violating
invariants the collector path establishes for free (write ordering,
self-exclusion preconditions, serve-coverage). Full diagnosis: #33.

## Options considered

- **A. Add a run identifier to `raw_records`; replay run-by-run.** Schema
  change to the system of record; restores a single projection path (the
  collectors), each historical pass replayed in capture order.
- **B. Keep the one-run replay + hand-written backfills for older
  observations.** No schema change, but duplicates projection logic outside
  the collectors — empirically the defect generator #33 documents.
- **C. Single-run scope: refuse or warn on multi-run sources.** Honest but
  defers the feature's entire payoff — the real `default` archive is
  multi-run (7 collect passes).
- **Legacy-row segmentation:** (i) `observed_at` gap clustering — rejected:
  a FLOOD_WAIT sleep inside one run produces gaps larger than the gap
  between runs; (ii) join against `run_events` — rejected as the primary
  rule: it is a projection-side table, so leaning on it weakens the very
  "pure function of raw" contract being repaired (kept only as a manual
  cross-check); (iii) structural marker — every collect pass's **first** raw
  write is the redacted collecting-account `User` at `tier='self'`
  (`collectors/channel.py` writes self before anything else, and the CLI
  refuses dependent phases without `channel`), so a new `tier='self'` user
  record marks a run boundary.

## Decision

Option A, with structural-marker inference for legacy rows.

1. Migration `0003_run_id.sql`: `ALTER TABLE raw_records ADD COLUMN run_id
   TEXT` (nullable — legacy rows stay NULL; never rewritten) plus an index.
2. `Store.begin_run(run_id: str | None = None) -> str` generates an opaque
   id (uuid4 hex) and every subsequent `add_raw` stamps it.
   `recipes.collect_channel` begins a run at entry and accepts an injected
   `run_id` so a replay stamps the target DB with the *source* run's
   identity — a reprojected DB is itself faithfully re-reprojectable.
3. Run **ordering is derived from the log** (ascending `MIN(rowid)` per
   run), never encoded in the id. Runs are contiguous rowid ranges (one
   sequential process per collect); `ReplaySource` asserts contiguity and
   fails loudly if ever violated.
4. Legacy rows (NULL `run_id`) are segmented **at replay time, read-only**
   by the `tier='self'` marker rule; segments are labeled `legacy-0001…` in
   capture order. The source DB is never mutated.
5. `reproject` replays **once per run**: per-run targets (that run's
   `ResolvedPeer` records), per-run phase detection, per-run-scoped gateway
   queries (`id BETWEEN run.lo AND run.hi`). The target store carries
   `sync_state` across replayed runs exactly as the live store did across
   real runs. All `_backfill_older_*` shadow-projection code is deleted.
6. Independently (defense-in-depth and a live-collect correctness fix):
   `upsert_peer`/`upsert_channel` become order-independent —
   `first_seen = MIN`, `last_seen = MAX`, current-state fields updated only
   by an observation at least as new as the stored `last_seen`.

## Consequences

- One projection path again; replay ordering, self-exclusion, and serve
  coverage are correct by construction instead of by re-implementation.
- The round-trip identity contract (spec §7/D5) extends to **multi-run**
  sources, and the gate test suite gains a two-run fixture — the missing
  convergence signal from #33.
- Schema change to the system of record; old paperboy versions ignore the
  extra column (SQLite), new versions read old DBs via inference.
- Legacy segmentation is best-effort: exact for every archive produced by
  current collectors (self is always written first), inferential only in
  that the rule is derived, not recorded. `run_events` counts serve as a
  manual cross-check during the real-archive smoke.
- Residual (narrowed, not closed, #36): a historical run whose media phase
  ran but downloaded nothing new leaves no raw trace, so its
  duplicate-custody rows are not reproduced — phase detection is
  conservative by design (spec D4.5).
- #35's double-replay of one channel under two target spellings disappears:
  each spelling replays only within its own run(s).

## Notes

- Diagnosis and evidence: issue #33 (escalation comment, 2026-08-26).
- Related: #34 (non-channel resolution must be a `SkipAndRecord`, not a
  crash — surfaced by the same smoke), #36 (custody residual, above).
- Spec: `docs/superpowers/specs/2026-08-25-reproject-design.md` §11.
- Plan: `docs/superpowers/plans/2026-08-26-reproject.md`, revision R.
