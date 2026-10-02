# `fetch-media` — download media for an ordered list of message URIs (#68)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #68.
**Order:** 5 of 5 in the sequential chain (last). **Depends on:** #64
(streaming; the pending list holds files up to 2.4 GB), #62 (the report
prints stable keys) and #69 (pacing). Independent of #63 and #70. Read
`2026-09-28-media-storage-overview.md` first, including the live smoke
protocol.
**No ADR needed:** a new recipe over the existing `media` collector; storage
is unchanged. (The optional pre-resolved channel context in
`collect_channel` is a recipe change; note it in ADR-0005's consequences,
since it adds a replay marker.)

## 1. Goal

Hand paperboy a prioritized, cross-channel list of messages and have it fetch
their media **in list order**, skip whatever is already stored, survive
interruption with a cheap resume, and say what happened to every row.

The motivating list: 1,719 rows, 11 channels, ~146 GB declared, sorted P1 →
P2 → P3 → PH, rows interleaved across channels by date.

## 2. Input contract (the canonical format)

The contract is deliberately minimal so an analyst's catalogue can evolve
without touching paperboy:

- **CSV with a header containing `uri`** — each `uri` is
  `tg:msg:<channel_id>/<msg_id>` (paperboy's own `messages.uri`). All other
  columns are ignored, except an optional **`priority`** column, which is
  used only as an opaque grouping label (§3); paperboy never interprets or
  sorts its values.
- **Or a plain text file**, one entry per line: a `tg:msg:` URI or an
  `https://t.me/<username>/<msg_id>` link (username resolved offline via
  `channels.username`), or `https://t.me/c/<channel_id>/<msg_id>`. Blank lines
  and `#` comments are ignored.
- **Row order is fetch order.** Duplicate URIs keep their first occurrence
  (counted as `duplicate_row` in the report).
- Malformed rows fail the whole command up front with every offending line
  number — nothing is fetched from a half-parsed list.

Module: `src/paperboy/media_list.py` —
`parse_media_list(path) -> list[ListRow]` where `ListRow` holds
`(line_no, uri, channel_id, msg_id, priority: str | None)`.

## 3. Execution

1. **Offline classification** (no network) of every row against the store:
   `not_in_store` (no `messages` row), `no_media` (no downloadable media),
   `unresolvable` (channel has no stored `channels.username` — e.g. a linked
   discussion group, reachable only via its parent; see §4),
   `already_stored` (its Telegram document/photo id or sha is already in
   `media`), or `pending`.
2. **Segmenting:** pending rows are grouped by `(priority, channel_id)` in
   order of first appearance. For the motivating list that is ≤ 4 × 11 = 44
   segments (without a `priority` column: ≤ 11). All P1 segments run before
   any P2 segment, so an interrupted run has fetched the most important
   files first.
3. **Each segment is one ordinary pass:** `recipes.collect_channel(...,
   phases=["channel", "media"], media=True)` with `media_msgs` set to the
   segment's ids (later segments of an already-resolved channel skip
   `channel` — see the decision below). This reuses budget, guardrails, dedup, custody, streaming,
   `--media-max-mb`, the free-disk floor — and **`reproject` replays it with
   no new code**, because each segment is a normal collect run (ADR-0005).
   One gateway (one MTProto session, one `Budget`) is shared across all
   segments, so `max_rpc_per_run` bounds the whole command.
4. **Stops:** with #69, flood waits up to `--max-flood-sleep` are slept
   through, so a phase stop means a wait beyond the ceiling or repeated
   failures. A `HardStop` ends the command; a `PhaseStop` in a segment's
   `channel` phase marks that channel's remaining rows `not_attempted` (its
   later segments are skipped too — don't re-resolve a channel that just
   stopped) and continues with other channels; a `PhaseStop` from the
   free-disk floor ends the command. The report is written in every case.
5. **Per-row outcomes:** `CollectResult` gains an optional
   `outcomes: dict[str, str] | None` (message uri → outcome), populated by
   `MediaCollector` for every row it considers (`downloaded`, `duplicate`,
   `too_large`, `size_mismatch`, `unavailable`, `skipped`). Other collectors
   leave it `None`.

**Decision — resolve each channel once per run (operator, 2026-09-28: be
conservative with the API).** `contacts.resolveUsername` is one of
Telegram's most flood-limited methods, so the command must not re-resolve a
channel for every segment (that would be ≤ 44 calls here instead of 11):

