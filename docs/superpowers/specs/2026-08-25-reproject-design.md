# `reproject` — rebuild projections from the raw log, no network

**Status:** shipped, 2026-08-26 (see §10, Implementation notes). Realizes the
raw-first thesis (`raw_records` is the system of record; normalized tables
are a projection that can be rebuilt from raw) as an actual command.
**Execution:** one `single-feature-run` on `feat/reproject`.

## 1. Goal

Apply every projection-layer change (channel flags, self-exclusion, edge
idempotency, peer provenance, the `MessageEmpty` guard, and any future
projector) to an *already-captured* archive **without re-scraping Telegram**.
Today the only way to get corrected projections is a live re-run; `reproject`
makes it a local, offline, zero-network operation over the raw log.

## 2. Approach — replay through the Gateway seam

The raw log *is* the log of gateway responses: every collector called a Gateway
method, `to_dict()`'d the response, and appended it to `raw_records`. So the
projection can be rebuilt by running the **normal collectors** against a gateway
that serves `raw_records` back instead of Telegram.

- **`RawReplayGateway`** implements the existing `Gateway` Protocol, backed by a
  source `paperboy.sqlite`'s `raw_records`. **`RawReplayWebClient`** does the
  same for the `web` collector's HTTP vector.
- `recipes.collect_channel` runs unchanged against them, writing into a **fresh
  target DB**. Because it is the same collector code path, the reprojection is
  *provably identical* to a live collect — it reproduces edges, tombstones,
  revisions, snapshots, everything, not just messages — and needs **no collector
  refactor**.

This is exactly the seam `FakeGateway` already validates; `RawReplayGateway` is
`FakeGateway`'s production sibling, sourced from a real raw log rather than
hand-authored fixtures.

### Hard invariant: zero network

`reproject` constructs **only** the replay gateway/web-client — never a
`TelethonGateway` or real `WebClient`, never a session, never `Budget`. A
reproject must be incapable of touching Telegram or the web. Enforced by
construction (the composition root builds the replay pair) and asserted in test.

## 3. Method-by-method reconstruction

Each `Gateway` Protocol method maps to raw kinds, keyed by the `context_json`
already stored on every record:

| Protocol method | Served from `raw_records` |
|---|---|
| `resolve(target)` | `ResolvedPeer` whose `context.target` matches |
| `get_full_channel(ic)` | `ChatFull` for `context.channel_id` |
| `get_self()` | the `User` record written at `tier='self'` |
| `iter_history(ic, offset_id, limit)` | `Message`/`MessageService` for the channel, `id DESC`, `< offset_id`, `LIMIT` — reconstructs the original paging so the collector's cursor + gap-probe logic runs identically. **Excludes `MessageEmpty`** (getHistory never yielded them — they came from the probe) |
| `get_messages(ic, ids)` | the stored `Message`/`MessageEmpty` for each id; a synthetic `messageEmpty` for an id with no record (a true gap) |
| `get_channel_difference(ic, pts, limit)` | the stored `ChannelDifference*` for the channel, in capture order |
| `check_chat_invite(hash)` | `ChatInvite`/`ChatInvitePeek` for `context.hash` |
| `get_channel_recommendations(ic)` | the recommendations `Chats`/`ChatsSlice` for the channel |
| `get_sponsored_messages(ic)` | the stored sponsored response, or `SkipAndRecord` if none |
| `download_media(ic, message)` | **special — see §4** |
| (future) `get_participants`/`get_users`/`get_full_user` | their raw kinds, once the people layer exists — reproject picks them up automatically |

A method with no matching raw raises `SkipAndRecord`, so the collector skips
that phase cleanly — reproject reproduces exactly the phase set the original run
executed (a run that never did `graph` reprojects without `graph`).

## 4. Special cases

