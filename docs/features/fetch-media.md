# `fetch-media` — download media for an ordered cross-channel list (#68)

Spec: `docs/superpowers/specs/2026-09-28-media-list-fetch-design.md`. Plan:
`docs/superpowers/plans/2026-09-29-media-list-fetch.md`. Shared constraints and
the live smoke protocol: `docs/superpowers/specs/2026-09-28-media-storage-overview.md`.
Plain-language walk-through: `docs/how-it-works.md` §6. **No migration** (schema
unchanged); two recipe-written raw kinds are new (see "Replay").

## Purpose

Hand paperboy a prioritised list of messages, possibly spanning many channels,
and have it fetch their media **in list order**, skip whatever is already
stored, survive interruption with a cheap resume, and say what happened to
every row.

```
paperboy fetch-media LIST [--profile P] [--media-max-mb N] [--media-min-free-gb G]
                          [--report OUT.csv] [--dry-run] [--unsafe]
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
store never saw is `not_in_store`.

## Outcomes

Offline classification (no network), first match wins:

| Outcome | Meaning |
|---|---|
| `duplicate_row` | An earlier row already names this message. |
| `not_in_store` | No `messages` row (or an unknown username). |
| `deleted` | The message is tombstoned (`deleted_at`); the media collector never selects these. |
| `no_media` | The message has no downloadable media (photo/document only). |
| `unresolvable` | The channel has no stored username (e.g. a linked discussion group): paperboy does not persist access hashes, so it can only reach a channel by resolving its username this run. Reported once per channel as a WARNING. |
| `already_stored` | The Telegram document/photo id is already in `media` (anywhere in the store, not only this channel), or this message already has a `media` / `custody_log` row. Reposts share content ids, so this count can exceed the catalogue's own "already downloaded" flag. |
| `pending` | To fetch. |

After fetching, a `pending` row becomes one of the collector's outcomes:
`downloaded`, `duplicate` (same bytes/content id as a stored file; custody
recorded), `too_large` (`--media-max-mb`), `size_mismatch`, `unavailable`,
`skipped` (per-file skip, e.g. an expired file reference), or `not_attempted`
(the command stopped first). `unresolvable`, `no_media`, `too_large`, ... are
final; only `not_attempted` makes the exit code 1.

Classification order is deliberate: `unresolvable` is tested before
`already_stored`, so a linked-group row reads `unresolvable` even if its file is
stored.

## Segments and the one-resolve rule

Pending rows are grouped by `(priority, channel_id)` in order of first
appearance of the pair, so every P1 segment runs before any P2 segment (without
a `priority` column: one segment per channel). Within a segment the collector
fetches by ascending message id (`media_msgs` is a set); list order decides only
the order of segments.

Each segment is one ordinary `collect_channel` run over `channel` + `media`, with
`media_msgs` narrowed to the segment's ids. Budget, guardrails, dedup, custody,
streaming, `--media-max-mb` and the free-disk floor therefore apply unchanged. One
gateway (one MTProto session, one `Budget`) serves the whole command, so
`--max-rpc` bounds it all. `contacts.resolveUsername` (5 s base, 10 s at the
default `--pacing-factor 2`) runs **once per channel per command**: the first
segment of a channel runs `channel`; later ones receive the cached
`ChannelContext` (in-process only, never persisted) and skip it. A resolve that
answers with a *different* channel id than the segment's is treated as a channel
stop, never fetched under the wrong id.

## Stop policy

* `channel` phase skip/stop (private, renamed handle, ...): that channel's
  remaining rows stay `not_attempted`, WARNING once, the command continues with
  other channels.
* **Any** `media`-phase `PhaseStop` (free-disk floor, a FLOOD_WAIT over
  `--max-flood-sleep`, a sink write error, repeated failures) or a `HardStop`
  **ends the command**. Reason: a persisted flood cooldown is slept
  unconditionally by the next RPC (`Budget._pace`), which would defeat
  `--max-flood-sleep` on the next channel, and disk/sink errors are not
  channel-specific.
* Unreached rows are `not_attempted`; exit 1; the report is written in every case
  (`try/finally`). Re-running resumes: finished rows classify `already_stored`.
* The doctor preflight runs once (skipped by `--unsafe`); a block exits 1 before
  any segment, with a report of all-`not_attempted`.

## Report

`--report` (default `<data_dir>/<profile>/fetch-media-<YYYYmmddTHHMMSSZ>.csv`),
opened for writing **before** any segment (an unwritable path fails immediately),
rewritten at the end: `line_no,uri,outcome,sha256,key` for every input row in list
order. `uri` is normalised to `tg:msg:<channel_id>/<msg_id>` where resolvable.
`sha256`/`key` (the profile-relative media key, ADR-0007) are filled for
`downloaded`, `duplicate` and `already_stored` rows.

`--dry-run` runs parse and classify only (no keychain, no gateway, no doctor, no
report) and prints two tables: outcome / rows / declared GB, and the segment plan
(priority, channel **id**, rows, declared GB). A list with **zero pending rows**
(e.g. a re-run) builds no gateway either. Exit code: 0 iff no row is
`not_attempted`.

## Replay (reproject)

Each segment is a normal run, with two additions so `reproject` reproduces it
(see `docs/features/reproject.md`, "Replaying `fetch-media` runs", and ADR-0005):

* `ChannelContextReused` `{channel_id, source_run_id}` (no access hash) opens a
  segment that reused a channel; replay runs it as media-only.
* `MediaSelection` `{msg_ids}` is written whenever a media phase is scoped with
  `media_msgs`; replay walks only those ids. (Found by the parity test: a first
  segment replayed with `duplicates=2` because the repost of a listed file was
  walked; the live run never considered it.)

## Deviations from the plan / spec

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

* Linked-group rows (no username of their own) are `unresolvable`; resolving them
  through the parent's `getFullChannel` chats vector is out of scope (spec §8).
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
929 passed in 84.02s (0:01:24)
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

**Offline, the operator's full list on the `.backup` copy: PENDING.** The
permission classifier refused the command that runs `--dry-run` over the
operator's private list (reason given: "Sensitive-Source Provenance"), so this
agent did not read that list in any form and did not try another route. Nothing
is claimed about its counts. To close it, the operator runs:
`PAPERBOY_DATA_DIR=<scratch>/68 paperboy fetch-media <list> --dry-run --profile default`
on a `sqlite3 .backup` copy and checks that the outcome table has `unresolvable`
for the split-out investigation's linked group, and no `pacing:` log line.

**Live, 3-row slice and its re-run: PENDING** for the same reason (the smoke slice
is chosen from that list, and the run rules forbid choosing rows any other way).
No live call was made: the live-call counter is 0 of 5, no `STOP-LIVE` flag was
set, and the VPN check was not reached. Not a code failure; the driver, resume
(`already_stored` with no gateway) and report paths are covered offline by
`tests/test_fetch_media.py` and `tests/test_cli_fetch_media.py`.

### Docs updated

`docs/features/fetch-media.md` (new), `README.md` (command row, data layout,
documentation list), `CLAUDE.md` (commands, status line), `docs/how-it-works.md`
§6, `docs/features/pacing.md`, `docs/features/reproject.md`,
`docs/adr/0005-run-structure.md`, `docs/data-model.md` (two raw kinds; no
schema change), `docs/superpowers/plans/2026-09-29-media-list-fetch.md`.
