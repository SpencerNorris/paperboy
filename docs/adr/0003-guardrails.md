# ADR-0003: Guardrails and opsec as enforced requirements

**Status:** accepted (2026-08-20)

## Problem
The tool reads sensitive targets with a real Telegram account under ToS that
make bulk collection a gray area, and the operator wants to be invisible to
targets and pseudonymous to Telegram. Guardrails must be **enforced in code**,
not merely documented, or they will be skipped under time pressure.

## Decision
The spec §2/§3 rules are product requirements checked in code:
- **Read-only**: never send, react, vote, type, mark read, request to join, or
  `suggestBirthday`. Passive (un-joined) collection is the default; `--join` is
  explicit and prints what it exposes.
- **Every RPC through `Budget`** (per-method pacing, persisted cooldowns, daily
  cap); `FLOOD_WAIT` per-method; `PEER_FLOOD`/`FROZEN_METHOD_INVALID`/
  `AUTH_KEY_DUPLICATED` are hard stops.
- **Outbound HTTP allow-list** (`t.me`, `web.archive.org`), through the proxy;
  never dereference URLs found in collected content (issue #1).
- **Excluded**: `contacts.getLocated`, poll-voter collection, any add-member/
  invite capability, AI-training export. **Flag-gated**: phone lookup, joins.
- **`paperboy doctor`** enforces the account's opsec posture (privacy keys,
  2FA, minimal profile, session age, proxy) and blocks `collect` on failure.
- Credentials never logged; exports scrub the collecting account; tri-state
  privacy fields.

## Consequences
- Collectors cannot call the gateway raw — the `Budget` gate is mandatory.
- Some capabilities are deliberately unreachable; that is the point.
- The human-side opsec steps the tool cannot perform live in `docs/opsec.md`.