- **Media.** `download_media` returned bytes the collector content-addressed to
  disk; the bytes are not in `raw_records` (only a `MediaDownload` metadata
  record). On replay the files already exist under
  `<data_dir>/<profile>/media/<sha>`, so `RawReplayGateway.download_media` reads
  and returns the bytes from that content-addressed path (keyed by the hash in
  the `MediaDownload` record), letting the media collector re-hash and re-project
  the `media`/`custody_log` rows identically. If the target DB is a different
  profile, media is read from the *source* profile's directory. No file is
  re-downloaded or re-written.
- **Web.** `RawReplayWebClient` serves `tme_page` / `wayback_cdx` records by
  `context.channel_username`, reproducing the `web_snapshots` projection.

## 5. Timestamp fidelity (the one clock decision)

Projections stamp `observed_at`/`first_seen`/`last_seen`/snapshots with
`utc_now_iso()`. To make a reproject a faithful copy — and to make the
round-trip identity test (§7) hold — reproject **preserves the original
observation time**: it threads each raw record's stored `observed_at` as the
clock for that object's projection, rather than stamping "now". This needs a seam:
a `ctx.now()` the collectors read at projection sites, defaulting to
`utc_now_iso()` in live mode and returning the raw record's stored `observed_at`
in replay mode. Threading it through every `observed_at = utc_now_iso()`
projection site is the **main implementation effort and the only
collector-touching change** in this feature — mechanical but broad (each
collector + the store snapshot writers), so it carries its own test that a live
collect is byte-identical before and after the seam is introduced. (Without the
seam, reproject still produces correct *content* but resets all timestamps to
reproject-time, and the §7 round-trip test must exclude timestamp columns;
timestamp fidelity is worth the seam.)

## 6. CLI and composition

```
paperboy reproject [--profile P] --out PATH [--phases ...]
```
Reads `<data_dir>/<P>/paperboy.sqlite`'s `raw_records`, rebuilds into a new DB at
`--out` (default `<data_dir>/<P>/paperboy.reprojected.sqlite`), original
untouched. The composition root (`app.py`) gets a `build_reproject` that wires
the replay pair + a fresh Store; `cli.py` gains the `reproject` command.
`--phases` optionally limits which projections to rebuild. On success it prints
a per-phase counts table and a short diff summary vs the source (row counts per
table), so the operator can verify before swapping.

## 7. Testing — round-trip identity

The correctness contract is a **round-trip**: collect against a `FakeGateway`
into DB1 (populating `raw_records` + projections), `reproject` DB1's raw into
DB2, and assert **DB2's projection tables equal DB1's** (channels, peers,
messages, revisions, tombstones, edges, media, web_snapshots — modulo primary
keys / autoincrement ids). With timestamp preservation (§5) this is exact
equality; it is the single most powerful test in the suite — it proves the
projection is a pure function of the raw log.

Plus: a zero-network assertion (no `TelethonGateway`/`WebClient`/session ever
constructed); a corrected-projection test (a DB1 whose projections were written
by *old* code — e.g. a self peer row, a fixed 10-flag channel — reprojects to
the *new* correct shape: no self, 48 flags); a missing-raw test (a source
missing `graph` raw reprojects without graph, no crash); the media file-read
path; and a partial/interrupted source (budget/phase-stop) reprojects to the
same partial state.

## 8. Guardrails

Zero network (§2). Read-only w.r.t. the source DB (only `raw_records` is read;
its projections are never mutated). No credentials/session required (a reproject
must run with no keychain access at all — it is the one command that needs no
Telegram identity). Self-exclusion, tri-state, idempotency all apply because the
real collectors run.

## 9. Scope and follow-ups

- **In scope:** the replay gateway + web client, the reproject command +
  composition, the observed-at seam, the round-trip test.
- **Deferred:** an in-place `--force` mode (this design writes a fresh DB only);
  a `--verify` mode that only diffs without writing; incremental reproject of a
  single phase into an existing DB. All are additive later.
- **Payoff for the current archive:** once shipped, one `reproject` of the
  existing `default` DB yields corrected flags (48 not 10), a self-free `peers`,
  #11 provenance, idempotent edges, and the `MessageEmpty` guard applied — over
  everything already captured, including the since-deleted content that lives
  only in raw — with no Telegram call. A live re-run is then needed only for
  genuinely-new data and the people layer.

