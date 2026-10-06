# fetch-from-list: fetch the listed posts and their media (#91)

**Status:** draft for operator approval, 2026-10-06. **Tracking:** #91.
**Order:** 2 of 2, after #63 (bucket stores). **Base:** `dev/gcs-pull` with
#63 merged.
**ADR:** none needed; no storage or guardrail decision changes. Note the
rename in the #68 feature doc.

## 1. Problem

`fetch-media LIST` downloads media only for posts **already in the store**. A
listed post that was never collected is reported `not_in_store` and skipped,
and its text, metadata and counters are never fetched. The operator doesn't
want posts and media separated. Fetching posts by id is cheap: one
`channels.getMessages` call per 100 ids, already a `Gateway` method, paced by
`Budget` and replayable.

## 2. Design

### 2.1 Rename

`paperboy fetch-media` becomes **`paperboy fetch-from-list`**. The project is
pre-release, so there's no alias. Rename the module (`fetch_media.py` →
`fetch_from_list.py`), the report's default name, table titles, docs, and the
feature doc (`docs/features/fetch-media.md` → `fetch-from-list.md`). Keep
`media_list.py` (it parses the list) and leave existing receipt kinds
unchanged.

### 2.2 Per segment: channel → posts → media

Each channel segment already runs the `channel` phase (by id, #84) and then
`media`. Insert a **posts** step between them:

1. **channel**: unchanged (#84 Step A, `ChannelAccess`).
2. **posts**: `gateway.get_messages(input_channel, ids)` in batches of ≤ 100
   for **every** listed id in the segment, including ids already in the store,
   so counters and edits are current as of this run. Each returned object is
   appended as a receipt first (context `{"method": "channels.getMessages",
   "channel_id": …}`), then projected through the **same** message projection
   history uses: new rows, `message_revisions` on edits, `message_metrics` on
   counter changes, and referenced peers and edges. A `MessageEmpty` answer
   (deleted upstream) is projected as a tombstone, exactly as history does.
   Factor out a shared "project these message objects" function rather than
   duplicating history's logic.
3. **media**: unchanged, but the set of eligible rows is now computed *after*
   the posts step, so freshly fetched posts are downloaded in the same run,
   into the run's media store (#63). `--no-media` skips this step.

`posts` is a named phase in the segment's recipe and in `run_events`. Replay
must treat it like any other phase: `detect_phases` sees its `getMessages`
receipts, and `RawReplayGateway.get_messages` already serves them.

### 2.3 Outcomes and dry run

- `not_in_store` disappears as a final outcome. Those rows become `pending`,
  and their posts are fetched.
- New live outcomes: `deleted_upstream` (Telegram returned `MessageEmpty`),
  `no_media` (fetched, but the post has no media, so there's nothing to
  download), and `post_only` (fetched with `--no-media`).
- `already_stored` stays a **media** outcome, per store (#63 §3.3). The post is
  still re-fetched.
- `--dry-run` stays Telegram-free. It reports, per channel, how many listed
  posts are already in the store vs. not yet collected, and media-already-in-
  store per the run's store (a bucket store means HEAD requests; see #63).
- The report CSV gains a `post` column (`fetched` / `deleted_upstream` /
  `skipped`) beside the media outcome.

### 2.4 Budget and safety

- RPC cost: ⌈ids/100⌉ extra calls per segment. For the pending list, about 20
  calls in total. `--max-rpc` counts them as usual.
- The stop policy is unchanged: any phase stop ends the command after the
  report is written (the #68 decision).
- `--exclude-target` and the linked-group exclusion work as today.
- Read-only: `getMessages` fetches. It never marks anything as read.

## 3. Tests (write first)

- Rename: `fetch-from-list --help` works and `fetch-media` is gone. The default
  report name and docs are updated.
- A listed post absent from the store is fetched, projected (message row,
  peers) and its media downloaded in the same run.
- A listed post already in the store is re-fetched. An edit produces a
  revision and a counter change produces a metric row, through the shared
  projection function. History's existing tests pass unchanged.
- `MessageEmpty` → tombstone + `deleted_upstream`, no media attempt.
- `--no-media` → posts fetched, no media phase, outcome `post_only`.
- Batching: 250 ids → 3 `get_messages` calls.
- Dry run makes zero Telegram calls and counts not-yet-collected posts.
- Reproject of a source with fetch-from-list runs reproduces messages and
  media tables (parity: posts phase detected and replayed).

## 4. Definition of done

Offline: all of §3, full suite, ruff, pyright and parity green.

Live, under the overview protocol (VPN check before each call, scratch
`.backup`, `--max-rpc 60`, `--max-flood-sleep 60`, ≤ 4 invocations, never the
split-out investigation), a 3-row smoke list from 2 channels: one post
**not** in the scratch store, one already in it, and one with media. Run once
against a local store and once against the #63 smoke bucket prefix (small
files only). Show posts fetched and projected, media placed in the right store
with sha verified, and a re-run showing media `already_stored` while posts are
re-fetched. Then an offline reproject of the scratch store (source vs output
table).

Docs: `docs/features/fetch-from-list.md` (renamed and updated), README,
CLAUDE.md, `docs/how-it-works.md` §6, and the code atlas note in the PR body.

## 5. Out of scope

Fetching comment threads of listed posts (`discussion`), fetching posts by
search query, and parallel segments.
