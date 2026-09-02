# Feature: person layer — `participants` + `profiles`

**Status:** implemented on `feat/person-layer` (#41; PRs #45/#47/#48/#49 —
schema+store+gateway, `profiles`, `participants`, reproject-replay+wiring).
**Spec:** `docs/superpowers/specs/2026-08-26-person-layer-design.md`.
**Plan:** `docs/superpowers/plans/2026-08-27-person-layer.md` (D3/D6 amended).
**ADR:** `docs/adr/0006-person-layer-storage.md`.

## Purpose

Turn the `min` peer stubs paperboy already collects into full people, and
discover the linked discussion group's roster — within Telegram's hard walls,
read-only and passive by default. Two collectors, both in the default set:

- **`participants`** — the discovery front-end: enumerate the linked group's
  roster where permitted (`channels.getParticipants` `Recent`, un-joined),
  the per-user `channels.getParticipant` oracle for known users a partial
  roster missed, `--join` → `Recent ∪ Admins ∪ Bots`, plus the zero-RPC
  vectors (join/leave service messages, the `recent_reactions` sample, the
  #11 forward/mention backfill) and the bounded `messages.getMessageReactionsList`.
  Records join dates, rank, status, and `member_of`/`admin_of`/`invited_by`/
  `added_by`/`reacted_to` edges.
- **`profiles`** — the single enrichment authority: batched `users.getUsers`
  triage for *every* discovered user (always on), then `users.getFullUser` +
  `photos.getUserPhotos` + avatar download under `--profiles`, priority-ordered
  (admins → authors → commenters → others), budgeted, and converging across
  runs.

## Inputs

- `paperboy collect TARGET` runs both by default (`--phases` may select a
  subset; `participants`/`profiles` each need `channel` in the same run).
- `--profiles` turns on full enrichment (off = `getUsers` triage only + a
  warning naming what was not fetched). `--profile-budget N` (default 2000)
  caps `getFullUser` per run; `--profile-interval SECONDS` paces the
  per-user profile RPCs through the budget chokepoint; `--profile-refresh-after
  DURATION` (e.g. `7d`) skips re-enriching users seen more recently.
- `--join` joins a linked group that gates reading (`join_to_send`) — the one
  audited write, off by default. `--unsafe` skips the doctor gate and the
  per-phase session-age gate.
- Env: `PARTICIPANT_ORACLE_BUDGET` (100), `PARTICIPANT_REACTIONS_BUDGET`
  (200), `ENRICH_PROFILES`, `PROFILE_INTERVAL`, `PROFILE_REFRESH_AFTER`,
  `UNSAFE` mirror the flags.

## Outputs

- `users` (current profile state, tri-state `field_states_json`),
  `user_snapshots` (append-only per-method observation log), `user_photos`
  (dated avatar history + downloaded sha), `participants` (roster membership
  facts keyed `(group_id, uri)`), `participant_snapshots` (per-run enumerated
  set + `enumerated/true_count` accounting rows). Migration `0004_people.sql`.
- `edges`: `member_of`, `admin_of`, `invited_by`, `added_by`, `reacted_to`.
- `raw_records`: `users.UserFull`, `channels.ChannelParticipants`,
  `channels.ChannelParticipant`, `photos.Photos`/`PhotosSlice`,
  `messages.MessageReactionsList`, `account.PrivacyRules`, and the synthetic
  `RosterWalled` / `UserNotParticipant` / `AvatarDownload`.
- `run_events`: `roster`, `roster_walled`, `admin_only_skipped`,
  `privacy_posture`, `preflight_mismatch`, `oracle_walled`, `join`, and
  `warning` (`session_age_gate` / `roster_partial` / `profiles_enrichment_off`
  / `reaction_list_truncated`).
- `sync_state('profiles', <channel_id>)`: the convergence summary; the
  `profile_attempts` table is the rotation cursor.
- `paperboy status` shows `users` and `participants` counts; `paperboy
  reproject` rebuilds all five person tables from raw.

## How it works

Behaviour statements live in the code's own docstrings (the reproject doc's
single-source-of-truth rule); this section only orients:

- `src/paperboy/collectors/participants.py` — the walls, the roster
  accounting, the oracle, `--join`, the reaction vector. `_roster_wall_reason`
  returns `(reason, terminal)` (broadcast terminal; hidden-member bulk-only —
  the oracle+reaction fallback still runs). One join site with a membership
  check (`_already_member` / `prejoined`).
