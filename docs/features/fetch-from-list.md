# `fetch-media` — download media for an ordered cross-channel list (#68)

Spec: `docs/superpowers/specs/2026-09-28-media-list-fetch-design.md` (§9 is
the by-id amendment and supersedes §3.3/§4). Plans:
`docs/superpowers/plans/2026-09-29-media-list-fetch.md` (first design, handle
based) and `docs/superpowers/plans/2026-10-01-media-list-fetch-by-id.md` (the
by-id delta). Shared constraints and
the live smoke protocol: `docs/superpowers/specs/2026-09-28-media-storage-overview.md`.
Plain-language walk-through: `docs/how-it-works.md` §6. **No migration** (schema
unchanged); two recipe-written raw kinds are new (see "Replay"). Builds on #84
(collect by channel id).

## Purpose

Hand paperboy a prioritised list of messages, possibly spanning many channels,
and have it fetch their media **in list order**, skip whatever is already
stored, survive interruption with a cheap resume, and say what happened to
every row.

```
paperboy fetch-media LIST [--profile P] [--media-max-mb N] [--media-min-free-gb G]
                          [--report OUT.csv] [--dry-run] [--unsafe]
                          [--exclude-target T ...]
                          [--max-rpc N] [--pacing-factor F] [--max-flood-sleep S]
```

## Input

* **CSV** whose header contains `uri`. Each `uri` is `tg:msg:<channel_id>/<msg_id>`,
  `https://t.me/c/<channel_id>/<msg_id>` or `https://t.me/<username>/<msg_id>`
  (scheme optional). Other columns are ignored, except an optional `priority`,
  used only as an opaque grouping label (never interpreted or sorted).
* **Plain text**, one entry per line, same three forms; blank lines and `#`
  comments ignored.
* Row order is fetch order. A repeated message keeps its first occurrence; later
  ones are `duplicate_row` (also when a `t.me/<username>/<id>` link and a
  `tg:msg:` uri name the same message).
* Any malformed row fails the whole command **before the store is opened**, with
  every offending line number.

Username links resolve **offline** through `channels.username`; a username the
store never saw is `not_in_store`. Nothing is ever looked up on Telegram by
handle for a list row: the channel id is the address (see "Segments and Step A").

`--exclude-target T` (repeatable; the forms `reproject --exclude-target` takes:
a handle, `123`, `-100123`, `t.me/c/123`) marks every row of that channel
`excluded`, offline, with no network. A channel's linked discussion group
follows its parent (via the stored `linked_group` edge). It is a guard for
running against a store that still holds an investigation the operator does not
want pulled. A target the store has never seen, or one that is not a channel
handle/id, exits 1 before anything else is done (excluding nothing by accident
would download what was meant to be kept out).

## Media store (#63)

`--media-store gs://<bucket>/<prefix>` (or `PAPERBOY_MEDIA_STORE`; the bucket
must be listed in `PAPERBOY_MEDIA_STORE_BUCKETS`) sends the pull to a bucket
with no local copy; see `media-stores.md`. The classification above is then
per store: a custody row naming the bucket answers offline, otherwise one
metadata GET per candidate (`MediaStore.exists`). **A bucket `--dry-run`
therefore touches GCS (read-only metadata GETs, needs Application Default
Credentials), never Telegram.** An unreachable bucket exits 1. `--media-store`
is validated before anything runs; an unlisted bucket is a one-line config
error (exit 1).

## Outcomes

Offline classification (no network), first match wins:

| Outcome | Meaning |
|---|---|
| `duplicate_row` | An earlier row already names this message. |
| `excluded` | The row's channel (or the linked group of one) is named by `--exclude-target`. Tested right after `duplicate_row`, so it wins over every later label, `already_stored` included. |
| `not_in_store` | No `messages` row (or an unknown username). |
| `deleted` | The message is tombstoned (`deleted_at`); the media collector never selects these. |
| `no_media` | The message has no downloadable media (photo/document only). |
| `already_stored` | THIS run's media store (#63) already holds the file: the Telegram document/photo id (anywhere in the store, not only this channel) or this message resolves to a stored file, and a `custody_log` row names the run's store, or the store answers an existence check. Reposts share content ids, so this count can exceed the catalogue's own "already downloaded" flag. A file the database holds from a LOCAL run is `pending` for a bucket run until the bucket holds it. |
| `pending` | To fetch. |