## 10. Implementation notes (2026-08-26)

Shipped per `docs/superpowers/plans/2026-08-26-reproject.md`. Deviations from
§3/§7 above, each forced by the round-trip identity test (D5 below) being the
correctness contract rather than a direction change:

- **D4.1 — `get_messages` placeholder.** An id with no raw record returns
  `{"_": "ReplayUnknownMessage", "id": i}`, not a synthetic `messageEmpty` —
  the latter would fabricate deletion evidence (`mark_deleted(evidence=
  'empty')`) for ids the original run never actually observed as deleted
  (a gap the probe found alive, or a range only reachable via replayed
  catch-up). The collector skips any non-`messageEmpty` shape, so the
  placeholder projects nothing — exactly the source's state.
- **D4.2 — `get_sponsored_messages` envelope reconstruction.** The original
  collector never stores the `SponsoredMessages` envelope, only each
  `SponsoredMessage` individually; replay reconstructs
  `{"_": "SponsoredMessages", "messages": [...]}` from those records, and
  serves `{"_": "sponsoredMessagesEmpty"}` when none exist (empty-and-skipped
  originals are indistinguishable; both project nothing).
- **D4.3 — `join_channel` is a synthetic no-op**, and reproject always runs
  with `allow_join=True` — otherwise a source whose original run used
  `--join` would skip its discussion sweep and lose projections on replay.
  No network is involved; nothing is joined, because no session exists.
- **D4.4 — `get_channel_difference` past the last stored page** serves a
  synthetic final `{"_": "updates.channelDifferenceEmpty", "final": True,
  "pts": pts}`, not `SkipAndRecord` — a mid-`catch_up` `SkipAndRecord` would
  mark the whole folded history phase skipped and discard the backfill
  counts already applied that run. This is why the round-trip test excludes
  `raw_records` specifically for an interrupted/partial source
  (`test_partial_interrupted_source_reprojects_to_same_partial_state`): the
  synthetic closing page has no raw counterpart in the (crashed) original.
- **D4.5 — phase-set reproduction is by raw-*kind* detection**
  (`detect_phases`), not per-method `SkipAndRecord` alone — the `graph`
  phase's mention scan is RPC-free and would otherwise project edges into a
  graph-less source's reproject that the original run never had.
- **D4.6 — kind matching is namespace-tolerant, found live, not in
  design.** Telethon's `to_dict()` prefixes some RPC-result *envelope*
  types with their TL namespace (`contacts.resolvedPeer` for `ResolvedPeer`,
  `messages.chatFull` for `ChatFull`, `updates.channelDifference*` for
  `ChannelDifference*`) but not others (`Message`, `ChatInvite*`,
  `SponsoredMessage`, `MediaDownload`, `User`, ...). §3's method table
  implicitly assumed one consistent shape; every kind lookup in `replay.py`
  now matches a bare kind or any `<namespace>.<kind>` suffix
  (`_kind_clause`). Undetected against the hand-authored fixtures (which
  happened to use the real casing already) — only surfaced running
  `reproject` against the actual `default` archive, which is exactly why
  §9's real-archive smoke step exists as part of DoD, not just the fixture
  suite.
- **D4.7 — one bad historical target must not abort the whole run.** Not
  anticipated by §3/§7 at all: a source can carry raws from more than one
  historically-resolved target (successive live `collect` invocations), and
  `collect_channel` was designed for exactly one target per call — it has no
  notion of "this target, among several, turned out bad" (e.g. one later
  resolving to a non-channel peer, which crashes `channel.collect` with a
  bare `ValueError` even on a live run). Found on the real archive: a stray
  historical target crashed the *entire* multi-target reproject after the
  primary channel's full phase set had already completed and committed.
  Fixed with a per-target `try/except` in `reproject()` that logs the
  failure and records it as that target's own failed result, leaving every
  other target's already-committed projections intact. The underlying
  `channel.py` crash-instead-of-skip behavior is orthogonal to reproject (a
  live-collect defect this smoke test happened to surface) and is tracked
  separately as issue #34; the source-wide-not-per-target phase-detection
  scope this multi-target case also exposed (one source, two spellings of
  the same channel, redundantly reprojected twice) is tracked as issue #35.

