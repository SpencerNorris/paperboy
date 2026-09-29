# Plan: replay lookups served from a per-run in-memory raw index (#75)

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-29-replay-lookup-performance.md` in your first commit.)

**Branch:** `perf/replay-lookup`, cut from `origin/dev/media-storage` (`3474c87`). PR targets `dev/media-storage`.
**Spec:** `docs/superpowers/specs/2026-09-29-replay-lookup-performance-design.md` (read first), overview `2026-09-28-media-storage-overview.md` (docs DoD, redaction, scratch protocol), `docs/features/reproject.md`, ADR-0002/0005, `gh issue view 75`.
**Rules:** TDD (failing test → code → green), `uv run pytest -q && uv run ruff check && uv run pyright` green at every commit, `TMPDIR=<scratch>/tmp` for pytest. Commits end with `Co-Authored-By:` only. No live Telegram calls anywhere in this feature. Never name real targets/ids or local paths in anything committed.

## 1. Measurements (planner, 2026-09-29, on a `.backup` copy of the real store)

Copy: 65,827 raw rows, 603 MB, 61 runs, USB volume, host under memory pressure (timings ×3, cache state unreliable — the plan text is the reliable signal). Biggest run: 27,567 rows (25,154 `Message`, 2,170 `MessageEmpty`, 17,654 media-bearing messages, 0 `MediaDownload`). Media lookup = `download_media`'s `_latest` (miss on the biggest run; hit on an 8,854-row run).

| # | Mechanism | `EXPLAIN QUERY PLAN` (media lookup) | media miss | Needs migration? |
|---|---|---|---|---|
| M0 | today: `lower(kind)=? OR LIKE '%.k'` + `id BETWEEN` | `SEARCH raw_records USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)` | 9.75 / 9.40 / 9.98 s (hit near run end: 16 ms — reverse scan exits early) | — |
| M1 | `kind IN (<exact stored spellings>)`, existing `idx_raw_records_kind` | `SEARCH … USING INDEX idx_raw_records_kind (kind=?)` + temp b-tree | 60.6 / 0.0 / 0.0 ms | no |
| M2 | new index `(kind, id)` + M1 rewrite | planner still picks `idx_raw_records_kind` | 0.3 ms | yes |
| M3 | expression index `(lower(kind), id)` | unused with today's clause (still rowid range); 0.8 ms only after rewriting to `lower(kind) IN` | 0.8 ms | yes |
| M4 | index `(kind, json_extract(ctx,'$.channel_id'), json_extract(ctx,'$.msg_id'))` | `idx_raw_records_kind` chosen | 0.4 ms | yes, one per call-site shape |
| **M5** | **per-run in-memory index: one walk per run, dict lookups, payload by rowid** | walk once: `rowid>? AND rowid<?`; serve: `SEARCH … (rowid=?)` | **17,654 lookups in 20 ms** (build 4.9–5.2 s, 23.7 MB) | **no** |

Other per-lookup costs on the biggest run today → with M5 (prototype): `get_messages` miss 6.9–7.9 s → 2,170 lookups in 89 ms; `iter_history` 2.8–3.9 s **per page** × 252 pages (≈ 12–16 min) → 252 pages incl. payload fetch 4.65 s; `RawReplayWebClient.get` 1.5–3.9 s per URL → O(1); `has_context_value`/`MAX(observed_at)` 3.7–3.8 s each → in-memory; 1,000 payload fetches by rowid 35 ms.

**Chosen: M5.** Every walk of the run costs the same ~5 s whether it serves one lookup or all of them, so walk once per run and serve every call site from memory. It needs no migration (the source is `mode=ro`/`immutable` and is never migrated — `app.build_reproject` opens it with `ReplaySource.open`; only the *output* goes through `Store.open`), so an old, read-only or hand-copied source is exactly as fast as a fresh one, and results are identical by construction (same rows, same order, payload text read verbatim by rowid).
**Rejected:** M1 alone — fast for small kinds but message-kind lookups (`get_messages`, diff nested lookups, `iter_history`) stay O(run) at 1.1–1.6 s and lose today's early-exit on hits (a regression for gap probes); M2/M3/M4 — need a migration the source never receives, so an unmigrated source would silently keep today's speed, and M4 needs one index per lookup shape; none helps `iter_history`.
**ADR:** none. No schema, index or raw change; raw-first (ADR-0002) and per-run scoping (ADR-0005 §5) are untouched — the run's `id BETWEEN lo AND hi` window is now materialised once instead of once per lookup. Add one "Notes" line to ADR-0005 pointing at the feature doc section.

## 2. Design

`src/paperboy/replay.py` (add `log = logging.getLogger("paperboy.replay")`):

- `RawEntry` (frozen, slots): `id, kind (lower-cased stored kind), tier, observed_at, ctx (dict; {} when NULL or not a JSON object), payload_id (int|None), url (str|None)`.
- `RunIndex.build(conn, run)` runs the **one** walk (`WALK_SQL`, the only remaining range query): `SELECT id, lower(kind), tier, observed_at, context_json, CASE WHEN <_kind_clause(message,messageservice,messageempty)> THEN CAST(json_extract(payload_json,'$.id') AS INTEGER) END, CASE WHEN lower(kind) IN ('tme_page','wayback_cdx') THEN json_extract(payload_json,'$.url') END FROM raw_records WHERE id BETWEEN ? AND ? ORDER BY id`. The `CAST` stays in SQL so `'12'`→12, `'abc'`→0 behave exactly as today. Holds `entries` (id ASC) and `by_kind: dict[str, list[RawEntry]]`. Logs INFO: run id, rows, message-kind rows, context bytes, elapsed.
- `RunIndex.entries(kinds, mode)` → id-ASC merge of buckets whose stored kind matches: `mode="suffix"` = `kind_matches` (== or `.endswith("."+k)`, the `_kind_clause` rule); `"exact"` (`lower(kind) IN`, web kinds); `"contains"` (`channeldifference`).
- `RunIndex.lookup(kinds, fields, values, mode="suffix") -> list[RawEntry]` (id ASC). `fields` are context keys or `@tier` / `@payload_id` / `@url`. First call per `(mode, kinds, fields)` builds a key map over `entries(kinds, mode)`, cached; later calls are dict hits. **Any `None` in `values` returns `[]`** (SQL `NULL = ?` never matches); entries whose key contains `None` are stored but unreachable.
- `ReplaySource.index(run) -> RunIndex`: single-entry cache keyed by `(run.lo, run.hi)` (reproject is sequential; a multi-target run reuses it; the next run replaces it, freeing memory). `ReplaySource.payloads(ids) -> dict[int, sqlite3.Row]` fetches `observed_at, payload_json` by `id IN (…)` in chunks of 500; a missing id raises `ReprojectSourceError`.
- Memory bound: ~0.9 KB/row measured (23.7 MB / 27,567 rows, Python overhead included); one run resident at a time; size logged per run. No hard cap (a hypothetical 1M-row run ≈ 1 GB — file an issue if ever hit; not in scope).
- `_kind_clause` is kept **only** for `WALK_SQL`; `kind_matches` is its Python twin, proven equivalent by test.

### Call-site mapping (all 15 `_latest` call sites — the 16th grep hit is the definition — plus every other raw read)

`_latest(kinds, fields, values)` → `lookup(...)[-1] or None`; `_serve(entry)` fetches the payload row by rowid, then `clock.begin_batch(); clock.serve_json(...)` exactly as today.

| Method | kinds (suffix mode) | key fields → values |
|---|---|---|
| `get_full_channel` | chatfull | `channel_id` |
| `get_self` | user | `@tier` → `'self'` |
| `get_messages` (per id) | message, messageservice, messageempty | `channel_id`, `@payload_id` |
| `get_channel_difference` nested `m_row` | same three | `channel_id`, `@payload_id` → `m["id"]`; then among the hits fetch payloads and keep the **latest** whose `payload_json == dumps(m)` (today's exact-text match) |
| `get_privacy` | account.privacyrules, privacyrules | `key` |
| `download_media` | mediadownload | `channel_id`, `msg_id` |
| `get_channel_recommendations` | chats, chatsslice | `channel_id` |
| `check_chat_invite` | chatinvite, chatinvitealready, chatinvitepeek | `hash` |
| `get_participants` | channels.channelparticipants, …notmodified | `channel_id`, `filter` → `filter.get("_")`, `offset` |
| `get_participant` | channels.channelparticipant, usernotparticipant | `channel_id`, `user_id` |
| `get_users` (per ref) | user, userempty | `method` → `'users.getUsers'`, `user_id` |
| `get_full_user` | users.userfull | `user_id` |
| `get_user_photos` | photos.photos, photos.photosslice | `user_id` |
| `download_user_photo` | avatardownload | `photo_id` |
| `get_message_reactions_list` | messages.messagereactionslist | `channel_id`, `msg_id`, `offset` → `offset or ""` |

Non-`_latest` reads: `resolve` → `entries(("resolvedpeer",))` reversed, first whose `parse_target(ctx["target"]).value` matches (skip entries without `target`). `iter_history` → `lookup(("message","messageservice"), ("channel_id",), (cid,))`, keep `payload_id < offset_id` unless `offset_id == 0` (then keep all, `None` ids last), sort by `(payload_id DESC, id ASC)`, page + today's never-split rule, then `payloads(page ids)` and serve in page order. `get_channel_difference` main → `lookup(("channeldifference",), ("channel_id",), (cid,), mode="contains")[idx]`; synthetic stamp → `max(e.observed_at for e in index.entries_with_ctx("channel_id", cid))` (None when none, as today's `MAX`). `get_sponsored_messages` → `lookup(("sponsoredmessage",), ("channel_id",), (cid,))`, payloads in id order. `RawReplayWebClient.get` → `lookup(("tme_page","wayback_cdx"), ("@url",), (url,), mode="exact")`, first entry with `id > served cursor`. `ReplaySource.resolve_targets` / `linked_group_ids` (payloads of the chatfull bucket, `full_chat.linked_chat_id` truthy) / `has_kind` / `has_context_channel` / `has_context_value` → index-backed; `detect_phases` therefore builds the run's index once, before the gateway exists.

## 3. Tasks (each: failing test → code → green → commit)

**T0** `git fetch && git checkout -b perf/replay-lookup origin/dev/media-storage`; add this plan; commit `docs(plan): replay lookup performance — per-run raw index (#75)`.

**T1** `tests/test_replay_index.py::test_kind_matches_equals_kind_clause_sql` — parametrise stored spellings `Message, message, contacts.resolvedPeer, ResolvedPeer, x.y.chatfull, chatfullx, users.UserFull, UserFull, .chatfull, tme_page` × kinds tuples used in replay.py; assert `kind_matches(s.lower(), kinds) == bool(conn.execute(f"SELECT 1 FROM (SELECT ? AS kind) WHERE {sql}", (s, *params)).fetchone())`. Code: `kind_matches`. Commit `feat(replay): kind_matches, the Python twin of _kind_clause (#75)`.

**T2** `RunIndex` + `ReplaySource.index/payloads`. Tests (same file, hand-built sources via `Store.add_raw`): `test_index_entries_are_id_ordered_and_bucketed_by_lowercased_kind`; `test_lookup_none_value_never_matches` (context lacking the key, and a `None` query value); `test_lookup_payload_id_keeps_sql_cast_semantics` (payload `"id": "12"` found by `12`); `test_index_is_built_once_per_run_and_replaced_on_the_next` (`conn.set_trace_callback`: `WALK_SQL` once for two `index(run)` calls, again for another run; reset the callback in `finally`); `test_index_logs_rows_and_size` (`caplog`, INFO, `paperboy.replay`). Commit `feat(replay): per-run in-memory raw index (#75)`.

**T3** Port `RawReplayGateway` (table above + `resolve`, `iter_history`, diff, sponsored, `_serve`). New test `test_gateway_lookups_issue_no_sql_beyond_a_point_fetch`: trace callback on the source; 50 `download_media` misses → no statement touches `raw_records`; one hit → exactly one statement, and `EXPLAIN QUERY PLAN` of it contains `USING INTEGER PRIMARY KEY (rowid=?)` (the spec §4 plan assertion, mapped to this mechanism: lookups run no range query at all). `tests/test_replay_gateway.py`, `tests/test_replay_people.py`, `tests/test_history_catchup.py`, `tests/test_reproject*.py` unchanged and green (parity golden untouched). Commit `perf(replay): serve every gateway lookup from the run index (#75)`.

**T4** Port `ReplaySource` per-run helpers and `RawReplayWebClient.get`; delete the dead per-lookup SQL. `tests/test_reproject.py::test_detect_phases_*`, `tests/test_collector_web.py` green; add `test_web_client_serves_repeat_captures_in_order` if not already covered. Commit `perf(replay): phase detection and web replay read the run index (#75)`.

**T5** `test_media_and_message_lookups_do_not_scale_with_run_size`: one run of ≥ 50,000 raw rows (self/resolve/chatfull + 5,000 media-bearing `Message`s + 45,000 plain `Message`s, unique ids; bulk-insert with `BEGIN`/`executemany`/`COMMIT` on `st.conn` after `st.begin_run()` — `add_raw` per row is too slow); time `index(run)` + 5,000 `download_media` misses (a `MediaSink` is never touched on a miss) + 5,000 `get_messages` hits; assert elapsed < 10× your measured value, floor 10 s (planner's proxy: 27.5k-row index 5 s on a USB volume, 17,654 lookups 20 ms — expect ~1–2 s here). Plus `test_namespaced_kinds_resolve` (`contacts.resolvedPeer`, `messages.chatFull` served by `resolve`/`get_full_channel`). Commit `test(replay): run-size regression bound and namespaced kinds (#75)`.

**T6** Docs (overview docs DoD): `docs/features/reproject.md` — new section "Performance — per-run raw index (#75)" (mechanism, §1 table, rejected options, memory/log line, DoD transcript placeholder) and fix the `_kind_clause` sentence in "Design deviations" (now `kind_matches` + the walk); `docs/how-it-works.md` §4 — one sentence: replay reads each run's receipts into an in-memory index once, then answers from it; `README.md` reproject row — note the per-run index and its INFO size line; `CLAUDE.md` status — one sentence for #75; `docs/adr/0005-run-structure.md` Notes — one line. `docs/data-model.md`: **no change** (no schema change) — say so in the PR. Commit `docs(reproject): per-run raw index — mechanism, measurements, docs DoD (#75)`.

**T7** DoD run (§4) → paste into the feature doc + PR body. Commit `docs(reproject): #75 DoD transcript`.

## 4. Definition of done (spec §5; all offline)

`<scratch>` = this feature's scratch dir; `<data-dir>` = the real data dir (read only). Never write under `<data-dir>`; never touch the real store. The operator's scratch `.backup` copy is the source.

1. Setup: `mkdir -p <scratch>/75/default <scratch>/75/out && sqlite3 <copy> ".backup '<scratch>/75/default/paperboy.sqlite'"`. Media: the **per-file symlink farm** from `docs/superpowers/plans/2026-09-29-media-streaming-resume.md` T5 step 4 (one link per `MediaDownload` path via `normalize_legacy_location` + `resolve_key_under`, into `<scratch>/75/default/media`; never a link to the whole dir). Before and after the run paste `find -L <scratch>/75/default/media -type f | wc -l` and `du -shL <scratch>/75/default/media`, and `find <scratch>/75/default -name '*.log' -o -name '.incoming'` → nothing.
2. Before (on the copy, `sqlite3 … "EXPLAIN QUERY PLAN <today's media SQL>"` and a Python `time.perf_counter()` of the same SQL, biggest run, a missing msg id) — expect the rowid-range plan and ~10 s. After: EQP of `RunIndex.WALK_SQL` (rowid range, once per run) and of the payload fetch (`rowid=?`), plus `ReplaySource.open` → `runs()` → biggest run → timed `index(run)` build and timed 17,654 `download_media` misses through the gateway (list the media-bearing ids with `json_extract(payload_json,'$.media') IS NOT NULL`). Paste both.
3. Full run, background, PID recorded:
   `date; PAPERBOY_DATA_DIR=<scratch>/75 nohup /usr/bin/time -l .venv/bin/paperboy reproject --profile default --phases channel,history,media --out <scratch>/75/out/paperboy.sqlite > <scratch>/75/reproject-75.txt 2>&1 & echo $! | tee <scratch>/75/reproject-75.pid`
   Poll `ps -p $(cat …pid)` and `tail -3 <scratch>/75/out/paperboy.sqlite.log` every few minutes; do not narrow the phases. When it exits, paste: wall time and "maximum resident set size" from `/usr/bin/time -l`, the per-run INFO index lines (count + max rows), the media phase line, and the row-count table with columns labelled **source (backup)** / **reprojected (output)** (`raw_records`, `messages`, `media`, `custody_log`, `web_snapshots` — note #74's known `web_snapshots` loss is out of scope). **Stop rule:** after 2 h of waiting, stop polling but **do not kill the process**; record the PID, start time, runs/phases completed so far (from the log) and mark the item **PENDING (operator)** in the feature doc and PR — the transcript file completes itself.
4. Redact per the overview §6 (`@<channel>`, `<id>`); keep the unredacted transcript in `<scratch>/75/`.

## 5. Edge cases and failure modes

- `context_json` NULL or not a JSON object → `ctx = {}` (`json_extract` on a non-object yields NULL today → same no-match).
- A `None` query value, or a context missing the key → no match (SQL NULL semantics), never a `None == None` hit.
- `payload_id`: computed in SQL with today's `CAST`, only for message kinds; `iter_history` keeps `NULL` ids only when `offset_id == 0`, sorted last (SQL `DESC` puts NULL last).
- Multiple revisions of one key → list in id order; `_latest` = last; the diff nested match compares full payload text among those hits only.
- Immutable-mode sources: one read statement per run + point fetches — nothing new is written or locked. A source concurrently appended to: `runs()` bounds are a snapshot; the walk is bounded by `hi` (unchanged behaviour).
- A missing `idx_raw_records_kind` (hand-edited DB): irrelevant, no index is required; results identical, only the walk's speed changes.
- Memory: one run resident; ~0.9 KB/row; INFO line per run. Not capped — documented.
- `sqlite3.Row` → `RawEntry`: every `row["…"]` at the old call sites must go; pyright (`standard`) flags leftovers.
- `payloads()` chunking (≤ 500 ids) keeps well under any SQLite parameter limit; a row missing for a known id is a corrupt source → `ReprojectSourceError`, never silently skipped.
- Tests using `set_trace_callback` must reset it in `finally`; the perf test must not run under `tracemalloc` (it was only used for the planner's measurement).

## Orchestrator decisions (2026-09-29, binding)

1. **Memory:** log each run's in-memory index size (rows, approximate bytes) at INFO, and log a WARNING when one run's index exceeds 512 MB. No hard cap and no failure. Test the WARNING threshold with a monkeypatched limit.
2. **Scope:** include `iter_history`, `resolve`, the once-per-run `has_context_value`/`MAX(observed_at)` walks, and the other raw lookups mapped in this plan, not just the 15 `_latest` sites. Results must stay identical (the parity suite is unchanged).
3. **ADR:** none needed, since there is no schema, index or raw change. Add a one-line note to ADR-0005 pointing at the feature doc.
