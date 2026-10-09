# `fetch-from-list` — fetch the listed posts, then their media (#68, #91)

Specs: `docs/superpowers/specs/2026-10-06-fetch-from-list-design.md` (#91, the
posts step and the rename; authoritative for what changed) and
`docs/superpowers/specs/2026-09-28-media-list-fetch-design.md` (#68; §9 is the
by-id amendment). Plans: `docs/superpowers/plans/2026-10-06-fetch-from-list.md`
(#91), `docs/superpowers/plans/2026-10-01-media-list-fetch-by-id.md` and
`docs/superpowers/plans/2026-09-29-media-list-fetch.md` (#68). Shared
constraints and the live smoke protocol:
`docs/superpowers/specs/2026-09-28-media-storage-overview.md`. Plain-language
walk-through: `docs/how-it-works.md` §6. **Migration `0008_custody_content_key`**
(`custody_log.content_key`, ADR-0009, `docs/data-model.md`); the only raw-log
addition in #91 is a context tag (see "Replay"). Builds on #84
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
and the rows then carry the resolved `tg:msg:` uri. The channel id is taken
from the run's resolved `ChannelContext`, never from a `username` lookup, so a
list handle that is not the channel's stored primary username (an alias of a
multi-username channel, a renamed handle) still settles its rows. The `channel`
phase runs ALONE first for such a segment; `--exclude-target` is then re-checked
against the resolved id (the id itself, or the linked group of an excluded
parent) before any post is fetched, so an excluded channel reached through an
unknown handle is `excluded`, not fetched (its metadata is stored by the
resolving `channel` phase: the id is unknowable without it). `--dry-run` cannot
do that lookup (it is offline), so it reports these rows as `needs_resolve`. If
two rows of one list name the same message once by handle and once by id, they
cannot be matched offline; once the handle resolves, the later row is marked
`duplicate_row` and nothing is fetched twice.

`--exclude-target T` (repeatable; the forms `reproject --exclude-target` takes:
a handle, `123`, `-100123`, `t.me/c/123`) marks every row of that channel
`excluded`, offline, with no network. **Exclusion is one-way.** Excluding a
parent channel also excludes its linked discussion group; excluding only a group
does NOT exclude its parent. The link is read from the stored `linked_group`
edge in either direction (Telegram's `linked_chat_id` is bidirectional, so the
edge points from whichever side was collected), and the group is the end whose
`channels.kind` is not `broadcast` (`media_list.linked_discussion_groups`; a
linked channel not yet in `channels` counts as a group, a channel that is itself
a group has no followers). A channel found to be a group of an excluded one is
added to the excluded set, so its later segments are caught too. Tests:
`test_excludes_a_group_linked_to_an_excluded_parent_in_either_edge_direction`,
`test_excluding_only_a_group_does_not_exclude_its_parent_channel`. It is a guard for
running against a store that still holds an investigation the operator does not
want pulled. **Exclusion is fail-closed.** The stored edge may not exist yet (a group whose
parent was never collected with it), and a group of an excluded parent is
recognisable only from the `ChatFull` it reports (`linked_chat_id`). So an id
segment's channel is established ALONE first (the `channel` phase records the
edge), `--exclude-target` is re-checked against the result, and only then are
posts fetched; before this, an unknown group was fetched in the same run that
revealed it. Test:
`test_group_of_an_excluded_parent_without_a_stored_edge_is_not_fetched`.
**Limitation:** that establishing run stores the channel's own metadata
(`channels`, snapshots, edges, peers) even for a channel that turns out to be
excluded; no posts, media or messages are fetched for it. A target the store has never seen, or one that is not a channel
handle/id, exits 1 before anything else is done (excluding nothing by accident
would download what was meant to be kept out).

## Media store (#63)

`--media-store gs://<bucket>/<prefix>` (or `PAPERBOY_MEDIA_STORE`; the bucket
must be listed in `PAPERBOY_MEDIA_STORE_BUCKETS`) sends the pull to a bucket
with no local copy; see `media-stores.md`. Whether the media phase still has to
download a row is then per store: a custody row naming the bucket answers
offline; otherwise a store that cannot verify objects (the local folder) is
trusted on `exists`, while an object in a bucket that no custody row names (an
orphan) is NOT held - the media phase re-fetches and CRC-verifies it before
adopting it, so the selection (`media_list.held_file`) agrees. **A bucket
`--dry-run` therefore makes no GCS call and never contacts Telegram.** An unreachable bucket exits
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
file for the message's CURRENT media: a custody sighting recorded under the
same Telegram photo/document id (`custody_log.content_key`, ADR-0009) names a
file, and a `custody_log` row names the run's store or the store answers an
existence check; a file the database holds from a LOCAL run is not held by a
bucket run until the bucket has it. This is the OFFLINE estimate: after the
`posts` phase has refreshed the row the fetch decides again, see "Segments") and `needs_resolve` (a handle
row for a channel the store has never seen, above). A tombstoned row is
pending too: Telegram's answer decides between `deleted_upstream` and a live
post. A LIVE answer for a row the store had tombstoned clears its `deleted_at`
(the `message_tombstones` history stays), so the media phase selects it like any
live post; `reproject` replays the same.