**D5 — round-trip equality contract.** All of `raw_records`, `channels`,
`channel_snapshots`, `peers`, `messages`, `message_revisions`,
`message_metrics`, `message_tombstones`, `edges`, `media`, `custody_log`,
`web_snapshots` compared as **sets of distinct rows** after dropping
autoincrement pk columns and `source_raw_id` (`source_raw_id` is such an id
transitively — the referenced record's *content* round-trips via the
`raw_records` comparison itself). Set, not multiset: a message delivered by
both `getHistory` and `getChannelDifference` is legitimately served twice on
replay, producing byte-identical duplicate raw/metrics rows — real signal
(two independent observations), not a bug to dedupe away. `run_events`,
`sync_state`, `sync_ranges`, `flood_log`, `schema_migrations`,
`messages_fts` are excluded as operational/bookkeeping, not projections of
raw content. Verified live against the real `default` archive
(`docs/features/reproject.md`): source `channels.flags_json` (10 flags,
pre-#20-fix) reprojects to the current 48-flag set; the source's one
leftover self peer row (pre-#12-fix) reprojects to zero.

## 11. Revision 2 (2026-08-26): run structure — replay per collect pass (ADR-0005)

The first implementation round failed its review gate (#33): a source built
from **several historical collect runs** — the ordinary shape, the real
`default` archive has seven — cannot be faithfully replayed by a one-run
model, because the projection is a function of the raw log *plus the run
structure*, which §2–§3 above never recorded. The review loop showed
negative convergence against hand-written compensation ("backfill older
observations" outside the collectors); the design fix is to record the
missing datum and delete the compensation. Full rationale and options:
`docs/adr/0005-run-structure.md`.

This section **supersedes** the affected wording above:

- **§2/§3 (one replay pass):** `reproject` now replays **once per historical
  run**, in capture order. Per run: that run's targets (its `ResolvedPeer`
  records), that run's detected phase set, and a gateway whose every query is
  scoped to that run's rowid range. `_latest()`-style "serve the newest
  record" semantics apply *within one run* (where each call site has at most
  one record — a live RPC's one "now"), never across runs. The target store
  carries `sync_state`/projections across replayed runs exactly as the live
  store did across real runs, so incremental backfills, repeat snapshots,
  `first_seen`/`last_seen` evolution, and per-run web captures reproduce by
  construction.
- **Schema:** `raw_records.run_id TEXT` (migration `0003_run_id.sql`),
  stamped by `Store.begin_run()` per `collect_channel` invocation. Replay
  passes the source run's id through, so a reprojected DB is itself
  re-reprojectable. Legacy rows (NULL) are segmented at replay time by the
  `tier='self'` User marker — every collect pass's first raw write — labeled
  `legacy-0001…`; the source is never mutated.
- **§5 unchanged** (the clock seam is orthogonal and stands), but
  `upsert_peer`/`upsert_channel` additionally become order-independent
  (`first_seen = MIN`, `last_seen = MAX`, current-state fields only from an
  observation ≥ the stored `last_seen`) — a live-collect correctness fix in
  its own right, not a replay workaround.
- **§7 (round-trip):** the identity contract now explicitly covers a
  **two-run source** (two full collect passes at distinct times into one DB,
  then reproject) — this fixture is the convergence gate the first round
  lacked, and is written red-first.
- **D4.7's per-target error isolation** stays; #34's underlying
  crash-instead-of-skip (`_resolved_channel_id` raising a bare `ValueError`
  for a non-channel resolution) is fixed in this revision as `SkipAndRecord`
  (it is on this feature's replay path for the real archive: `@atom8388`).
- **Known residual (narrowed #36):** a historical run whose media phase
  downloaded nothing new leaves no raw trace, so per-run phase detection
  (D4.5, conservative by design) cannot replay its duplicate-custody rows.
