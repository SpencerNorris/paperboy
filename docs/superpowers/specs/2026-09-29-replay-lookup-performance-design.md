# Replay lookups must not scan the run: indexed raw-record lookup (#75)

**Status:** approved by the operator for the chain, 2026-09-29. **Tracking:** #75.
**Order:** runs after #64 and before #70 in the sequential chain (read
`2026-09-28-media-storage-overview.md` first). #70's split is two full
reprojections of the real store, so it is unusable until this lands.

## 1. Problem (measured)

`RawReplayGateway._latest` (`src/paperboy/replay.py`) serves every replayed
request with one query of this shape:

```sql
SELECT observed_at, payload_json FROM raw_records
WHERE (lower(kind) = ? OR lower(kind) LIKE ?)      -- _kind_clause
  AND <json_extract(context_json, ...) = ? ...>
  AND id BETWEEN ? AND ?                           -- the run's rowid range
ORDER BY id DESC LIMIT 1
```

Measured on a `.backup` copy of the operator's store (65,827 raw rows, about
600 MB, on the data volume), 2026-09-29:

- `EXPLAIN QUERY PLAN` → `SEARCH raw_records USING INTEGER PRIMARY KEY
  (rowid>? AND rowid<?)`. `idx_raw_records_kind(kind, observed_at)` is unused,
  because `lower(kind)` and the leading-wildcard `LIKE` can't use it. Every
  lookup walks the run's whole rowid range and reads each row's page,
  `payload_json` included.
- One `MediaDownload` lookup that finds no match (the common case: most
  media-bearing messages were never downloaded) takes **~13 s**, cold or warm.
- The store has **44,596 messages with media**, so a full media replay is on
  the order of days. Observed: a real-data `reproject --phases
  channel,history,media` made no visible progress in `media` for 3.5 h.

The same shape serves all 16 `_latest` call sites, so history, profiles and
other phases pay a smaller version of the same cost.

## 2. Requirement

Per-lookup cost must not grow with the size of the run: an indexed search,
not a range walk. Results must be **identical** to today's: the same row
(latest `id` in the run matching the kind variants and context), so the
reproject parity suite passes unchanged.

## 3. Design constraints (the planner picks the mechanism)

- **Raw first is untouched.** `raw_records` stays append-only; no existing
  column is rewritten. A new index (including an expression index) or a new
  *derived* column filled by a migration is allowed. It must be rebuildable
  from existing columns, and the migration must say so.
- **Kind variants:** `_kind_clause` matches `lower(kind)` exactly or with a
  dotted namespace prefix (see its docstring). Keep exactly those semantics.
- Candidate mechanisms, to be chosen by measurement (`EXPLAIN QUERY PLAN` plus
  timing on the scratch copy):
  (a) an expression index on the normalised kind and `id`, with the query
  rewritten to hit it (e.g. equality on a stored normalised-kind value rather
  than a leading-wildcard `LIKE`);
  (b) per run, load the matching `MediaDownload` (and other hot kinds')
  context keys into a dict once, then serve lookups from memory;
  (c) indexes on the `json_extract(context_json, …)` keys used by the hot
  lookups.
  Document the choice and the rejected options in the feature doc. If the
  choice adds a migration, append it after the last one on the branch; don't
  renumber.
- Memory: an in-memory preload must be bounded per run, and its size logged.
- Reproject stays read-only against the source (spec #64 §2.3). A new index
  therefore cannot be created in the source during reproject. Either the
  migration adds it (paperboy opens stores through `Store.open`, which
  migrates, so check how `ReplaySource` opens the source and say which
  applies), or the mechanism must not need it.

## 4. Tests (write first)

- Parity suite unchanged and green.
- For each `_latest` call site's query, `EXPLAIN QUERY PLAN` no longer shows a
  bare rowid-range `SEARCH` as the only access path (assert on the plan text
  in a unit test over a fixture store).
- A synthetic source with ≥ 50,000 raw rows in one run and ≥ 5,000
  media-bearing messages: the media phase replays within a stated bound
  (measure; set the bound at about 10× the measured time, so it catches a
  regression without flaking).
- Namespaced-kind fixtures (`contacts.resolvedPeer`, `messages.chatFull`) still
  resolve.

## 5. Definition of done (offline, no live calls)

On a `.backup` copy of the real store in the scratch dir (never the real
store; follow the overview's protocol for the per-file symlink media dir):

1. `EXPLAIN QUERY PLAN` of the media lookup, before and after (pasted).
2. The one-lookup timing from §1, before and after (pasted `time` output).
3. A **full** `reproject --phases channel,history,media` of the copy, run in
   the background with its wall time recorded. Paste the table with source
   and output columns labelled. If it doesn't finish within the agent's time
   budget, record the progress (runs and phases completed) and mark it
   PENDING for the operator. Don't narrow the phases to make it finish.

## 6. Out of scope

#74 (`web_snapshots` row loss on reproject). Fix it only if the change
naturally touches its cause; otherwise leave it.
