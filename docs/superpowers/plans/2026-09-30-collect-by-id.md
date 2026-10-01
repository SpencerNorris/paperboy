# Collect by id — an id-first `channel` phase (#84) — Implementation Plan

(Planner: Fable. Commit this file as `docs/superpowers/plans/2026-09-30-collect-by-id.md` in your first commit.)

**Branch:** `feat/collect-by-id`, cut from `origin/dev/media-storage` (9946941). PR target `dev/media-storage`. Never `main`.
**Spec (authoritative):** `docs/superpowers/specs/2026-09-30-collect-by-id-design.md`. Issue #84. Read also: overview spec
§"Live smoke protocol" and "Docs are part of every feature's DoD", `docs/how-it-works.md` §2, `docs/features/collect-channel.md`,
`docs/features/reproject.md` §"Run structure", ADR-0001, ADR-0005 point 5, research §2.1 (access_hash is per-account, non-transferable).
**Verified facts (Telethon 1.44.0, installed wheel):** `InputChannelFromMessage(peer, msg_id, channel_id)` and
`InputPeerChannelFromMessage(peer, msg_id, channel_id)` exist; same field order as `InputUserFromMessage(peer, msg_id, user_id)`.
**TDD:** every task = failing test first, then the smallest change, then `pytest -q`, `ruff check`, `pyright` green, then commit.
Commits end with the single `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` trailer. No session URLs.

## 0. Ambiguities and resolutions (read before coding)

