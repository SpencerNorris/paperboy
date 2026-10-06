# `fetch-from-list` — fetch the listed posts, then their media (#68, #91)

Specs: `docs/superpowers/specs/2026-10-06-fetch-from-list-design.md` (#91, the
posts step and the rename; authoritative for what changed) and
`docs/superpowers/specs/2026-09-28-media-list-fetch-design.md` (#68; §9 is the
by-id amendment). Plans: `docs/superpowers/plans/2026-10-06-fetch-from-list.md`
(#91), `docs/superpowers/plans/2026-10-01-media-list-fetch-by-id.md` and
`docs/superpowers/plans/2026-09-29-media-list-fetch.md` (#68). Shared
constraints and the live smoke protocol:
`docs/superpowers/specs/2026-09-28-media-storage-overview.md`. Plain-language
walk-through: `docs/how-it-works.md` §6. **No migration** (schema unchanged);
the only raw-log addition in #91 is a context tag (see "Replay"). Builds on #84
(collect by channel id) and #63 (per-run media stores).

The command was named `fetch-media` until #91 (pre-release: no alias).

## Purpose

Hand paperboy a prioritised list of messages, possibly spanning many channels,
and have it fetch **each post first, then its media**, in list order. A post
that was never collected is fetched and stored like any other message; a post
that is already stored is fetched again, so its edits and counters are current as
of this run. Whatever the run's store does not hold yet is downloaded. The
command survives interruption with a cheap resume and says what happened to
every row.

```
paperboy fetch-from-list LIST [--profile P] [--no-media] [--media-max-mb N]
                              [--media-min-free-gb G] [--media-store gs://B/P]
                              [--report OUT.csv] [--dry-run] [--unsafe]
                              [--exclude-target T ...]
                              [--max-rpc N] [--pacing-factor F] [--max-flood-sleep S]
```

`--no-media` fetches and projects the posts only (no media phase, no
`MediaSelection`); `--media-max-mb` and `--media-min-free-gb` are then accepted
and unused.

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

A username link resolves **offline** through `channels.username` when the store
knows the channel; the row is then addressed by id like any other. A username the
store has never seen cannot be addressed by id, so (orchestrator decision,
2026-10-06, spec §2.3) its segment targets the **handle** and the channel is
resolved live through #84's `handle` route: the standard `channel` phase calls
`contacts.resolveUsername`, records a `ChannelAccess` receipt with `via: handle`,
and the rows then carry the resolved `tg:msg:` uri. `--dry-run` cannot do that
lookup (it is offline), so it reports these rows as `needs_resolve`. If two rows
of one list name the same message once by handle and once by id of a channel the
store has never seen, they cannot be matched offline: both are fetched (the post
fetch is idempotent; the media phase would add one `duplicate` custody row).

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
with no local copy; see `media-stores.md`. Whether the media phase still has to
download a row is then per store: a custody row naming the bucket answers
offline, otherwise one metadata GET per candidate (`MediaStore.exists`).
**A bucket `--dry-run` therefore touches GCS (read-only metadata GETs, needs
Application Default Credentials), never Telegram.** An unreachable bucket exits
1. `--media-store` is validated before anything runs; an unlisted bucket is a
one-line config error (exit 1).

## Outcomes

**Offline classification** (no network), first match wins. Since #91 only three
labels are decided offline, because every addressable row has its post fetched:

| Outcome | Meaning |
|---|---|
| `duplicate_row` | An earlier row already names this message. |
| `excluded` | The row's channel (or the linked group of one) is named by `--exclude-target`. Tested right after `duplicate_row`, so it wins over everything else. |
| `pending` | To fetch: its post, then its media unless the run's store holds the file. |

Three flags say what the store already knows about a pending row, and drive the
`--dry-run` tables: `in_store` (a `messages` row exists; otherwise the post is
"not yet collected"), `media_held` (THIS run's media store already holds the
file: the Telegram document/photo id, anywhere in the store, or this message
resolves to a stored file, and a `custody_log` row names the run's store or the
store answers an existence check; a file the database holds from a LOCAL run is
not held by a bucket run until the bucket has it) and `needs_resolve` (a handle
row for a channel the store has never seen, above). A tombstoned row is
pending too: Telegram's answer decides between `deleted_upstream` and a live
post.

**Final outcomes** (the report's `outcome` column), per row, in this
precedence:

| Outcome | Meaning |
|---|---|
| `downloaded`, `duplicate`, `too_large`, `size_mismatch`, `unavailable`, `skipped` | The media phase's own result for the file (`duplicate`: same bytes/content id as a stored file, custody recorded; `too_large`: `--media-max-mb`; `skipped`: a per-file skip such as an expired file reference). |
| `already_stored` | THIS run's media store already held the file (`media_held`); the post was still re-fetched. |
| `deleted_upstream` | Telegram answered `MessageEmpty` for the id: deleted, or never existed (an id above the channel's newest post answers the same). It is projected as a tombstone with evidence `empty`, exactly as `history` does, and no media is attempted. |
| `post_only` | Fetched with `--no-media`. |
| `no_media` | Fetched, but the post has nothing downloadable (not a photo/document). |
| `no_access` | The channel could not be reached at run time (below). |
| `not_attempted` | The command stopped first. |

`excluded`, `no_media`, `no_access`, `too_large`, ... are final; only
`not_attempted` makes the exit code 1. A live post that an earlier run
tombstoned reports `skipped` with a reason (the media phase never selects
tombstoned rows). A row both held and deleted upstream says `already_stored`;
its `post` column says `deleted_upstream`, so both facts are in the report.

There is no `unresolvable` and no `not_in_store`: a channel needs no stored
username (a linked discussion group is fetched like any other channel, by id),
and a post that is not stored is fetched.

## Segments: channel, then posts, then media

Pending rows are grouped by `(priority, channel)` in order of first appearance
of the pair, so every P1 segment runs before any P2 segment (without a
`priority` column: one segment per channel). Within a segment ids are fetched
ascending; list order decides only the order of segments.

Each segment is one ordinary `collect_channel` run over the **standard**
collectors, in this order:

1. **`channel`**, targeting the channel id from the list row (or the handle,
   above). It reaches the channel through #84's Step A (saved key, then a
   message that referenced it, then a verified stored handle; see
   `docs/features/collect-channel.md`), records a `ChannelAccess` receipt, and
   refuses an answer for any other id (`full_chat.id == requested id`). A handle
   that has changed hands cannot redirect an id segment, and no wrapper
   collector exists: **there is no fetch-from-list-only collector in the live or
   the replay list**, so the two cannot drift.
2. **`posts`** (`collectors/posts.py`): `channels.getMessages` for **every**
   listed id of the segment, in batches of at most 100, stored ids included.
   Each answer is appended to the raw log first (context `{channel_id, method:
   "channels.getMessages"}`) and then projected by `observe_message`, the same
   function `history` uses (hoisted out of `HistoryCollector`; `history` and
   `discussion` behave and test exactly as before): a new post gets its message
   row, author peer and forward edge; an edit appends a `message_revisions` row;
   a counter change appends a `message_metrics` row; `MessageEmpty` becomes a
   tombstone, never a blank row. Cost: ⌈ids/100⌉ calls per segment.
3. **`media`**, unless `--no-media` or nothing is left to download, narrowed to
   the segment's ids whose file this run's store does not hold (`media_msgs`).
   Held rows are left out on purpose: walking one would add a `duplicate`
   custody row on every re-run. The media phase selects its rows after `posts`
   ran, so a post fetched in this very run is downloaded in the same run.

Budget, guardrails, dedup, custody, streaming, `--media-max-mb` and the
free-disk floor apply unchanged. One gateway (one MTProto session, one
`Budget`) serves the whole command, so `--max-rpc` bounds it all.

A channel is established once per command: later segments of it receive the
cached `ChannelContext` (in-process only, never persisted) and skip `channel`,
appending one `ChannelContextReused` marker instead. With a saved key (the
usual case) a list run makes no `contacts.resolveUsername` call at all.

Why identity holds: the only input that shapes which channel an id segment
fetches from is its id, and that id is in the raw log twice over (the
`ChannelAccess` receipt and the `MediaSelection` below).

## Stop policy

* `channel` or `posts` phase **skip** (Step A found no route, every route was
  refused, the channel is private, `getMessages` answered `CHANNEL_PRIVATE`):
  the segment's unfinished rows **and every later segment of that channel** are
  `no_access` (reason in the report's `reason` column), one WARNING per channel
  carries the reason, and the command continues with other channels. Later
  segments are not retried: the routes come from the store, which does not
  change within one command. A failed `posts` phase withdraws the channel
  context from the run, so the `media` phase after it stops at its own guard
  without an RPC.
* **Any** `PhaseStop` in `channel`, `posts` (a FLOOD_WAIT over
  `--max-flood-sleep`, reported with the counts of the batches already
  projected) or `media` (free-disk floor, a FLOOD_WAIT over the ceiling, a sink
  write error, repeated failures), or a `HardStop`, **ends the command**.
  Reason: a persisted flood cooldown is slept unconditionally by the next RPC
  (`Budget._pace`), which would defeat `--max-flood-sleep` on the next channel,
  and disk/sink errors are not channel-specific.
* `media_since` (a collect-era window) is ignored: the explicit list is the
  selection, so no list row is silently filtered out.
* Unreached rows are `not_attempted`; exit 1; the report is written in every
  case (`try/finally`). Re-running resumes: posts are fetched again (cheap), and
  media already in the run's store is not downloaded again.
* The doctor preflight runs once (skipped by `--unsafe`); a block exits 1 before
  any segment, with a report of all-`not_attempted`.

## Report

`--report` (default
`<data_dir>/<profile>/fetch-from-list-<YYYYmmddTHHMMSSZ>.csv`), opened for
writing **before** any segment (an unwritable path fails immediately), rewritten
at the end: `line_no,uri,outcome,post,sha256,key,reason` for every input row in
list order. `post` is `fetched`, `deleted_upstream` or `skipped` (not reached, or
the channel was refused). `reason` is empty except for `no_access` (why the
channel could not be reached), `excluded` and the tombstoned `skipped`. `uri` is
normalised to `tg:msg:<channel_id>/<msg_id>` once the channel is known.
`sha256`/`key` (the profile-relative media key, ADR-0007) are filled for
`downloaded`, `duplicate` and `already_stored` rows.

`--dry-run` runs parse and classify only (no keychain, no gateway, no doctor, no
report) and prints three tables: outcome / rows / declared GB (`pending`,
`needs_resolve`, `excluded`, `duplicate_row`); channel **id** x state
(`pending`, of which `in_store` and `not_yet_collected`, `media_stored`,
`needs_resolve`, `excluded`, `duplicate_row`, `total`: what each channel would
cost, before any network call; handle rows sit under `-`); and the segment plan
(priority, channel **id** or `(by handle)`, rows, `posts calls`, declared GB). A
list with **zero pending rows** builds no gateway. Exit code: 0 iff no row is
`not_attempted`.

## Replay (reproject)

Each segment is a normal run, with additions so `reproject` reproduces it (see
`docs/features/reproject.md`, "Replaying `fetch-from-list` runs", and ADR-0005):

* `ChannelContextReused` `{channel_id, source_run_id}` (no access hash) opens a
  segment that reused a channel; replay runs it without a `channel` phase,
  rebuilding the context from the source run's `ChatFull` (which carries the
  channel object whichever route got the run in; a first segment that took route
  1 has no `ResolvedPeer`).
* `MediaSelection` `{channel_id, msg_ids}` is written whenever a media phase is
  scoped with `media_msgs`, **just before the media phase runs** and only if the
  channel was established; replay walks only those ids. Pre-amendment runs carry
  `{msg_ids}` only and replay as before.
* The `posts` receipts (#91) are ordinary message raw records tagged
  `method: "channels.getMessages"`. **No new raw kind.** Replay detects the
  phase from the tag, sets `post_msgs` to the recorded ids, and
  `RawReplayGateway.get_messages` serves them, so the replayed projection
  (messages, revisions, metrics, tombstones, peers, edges) equals the live one.
  The tag also keeps these receipts from reading as `history` evidence.
* `ChannelContextReused` and `MediaSelection` are paperboy-authored, not
  gateway responses, and the recipe may write them between gateway calls.
  `ReplayClock.pin_json` keeps their stored `observed_at` across the re-batching
  every served response does.

**Rule: replay what executed, not what was intended.** `media` replays for a run
iff it has `MediaDownload` rows, or its `MediaSelection` names a channel the run
established; `posts` iff the run holds `getMessages` receipts. A segment
recorded before #91 (a selection, no receipts) replays exactly as it did;
access refused replays `channel` only and leaves no posts, media or custody
rows, exactly as live.

## Deviations from the plan / spec

* Orchestrator decision 1 (2026-10-06): handle rows for a channel the store has
  never seen are resolved live by handle rather than ending as a dead-end
  outcome; the spec's §2.3 (which kept `unresolvable`-style handling out) is
  amended by this document. `--dry-run` reports them as `needs_resolve`.
* The spec says "a post already in the store is re-fetched"; tombstoned-in-store
  rows are included under the same rule (orchestrator decision 2).
* A failed `posts` phase withdraws the channel context so `media` cannot act
  after it (not in the plan; found by the posts-stop test: media ran after the
  stop and its first RPC would have slept the flood cooldown).
* `deleted_upstream` for an id that never existed (above the newest post) is
  what Telegram says; it cannot be told apart from a deletion.
* Earlier (#68, by-id amendment): per-row outcomes are a caller-owned
  `MediaCollector(outcomes=dict)` (a `DiskFloorStop`/`HardStop` discards the
  result yet the report needs the rows finished before the stop), and
  `CollectResult.stop_exc` carries the exception behind a stop. `PostsCollector`
  follows the same pattern.

## Known limitations

* A channel for which Step A finds no route and has nothing to try (route 4,
  nothing tried) writes no `ChannelAccess`, so that segment's run holds only the
  self `User` raw and `reproject` skips it with its "no resolve records"
  WARNING (#84 behaviour). Nothing was fetched for it.
* A re-fetch can append a revision with no human edit: Telegram rotates the
  `file_reference` inside a photo or document's `media_json`, and the content
  hash covers `media_json`. The live smoke below shows it (one revision per
  re-fetch of a photo post).
* Within a segment ids arrive in id order, not list order.
* A SIGKILL mid-segment leaves no report (rows persist; `.incoming` parts are
  swept next run; a re-run re-fetches posts and skips held media).
* `--dry-run` tables are wide: in an 80-column terminal Rich truncates the
  per-channel column titles.
