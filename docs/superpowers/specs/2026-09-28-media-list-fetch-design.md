# `fetch-media` — download media for an ordered list of message URIs (#68)

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #68.
**Batch:** 2. **Depends on:** #64 (streaming; the pending list holds files up
to 2.4 GB), #62 (the report prints stable keys) and #69 (pacing safety
factor). Independent of #63.
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

1. `paperboy fetch-media download-list.csv --dry-run` on the real list —
   paste the outcome and segment tables (expect ~99 `already_stored`, 40
   `unresolvable`).
2. A real fetch of a **5-row slice** of the list (mix of video and photo,
   two channels) with `--report` — paste the table and the report.
3. Re-run the same slice — every row `already_stored`, zero downloads.

## 8. Out of scope

GCS (#63), resolving linked-group rows through the parent, parallel
downloads, writing back to the analyst's catalogue.
