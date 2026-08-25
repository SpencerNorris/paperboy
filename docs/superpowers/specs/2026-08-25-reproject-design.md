# `reproject` — rebuild projections from the raw log, no network

**Status:** design, 2026-08-25. Realizes the raw-first thesis
(`raw_records` is the system of record; normalized tables are a projection that
can be rebuilt from raw) as an actual command.
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
