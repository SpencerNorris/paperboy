# ADR-0001: MTProto library — Telethon behind a gateway seam

**Status:** accepted (2026-08-20)

## Problem
We need a Python MTProto user-account library that exposes the *entire* TL
schema (raw methods like `channels.getChannelRecommendations`,
`updates.getChannelDifference`, `premium.getBoostsList`), stays current with
Telegram's layer, gives us `to_dict()` on every object (for raw-first
persistence), and has a large body of prior art to borrow from.

## Options
- **Telethon 1.44.x** — layer 227 in the installed wheel (verified), MIT, pure
  Python, full `telethon.tl.functions.*` raw access, `to_dict()` everywhere,
  per-method `flood_sleep_threshold`, `*FromMessage` support for `min` peers.
  Development moved to Codeberg (GitHub repo archived — *not* abandoned).
- **Kurigram** (Pyrogram fork) — layer 228, LGPL, richer high-level API, single
  maintainer.
- **TDLib** (tdjson) — most correct, but hides `access_hash`/raw constructors
  and is a C++ dependency; loses raw-fidelity we need.
- **Pyrogram upstream** — dead (layer 158). **gotd (Go)** — great, wrong runtime.

## Decision
Telethon 1.44.x, **behind a thin `Gateway` Protocol** (`gateway.py`) that
returns plain dicts (`to_dict()`), never Telethon types. Collectors depend on
the Protocol, not on Telethon, so a future swap to Telethon v2 (an unreleased
Rust-backed alpha today) or Kurigram is a one-module change, and collectors are
testable against a `FakeGateway`.

## Consequences
- Every RPC we use must be reachable as a raw `functions.*` request (verified
  for the core set at layer 227).
- We own flood/pacing at the `Budget` layer rather than relying on library
  defaults, so behaviour is identical across a library swap.
- The wheel's layer (227) can lag the docs (228); we validate against the
  installed schema, not the docs' headline layer.

## Amendment 2026-09-30 (#84): the seam gains an access step
Collecting a channel by id (not only by handle) means the account must first
*obtain access* to it. Telegram gives no request that takes a bare id: every
channel request needs the id plus a per-account `access_hash`. So:

- `Gateway.channel_access_receipt(target_raw) -> dict | None` is a replay-only
  hook. Live gateways return `None` (Step A is computed from the store by the
  `channel` collector and recorded as a `ChannelAccess` raw record);
  `RawReplayGateway` serves the run's recorded receipts in order (one per
  route attempted), so replay never re-derives access from the output store's
  `peers`. A replay gateway sets `replay = True`; a replayed run whose first
  call returns `None` is a pre-#84 run and takes the legacy handle path with no
  receipt written.
- Every channel-taking gateway method accepts the `from_msg` form of an
  `input_channel` dict (`{"channel_id", "from_msg": {"channel_id",
  "access_hash", "msg_id"}}`), built by `_input_channel` /
  `_input_peer_channel` as `InputChannelFromMessage` /
  `InputPeerChannelFromMessage`, mirroring `_input_user`.
- An `access_hash` is only ever obtained by three routes: a saved full key, a
  from-message reference through a chat whose own key is known, or a verified
  handle lookup. A `min` hash is never used as a key; hashes are never guessed.