**Final outcomes** (the report's `outcome` column), per row, in this
precedence:

| Outcome | Meaning |
|---|---|
| `downloaded`, `duplicate`, `too_large`, `size_mismatch`, `unavailable`, `skipped` | The media phase's own result for the file (`duplicate`: same bytes/content id as a stored file - a repost - so no download, but this message's own custody row is recorded; `too_large`: `--media-max-mb`; `skipped`: a per-file skip such as an expired file reference). |
| `deleted_upstream` | Telegram answered `MessageEmpty` for the id: deleted, or never existed (an id above the channel's newest post answers the same). It is projected as a tombstone with evidence `empty`, exactly as `history` does, and no media is attempted. |
| `already_stored` | THIS run's media store already held the file for the post's current media and this message already has its custody row; the post was still re-fetched. |
| `post_only` | Fetched with `--no-media`. |
| `no_media` | Fetched, but the post has nothing downloadable (not a photo/document). |
| `no_access` | The channel could not be reached at run time (below). |
| `not_attempted` | The command stopped first. |

`excluded`, `no_media`, `no_access`, `too_large`, ... are final; only
`not_attempted` makes the exit code 1. A row whose file this store holds but
whose post Telegram now answers `MessageEmpty` reports `deleted_upstream`
(Telegram's current answer wins); the held file is untouched.

There is no `unresolvable` and no `not_in_store`: a channel needs no stored
username (a linked discussion group is fetched like any other channel, by id),
and a post that is not stored is fetched.

## Segments: channel, then posts, then media

Pending rows are grouped by `(priority, channel)` in order of first appearance
of the pair, so every P1 segment runs before any P2 segment (without a
`priority` column: one segment per channel). Within a segment ids are fetched
ascending; list order decides only the order of segments.

A channel's first segment starts with a run of its own, `channel` alone; every
segment then has a `posts` run and, only when something needs media, a `media`
run. All are ordinary `collect_channel` runs over the **standard** collectors
(the later ones reuse the established channel). In order:

1. **`channel`**, alone, once per channel per command, targeting the channel id
   from the list row (or the handle, above). Exclusion is decided only after it
   (see "Exclusion is fail-closed" below). It reaches the channel through #84's Step A (saved key, then a
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
   tombstone, never a blank row. Cost: ⌈ids/100⌉ calls per segment. After
   it, every fetched row is re-judged on the post as stored now (held file,
   `stored`, `media_held`), **whether or not a media run follows**: under
   `--no-media`, or when the phase stopped after some batches, the report still
   describes the stored post (an edited post whose new photo is not held is
   `post_only` with an empty sha, never `already_stored` with the old file).
3. **`media`** (a further run of the segment, a marker run that reuses the
   channel), unless `--no-media` or nothing needs it. What
   needs it is decided only now, on the posts the first run just stored (spec
   2.2 step 3): for each fetched post the CURRENT `media_json` is turned into a
   content key and looked up in the custody index (`load_content_index`,
   ADR-0009). The media run walks (`media_msgs`, recorded as `MediaSelection`)
   exactly the ids that are
   * **not held**: no file for that content in this run's store - it is
     downloaded (this is what makes a post edited to a different photo download
     the new one, not report the old file); or
   * **held but unrecorded, same channel**: the file is held under a custody
     sighting of THIS channel (a repost) but this message has no custody row
     for the content yet - the media phase records the sighting and skips the
     download (outcome `duplicate`).
   A message that already has its own custody row for the content is left out:
   walking it again would add a `duplicate` row on every re-run. So **every
   same-channel sighting keeps its own custody row** (provenance: where and
   when a file appeared, with its content key); only the download is skipped.

   **A cross-channel repost is different (operator decision, ADR-0009, follow-up
   #95).** A row whose current content is held only under ANOTHER channel is
   reported `already_stored` with the holding file and is **not walked**: no
   download and no custody row of its own. It is decided before any size cap, so
   `--media-max-mb` can never turn it into `too_large`, and a re-run does not
   select it again. The selection and the media phase use the same per-channel
   index (`load_content_index(conn, channel_id)`), so they agree on what is a
   download and what a duplicate. The sighting is still recoverable from the
   data: `SELECT m.uri, c.sha256, c.path FROM messages m JOIN custody_log c ON
   c.content_key = CASE lower(json_extract(m.media_json,'$._')) WHEN
   'messagemediaphoto' THEN 'photo:' || json_extract(m.media_json,'$.photo.id')
   WHEN 'messagemediadocument' THEN 'document:' ||
   json_extract(m.media_json,'$.document.id') END` finds every message
   carrying a content that a custody row recorded, in any channel.
   The offline `media_held` flag is only a preview.

Budget, guardrails, dedup, custody, streaming, `--media-max-mb` and the
free-disk floor apply unchanged. One gateway (one MTProto session, one
`Budget`) serves the whole command, so `--max-rpc` bounds it all.

A channel is established once per command: its later runs (every segment's
posts and media run) receive the cached `ChannelContext` (in-process only,
never persisted) and skip `channel`, appending one `ChannelContextReused` marker
instead. Likewise an unknown `@handle` is resolved once per command: if it
appears under two priorities, the second segment reuses the id the first
resolved and makes no second `contacts.resolveUsername` call. With a saved key (the
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
  media already in the run's store is not downloaded again and adds no custody
  row.
* The doctor preflight runs once (skipped by `--unsafe`); a block exits 1 before
  any segment, with a report of all-`not_attempted`.

## Report

`--report` (default
`<data_dir>/<profile>/fetch-from-list-<YYYYmmddTHHMMSSZ>.csv`), opened for
writing **before** any segment (an unwritable path fails immediately), rewritten
at the end: `line_no,uri,outcome,post,sha256,key,reason` for every input row in
list order. `post` is `fetched`, `deleted_upstream` or `skipped` (not reached, or
the channel was refused). `reason` is empty except for `no_access` (why the
channel could not be reached), and `excluded`. `uri` is
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
  run (a segment's posts run or media run) that reused a channel; replay runs it without a `channel` phase,
  rebuilding the context from the source run's `ChatFull` (which carries the
  channel object whichever route got the run in; a first segment that took route
  1 has no `ResolvedPeer`).
* `MediaSelection` `{channel_id, msg_ids}` is written whenever a media phase is
  scoped with `media_msgs`, **just before the media phase runs** and only if the
  channel was established; replay walks only those ids. Since #91 the media run
  is its own run (marker run, posts evidence absent), so the selection is the
  ids decided after the posts phase, repost sightings included: replay writes
  the same custody rows, `content_key` and all (the replayed media phase
  derives it from the replayed message exactly as live does). Pre-amendment runs
  carry `{msg_ids}` only and replay as before.
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

## Migration 0008 (`custody_log.content_key`)

`0008_custody_content_key.sql` (ADR-0009) adds a nullable `content_key` to
`custody_log` and backfills it only where certain: a sighting of a message whose
current media and every revision agree on one content key. Sightings of an
edited message, and avatars, stay NULL ("unknown"): the media phase then
downloads that content again (a redundant download, never a missed one).
`raw_records` is untouched. It is applied by `Store.open` like every migration;
`docs/data-model.md` documents the column. The backfill's temp tables are indexed
by `uri` (without that it is quadratic), and the migration runner logs each
migration's start and duration (`migration <name>: applying` / `applied in Ns`; DEBUG, INFO when it takes a second or more).

## Review fixes (round 1)

* **Media eligibility after posts (B1).** Decided on the post as just stored,
  keyed by content (Segments, step 3). Before, a post edited to a new photo was
  reported with the old file and never downloaded.
* **Each unknown handle resolved once (M1)** and **exclusion symmetric in edge
  direction but one-way (M3)**: see "Input" and "Segments".
* **`deleted_upstream` before `already_stored` (M2)** in the outcome table, as in
  `_settle`.
* **`MessageEmpty` above the newest post (M4)** is projected as an `empty`
  tombstone, as `history` does; it cannot be told from a deletion (see
  "Deviations"), and a later live answer clears it.

## Review fixes (round 2)

* **Exclusion fail-closed (F2):** the channel is established alone before
  exclusion is decided (see "Input").
* **Backfill key (F4):** migration 0008 stamps a sighting only when its own
  message AND the message its file was originally stored for are both stable on
  that key (`test_0008_does_not_stamp_a_dedup_sighting_of_a_file_stored_for_an_edited_post`).
* **Cross-channel repost (F1):** `already_stored`, not walked, no custody row
  (Segments, step 3; ADR-0009; follow-up #95).
* **Sha-dedup receipt:** the media phase's safety-net dedup (two photo ids, same
  bytes) wrote a custody row with no raw receipt, so `reproject` dropped it - in
  plain `collect` too. It now leaves a `MediaDownload` receipt (the bytes were
  fetched), stamped with the same time as the custody row
  (`test_reproject_reproduces_a_sha_dedup_custody_row`).
* **`--no-media` misreport (F3):** rows are re-judged after the posts run
  whether or not media follows.

## Deviations from the plan / spec

* Orchestrator decision 1 (2026-10-06): handle rows for a channel the store has
  never seen are resolved live by handle rather than ending as a dead-end
  outcome; the spec's §2.3 (which kept `unresolvable`-style handling out) is
  amended by this document. `--dry-run` reports them as `needs_resolve`.
* The spec says "a post already in the store is re-fetched"; tombstoned-in-store
  rows are included under the same rule (orchestrator decision 2). If Telegram
  answers such a row LIVE, `deleted_at` is cleared (new `clear_deleted`, called by
  the `posts` collector) so the row is a live post and its media is downloaded,
  as spec §2.3 says; tombstone history rows are kept. This is a new semantic for
  `deleted_at` ("currently believed deleted"), limited to `posts`.
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

## Definition of done

Redaction: channels are `@<channel>`, ids `<id>`, the bucket `gs://<bucket>/<prefix>`,
local paths `<scratch>`. Unredacted transcripts stay in `<scratch>/91/`
(`live1..4.unredacted.txt`, `dryrun-*.unredacted.txt`, `compare.out`), referenced by
filename only. The operator's list appears nowhere: the smoke list is a 3-row list
built for this run from two channels already in the store.

### Gates (pasted output)

The full suite was run with `TERM=dumb` and `FORCE_COLOR` unset (the CLI tests
assert on Rich output, which colour codes break). Run on the final code
(round-2 fixes; later commits are documentation only):

```
$ uv run pytest -q --basetemp=<scratch>/pytest-91-fix
1142 passed in 180.44s (0:03:00)
$ uv run ruff check
All checks passed!
$ uv run pyright
0 errors, 0 warnings, 0 informations
```

Files in scope (`git diff --name-only origin/dev/gcs-pull...HEAD`, run after the last
code commit; the docs commit that records this block adds no new path):

```
CLAUDE.md README.md
docs/adr/0005-run-structure.md docs/adr/0008-media-stores.md
docs/adr/0009-custody-records-content-key.md docs/data-model.md
docs/features/{collect-channel,fetch-from-list,fetch-media,media-stores,pacing,reproject}.md
docs/how-it-works.md docs/superpowers/plans/2026-10-06-fetch-from-list.md
docs/superpowers/specs/2026-10-06-fetch-from-list-design.md
src/paperboy/{cli,config,fetch_from_list,fetch_media,gateway,media_list,media_store,progress,replay,reproject}.py
src/paperboy/collectors/{base,history,media,posts}.py src/paperboy/store/{db,messages}.py
src/paperboy/store/migrations/0008_custody_content_key.sql
tests/{conftest,test_cli,test_cli_fetch_from_list,test_collector_media,test_collector_media_stores,
  test_collector_posts,test_fetch_from_list,test_fetch_media,test_media_list,test_reproject_bucket,
  test_reproject_fetch_from_list,test_reproject_fetch_media,test_store_migrations}.py
tests/fixtures/reproject/parity_golden.json
```

(`fetch-media.md`, `fetch_media.py` and the two `*_fetch_media` test files appear as the
deleted/renamed halves of the rename.)

**Live status.** The final live re-smoke (operator-approved, 2026-10-09) ran twice
against the scratch store only (no bucket, no downloads). Redaction as above:
`<A>` is the channel of row 1, `<B>` that of row 3, `<id1>`/`<id2>` the message
ids. Transcripts, by filename in `<scratch>/91/`: `smoke7-dryrun.unredacted.txt`,
`smoke7.unredacted.txt`, `smoke7-report.csv`, `vpn-7.txt`, `smoke8.unredacted.txt`,
`smoke8-report.csv`, `vpn-8.txt` (live-call log: 8 lines, 7 and 8 annotated as
operator-approved). Before each call both Telegram DCs routed via `utun4`
(`vpn-7.txt`, `vpn-8.txt`). The list is the 3-row list of the earlier re-smoke:

```
t.me/<A>/<id1>      (a text-only post, username link)
tg:msg:<A>/<id1>    (the same message by id)
tg:msg:<B>/<id2>    (a photo post whose file the store holds)
```

DEBUG `rpc ...` lines are omitted from the excerpts (12 RPCs in each run); everything
else is verbatim apart from ANSI codes and the redactions.

**Invocation 7 (commit 6cbc742)**, offline dry run first on the smoke store. It
applied migration 0008 to the real-size store, and the new migration log shows
how long that took:

```
[10/08/26 22:13:10] DEBUG    migration 0008_custody_content_key: applying
[10/08/26 22:13:51] INFO     migration 0008_custody_content_key: applied in
                             41.07s
```

(41.07 s wall on this run, on a disk busy at the time; the separate measurement
above gave 21.87 s with the indexes and 106.68 s without, on a `.backup` copy of the
same store. The first open of the store runs the backfill.) The live run, exit 0:

```
       fetch-from-list: offline
            classification
┏━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ outcome       ┃ rows ┃ declared GB ┃
┡━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ pending       │    2 │        0.00 │
│ needs_resolve │    0 │        0.00 │
│ excluded      │    0 │        0.00 │
│ duplicate_row │    1 │        0.00 │
│ total         │    3 │        0.00 │
└───────────────┴──────┴─────────────┘
                  fetch-from-list: per channel (rows by state)
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━┓
┃ chann… ┃        ┃        ┃        ┃        ┃        ┃       ┃        ┃       ┃
┃     id ┃ pendi… ┃ in_st… ┃ not_y… ┃ media… ┃ needs… ┃ excl… ┃ dupli… ┃ total ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━┩
│ <B>… │      1 │      1 │      0 │      1 │      0 │     0 │      0 │     1 │
│ <A>… │      1 │      1 │      0 │      0 │      0 │     0 │      1 │     2 │
└────────┴────────┴────────┴────────┴────────┴────────┴───────┴────────┴───────┘
              fetch-from-list: segment plan (list order)
┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ segment ┃ priority ┃ channel id ┃ rows ┃ posts calls ┃ declared GB ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│       1 │ -        │ <A> │    1 │           1 │        0.00 │
│       2 │ -        │ <B> │    1 │           1 │        0.00 │
└─────────┴──────────┴────────────┴──────┴─────────────┴─────────────┘
[10/08/26 22:14:08] INFO     pacing: factor=2.0 default=2.0s
                             contacts.resolveUsername=10.0s; flood ceiling=60s
                    INFO     ▶ channel
[10/08/26 22:14:12] INFO     channel access: id=<A> via=saved_key
                             granted=True error=None
                    INFO     ✓ channel · channels=1 peers=2 · 2s
                    INFO     fetch-from-list: segment 1/2 priority=None
                             channel=<A> rows=1 start
                    INFO     ▶ posts
                    INFO     posts: batch 1/1 (1 ids) done
                    INFO     ✓ posts · messages=1 revisions=0 tombstones=0
                             edges=0 · 0s
[10/08/26 22:14:15] INFO     ▶ media
[10/08/26 22:14:16] INFO     media: store local
[10/08/26 22:14:17] INFO     media: 0 selected message(s) have media to fetch;
                             770 other(s) not selected
                    WARNING  media: 1 selected id(s) have no stored, in-window
                             media: [<id1>]
                    INFO     ✓ media · downloaded=0 duplicates=0 unavailable=0
                             skipped_kind=0 skipped=0 size_mismatch=0
                             out_of_window=0 not_selected=770 too_large=0 · 2s
                    INFO     fetch-from-list: segment 1/2 priority=None
                             channel=<A> rows=1 end: posts {'fetched':
                             1}; media no files considered
                    INFO     ▶ channel
                    INFO     channel access: id=<B> via=saved_key
                             granted=True error=None
                    INFO     ✓ channel · channels=1 peers=1 · 0s
                    INFO     fetch-from-list: segment 2/2 priority=None
                             channel=<B> rows=1 start
                    INFO     ▶ posts
                    INFO     posts: batch 1/1 (1 ids) done
                    INFO     ✓ posts · messages=1 revisions=1 tombstones=0
                             edges=0 · 0s
[10/08/26 22:14:24] INFO     fetch-from-list: segment 2/2 priority=None
                             channel=<B> rows=1 end: posts {'fetched':
                             1}; media no files considered
                    INFO     fetch-from-list: {'no_media': 1, 'duplicate_row':
                             1, 'already_stored': 1}; 0 bytes downloaded; report
                             <report>.csv
  fetch-from-list: result
┏━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ outcome          ┃ rows ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ already_stored   │    1 │
│ duplicate_row    │    1 │
│ no_media         │    1 │
│ bytes downloaded │    0 │
└──────────────────┴──────┘
report: <scratch>/91/<report>.csv
```

```
line_no,uri,outcome,post,sha256,key,reason
1,tg:msg:<A>/<id1>,no_media,fetched,,,
2,tg:msg:<A>/<id1>,duplicate_row,skipped,,,
3,tg:msg:<B>/<id2>,already_stored,fetched,e7c822cc9750...,media/e7/e7c822cc9750....jpg,
```

It exposed a bug: row 1 is a text-only post, yet it got a `media` run and the
WARNING `media: 1 selected id(s) have no stored, in-window media`. The same
needless run had been in both October-7 smokes. Fixed in 3bd26b9
(`test_text_only_post_runs_no_media_phase`).

**Invocation 8 (commit 3bd26b9, the fix)**, same list, exit 0 (the offline tables
are the same as above and are omitted):

```
[10/08/26 22:20:50] INFO     pacing: factor=2.0 default=2.0s
                             contacts.resolveUsername=10.0s; flood ceiling=60s
[10/08/26 22:20:52] INFO     ▶ channel
                    INFO     channel access: id=<A> via=saved_key
                             granted=True error=None
                    INFO     ✓ channel · channels=1 peers=2 · 2s
                    INFO     fetch-from-list: segment 1/2 priority=None
                             channel=<A> rows=1 start
                    INFO     ▶ posts
[10/08/26 22:20:54] INFO     posts: batch 1/1 (1 ids) done
                    INFO     ✓ posts · messages=1 revisions=0 tombstones=0
                             edges=0 · 0s
[10/08/26 22:20:57] INFO     fetch-from-list: segment 1/2 priority=None
                             channel=<A> rows=1 end: posts {'fetched':
                             1}; media no files considered
                    INFO     ▶ channel
                    INFO     channel access: id=<B> via=saved_key
                             granted=True error=None
                    INFO     ✓ channel · channels=1 peers=1 · 1s
                    INFO     fetch-from-list: segment 2/2 priority=None
                             channel=<B> rows=1 start
                    INFO     ▶ posts
                    INFO     posts: batch 1/1 (1 ids) done
                    INFO     ✓ posts · messages=1 revisions=1 tombstones=0
                             edges=0 · 0s
[10/08/26 22:21:01] INFO     fetch-from-list: segment 2/2 priority=None
                             channel=<B> rows=1 end: posts {'fetched':
                             1}; media no files considered
                    INFO     fetch-from-list: {'no_media': 1, 'duplicate_row':
                             1, 'already_stored': 1}; 0 bytes downloaded; report
                             <report>.csv
  fetch-from-list: result
┏━━━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ outcome          ┃ rows ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━┩
│ already_stored   │    1 │
│ duplicate_row    │    1 │
│ no_media         │    1 │
│ bytes downloaded │    0 │
└──────────────────┴──────┘
report: <scratch>/91/<report>.csv
```

```
line_no,uri,outcome,post,sha256,key,reason
1,tg:msg:<A>/<id1>,no_media,fetched,,,
2,tg:msg:<A>/<id1>,duplicate_row,skipped,,,
3,tg:msg:<B>/<id2>,already_stored,fetched,e7c822cc9750...,media/e7/e7c822cc9750....jpg,
```

There is no `media` run for the text-only post and no WARNING; the photo post
re-fetched its post and was `already_stored` (held, with its own custody row), so
it had no media run either.

Results of both runs: no FLOOD_WAIT, PEER_FLOOD or auth error; 12 RPCs each; 0 bytes
downloaded; `custody_log` stayed at 1838 rows and `media` at 306 (no downloads, no
new custody rows).

Known issue the smoke surfaced (not part of this change): every fetch of a media
post appends a revision (`posts ... revisions=1` on every run). The only difference
is the rotating `file_reference` inside `media_json`, which the content hash covers.
It predates #91 and is tracked in #96, which must land before the real pull.

**Offline rebuild of the smoke store: not run this round.** These smokes downloaded
nothing, so they wrote no new receipts to round-trip; the parity tests
(`tests/test_reproject_fetch_from_list.py`, `tests/test_reproject_bucket.py`) cover
reproject. The earlier reproject of this store is in the section above.

### Follow-ups

* #95: record a custody sighting for a cross-channel repost (today it is
  `already_stored` with no custody row of its own; ADR-0009).
* #96: stop appending a revision for a rotated `file_reference` alone. Must land
  before the real pull.

Tests added or changed in review rounds 1-2 (all committed; each new one was run
against the code before its fix and failed there, the failing line is quoted):

* `test_repost_in_a_later_segment_writes_its_own_custody_row` (same-channel repost
  keeps its custody row; re-run adds none). With the one condition that walks a
  held-but-unrecorded message removed it failed with `assert [('tg:msg:10/...eady_stored')]
  == [('tg:msg:10/... 'duplicate')]`. (A round-2 DoD cited this test but it had been
  lost by an overwrite of the test file's tail; it is restored here.)
* `test_cross_channel_repost_is_already_stored_without_download_or_custody`:
  failed on the round-2 code with `assert [11] == []` (the repost was downloaded).
* `test_cross_channel_repost_is_never_too_large`: failed with `assert ('too_large'
  == 'already_stored'`.
* `test_group_of_an_excluded_parent_without_a_stored_edge_is_not_fetched`: failed
  with `assert ('get_messages' not in ['get_self', 'get_full_channel',
  'get_messages', 'download_media'])`.
* `test_no_media_edited_post_is_post_only_without_the_old_file`: failed with
  `assert ('already_sto...f6cfd114.jpg') == ('post_only', ...`.
* `test_posts_stop_after_partial_batches_still_reports_the_fetched_rows`: failed with
  `assert ['already_sto..._stored', ...] == ['post_only',...st_only', ...]`.
* `test_0008_does_not_stamp_a_dedup_sighting_of_a_file_stored_for_an_edited_post`:
  failed with `assert [None, 'photo:2002'] == [None, None]`.
* `test_reproject_reproduces_a_sha_dedup_custody_row`: failed without the receipt
  with `custody_log diverged: only in source: [(... 'tg:msg:10/2', 'local',
  'photo:102')]`.
* `test_reproject_reproduces_a_cross_channel_repost`: failed on the round-2 code with
  `assert (1, 0) == (1, 1)`.
* `test_excluding_only_a_group_does_not_exclude_its_parent_channel` and
  `test_excludes_a_group_linked_to_an_excluded_parent_in_either_edge_direction`
  (round 2; the former failed before the one-way rule with `assert not True`).
* Updated expectations (run layout is now channel / posts / media; reposts are
  `duplicate`): `tests/test_reproject_fetch_from_list.py` (19 tests),
  `test_end_to_end_two_channels_two_tiers`, `test_live_collector_list_is_the_standard_one`,
  `test_file_already_in_bucket_is_duplicate_without_upload`.
* Review round 3 (minors):
  * `test_failed_refresh_drops_stale_held_flags` (a failed re-judge drops the stale
    flags; failed before with `sqlite3.OperationalError: disk I/O error` escaping
    the `finally`).
  * `test_failed_refresh_does_not_hide_the_original_error` (failed before with the
    refresh error replacing `Boom: posts bug`).
  * `test_cross_channel_repost_on_an_orphan_bucket_object_is_not_already_stored`
    (failed before with `assert ('already_stored' != 'already_stored')`).
  * `test_bucket_duplicate_receipt_round_trips_through_reproject` (failed against
    the pre-receipt `media.py` with `custody_log diverged: only in source:
    [(... 'tg:msg:10/2', 'gs://bkt/p/x', 'photo:102')]`).
  * `test_migration_runner_logs_start_and_duration` (failed before with
    `assert 'migration 0008_custody_content_key: applying' in []`).
  * Updated for the rule that an unrecorded bucket object is not held:
    `test_already_stored_is_per_store`,
    `test_dry_run_against_bucket_makes_no_gcs_call_and_never_builds_a_gateway`
    (renamed), `test_collect_media_flag_downloads_and_stays_off_without_it`
    (asserts `▶ media`, since migration log lines mention "media").
* Live-smoke finding (a text-only post got a needless `media` marker run and a
  "no stored, in-window media" WARNING): `test_text_only_post_runs_no_media_phase`
  (failed before with `assert 1 == 0` on the `MediaSelection` count). A post whose
  stored media is absent or not in `DOWNLOADABLE_KINDS` is never walked; it settles
  as `no_media`. No existing run-layout expectation changed: the replay sources
  mix text and photo posts per segment, so their media runs remain.
* `established is None` (the channel phase returning no context without a stop) is
  handled as a dead channel; it cannot be produced through the fake gateway, so it
  is covered only through the refused-channel test
  (`test_channel_phase_skip_continues_with_other_channels`).

Reviewer checks, with output:

```
$ git diff --stat origin/dev/gcs-pull -- tests/test_collector_history.py tests/test_history_catchup.py tests/test_collector_discussion.py
(empty: history's and discussion's tests are unchanged, and pass)
```

`fetch-media` no longer appears outside historical plans and specs, the
`media-stores.md` transcripts (annotated), and the "named `fetch-media` until #91"
notes.

### Migration 0008 timing (offline, `.backup` copy of the scratch store)

The backfill's temp tables had no index, so its probes were quadratic. Each run
applied the migration SQL to its own fresh `sqlite3 .backup` copy of the scratch
store (at migration 0007, 1838 `custody_log` rows), measured with
`<scratch>/mig91/run.py`:

```
old-0008.sql (no indexes): wall 106.68s cpu 85.53s
0008 with indexes:         wall  21.87s cpu  1.14s
$ cmp keys-old.txt keys-new.txt   (id|content_key of every custody_log row)
content_key columns IDENTICAL
```

(The remaining wall time is I/O on the 560 MB file; the CPU time is 1.14 s.)

### Offline smokes

* `tests/test_reproject_fetch_from_list.py` (19 tests) is the parity gate: a
  source built from `collect` runs plus `fetch-from-list` segments, including an
  edited post (a revision and a metric row), a new text post with author peer and
  forward edge, a `MessageEmpty` tombstone, a refused channel, a zero-download
  segment, a legacy selection and a `--no-media` run, reprojects to identical
  tables (`assert_round_trip`).
* `--dry-run` of the 3-row list on a `sqlite3 .backup` copy (no Telegram, no keychain):
  `pending 3`, `needs_resolve 0`; per channel `@<channel A>`: pending 2 / in_store 2 /
  not_yet_collected 0 / media_stored 0, `@<channel B>`: pending 1 / in_store 0 /
  not_yet_collected 1; segment plan 2 segments, `posts calls` 1 each.
  Bucket dry run of rows 1-2 (taken before the orphan-object rule: one read-only metadata GET, `exists(...) -> False`; it now makes none):
  `pending 2`, `media_stored 0`.

### Smoke test transcript

Scratch store: `sqlite3 .backup` of the real profile (never `cp`), run with
`PAPERBOY_DATA_DIR=<scratch>`, `PAPERBOY_REQUIRE_PROXY=false`, `--profile default`,
`--max-rpc 60 --max-flood-sleep 60`, never `--unsafe`/`--join`/`--profiles`. Before
every invocation: `STOP-LIVE` absent, the live-call counter below the cap, and

```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```

List (`<scratch>/91/list3.txt`): row 1 `tg:msg:<A>/<id>` (stored text-less post), row 2
`tg:msg:<A>/<id>` (stored photo, 54 KB, never downloaded), row 3
`tg:msg:<B>/<id>` (the id after channel B's newest stored post, 13 days old).
Every live call used `--media-max-mb 1` (invocations 1-3).

**Invocation 1, local store** (3-row list; exit 0; 14 RPCs of the 60 cap, no
FLOOD_WAIT; `rpc channels.getMessages` once per segment plus the media phase's own
reference refresh):

```
line_no,uri,outcome,post,sha256,key,reason
1,tg:msg:<A>/<id>,no_media,fetched,,,
2,tg:msg:<A>/<id>,downloaded,fetched,47748138...45a1,media/47/47748138....jpg,
3,tg:msg:<B>/<id>,deleted_upstream,deleted_upstream,,,
INFO fetch-from-list: {'no_media': 1, 'downloaded': 1, 'deleted_upstream': 1}; 54313 bytes downloaded
```

Row 3 is the honest result of the "id after the newest stored post" pick:
Telegram answered `MessageEmpty` (no newer post existed), so it was projected as
a tombstone (evidence `empty`) and no media was attempted. Checked in the store:
`shasum -a 256` of the file under `<scratch>/91/default/media/` equals
`media.sha256`; `ls .incoming` is empty; `run_events` shows `channel`, `posts`,
`media` per segment (`posts` counts: segment A `messages=2 revisions=1`, segment B
`tombstones=1`); `message_metrics` gained a row for both stored posts (the
counters were refreshed); the one revision is the photo post's `media_json`
(Telegram rotates `file_reference`), not a text edit; raw records
`Message` with context `method: channels.getMessages` (2) and `MessageEmpty` (1).

**Invocation 2, bucket store** (rows 1-2 only, `--media-store
gs://<bucket>/<prefix>` with a fresh `paperboy/smoke-<date>-91` prefix that
`gcloud storage ls` showed empty first; exit 0; 11 RPCs): row 2 `downloaded` again
(per store: the local copy does not count for the bucket), row 1 `no_media`. The
custody rows for row 2 now name both `local` and `gs://<bucket>/<prefix>`; the
`MediaStore` marker `{"store": "gs://<bucket>/<prefix>"}` is in the run; the
object exists under `media/47/` and `gcloud storage cat | shasum -a 256` equals
`media.sha256` (`47748138...45a1`). The bucket was written only under that prefix,
with one create-only upload; no delete, overwrite, retention or IAM command was run.

**Invocation 3, re-run of the local 3-row list** (exit 0; 12 RPCs):

```
1,tg:msg:<A>/<id>,no_media,fetched,,,
2,tg:msg:<A>/<id>,already_stored,fetched,47748138...45a1,media/47/47748138....jpg,
3,tg:msg:<B>/<id>,deleted_upstream,deleted_upstream,,,
INFO fetch-from-list: {'already_stored': 1, 'deleted_upstream': 1, 'no_media': 1}; 0 bytes downloaded
```

`channels.getMessages` was called again (once per segment), `upload.getFile`
never, `custody_log` stayed at the same count (1837 before and after), and
`message_metrics` grew by 2.

**Invocation 4 (documented extra), `--no-media`, one row**: the row-3 pick above
did not exercise a post that was really absent from the store, so one more call
fetched the id after the newest stored post of the busiest stored channel
`@<C>` (a different channel; no media requested):

```
1,tg:msg:<C>/<id>,post_only,fetched,,,
```

A new `messages` row exists, `run_events` has `channel` and `posts` only, no
`MediaSelection` and no `media` event for the run. 9 RPCs.

Totals: 4 of 4 permitted live invocations (counter file `<scratch>/91/live-calls.log`),
2 media files (one local, one bucket; 54 KB each), 0.1 MB. No STOP condition was
hit; `STOP-LIVE` was never created. The transcripts above ran on code
from before commit `4bdaa45`, which changed the live path (a post Telegram
answers live is restored from its tombstone, and `--exclude-target` is
re-checked on a handle-resolved channel); the re-smoke at the end of this
section ran on the final commit.

**Reproject** (offline; `PAPERBOY_DATA_DIR=<scratch>`, the bucket in
`PAPERBOY_MEDIA_STORE_BUCKETS`, `reproject --profile default --include-target <A>
--include-target <B> --include-target <C> --out <scratch>/91/reprojected.sqlite`),
source vs output, restricted to the three channels (`compare.out`):

```
messages                       source   31241 reprojected   31241 equal
message_revisions              source   31244 reprojected   31244 equal
message_metrics                source   31213 reprojected   31213 equal
message_tombstones             source    2339 reprojected    2339 equal
media(smoke msg)               source       1 reprojected       1 equal
custody(smoke msg)             source       2 reprojected       2 equal
new post <C>/<id>              source       1 reprojected       1 equal
tombstone <B>/<id>             source       2 reprojected       2 equal
source run_events      [('channel', 13), ('graph', 3), ('history', 3), ('media', 9), ('participants', 6), ('posts', 6), ('profiles', 9)]
reprojected run_events [('channel', 13), ('graph', 3), ('history', 3), ('media', 9), ('participants', 6), ('posts', 6), ('profiles', 9)]
```

`posts` was replayed for all six fetch runs. The bucket object was read back
read-only for the custody row.

### Re-smoke on the final commit (operator-approved, 2026-10-07)

Run on the branch tip `aee9e90` (after `4bdaa45`), against the scratch store only
(no bucket write), with two more live invocations (live-call log: 6 lines) and
one media file (a photo, 85 KB). VPN check before each call: both Telegram DCs
routed via `utun*`. List of three rows, built in the scratch dir:

```
t.me/<A>/<id1>
tg:msg:<A>/<id1>
tg:msg:<B>/<id2>
```

Row 1 is a username link to a channel the store knows; row 2 names the same
message by id; row 3 is a never-downloaded photo. Setup note: a username the
store knows resolves offline (see "Input"), so to exercise the live handle
route the scratch store's `channels.username` for `@<A>` was blanked first (the
`channel` phase writes it back). The tombstone row the operator asked for could
not be built: the scratch store holds no message row with `deleted_at` set and
every tombstone is an `empty` answer from Telegram, so no tombstoned post was
plausibly live. The second row above (same message as row 1) exercises the
handle+id dedupe instead; the "tombstone cleared" path is covered by unit tests
only.

**Invocation 5** (`--max-rpc 30 --max-flood-sleep 60 --media-max-mb 1`), report:

```
line_no,uri,outcome,post,sha256,key,reason
1,tg:msg:<A>/<id1>,no_media,fetched,,,
2,tg:msg:<A>/<id1>,duplicate_row,skipped,,,
3,tg:msg:<B>/<id2>,downloaded,fetched,e7c822cc9750...,media/e7/e7c822cc9750....jpg,
```

The log shows `channel access: id=<A> via=handle` (the live
`contacts.resolveUsername`), then the posts and media phases for segment 1;
segment 2 never ran (its only message was fetched once), and segment 3 ran
through `via=saved_key`. 87022 bytes downloaded, 15 RPCs (four of them the start-up account checks).

The `ChannelAccess` receipt (`raw_records`) carries `via: handle`,
`granted: true`, `handle: @<A>`. The `--exclude-target` re-check on the resolved
channel excluded nothing (none was passed).

**Invocation 6** (same command, re-run), report:

```
line_no,uri,outcome,post,sha256,key,reason
1,tg:msg:<A>/<id1>,no_media,fetched,,,
2,tg:msg:<A>/<id1>,duplicate_row,skipped,,,
3,tg:msg:<B>/<id2>,already_stored,fetched,e7c822cc9750...,media/e7/e7c822cc9750....jpg,
```

0 bytes downloaded, 12 RPCs: the posts were fetched again, the media was
`already_stored`. Both channel-access receipts were `via=saved_key` (row 1 now
resolved offline, since invocation 5 wrote the username back).

File check: `sha256` of the stored file equals `media.sha256` (`e7c822cc9750...`),
and `media.size` is 87022 = the file's size.

**Reproject** (offline, `reproject --profile default --include-target <A>
--include-target <B> --include-target <C> --out <scratch>/reprojected-3.sqlite`),
source vs output for the three channels (`compare3.out`, pasted verbatim apart from
the redacted ids; the two DIFF lines are real):

```
messages                       source   31241 reprojected   31241 equal
message_revisions              source   31246 reprojected   31246 equal
message_metrics                source   31217 reprojected   31217 equal
message_tombstones             source    2339 reprojected    2339 equal
media(re-smoke msg)            source       1 reprojected       1 equal
custody(re-smoke msg)          source       1 reprojected       1 equal
media (all 3 channels)         source      50 reprojected       2 DIFF
custody_log (all 3 ch)         source     161 reprojected       2 DIFF
new post <C>/<id>       source       1 reprojected       1 equal
tombstone <B>/<id>     source       2 reprojected       2 equal
source run_events [('channel', 17), ('graph', 3), ('history', 3), ('media', 12), ('participants', 6), ('posts', 10), ('profiles', 9)]
reprojected run_events [('channel', 17), ('graph', 3), ('history', 3), ('media', 12), ('participants', 6), ('posts', 10), ('profiles', 9)]
```

`posts` replayed for all ten fetch runs (six from the earlier smokes plus two
segments in each of these two invocations). Every table that `fetch-from-list`
writes (messages, revisions, metrics, tombstones, the re-smoke message's `media` and
`custody_log` rows, the new post, the tombstone) is `equal`. **Two lines are
not: `media` over all three channels (source 50, reprojected 2) and
`custody_log` over all three channels (source 161, reprojected 2).** They are
not a `fetch-from-list` defect:

* **Cause (evidence).** The scratch source store is a `sqlite3 .backup`: it holds
  the database only, not the media files. Of the 50 `media` rows of the three
  channels, 48 point at files absent from the scratch `media/` directory
  (checked by file existence; 2 exist). `reproject` skips a media row whose file
  is missing, by design (it re-verifies each file's sha). Each reproject log, the
  #91 branch's and the base branch's, holds exactly 48 `replay: media file
  missing for sha ...` warnings, one per lost row. Of the 159 lost
  `custody_log` rows, 158 are the same cause (their file is absent); the other is
  the bucket sighting written by smoke invocation 2, which replay skipped with
  "receipt names a bucket outside the allow-list" because
  `PAPERBOY_MEDIA_STORE_BUCKETS` was not set for the reproject. 158 + 1 + the 2
  kept rows = 161.
* **Base-branch check.** `reproject` from the base branch (`dev/gcs-pull` @
  7e6de9d, no #91 code), same source, same scope, fresh output (offline): `media`
  source 50, base-reprojected 2, #91-reprojected 2; `custody_log` source 161, 2,
  2 (whole store: `media` 306 vs 2, `custody_log` 1838 vs 2, in both). The base
  loses the same rows, so it is not a #91 regression, and the cause above makes
  it not a bug at all in a profile that has its files. Issue #94 (which had
  guessed "no `MediaSelection` marker") was corrected and closed as not planned.
* **PENDING:** the bucket sighting's replay (custody id 1837) is unverified. It
  needs an operator reproject with the bucket allow-listed
  (`PAPERBOY_MEDIA_STORE_BUCKETS`) and ADC; I did not run it (it would read the
  bucket).

### Docs updated

`docs/features/fetch-from-list.md` (renamed from `fetch-media.md`, rewritten),
`README.md` (commands row, documentation list, report filename, config table),
`CLAUDE.md` (commands and the "In progress on `dev/gcs-pull`" status, migration 0008),
`docs/how-it-works.md` §6, `docs/features/reproject.md` ("Replaying
`fetch-from-list` runs"), `docs/features/media-stores.md`,
`docs/features/collect-channel.md`, `docs/features/pacing.md`,
`docs/data-model.md` (the `getMessages` receipts' context; `custody_log.content_key`,
migration 0008), `docs/adr/0009-custody-records-content-key.md` (new),
`docs/adr/0005-run-structure.md` and `docs/adr/0008-media-stores.md` (rename only), `docs/superpowers/specs/2026-10-06-fetch-from-list-design.md`
(§2.3 amendment for handle rows), `docs/superpowers/plans/2026-10-06-fetch-from-list.md`.
The PR body carries the code-atlas note: the standard collector list gained `posts`
(run by `fetch-from-list` and by `reproject`'s replay list, never by `collect`).
