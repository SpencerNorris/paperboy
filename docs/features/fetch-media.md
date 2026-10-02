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

## Outcomes

Offline classification (no network), first match wins:

| Outcome | Meaning |
|---|---|
| `duplicate_row` | An earlier row already names this message. |
| `excluded` | The row's channel (or the linked group of one) is named by `--exclude-target`. Tested right after `duplicate_row`, so it wins over every later label, `already_stored` included. |
| `not_in_store` | No `messages` row (or an unknown username). |
| `deleted` | The message is tombstoned (`deleted_at`); the media collector never selects these. |
| `no_media` | The message has no downloadable media (photo/document only). |
| `already_stored` | The Telegram document/photo id is already in `media` (anywhere in the store, not only this channel), or this message already has a `media` / `custody_log` row. Reposts share content ids, so this count can exceed the catalogue's own "already downloaded" flag. |
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
`<scratch>/68/`. The operator's list, its rows and channels appear nowhere here.

### Gates (pasted output)

```
$ uv run pytest -q
933 passed in 77.40s (0:01:17)
$ uv run ruff check
All checks passed!
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

### Smoke test transcript

**Offline, synthetic list** (`tests/fixtures/media_list/synthetic.csv` against a
synthetic two-channel store; no gateway, no keychain, no network; the CLI tests
assert both constructors are never called). Command:
`PAPERBOY_DATA_DIR=<scratch>/68/synthetic paperboy fetch-media tests/fixtures/media_list/synthetic.csv --dry-run`

```
  fetch-media: offline classification  
┏━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ outcome        ┃ rows ┃ declared GB ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ duplicate_row  │    1 │        0.00 │
│ not_in_store   │    1 │        0.00 │
│ deleted        │    0 │        0.00 │
│ no_media       │    0 │        0.00 │
│ unresolvable   │    0 │        0.00 │
│ already_stored │    0 │        0.00 │
│ pending        │    5 │        0.00 │
│ total          │    7 │        0.00 │
└────────────────┴──────┴─────────────┘
         fetch-media: segment plan (list order)         
┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ segment ┃ priority ┃ channel id ┃ rows ┃ declared GB ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│       1 │ P1       │         10 │    1 │        0.00 │
│       2 │ P1       │         20 │    1 │        0.00 │
│       3 │ P2       │         10 │    2 │        0.00 │
│       4 │ P2       │         20 │    1 │        0.00 │
└─────────┴──────────┴────────────┴──────┴─────────────┘
```

No `pacing:` line appears (no gateway was built).

**Offline, the operator's full list on a `sqlite3 .backup` copy** (`<list>`, 1,719
rows; `PAPERBOY_DATA_DIR=<scratch>/68 paperboy fetch-media <list> --dry-run --profile default`;
exit 0; ids redacted; unredacted output in `<scratch>/68/full-dryrun.unredacted.txt`):

```
[..] WARNING  fetch-media: channel <id> has no stored username and cannot be
              resolved; its rows are reported unresolvable
  fetch-media: offline classification
┏━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ outcome        ┃ rows ┃ declared GB ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ duplicate_row  │    0 │        0.00 │
│ not_in_store   │    0 │        0.00 │
│ deleted        │    0 │        0.00 │
│ no_media       │    0 │        0.00 │
│ unresolvable   │   40 │        0.05 │
│ already_stored │   99 │       12.36 │
│ pending        │ 1580 │      149.80 │
│ total          │ 1719 │      162.22 │
└────────────────┴──────┴─────────────┘
```

The segment plan has 36 segments (P1 x9, P2 x8, P3 x9, PH x10; every row of the
`pending` count is in exactly one segment; the largest is 294 rows). The single
channel with no stored username (its 40 rows) is the linked group that was split
out into a separate investigation: it is reported `unresolvable` and never
fetched. `already_stored` is 99, equal to the catalogue's own "already
downloaded" flag on this store (the plan's earlier 118 estimate does not hold
against the actual store). `grep -c "pacing:"` on the unredacted output is 0: no
gateway was built.

**Live, 3-row slice** (photo + 2 videos, 2 channels already in the store, each
<= 20 MB, chosen offline from `<list>` and copied to `<scratch>/68/list-smoke.csv`).
Its `--dry-run` reports `pending` 3, `already_stored` 0, 2 segments (PH 1 row, P1 2
rows), 0.02 GB declared. Before each live invocation: `STOP-LIVE` absent,
live-call counter 0 then 1 (of 5), and

```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```

Invocation 1: `PAPERBOY_REQUIRE_PROXY=false paperboy fetch-media <scratch>/68/list-smoke.csv --profile default --max-rpc 60 --max-flood-sleep 60 --report <scratch>/68/report-1.csv`
(exit 0, 18 RPCs of the 60 cap, no FLOOD_WAIT). Segment log excerpt:

```
fetch-media: segment 1/2 priority=PH channel=<id> rows=1 end: {'downloaded': 1}
fetch-media: segment 2/2 priority=P1 channel=<id> rows=2 end: {'downloaded': 2}
fetch-media: {'downloaded': 3}; <bytes> bytes downloaded; report report-1.csv
      fetch-media: result
┃ outcome          ┃  rows ┃
│ downloaded       │     3 │
│ bytes downloaded │ <n>   │
```

Report (`report-1.csv`, URIs redacted, sha256/key abbreviated):

```
line_no,uri,outcome,sha256,key
2,tg:msg:<id>/<id>,downloaded,45f4f24f...7112,media/45/45f4f24f....jpg
3,tg:msg:<id>/<id>,downloaded,79d8c006...,media/79/79d8c006....mp4
4,tg:msg:<id>/<id>,downloaded,c46d95eb...78bb,media/c4/c46d95eb....mp4
```

- `SELECT count(*) FROM raw_records WHERE kind='ChannelContextReused'` = 0. Each
  channel had a single segment, so no channel was resolved twice and no marker was
  needed (the marker appears only when a channel spans several segments; that path is
  covered by `tests/test_recipe_context.py` and `tests/test_reproject_fetch_media.py`).
- `ls -A <scratch>/68/default/media/.incoming | wc -l` = 0.
- `shasum -a 256` of the third file =
  `c46d95eb04b4decfb2e392fb1277a96026e668fbe297674970e5e6b02b4578bb`, equal to its
  `media.sha256`.
- The single `pacing:` line (`factor=2.0 default=2.0s`) appears in this run, where
  a gateway is built.

Invocation 2 (same command, `--report <scratch>/68/report-2.csv`, counter 1 then 2
of 5, VPN check repeated and both `utun4`): exit 0, no `pacing:` line (no gateway
built), no RPC.

```
| already_stored   |    3 |      (offline classification: pending 0)
| bytes downloaded |    0 |
report-2.csv: 3 rows, all already_stored
```

Totals: 2 of 5 live invocations, 3 files, well under the 3 GB cap. No STOP
condition was hit; `STOP-LIVE` was never created.

### Docs updated

`docs/features/fetch-media.md` (new), `README.md` (command row, data layout,
documentation list), `CLAUDE.md` (commands, status line), `docs/how-it-works.md`
§6, `docs/features/pacing.md`, `docs/features/reproject.md`,
`docs/adr/0005-run-structure.md`, `docs/data-model.md` (two raw kinds; no
schema change), `docs/superpowers/plans/2026-09-29-media-list-fetch.md`.
