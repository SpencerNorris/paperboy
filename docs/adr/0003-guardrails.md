# ADR-0003: Guardrails and opsec as enforced requirements

**Status:** accepted (2026-08-20); amended 2026-09-28 (#69)

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

## Amendment (2026-09-28, #69): conservative pacing and patient flood handling

### Context
The original guardrails paced every method at 1 s and treated any `FLOOD_WAIT`
over 60 s (or a second consecutive one) as a phase stop. The operator's policy
is to never push Telegram harder than we believe it tolerates, to honour
server-mandated waits rather than abandon a phase because a wait is long, and
to keep phases resumable.

### Options considered
- (a) Keep 1 s pacing and the 60 s stop threshold.
- (b) Multiply server-mandated waits by a safety factor.
- (c) Apply the factor only to intervals *we* assume, add a small margin to
  server waits, and sleep through long waits up to an operator-set ceiling.
  **Chosen.**

### Decision
- Every interval `Budget` enforces is `base x pacing_factor` (`--pacing-factor`,
  `PAPERBOY_PACING_FACTOR`, default 2.0, minimum 1.0). Base intervals are
  *assumptions*, not measured limits: `contacts.resolveUsername` 5 s, every
  other method 1 s; `--profile-interval` and the web collector scale the same way.
- A server `FLOOD_WAIT` of `s` seconds is waited as `applied = ceil(s x 1.1) + 5`.
  The margin avoids re-calling at the exact instant the window closes; it is
  not a multiplier because the server's number is not a guess.
  `flood_log` records both `seconds` and `applied_seconds`; the persisted
  cooldown uses `applied`.
- The ceiling is `flood_sleep_threshold` (default raised 60 -> 3600 s,
  `--max-flood-sleep`). `applied <= ceiling` is slept through with an INFO
  heartbeat every 60 s; `applied > ceiling` persists the cooldown and stops the
  phase (the next run waits it out).
- Up to 3 consecutive flood waits per call are slept and retried; a 4th stops
  the phase. Transient network errors retry 3 times at 5/10/20 s x factor;
  a 4th stops the phase. Every retry counts toward `max_rpc_per_run`.
- Unchanged: `PEER_FLOOD` / `FROZEN_METHOD_INVALID` / `AUTH_KEY_DUPLICATED` are
  hard stops; Telethon's own `flood_sleep_threshold: 0` stays, so every wait
  passes through `Budget`.

### Consequences
- Runs are slower by default (2x) and can sleep up to an hour per wait; the
  heartbeat keeps that visible. Operators wanting the old behaviour pass
  `--pacing-factor 1 --max-flood-sleep 60`.
- Retried calls re-invoke the factory, so streaming callers must reset state
  per attempt.

### Notes
Spec: `docs/superpowers/specs/2026-09-28-pacing-safety-factor-design.md`.
Plan: `docs/superpowers/plans/2026-09-28-pacing-safety-factor.md`. Issue #69.

## Amendment (2026-10-06, #63): GCS egress for media stores

`storage.googleapis.com` joins the outbound allow-list under three limits:

- Only when a GCS media store is configured (`media_store`), or, for
  `reproject`, when a `MediaDownload`/`AvatarDownload` receipt names a bucket
  that is in the explicit `media_store_buckets` allow-list. A receipt naming
  any other bucket is skipped with a WARNING and never fetched.
- Only for those buckets. The bucket must be in `media_store_buckets` at
  settings load, so a typo cannot send evidence to someone else's bucket.
- `reproject` is read-only against a bucket (ranged GETs). It still never
  touches Telegram, `t.me` or `web.archive.org`, and never writes or deletes
  in any bucket.

GCS traffic is not Telegram traffic, and it does **not** use `settings.proxy`.
That setting is an MTProto/SOCKS route for the Telegram session; the Google
client speaks HTTPS and would not honour it, and forcing it through the proxy
would route evidence bytes via an unrelated hop. On the operator's Mac the
system VPN already covers all egress; on the VM, egress policy is the VM's
(see `docs/opsec.md`). Credentials are Application Default Credentials only;
nothing is stored by paperboy or logged.