- The first segment of a channel runs the `channel` phase as normal; its
  resolved `input_channel`/`channel_id` are cached in-process for the rest of
  the command (never persisted — access hashes rotate).
- Later segments of that channel run `media` only, with the cached context.
  This needs a small, explicit recipe change: `collect_channel` accepts an
  optional pre-resolved channel context and, when given, skips the `channel`
  phase instead of requiring it.
- **Reproject must still replay those runs.** A media-only run has no
  channel-phase raws to rebuild context from, so each such run appends one
  raw marker (e.g. kind `ChannelContextReused`, payload `{channel_id,
  source_run_id}` — no access hash) that the replay recipe uses to establish
  context. Acceptance test: reproject parity over a store containing a
  multi-segment fetch-media run.
- All pacing goes through `Budget` with the #69 safety factor (effective
  10 s between `resolveUsername` calls, 2 s between per-file calls).
- **Depends on #69** landing first.

## 4. Unresolvable rows

paperboy doesn't persist access hashes (they rotate), so a channel is
fetchable only if it can be resolved by its stored username this run. Rows in
a linked discussion group (no username of its own) are reported
`unresolvable` with the reason. Resolving them through the parent channel's
`getFullChannel` chats vector (what the `discussion` collector does) is a
possible follow-up — out of scope here.

## 5. CLI

```
paperboy fetch-media LIST [--profile P] [--media-max-mb N] [--media-min-free-gb G]
                          [--report OUT.csv] [--dry-run] [--unsafe]
```

- Doctor preflight once (unless `--unsafe`), as `collect` does.
- `--dry-run`: steps 1–2 only, no keychain, no network. Prints a table of
  outcome → rows → declared GB, and the segment plan (priority, channel,
  rows, GB).
- `--report`: CSV `line_no, uri, outcome, sha256, key` for every input row,
  written even on a stop. Suggested default: `<profile_dir>/fetch-media-<run
  timestamp>.csv`.
- Final table: outcome counts and bytes downloaded. Exit 0 when every row
  reached a final outcome; exit 1 if the command stopped early (the report
  says which rows are `not_attempted`; re-running resumes).
- Logging: one INFO line per segment start/end with counts; WARNING per
  unresolvable channel (once, not per row).

## 6. Tests (write first, see them fail)

- Parser: CSV with extra columns; plain lines with all three link forms;
  `#` comments; malformed rows fail listing every bad line; duplicates keep
  first.
- Classification against a fixture store covers every offline outcome.
- Segmenting preserves priority tiers and first-appearance channel order.
- End to end with `FakeGateway` over two channels and two tiers: files land,
  report rows match, a second run reports `already_stored` for all and makes
  no download calls.
- `HardStop` mid-run: report written, remaining rows `not_attempted`, exit 1.
- `reproject` of a store containing a fetch-media run reproduces the same
  `media`/`custody_log` rows.
- `--dry-run` constructs no gateway (assert `build_gateway` not called).

## 7. Definition of done (smoke on real data)

All under the overview's smoke protocol (scratch data dir, redacted
evidence; the operator's list lives outside the repo — the handoff prompt
gives its path; never copy it into the repo).