**A. How live Step A reads the store while replay serves the receipt.** `TelethonGateway` has no `Store` (ADR-0001: the seam is
"Telegram-shaped operations returning dicts"; store reads live in `store/`), and a route policy inside the gateway would have to be
duplicated in `FakeGateway`. So: the *policy* stays in `collectors/channel.py`; the *store read* is a new `store.peers.input_channel_ref`
mirroring `input_user_ref` (`store/peers.py:198`); and the replay override is ONE new `Gateway` Protocol method,
`async def channel_access_receipt(self, target_raw: str) -> dict | None` — `TelethonGateway` and `FakeGateway` return `None`
(`FakeGateway` returns fixture `channel_access` when given, so a test can prove the receipt beats the store), `RawReplayGateway`
serves the run's latest `ChannelAccess` whose `context.target == target_raw` through `_serve` (so `ReplayClock.for_payload(receipt)`
returns the original stamp — `clock.py:79`). The collector then does, in both modes: `receipt = await gw.channel_access_receipt(raw)
or compute_live(...)`; `add_raw("ChannelAccess", receipt, ...)`; `get_full_channel(receipt["input_channel"])`. Invariant (spec §2.6):
replay never reads the output store's `peers` for Step A. Precedent for a replay-only protocol surface: `join_channel` (synthetic in
replay) and `RawReplayGateway.replay = True` (`replay.py:669`).
**B. Does a handle target's `ChannelAccess` duplicate the `ResolvedPeer`?** Information-wise yes (its `input_channel` is copied from
the `ResolvedPeer.chats` entry). Keep both (spec §2.3): `ResolvedPeer` is Telegram's verbatim answer; `ChannelAccess` is paperboy's
decision receipt, and it is the ONE kind replay/catalogue read for "which channel did this (run, target) address" — `via: "handle"`
is the only record that route 3 was taken. The receipt's `key_source_raw_id` is the `ResolvedPeer` raw id (live store's id;
informational, documented as not a cross-store foreign key).
**C. Route 3 for an id target, and replay.** Live records the `ResolvedPeer` with context `{"target": raw, "handle": name}` (the
`handle` key is new; `RawReplayGateway.resolve` (`replay.py:703`) matches `ctx.handle == target_value` first, then today's
`parse_target(ctx.target).value == target_value`, so legacy records still match). If the handle resolves to a different id the
collector raises `SkipAndRecord("handle @x now belongs to channel M, not N")` AFTER writing a `ChannelAccess` with `"granted": false,
"resolved_channel_id": M, "input_channel": null`. Spec §4 says "records the attempt"; writing the refusal as a receipt is what lets
replay reproduce it from raw instead of recomputing (raw-first). Successful receipts carry `"granted": true`. **Operator decision 1:**
confirm this extension of the §2.3 payload (one extra boolean + one id). Without it a verification-failed run has a `ResolvedPeer`
for channel M and no receipt, and the #70 filter would file the run under M.
**D. `resolve_catalogue` pairing when both receipts exist.** One query over both kinds (`_kind_clause(("resolvedpeer",
"channelaccess"))`, `ORDER BY id`), keyed `(run_id, target)` as today (`replay.py:570-604`). For a `ChannelAccess` row:
`channel_id = payload.channel_id if payload.granted else None`; `username = previous record's username (from ResolvedPeer) or
payload.get("handle")`. Because the receipt is written after the `ResolvedPeer` (higher id), it wins the pair — same id, and a
refused verification correctly becomes a stray (`channel_id None`, `TargetFilter.decide_run` semantics unchanged, `reproject.py:54`).
`resolve_targets` (`replay.py:559`) likewise unions both kinds' `ctx.target`, first-seen order (a dict dedups the handle case).
**E. `min` rows vs `upsert_peer` after `getFullChannel`.** No change to `upsert_peer` (#38/#39 lattice, `store/peers.py:59-176`).
`ChatFull.chats` carries the target as a full `Channel` (`min` absent): the `INSERT..ON CONFLICT` richness clause flips `is_min`→0
and stores the full `access_hash` regardless of recency; provenance (`seen_in_chat/_msg`) is replaced by the NULLs channel.py passes
(recency guard) — acceptable and already today's behaviour for every chat in a `ChatFull`; the full key supersedes provenance for
route 1. Route 1 condition: `is_min = 0 AND access_hash IS NOT NULL`; a `min` hash is never used as a key (research §8.7).
**F. `ctx.input_channel` after Step B** is always rebuilt from the `ChatFull` chat (`{"channel_id", "access_hash":
chan_for_channel["access_hash"]}`) for every route (spec §2.4). Fixtures agree (`resolve_durov.json` and `full_channel.json` both
hash 99), so `test_channel_collector_sets_context_for_history` stays green. If `full.chats` lacks the channel (today's `else chan`
fallback at `channel.py:130`), raise `SkipAndRecord` for routes 1/2 (no `chan` exists); keep the fallback for route 3.
**G. `_record_matches` for `-100…`** needs no reproject change once `targets.py` normalises the marked form (task 2).
**H. Parity golden.** `tests/test_reproject_parity.py` pins `raw_records`; the new `ChannelAccess` row changes it by design.
Regenerate with `UPDATE_GOLDEN=1` in task 5's commit and state in the DoD that the golden diff is exactly the added rows. The parity
test code itself does not change. **Operator decision 2:** acknowledge the golden regeneration.
**I. "Nothing worked" (route 4)** writes no receipt (spec: `SkipAndRecord`); the run then has no target for replay and reproject
logs its existing "no resolve records" warning (`reproject.py:358`). Documented as a known limitation, not fixed here.
**J. Logging** names channels by id only: `log.info("channel access: id=%s via=%s", ...)`; never the handle.

## 1. Tasks (in order; one commit each)

**Task 1 — ADR amendments + plan file.** No test. Append to `docs/adr/0001-library.md` an "Amendment 2026-09-30 (#84)" section: the
seam gains an access step — `Gateway.channel_access_receipt`, the `from_msg` form of `input_channel` dicts accepted by every
channel-taking method (`_input_channel`/`_input_peer_channel` mirror `_input_user`), and the rule that an `access_hash` is only ever
obtained by the three §2.2 routes. Add to `docs/adr/0005-run-structure.md` point 5 one sentence: a run's targets are identified by its
`ResolvedPeer` **or `ChannelAccess`** records; `ChannelAccess` appears only in stamped runs, so legacy segmentation is untouched.
Add this plan at `docs/superpowers/plans/2026-09-30-collect-by-id.md`. Commit: `docs(adr): gateway access step and ChannelAccess
receipts (#84)`.

**Task 2 — targets: id forms.** Tests in `tests/test_targets.py`: parametrize `("123"|"-100123"|"t.me/c/123"|"https://t.me/c/123/456")
→ (PEER_ID, value "123")`; `parse_target("t.me/c/123/456").msg_id == 456`; `parse_target("-123")` raises `UnsupportedTarget` whose
message contains "basic group or user id"; update the existing `-1001234567890` row's expected value to `"1234567890"`. Code
(`targets.py`): add `_PRIVATE_LINK_RE = r"^c/(\d+)(?:/(\d+))?$"` matched BEFORE `_MSG_LINK_RE` (`targets.py:79`, else `c/123` parses
as a message link for user `c`); add `_MARKED_ID_RE = r"^-100(\d+)$"` before `_PEER_ID_RE`; `_PEER_ID_RE` becomes `^\d+$`, and a
leading `-` without `-100` raises `UnsupportedTarget`. Module docstring lists the forms. Commit: `feat(targets): accept channel ids
in bare, marked and t.me/c forms (#84)`.

**Task 3 — gateway builders + fakes.** Tests in `tests/test_input_user.py` (same style as `test_case_1_...`): `_input_channel({"channel_id":
5, "from_msg": {"channel_id": 7, "access_hash": 11, "msg_id": 3}})` is an `InputChannelFromMessage` with `peer` an `InputPeerChannel(7,
11)`, `msg_id 3`, `channel_id 5`; same for `_input_peer_channel` → `InputPeerChannelFromMessage`; the plain form still builds
`InputChannel`. In `tests/test_gateway_fake.py`: `FakeGateway.get_full_channel` with the `from_msg` form answers `full_channel` and
appends the dict to a new `gw.full_channel_inputs`; `await gw.channel_access_receipt("123")` is `None` without a fixture and the
fixture dict with `channel_access`; it is NOT appended to `gw.calls` (not an RPC; `test_gateway_fake.py:248` pins exact lists).
Code: `gateway.py:472/480` grow the `from_msg` branch; Protocol gains `channel_access_receipt` (docstring: "replay-only: live
gateways return None; Step A is computed from the store and recorded"); `TelethonGateway.channel_access_receipt` returns `None`.
Commit: `feat(gateway): from-message input channels and the access-receipt seam (#84)`.

**Task 4 — store: `input_channel_ref`.** Tests in `tests/test_store_peers.py` against a seeded `Store`: (a) full row → `{"channel_id",
"access_hash"}, via "saved_key", key_source_raw_id = row.source_raw_id`; (b) `min` row with provenance into a full-key chat →
`{"channel_id", "from_msg": {...}}, via "from_message"`; (c) `min` row whose `seen_in_chat` is itself `min` → `None` (min hash never
used); (d) `min` row without provenance → `None`; (e) no row → `None`; (f) `is_min=0` with `access_hash NULL` → `None`. Code
(`store/peers.py`, after `input_user_ref`): `def input_channel_ref(store, channel_id) -> ChannelRef | None` returning a small frozen
dataclass `ChannelRef(via, input_channel, key_source_raw_id)`; also `def stored_channel_username(store, channel_id) -> str | None`
(`channels.username` first, else `peers.username`). Commit: `feat(store): input_channel_ref mirrors input_user_ref for channels (#84)`.

**Task 5 — the id-first channel phase (live).** Tests in `tests/test_collector_channel.py` (fixtures: `self`, `full_channel` for id 5;
a helper seeds `peers` rows via `upsert_peer` with a dummy raw id):
- `test_id_target_saved_key_takes_route_1`: `parse_target("5")`, full row → `"resolve" not in gw.calls`; `gw.full_channel_inputs[0]
  == {"channel_id": 5, "access_hash": 99}`; the `ChannelAccess` raw row (kind exact `ChannelAccess`, `context_json` target "5",
  channel_id 5) has `via == "saved_key"`, `granted` true, and a LOWER id than the `ChatFull` row.
- `test_id_target_from_message_takes_route_2`: min row for 5 with `(seen_in_chat=7, seen_in_msg=3)`, full row for 7 → `via ==
  "from_message"`, `input_channel["from_msg"] == {"channel_id": 7, "access_hash": 11, "msg_id": 3}`, no `resolve` call; afterwards
  `ctx.input_channel == {"channel_id": 5, "access_hash": 99}` and `peers` has `is_min=0, access_hash=99` for `tg:channel:5`; a second
  `collect` on a fresh ctx takes route 1 (`via == "saved_key"`, `gw.full_channel_inputs[1]` is the plain form).
- `test_id_target_min_hash_is_never_a_key`: min row for 5 with a hash, no usable provenance, no username → `SkipAndRecord` whose
  message names all three routes; no `ChannelAccess` row; no `get_full_channel` call.
- `test_id_target_stored_handle_verified`: `channels.username="durov"` for id 5 (seed via `upsert_channel`), no key → `gw.calls`
  contains `resolve`; `ResolvedPeer` context is `{"target": "5", "handle": "durov"}`; receipt `via == "handle"`, `handle == "durov"`,
  `key_source_raw_id` == the `ResolvedPeer` raw id; then `ChatFull`.
- `test_id_target_stored_handle_now_another_channel`: resolve fixture answers `peer.channel_id == 6` → `SkipAndRecord` mentioning
  both `6` and `5`; a `ChannelAccess` row with `granted == False`, `resolved_channel_id == 6`, `input_channel is None`; no `ChatFull`.
- `test_handle_target_unchanged_plus_receipt`: `@durov` → the existing call order (`get_self`, `resolve`, `get_full_channel`) and a
  `ChannelAccess` `via == "handle"` after the `ResolvedPeer`.
- `test_replay_receipt_beats_the_store`: fixture `channel_access` = a saved_key receipt for 5 while the store has NO row for 5 →
  route 1, no resolve, the recorded receipt equals the fixture dict.
Code (`collectors/channel.py`): split `collect` into `_step_a_access(ctx) -> tuple[dict receipt, dict | None resolved, int | None
resolve_raw_id]` and the unchanged Step B. Step A order: `gw.channel_access_receipt(raw)`; else id target → `input_channel_ref`
(routes 1–2) → `stored_channel_username` + `gw.resolve(handle)` + `_resolved_channel_id == requested` check (route 3) → route 4
`SkipAndRecord` naming the routes and what would make each work (spec §2.2.4). Handle target → today's resolve, receipt `via:
"handle"`. Receipt payload exactly spec §2.3 plus `granted`, `handle` (route 3), `resolved_channel_id` (refusal). `add_raw(
"ChannelAccess", receipt, ctx.tier, {"target": raw, "channel_id": cid}, observed_at=ctx.clock.for_payload(receipt))` — before
`get_full_channel`. The peer loop at `channel.py:147` iterates `[(resolve_raw_id, resolved, t)] if resolved else []` + the
`ChatFull`. `ctx.input_channel` per §0.F. Regenerate the parity golden (`UPDATE_GOLDEN=1 uv run pytest tests/test_reproject_parity.py`)
and verify with `git diff --stat` that only `tests/fixtures/reproject/parity_golden.json` changed and only by added `ChannelAccess`
rows. Commit: `feat(channel): id-first Step A with a ChannelAccess receipt (#84)`.

**Task 6 — replay + reproject read the receipt.** Tests in `tests/test_replay_gateway.py` / `tests/test_profile_split.py` (reuse
`seed_two_target_source`; add a third run collected with `parse_target(str(ALPHA_ID))` after seeding alpha's key, so its Step A is
route 1 — build it by running `collect_channel` with a `FakeGateway` whose `resolve` fixture is a `SkipAndRecord` to prove no
resolve happened):
- `RawReplayGateway.channel_access_receipt("5")` serves the run's receipt and stamps the clock; unknown target → `None`.
- `RawReplayGateway.resolve("durov")` matches a `ResolvedPeer` whose ctx is `{"target": "5", "handle": "durov"}`; legacy ctx
  (`{"target": "@durov"}`) still matches.
- `resolve_targets(run3) == ["5"]`; `resolve_catalogue()` has one record for `(run3, "5")` with `channel_id == 5`; a run with both
  kinds (handle target) yields ONE record whose `username` comes from the `ResolvedPeer`; a `granted: false` receipt yields
  `channel_id None`.
- Reproject of the three-run source to a fresh output, with the output store's `peers` deliberately pre-seeded with a WRONG hash
  for 5 (open the output `Store` first, `upsert_peer` a full row with hash 1), produces `ChannelAccess` rows equal to the source's
  (`via`, `input_channel` identical) — Step A was served, not recomputed; `channels`/`peers`/`channel_snapshots` rows for 5 match
  a source-side query.
- `--exclude-target 5` and `--exclude-target -1005` both exclude run 3 (log line `decision=excluded`); `--include-target 5`
  replays only it. `runs()` on the three-run source is unchanged by the receipts (assert `len == 3` and the same `lo/hi` with the
  `ChannelAccess` rows deleted from a copy).
- `tests/test_reproject_parity.py` passes unchanged (golden regenerated in task 5).
Code: `replay.py` — `channel_access_receipt` on `RawReplayGateway` (`_latest(("channelaccess",), ("target",), (raw,))`, exact
`mode="exact"` is not needed, suffix works); `resolve` handle match (§0.C); `resolve_targets`/`resolve_catalogue` (§0.D);
`ResolveRecord` docstring. Commit: `feat(replay): identify id-started runs from ChannelAccess (#84)`.

**Task 7 — `status`/`export` by id.** Tests in `tests/test_cli.py`: after a `collect` through the fake, `status 5`, `status -1005`
and `export 5 --out ...` succeed; `status 6` exits 1 with the "No local data" message. Code: `cli.py:73` `_find_channel_id(store,
target: Target)` — `PEER_ID` → `SELECT id FROM channels WHERE id=?`; username path unchanged; update both call sites
(`cli.py:366, 416`). Commit: `feat(cli): status/export accept channel ids (#84)`.

**Task 8 — docs (DoD).** Update: `docs/features/collect-channel.md` (Inputs: id forms; How it works: Step A routes and the receipt;
delete the "numeric-id targets aren't resolvable" limitation at lines 76-78; add the #84 smoke transcript section); `README.md`
command table row for `collect TARGET` (TARGET forms) and the `status`/`export` rows; `CLAUDE.md` (commands paragraph + the
"In progress on dev/media-storage" status line gains #84); `docs/how-it-works.md` §2 (a target is a handle or an id; the account
must already have been shown the channel); `docs/data-model.md` `raw_records` section (a "paperboy-authored kinds" note listing
`ChannelAccess` with its payload keys, beside `MediaDownload`/`RosterWalled`); `docs/features/reproject.md` §"Run structure" (targets
come from `ResolvedPeer` or `ChannelAccess`; `--exclude-target -100…` accepted). Commit: `docs: collect by id (#84)`.

**Task 9 — smokes + DoD report** (no code unless a smoke finds a bug — then fix it in this branch, no-shed, with a test).

## 2. Smoke plan

**Offline first** (paste verbatim): `uv run pytest -q --basetemp=...` summary line; `uv run ruff check`; `uv run pyright`;
`git diff --name-only origin/dev/media-storage...HEAD`.

**Scratch store.** `sqlite3 <real>/default/paperboy.sqlite ".backup '<scratch>/84/default/paperboy.sqlite'"`; every live command
runs with `PAPERBOY_DATA_DIR=<scratch>/84 PAPERBOY_REQUIRE_PROXY=false` and `--profile default --phases channel --max-rpc 20
--max-flood-sleep 60`. No `--media`, `--web`, `--join`, `--unsafe`, `--profiles`. Before each live command: the STOP flag check,
the live-call counter (append first), and the VPN check from the run rules — paste all three outputs with the transcript.

**Picking the two channels** (run against the scratch backup, never the real file; never print the ids into anything committed):
```
-- X = the two ids named in the run rules' PRIVATE section (never collected, never used as a from-message peer)
-- full key, previously collected, with a handle (used by smokes 1, 3-check and 4):
SELECT c.id, c.username FROM channels c JOIN peers p ON p.uri=c.uri
 WHERE p.is_min=0 AND p.access_hash IS NOT NULL AND c.username IS NOT NULL AND c.id NOT IN (X) ORDER BY c.last_seen DESC LIMIT 1;
-- min with provenance into a full-key chat (smoke 2, then smoke 3):
SELECT p.id, p.seen_in_chat, p.seen_in_msg, p.last_seen FROM peers p WHERE p.kind='channel' AND p.is_min=1
   AND p.seen_in_chat IS NOT NULL AND p.seen_in_msg IS NOT NULL AND p.id NOT IN (X) AND p.seen_in_chat NOT IN (X)
   AND EXISTS (SELECT 1 FROM peers c WHERE c.uri='tg:channel:'||p.seen_in_chat AND c.is_min=0 AND c.access_hash IS NOT NULL)
 ORDER BY p.last_seen DESC;
```
Measured on 2026-09-30: 11 full-key candidates with a handle; 3 `min` candidates, none with a username (they may be private
channels — see failure modes). Record the chosen ids only in `<scratch>/84/smoke-ids.txt`.

**Live (≤ 4 invocations; cap 5 leaves one spare for the private-channel case below):**
1. `collect <id-full> --phases channel …` → table shows `channel` complete; log has `channel access: id=<id> via=saved_key`; SQL:
   `SELECT json_extract(payload_json,'$.via') FROM raw_records WHERE kind='ChannelAccess' ORDER BY id DESC LIMIT 1` = `saved_key`;
   `SELECT count(*) FROM raw_records WHERE run_id=<run> AND lower(kind) LIKE '%resolvedpeer%'` = 0.
2. `collect <id-min> --phases channel …` → `via=from_message`; receipt `input_channel.from_msg` present; afterwards
   `SELECT is_min, access_hash IS NOT NULL FROM peers WHERE uri='tg:channel:<id-min>'` = `0|1`.
3. `collect <id-min> --phases channel …` again → `via=saved_key`, zero resolve rows.
4. `collect @<handle-of-id-full> --phases channel …` → unchanged behaviour: one `ResolvedPeer` then `ChannelAccess via=handle`.

**Offline reproject check:** `PAPERBOY_DATA_DIR=<scratch>/84 uv run paperboy reproject --profile default --out
<scratch>/84/out/paperboy.sqlite` (expect minutes: 65k rows, 60+ runs — #75 made this tractable); then
`SELECT json_extract(payload_json,'$.via'), count(*) FROM raw_records WHERE kind='ChannelAccess' GROUP BY 1` in source and output
(equal); then `… reproject … --exclude-target <id-min> --out <scratch>/84/out/excl.sqlite` and paste the two
`reproject: run=… target=… decision=excluded` lines (ids redacted) and the output's `ChannelAccess` count = source's minus 2.

## 3. Edge cases and failure modes

- `-123` (no `-100`): rejected at parse time with the basic-group/user message (spec §2.1). `t.me/c/<id>/<msg>`: id target, msg ignored.
- Smoke 2's `min` channel may be private: `getFullChannel` answers `CHANNEL_PRIVATE` → `SkipAndRecord`, phase `skip`. The receipt
  (`via=from_message`) is still written before the call — that is the evidence for route 2; smoke 3 then cannot show the saved key.
  Use the ONE spare invocation on the next `min` candidate; if that also skips, report smoke 3 PENDING with the skip output. Never
  retry the same id.
- `MSG_ID_INVALID` on route 2 (the referencing message was deleted): `SkipAndRecord`; the receipt records the stale provenance.
- Route 3 handle moved to another channel: refusal receipt (`granted: false`), `SkipAndRecord`, no `ChatFull`, later phases skip.
- Route 4: no receipt; the run only has the self record; reproject warns and drops it (known limitation; say so in the feature doc).
- `web` phase with an id target: `_resolve_username` (`collectors/web.py:55`) already falls back to `channels.username` and skips
  when none — no change needed; add one test only if touched.
- Replay of a legacy (NULL `run_id`) source: no `ChannelAccess` anywhere; `channel_access_receipt` returns `None`, handle targets take
  route 3 exactly as today. Replay of a stamped handle-run from before this feature: same.
- `FLOOD_WAIT > 60 s`, `PEER_FLOOD`, `FROZEN_METHOD_INVALID`, auth error, VPN check failure: touch the STOP flag, write the reason,
  no further live calls, mark remaining smokes STOPPED, finish offline work.
- Never log or print a handle beside an id in committed text; transcripts in the repo use `<id>` and `@<channel>`.

## Orchestrator decisions (2026-09-30, binding)

1. **`granted` field:** approved. Every `ChannelAccess` carries `granted: true|false`. A failed route-3 verification writes `granted: false` + `resolved_channel_id`, replay reproduces the refusal from raw, and the #70 filter never files such a run under the wrong channel. Add this to the spec's §2.3 in your docs commit.
2. **Parity golden:** regenerate with `UPDATE_GOLDEN=1`, but paste the golden diff summary in the DoD. It must show only added `ChannelAccess` raw rows (and the raw-id shifts they cause), with no change to any projected table's content. Reviewers verify this.
3. **`-100…` normalisation:** approved. Update the `tests/test_targets.py` expectation as the spec §2.1 requires.
4. **Live calls:** up to 5 (the overview cap). The 5th is only for one alternate route-2 candidate if the first is refused. A `CHANNEL_PRIVATE` refusal is a recorded finding, not a stop condition; every protocol stop condition still applies.