After fetching, a `pending` row becomes one of the collector's outcomes:
`downloaded`, `duplicate` (same bytes/content id as a stored file; custody
recorded), `too_large` (`--media-max-mb`), `size_mismatch`, `unavailable`,
`skipped` (per-file skip, e.g. an expired file reference), `no_access` (the
channel could not be reached at run time, below), or `not_attempted` (the
command stopped first). `excluded`, `no_media`, `no_access`, `too_large`, ...
are final; only `not_attempted` makes the exit code 1.

There is no `unresolvable`: a channel no longer needs a stored username (a
linked discussion group is fetched like any other channel, by id).

## Segments and Step A (by channel id)

Pending rows are grouped by `(priority, channel_id)` in order of first
appearance of the pair, so every P1 segment runs before any P2 segment (without
a `priority` column: one segment per channel). Within a segment the collector
fetches by ascending message id (`media_msgs` is a set); list order decides only
the order of segments.

Each segment is one ordinary `collect_channel` run over the **standard**
`ChannelCollector` and `MediaCollector`, with the **id target** `<channel_id>`
(from the list row) and `media_msgs` narrowed to the segment's ids. `channel`
therefore reaches the channel through #84's Step A (saved key, then a message
that referenced it, then a verified stored handle; see
`docs/features/collect-channel.md`), records a `ChannelAccess` receipt, and
refuses an answer for any other id (`full_chat.id == requested id`). A handle
that has changed hands cannot redirect a segment, and no wrapper collector
exists: **there is no fetch-media-only collector in the live or the replay
list**, so the two cannot drift. Budget, guardrails, dedup, custody, streaming,
`--media-max-mb` and the free-disk floor apply unchanged. One gateway (one
MTProto session, one `Budget`) serves the whole command, so `--max-rpc` bounds it
all.

A channel is established once per command: later segments of it receive the
cached `ChannelContext` (in-process only, never persisted) and skip `channel`,
appending one `ChannelContextReused` marker instead. With a saved key (the
usual case) a list run makes no `contacts.resolveUsername` call at all.

Why identity holds: the only input that shapes which channel a segment fetches
from is its id, and that id is in the raw log twice over (the `ChannelAccess`
receipt and the `MediaSelection` below). The old design's input, "the handle
must still be channel N", lived only in memory.
## Stop policy