- `src/paperboy/collectors/profiles.py` — triage, the `--profiles` sweep, the
  `profile_attempts` rotation (never derived from `enriched_at`), photos +
  avatars. `src/paperboy/collectors/posture.py` — the once-per-run privacy
  posture, shared with `participants`.
- `src/paperboy/store/users.py` / `participants.py` / `message_peers.py` /
  `reactions.py` — the projections; `store/users.py`'s module docstring is the
  tri-state / two-benchmark authority.
- `src/paperboy/replay.py` / `reproject.py` — the seven person-layer replay
  methods, `detect_phases`, and the per-run replay settings (ADR-0006 §8).

## Edge cases handled

Walled broadcast roster (recorded, zero enumeration RPC); no linked group (a
full phase skip, but the zero-RPC vectors on the target's own posts still
run); `participants_hidden` group (bulk-walled → oracle + reactions fallback);
session-age gate refusal before any group RPC; a preflight answering for the
wrong channel / carrying no `Channel` object; the `getParticipant` oracle wall
(`CHAT_ADMIN_REQUIRED`); `--join` never re-joining an already-member group nor
joining a linked broadcast; reaction lists bounded, resumable and hard-capped;
a permanently-failing `getFullUser` never starving the refresh rotation; a
`UserEmpty` triage answer never spending an enrichment slot; a `min` reactor's
identity never clobbered by the reaction write; `PhotoEmpty` never recorded as
a photo; the collecting account never projected as a subject (#12).

## Known limitations

- The `channelParticipantsMentions(top_msg_id)` per-thread-root union is
  deferred (its identities are redundant with a completed `discussion` sweep;
  see #41 follow-up); reactor lists are never fetched on a broadcast.
- Photo history is the first `getUserPhotos` page (limit 100) per run.
- Avatar downloads are sequential (no media-DC parallelism yet).
- A reproject of a multi-target profile serves per-user records by `user_id`
  within a run (plan D14); reprojected bookkeeping has two documented
  residuals (#50).

## Definition-of-Done smoke transcript

**Round-trip smoke (offline, no network) — PASSED.** The full default set
(`channel, history, discussion, participants, profiles, graph`) run with
`enrich_profiles=True` against `FakeGateway` fixtures into a real `Store`,
then `paperboy reproject`, round-trips all five person tables row-for-row; a
triage-only source reprojects triage-only. Driver + transcript:
`/Volumes/Storage/tmp/leg4_smoke.py` (leg-4 DoD). The person-layer replay
round-trip is also pinned by `tests/test_reproject_people.py` and the
regenerated frozen-clock golden (`tests/test_reproject_parity.py`), and each
collector's behaviour by `tests/test_collector_participants.py` /
`tests/test_collector_profiles.py`. Full suite: 586 passed, ruff + pyright
clean (leg-4).

**Live-Telegram smoke — DEFERRED (environment gap, handed off).** The design's
live DoD (`@national_resistance_movement` + its linked group *NRM Chat*, 307
members) MUST run on the main thread (Keychain) and requires the configured
proxy. In the implementing environment `require_proxy` is set but no proxy is
configured, so `doctor` blocks `collect` — running it would mean a direct-IP
connection from the collecting account, the exact opsec exposure the proxy
guards against. This is a verifier handoff, not "done":

> With the opsec proxy configured (`PAPERBOY_PROXY=socks5://…`) and a valid
> session in the Keychain, on the main thread:
>
> 1. `uv run paperboy doctor --profile default` → PASS (proxy present).
> 2. `uv run paperboy collect @national_resistance_movement --profile default`
>    (default set, passive — no `--join`). Expect: `RosterWalled` for the
>    broadcast channel (zero enumeration RPC against it); NRM Chat's `Recent`
>    roster enumerated with an `enumerated/307` accounting row; join dates /
>    rank / `member_of`/`admin_of` edges; `getUsers` triage over every
>    discovered user + the `profiles_enrichment_off` warning; `privacy_posture`
>    recorded once.
> 3. `uv run paperboy collect @national_resistance_movement --profile default
>    --profiles --profile-budget 5` → 5 `getFullUser` + photo history +
>    avatars; tri-state states populated (look for a `hidden_from_you` with its
>    `why`); a second `--profiles` run enriches the next 5 (convergence).
> 4. `uv run paperboy reproject --profile default --out /tmp/np.sqlite` →
>    the five person tables round-trip.
> 5. Guardrails: no third-party phone in `paperboy.log`; no `joinChannel`
>    without `--join`; `run_events` `join` count 0.
>
> Paste the transcript here and into #41 when run.

Verification steps recorded in #41.
