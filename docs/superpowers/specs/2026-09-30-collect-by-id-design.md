# Collect a channel by id or handle: an id-first channel phase (#84)

**Status:** draft for operator review, 2026-09-30. **Tracking:** #84.
**Base:** `dev/media-storage` (#69, #62, #64, #75, #70 merged).
**Unblocks:** #68 (`fetch-media --list`), whose amendment will fetch by id
using this feature (a separate, follow-up spec).
**ADR:** an amendment to ADR-0001 (the gateway seam gains an access step) and a
one-line note in ADR-0005 (runs can now be identified by a `ChannelAccess`
receipt as well as a `ResolvedPeer`). Land the amendment before code.

## 1. Problem

The `channel` phase always reaches a channel through its handle:
`collectors/channel.py` calls `ctx.gateway.resolve(ctx.target.value)`
(`contacts.resolveUsername`). `targets.py` already parses numeric ids
(`TargetKind.PEER_ID`), and `is_channel_like` accepts them, but
`paperboy collect 123` sends `123` to `resolveUsername` and fails.

Handles are not identities: a channel can rename itself, and a freed handle
can be claimed by a different channel. So resolving by handle can land on a
channel other than the one the operator meant. The channel id is permanent.
Evidence collection should be able to address a channel by that id.

### 1.1 Telegram's rule (not ours)

Every request about a channel needs the channel id **plus** an `access_hash`.
That value is specific to the pair (collecting account, channel), is computed
by Telegram, and is only handed to an account when Telegram shows it the
channel (research §2.1: "per-session and non-transferable"; §8.7 on `min`
peers). There is no request that takes a bare id. So "collect by id" means
"obtain this account's access to channel N by any legitimate route, then
proceed by id". It does not mean skipping the access step, and paperboy must
never try to guess or brute-force an `access_hash`.

Measured on the operator's store (counts only, 2026-09-30): 49 channel peers.
45 have a full (non-`min`) `access_hash`. The other 4 are `min` rows with no
usable hash, but all 4 carry `(seen_in_chat, seen_in_msg)` provenance, so the
from-message route (§2.2 route 2) reaches them. Every stored channel is
therefore reachable by id.

## 2. Design: the channel phase becomes id-first

Retrofit the existing phase; there is no second code path. The phase splits
into **Step A (get access)**, which produces an `input_channel` and one receipt
saying how, and **Step B (everything else)**, which is today's code from
`get_full_channel` onward, unchanged and keyed by id. A handle becomes one way
to perform Step A.

### 2.1 Target forms

- `@name`, `name`, `t.me/name`: a **handle** target (today's behaviour).
- `123` and `-100123` (Telegram's "marked" channel id): an **id** target.
  Normalise the marked form to the bare id in one place (`targets.py`) and test
  both. A negative id without the `-100` prefix is a basic-group or user id:
  reject it with a clear message, since collecting non-channel peers is out of
  scope.
- `t.me/c/123` and `t.me/c/123/456` (the private-link form people share ids
  in): an **id** target; the message part is ignored by `collect`.

### 2.2 Step A: obtain access, in order

For an **id** target, try each route and stop at the first that works:

1. **Saved key.** A `peers` row for `tg:channel:<id>` with `is_min = 0` and a
   non-null `access_hash` → `{"channel_id", "access_hash"}`.
2. **From a message.** A `peers` row with `(seen_in_chat, seen_in_msg)`
   provenance into a chat whose own full hash is known →
   `inputChannelFromMessage(peer=<that chat>, msg_id, channel_id)`. This mirrors
   `store.peers.input_user_ref` and `gateway._input_user` exactly (research
   §8.7 sanctions both contexts). A `min` hash is never used as a key.
3. **Stored handle, verified.** If the row (or `channels`) has a username,
   resolve it by handle and accept the result **only if** it resolves to the
   requested id. Otherwise fail with "the handle @x now belongs to channel M,
   not N". Never proceed under the wrong id.
4. **Nothing worked:** stop the phase with an error that names the routes
   tried and what would make each work, e.g. "no saved key, no message
   referencing it in this store, no known handle. Collect a channel that
   forwards it, or supply its handle or invite link". This is a
   `SkipAndRecord` for the channel phase, which ends the target's collect
   because later phases need the channel.

For a **handle** target: route 3 without the verification (the handle is what
was asked for), i.e. today's `resolveUsername`. The resolved id is what the
rest of the run uses.

All routes go through `Budget` (route 3 is the existing
`contacts.resolveUsername` at its 5 s base). Routes 1 and 2 cost no RPC in
Step A.

### 2.3 The `ChannelAccess` receipt (raw-first)

Routes 1 and 2 take their input from a **projection** (`peers`), not from a
Telegram answer. Replay must not re-derive them from the output store, which
can differ from the source's. This is the #68 lesson: every input that shapes a
projection is itself recorded. So Step A always appends one receipt **before**
`get_full_channel`:

```
kind: ChannelAccess
payload: {
  "channel_id": 123,
  "requested": "<target.raw>",
  "via": "saved_key" | "from_message" | "handle",
  "input_channel": { "channel_id": 123, "access_hash": … }
                 | { "channel_id": 123, "from_msg": {"channel_id", "access_hash", "msg_id"} },
  "key_source_raw_id": <raw id the key or provenance came from, when known>,
  "granted": true | false,
  "handle": "<handle>"            (via "handle" only),
  "resolved_channel_id": M        (granted false only)
}
context: { "target": "<target.raw>", "channel_id": 123 }
```

For `via: "handle"` the `ResolvedPeer` receipt is still written, as today, and
`ChannelAccess` follows it. A failed route-3 verification (the stored handle now
belongs to channel M) is itself recorded as a receipt with `granted: false`,
`resolved_channel_id: M` and `input_channel: null` before the phase is skipped, so
replay reproduces the refusal from raw and the #70 filter never files the run
under M (orchestrator decision, 2026-09-30). `access_hash` values already appear in raw
`ResolvedPeer`/`ChatFull` payloads, so recording one here changes no exposure.
Exports keep scrubbing as today.

### 2.4 Step B: unchanged, and the verification point

`get_full_channel(input_channel)` runs as today, and the existing identity
check (`full_chat.id == requested id`, `channel.py` "refusing to split channel
identity") is the verification for **every** route. After it succeeds:

- The `chats` returned by `getFullChannel` carry a full, non-`min` `access_hash`.
  Switch `ctx.input_channel` to it for the rest of the run, and let
  `upsert_peer` save it. A channel first reached from a message is reachable
  by saved key next time.
- Everything downstream (history, discussion, participants, profiles, graph,
  media, web) already keys off `ctx.channel_id`/`ctx.input_channel` and needs
  no change. `web` still needs a username; for an id target, use the channel's
  current username from `ChatFull`, and skip `web` if it has none.

### 2.5 Gateway

`_input_channel`/`_input_peer_channel` gain the `from_msg` form (mirroring
`_input_user`), so every gateway method that takes an `input_channel` accepts
either shape. The `Gateway` Protocol does not change shape. `FakeGateway`
records which form it was called with, for tests.

### 2.6 Replay and reproject

- **Step A in replay is served from the `ChannelAccess` receipt,** never
  recomputed from the output store's `peers`. The mechanism is the planner's
  choice (for example, a gateway method that live code implements as
  "compute and record" and replay implements as "serve the receipt"). The
  invariant is: *replay takes the route and `input_channel` from the receipt*.
- A run whose target was an id has no `ResolvedPeer` for that target. Extend
  `ReplaySource.resolve_targets`, `resolve_catalogue` and anything else that
  identifies a run's channel from `ResolvedPeer` to also read `ChannelAccess`
  (`context.target` → `payload.channel_id`). The #70 split filter then
  classifies id-started runs by the same resolved channel id.
- Legacy runs (before this feature) have no `ChannelAccess` and behave exactly
  as today. `ChannelAccess` only appears in stamped runs, so the legacy
  opening-cluster segmentation in `runs()` is unaffected. Say so in a test.
- The reproject parity suite must pass unchanged.

### 2.7 Other commands

- `status TARGET` and `export TARGET` resolve a target via `_find_channel_id`
  (username only). Extend it to accept the id forms from §2.1, offline, via
  `channels.id`.
- `reproject --include-target/--exclude-target` already accepts a bare id; also
  accept the marked `-100…` form (this closes #83 item 4).

## 3. Guardrails

Unchanged: read-only, no join, every RPC through `Budget`, no new outbound
hosts, logs reference targets by id. Route 2 uses only message context the
store already holds. Route 3 is today's handle lookup, now with verification
for id targets. No route sends anything that alters state.

## 4. Tests (write first)

- `targets`: `123`, `-100123`, `t.me/c/123`, `t.me/c/123/456` parse to the
  same id target; `-123` (not `-100`) is rejected with a clear message.
- Step A, each route against a fixture store: saved key; from-message (and a
  `min` channel hash is never used); stored handle that resolves to the right
  id; stored handle that resolves to a **different** id (fails, names both ids,
  records the attempt); nothing works (SkipAndRecord with the routes named).
- A handle target behaves exactly as today (existing channel tests unchanged).
- `ChannelAccess` is written before `ChatFull` for every route, with the right
  `via` and `input_channel` shape.
- After a from-message start, `ctx.input_channel` switches to the full hash and
  `peers` stores it; a second collect of the same id takes route 1.
- `FakeGateway` sees the `from_msg` form on `get_full_channel`.
- Replay: a source whose runs were started by id (each route) reprojects to the
  same tables, with Step A served from `ChannelAccess` even when the output
  store's `peers` would give a different answer.
- `resolve_catalogue` and the #70 filter include id-started runs;
  `--exclude-target -100123` works.
- `status 123` / `export 123` find the channel.

## 5. Definition of done

Offline: all of §4, full suite, ruff and pyright green, parity suite unchanged.

Live, under the overview's smoke protocol (scratch `.backup` data dir, VPN
check before each call, `PAPERBOY_REQUIRE_PROXY=false`, `--max-rpc 20`,
`--max-flood-sleep 60`, ≤ 4 invocations, channels already in the store, never
the split-out investigation, no media):

1. `collect <id> --phases channel` for a channel with a saved key: route 1,
   zero resolve calls in the log.
2. `collect <id> --phases channel` for a `min` channel with message
   provenance: route 2, then the saved key is written.
3. The same id again: route 1.
4. `collect @<handle> --phases channel`: unchanged behaviour, `ChannelAccess
   via: handle`.

Then, offline, `reproject` the scratch store and show the id-started runs
counted in the source vs output table and classified by `--exclude-target
<id>`. Paste transcripts redacted (`<id>`, `@<channel>`).

Docs: `docs/features/collect-channel.md` (targets and Step A), README command
reference, CLAUDE.md, `docs/how-it-works.md` §2 (targets by id or handle),
`docs/data-model.md` (the `ChannelAccess` kind), and the ADR amendments.

## 6. Out of scope

- #68's fetch-by-id (its own amendment, which uses this feature).
- Collecting users or basic groups by id.
- Invite-link targets (unchanged).
- Discovering an `access_hash` any way other than §2.2. There is no other
  legitimate way.