1. `PAPERBOY_DATA_DIR=<scratch> paperboy fetch-media <list> --dry-run` on
   the real list — offline; paste the outcome and segment tables (counts
   only; expect ~99 `already_stored` and 40 `unresolvable` — the latter are
   the split-out investigation's rows).
2. A live fetch of a **3-row slice** (write it to the scratch dir): one
   photo and two small videos, from two different channels, ≤ 3 GB total,
   none from the split-out investigation, with `--report` and `--max-rpc 60`.
   Paste the table and the report with URIs redacted.
3. Re-run the same slice — every row `already_stored`, zero downloads.

## 8. Out of scope

GCS (#63), resolving linked-group rows through the parent, parallel
downloads, writing back to the analyst's catalogue.

## 9. Amendment (2026-10-01): fetch by id, after the first run escalated

**Why.** The first implementation (branch `feat/fetch-media-list`, ac20097)
escalated at K=3 review (see #68's root-cause comment). It reached each channel
by resolving its stored `@username`, then guarded with a wrapper collector
(`_ExpectChannel`) that skipped the segment if the handle now pointed at a
different channel. The guard's input, "this segment wants channel N", lived only
in memory, never in `raw_records`, and the wrapper existed only on the live
path. So `reproject` could not reproduce the guard's decision, and each review
round found another parity symptom. #84 (merged) makes channels addressable by
id, which removes the reason for the guard altogether.

This section supersedes §3.3's handle resolution and §4 where they conflict.

### 9.1 Each segment targets the channel id

- A segment runs `collect_channel` with the **id target** `<channel_id>` taken
  from the list row (`tg:msg:<channel_id>/<msg_id>`), through #84's Step A
  (saved key → from-message → verified stored handle). There is no
  `@username` resolution in fetch-media and no wrapper collector:
  **`_ExpectChannel` is deleted.** Live and replay run the identical, standard
  collector list (`channel` + `media`), so they cannot drift.
- Channel identity is guaranteed by #84: Step A addresses channel N by id, and
  the channel phase's existing check (`full_chat.id == requested id`) refuses
  anything else. A handle that has changed hands can no longer redirect a
  segment.
- Later segments of the same channel may reuse the channel context, as §3
  decided, as long as the reuse is itself recorded in raw so replay reuses it
  identically.

### 9.2 The selection receipt names the channel

`MediaSelection` becomes `{"channel_id": N, "msg_ids": [...]}`. `collect
--media-msgs` (no list) writes the channel it resolved. Replay serves the
selection from this receipt only, never from the operator's list, which is not
in the database. Older `{"msg_ids"}`-only receipts replay as today (legacy).

### 9.3 Replay what was executed, not what was intended

`detect_phases` must not treat a `MediaSelection` alone as evidence that
`media` ran: the receipt is written before any phase runs. The `media` phase is
replayed for a run if and only if the live run executed it, i.e. the run's
channel phase was granted access (a granted `ChannelAccess`, or for legacy runs
a `ResolvedPeer` + `ChatFull`) for the selection's channel, or the run has
`MediaDownload` rows. Write the rule down and test both directions:
- access granted, zero files downloaded: media still replays (with zero rows);
- access refused: media does not replay, and no custody rows appear.

### 9.4 Classification (replaces §4)

- `unresolvable` is removed. A channel no longer needs a stored username to be
  fetched.
- `not_in_store` still applies (no `messages` row). After the profile split
  (#70), rows from the split-out investigation have no messages in the clean
  `default`, so they classify `not_in_store` and are never fetched.
- New `--exclude-target T` (repeatable, same parsing as `reproject`'s, by
  resolved channel id; a linked discussion group follows its parent) marks
  matching rows `excluded`, offline. It's a guard for running against a store
  that still holds an investigation the operator doesn't want pulled.
- `--dry-run` prints per-channel counts (by `<id>`) of each outcome, so the
  operator sees exactly which channels a pull would touch before any network
  call.
- A Step A refusal at run time (no route works) marks that segment's rows
  `no_access` with the reason from #84's route-4 message, and the command
  continues with the next segment. It's a `SkipAndRecord`, not a stop.

### 9.5 Tests (in addition to §6)

- A segment of a channel whose stored handle now resolves elsewhere still
  fetches the right channel by id (no handle lookup at all when a saved key
  exists).
- The live run and its reproject produce identical `media`/`custody_log` for:
  a normal segment; a refused segment; a selection whose access was granted but
  which downloaded zero files.
- `MediaSelection` carries `channel_id`; a legacy `{"msg_ids"}` receipt replays
  unchanged.
- `--exclude-target` marks rows `excluded` offline; dry-run per-channel counts.
- There is no `_ExpectChannel` (or any fetch-media-only collector) in the live
  or replay collector lists.

### 9.6 Definition of done (amends §7)

The §7 live smoke is re-run at the final commit, since the earlier one ran on
the superseded design. Same protocol: ≤ 5 invocations, ≤ 3 small files from ≥ 2
channels already in the store, never the split-out investigation. Then
reproject the scratch store and show source vs output `media` and
`custody_log` equal for the fetched segments.

### 9.7 Implementation note

Resume on `feat/fetch-media-list` (merge `dev/media-storage` into it first; it
predates #75, #70 and #84) rather than starting over. Remove the code this
amendment supersedes (`_ExpectChannel`, handle resolution in segments, the
`unresolvable` class, the intent-based phase detection) instead of leaving it
dormant.