* `channel` phase **skip** (Step A found no route, every route was refused, the
  channel is private): the segment's rows **and every later segment of that
  channel** are `no_access` (reason in the report's `reason` column), one
  WARNING per channel carries the reason (#84's route message), and the command
  continues with other channels. Later segments are not retried: the routes
  come from the store, which does not change within one command.
* **Any** `PhaseStop` in the `channel` phase (e.g. FLOOD_WAIT on
  `contacts.resolveUsername`) or the `media` phase (free-disk floor, a FLOOD_WAIT over
  `--max-flood-sleep`, a sink write error, repeated failures) or a `HardStop`
  **ends the command**. Reason: a persisted flood cooldown is slept
  unconditionally by the next RPC (`Budget._pace`), which would defeat
  `--max-flood-sleep` on the next channel, and disk/sink errors are not
  channel-specific.
* `media_since` (a collect-era window) is ignored: the explicit list is the
  selection, so no list row is silently filtered out.
* Unreached rows are `not_attempted`; exit 1; the report is written in every case
  (`try/finally`). Re-running resumes: finished rows classify `already_stored`.
* The doctor preflight runs once (skipped by `--unsafe`); a block exits 1 before
  any segment, with a report of all-`not_attempted`.

## Report

`--report` (default `<data_dir>/<profile>/fetch-media-<YYYYmmddTHHMMSSZ>.csv`),
opened for writing **before** any segment (an unwritable path fails immediately),
rewritten at the end: `line_no,uri,outcome,sha256,key,reason` for every input row
in list order. `reason` is empty except for `no_access` (why the channel could
not be reached) and `excluded`. `uri` is normalised to `tg:msg:<channel_id>/<msg_id>` where resolvable.
`sha256`/`key` (the profile-relative media key, ADR-0007) are filled for
`downloaded`, `duplicate` and `already_stored` rows.

`--dry-run` runs parse and classify only (no keychain, no gateway, no doctor, no
report) and prints three tables: outcome / rows / declared GB; channel **id** x
outcome (what each channel would cost, before any network call); and the segment
plan (priority, channel **id**, rows, declared GB). A list with **zero pending rows**
(e.g. a re-run) builds no gateway either. Exit code: 0 iff no row is
`not_attempted`.

## Replay (reproject)

Each segment is a normal run, with two additions so `reproject` reproduces it
(see `docs/features/reproject.md`, "Replaying `fetch-media` runs", and ADR-0005):

* `ChannelContextReused` `{channel_id, source_run_id}` (no access hash) opens a
  segment that reused a channel; replay runs it as media-only, rebuilding the
  context from the source run's `ChatFull` (which carries the channel object
  whichever route got the run in; a first segment that took route 1 has no
  `ResolvedPeer`).
* `MediaSelection` `{channel_id, msg_ids}` is written whenever a media phase is
  scoped with `media_msgs`, **just before the media phase runs** and only if the
  channel was established; replay walks only those ids. Pre-amendment runs carry
  `{msg_ids}` only and replay as before (the replayed record is minted in the
  current shape).
* Both are paperboy-authored, not gateway responses, and the recipe may write
  them between gateway calls. `ReplayClock.pin_json` keeps their stored
  `observed_at` across the re-batching every served response does.

**Rule (spec 9.3): replay what executed, not what was intended.** `media`
replays for a run iff it has `MediaDownload` rows, or its `MediaSelection` names
a channel the run established (a `ChatFull` for that id, which a granted
`ChannelAccess` always precedes). So: access granted but zero files downloaded
(every row a dedup) still replays media and re-derives the custody rows; access
refused (no route, or a handle that now answers for another channel) replays
channel only and leaves no media or custody rows, exactly as live.

## Deviations from the plan / spec

* By-id amendment (spec §9, orchestrator decisions of 2026-10-01): the
  `_ExpectChannel` guard, `Segment.username`, `resolve_by_target` and the
  `unresolvable` outcome are gone; `excluded` and `no_access` are new; the
  report gained a `reason` column; `MediaSelection` moved from "before any
  phase" to "just before media" and names the channel, which needed
  `ReplayClock.pin_json`.
* Per-row outcomes are a caller-owned `MediaCollector(outcomes=dict)` instead of
  `CollectResult.outcomes` (a `DiskFloorStop`/`HardStop` discards the result, yet
  the report needs the rows finished before the stop). Orchestrator decision 3.
* `CollectResult.stop_exc` carries the exception behind a stop, so the driver
  can name it.
* `deleted` is a sixth offline outcome (orchestrator decision 4).
* The `MediaSelection` raw and the `history`-evidence rule in `detect_phases` are
  additions the plan did not foresee (see "Replay").
* Stop policy is broader than the spec's (orchestrator decision 1).

## Known limitations

* A channel for which Step A finds no route and has nothing to try (route 4,
  nothing tried) writes no `ChannelAccess`, so that segment's run holds only the
  self `User` raw and `reproject` skips it with its "no resolve records"
  WARNING (#84 behaviour). Media and custody are unaffected: the segment
  downloaded nothing.
* Not fixed here, orthogonal (`fetch-media` never runs `history`):
  `has_history_evidence` can read a history phase that left no raw trace as
  never-run. The orchestrator files it as its own issue.
* Within a segment files arrive in id order, not list order.
* A segment whose files are all duplicates leaves no `MediaDownload` raw. Its
  dedup custody rows are still reproduced, because replay walks the recorded
  `MediaSelection` ids and re-derives them; an unscoped `collect --media` run
  is unchanged (the #36 residual there stands).
* A SIGKILL mid-segment leaves no report (media/custody rows persist; `.incoming`
  parts are swept next run; a re-run classifies finished rows `already_stored`).

## Definition of done

Redaction: channels are `@<channel>`, ids `<id>`; unredacted transcripts stay in
`<scratch>/68b/` (referenced by filename only). The operator's list, its rows and
channels appear nowhere here. The by-id amendment (spec §9) superseded the
earlier design, so every transcript below was re-run on this branch.

### Gates (pasted output)

```
$ uv run pytest -q
1013 passed in 201.62s (0:03:21)
$ uv run ruff check
All checks passed!
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

### Smoke test transcript

**Offline, synthetic list**, with `--exclude-target 20` to show the new label
(`tests/fixtures/media_list/synthetic.csv` against a synthetic two-channel store;
no gateway, no keychain, no network; the CLI tests assert both constructors are
never called):

```
  fetch-media: offline classification  
┏━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ outcome        ┃ rows ┃ declared GB ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ duplicate_row  │    1 │        0.00 │
│ excluded       │    2 │        0.00 │
│ not_in_store   │    1 │        0.00 │
│ deleted        │    0 │        0.00 │
│ no_media       │    0 │        0.00 │
│ already_stored │    0 │        0.00 │
│ pending        │    3 │        0.00 │
│ total          │    7 │        0.00 │
└────────────────┴──────┴─────────────┘
                fetch-media: per channel (rows by outcome)                
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┓
┃ channel id ┃ duplicate_row ┃ excluded ┃ not_in_store ┃ pending ┃ total ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━┩
│         10 │             1 │        0 │            1 │       3 │     5 │
│         20 │             0 │        2 │            0 │       0 │     2 │
└────────────┴───────────────┴──────────┴──────────────┴─────────┴───────┘
         fetch-media: segment plan (list order)         
│       1 │ P1       │         10 │    1 │        0.00 │
│       2 │ P2       │         10 │    2 │        0.00 │
```

**Offline, the operator's full list on a `sqlite3 .backup` copy** (`<list>`, 1,719
rows; `PAPERBOY_DATA_DIR=<scratch>/68b paperboy fetch-media <list> --dry-run
--profile default --exclude-target @<channel>`; exit 0; unredacted output in
`full-dryrun.unredacted.txt`). The log says `excluding 2 channel id(s)` (the
excluded channel and its linked group):

```
┃ outcome        ┃ rows ┃ declared GB ┃
│ duplicate_row  │    0 │        0.00 │
│ excluded       │   40 │        0.00 │
│ not_in_store   │    0 │        0.00 │
│ deleted        │    0 │        0.00 │
│ no_media       │    0 │        0.00 │
│ already_stored │   99 │       12.36 │
│ pending        │ 1580 │      149.80 │
│ total          │ 1719 │      162.17 │
```

The per-channel table has 11 rows (ids redacted; one is the excluded linked group
with `excluded` 40, 0 pending; the other ten have 2 to 800 rows each, `pending`
summing to 1580). The excluded channel itself has no row on the list, so
`excluded` is the 40 linked-group rows only. `already_stored` is 99, equal to the
catalogue's own flag (the plan's ~118 estimate does not hold against the store).
`grep -c "pacing:"` on the unredacted output is 0: no gateway was built. Network
absence is by construction: `cli.py` returns after `_print_plan` when `dry_run`,
before `_run_fetch` (`build_secrets`, `build_gateway`, doctor);
`excluded_channel_ids` and `classify_rows` are store queries;
`test_dry_run_builds_no_gateway_and_no_secrets` asserts it. Without
`--exclude-target` the same list reads `excluded` 0, `already_stored` 131,
`pending` 1588: the 40 rows that the superseded design called `unresolvable` are
now reachable by id.

**Live, 3-row slice** (1 photo + 2 videos from 2 channels already in the store,
each with a saved key, 0.9 MB in total, chosen offline and copied to
`<scratch>/68b/list-smoke.csv`; `--exclude-target @<channel>` on every command
because the real store is not yet split). Dry run: `pending` 3, 2 segments (PH 1
row, P1 2 rows). Before each live invocation: `STOP-LIVE` absent, live-call
counter 0 then 1 (of 5), and

```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```

Invocation 1: `PAPERBOY_REQUIRE_PROXY=false paperboy fetch-media <scratch>/68b/list-smoke.csv --profile default --exclude-target @<channel> --max-rpc 60 --max-flood-sleep 60 --report <scratch>/68b/report-1.csv`
(exit 0, 16 RPCs of the 60 cap, no FLOOD_WAIT, **no `resolve`**). Log excerpt:

```
INFO  fetch-media: segment 1/2 priority=PH channel=<id> rows=1 start
DEBUG rpc channels.getFullChannel attempt 1 (run call #8)
INFO  channel access: id=<id> via=saved_key granted=True error=None
INFO  fetch-media: segment 1/2 priority=PH channel=<id> rows=1 end: {'downloaded': 1}
INFO  fetch-media: segment 2/2 priority=P1 channel=<id> rows=2 start
INFO  channel access: id=<id> via=saved_key granted=True error=None
INFO  fetch-media: segment 2/2 priority=P1 channel=<id> rows=2 end: {'downloaded': 2}
INFO  fetch-media: {'downloaded': 3}; 883319 bytes downloaded; report report-1.csv
```

Report (`report-1.csv`, ids and hashes abbreviated):

```
line_no,uri,outcome,sha256,key,reason
2,tg:msg:<id>/<id>,downloaded,45f4f24f...7112,media/45/45f4f24f....jpg,
3,tg:msg:<id>/<id>,downloaded,5c837795...ce62,media/5c/5c837795....mp4,
4,tg:msg:<id>/<id>,downloaded,df805cf4...ab80,media/df/df805cf4....mp4,
```

- Raw kinds of the two fetch runs: `ChannelAccess` 2, `ChatFull` 2,
  `MediaSelection` 2 (one per segment), `MediaDownload` 3, `User` 2;
  `ChannelContextReused` 0 (each channel had a single segment, so the reuse
  path is covered by `tests/test_reproject_fetch_media.py`, not by this smoke).
- `MediaSelection` rows with `json_extract(payload_json,'$.channel_id') IS NULL`: 0.
- `ls -A <scratch>/68b/default/media/.incoming | wc -l` = 0.
- `shasum -a 256` of the third file equals its `media.sha256`
  (`df805cf4...ab80`).
- One `pacing:` line (`factor=2.0 default=2.0s`) in this run, where a gateway is built.

Invocation 2 (same command, `--report <scratch>/68b/report-2.csv`, counter 1 then
2 of 5, VPN check repeated, both `utun4`): exit 0, no `pacing:` line, no RPC;
`{'already_stored': 3}; 0 bytes downloaded`, `report-2.csv` has 3 rows, all
`already_stored`.

**Reproject equality** (offline; `PAPERBOY_DATA_DIR=<scratch>/68b paperboy reproject
--profile default --include-target <id> --include-target <id>`, the two smoke
channels; 41 other (run, target) pairs `decision=excluded`, 21 `decision=included`;
about 28 minutes on the 600 MB copy). The scratch media directory holds only the
three smoke files, so the older included runs' media rows replayed as
`SkipAndRecord` "media file missing" (213 log lines): expected. Source vs
`paperboy.reprojected.sqlite` for the three fetched uris:

```
media rows:    source 3, reprojected 3   -> (message_uri, sha256, path) equal: True
custody rows:  source 3, reprojected 3   -> equal: True
raw kinds of the two fetch runs (ChannelAccess 2, ChatFull 2, MediaDownload 3,
  MediaSelection 2, User 2): identical in both stores
```

(`<scratch>/68b/compare.py`; the full-store reproject was not run, per the
scoped-run decision.) Totals: 2 of 5 live invocations, 3 files, 0.9 MB, far under
the 3 GB cap. No STOP condition was hit; `STOP-LIVE` was never created.

Parity for the cases a live smoke cannot reach (a refused channel, a
granted-but-zero-download segment, a legacy selection) is pinned by
`tests/test_reproject_fetch_media.py`.

### Docs updated

`docs/features/fetch-media.md`, `README.md` (command row, documentation list),
`CLAUDE.md` (status line), `docs/how-it-works.md` §6, `docs/features/reproject.md`,
`docs/features/collect-channel.md`, `docs/adr/0005-run-structure.md`,
`docs/data-model.md` (`ChannelContextReused`, `MediaSelection` payloads; no
schema change), `docs/superpowers/plans/2026-10-01-media-list-fetch-by-id.md`.
