# ADR-0006: Person-layer storage — a `users` table distinct from `peers`, tri-state fields, and a scheduling key distinct from the provenance benchmark

## Status

Accepted (2026-09-01). Implements the approved person-layer design
(`docs/superpowers/specs/2026-08-26-person-layer-design.md`) on
`feat/person-layer` across five legs (#41; PRs #45/#47/#48/#49). Amends the
plan's D3 and D6 (`docs/superpowers/plans/2026-08-27-person-layer.md`) with
what the leg-2/leg-3 reviews forced; records the reproject-fidelity residuals
as #50.

## Context

The person layer turns the `min` peer stubs paperboy already collects into
full people (`profiles`) and discovers the linked group's roster
(`participants`). Two storage questions had to be settled before either
collector could be written:

1. **Where does profile richness live?** `peers` is the min-provenance stub
   table whose `upsert_peer` merge lattice already carries two open
   order-dependence residuals (#38/#39). Folding bios, photos, birthdays,
   tri-state privacy signals and a per-run enrichment cursor into it would
   entangle every new field with that lattice.
2. **How is "absent" recorded?** Telegram enforces privacy by *omitting*
   fields, not by failing calls — there is no privacy-denial error. The
   constitution's "never record 'no photo'" MUST therefore needs a real
   storage shape, not a convention: absence and "hidden from you" are
   different facts and only the second is ever positively provable.

Two further questions surfaced only under review, each after a K=3 workflow
escalation on #41:

3. **What drives the `profiles` enrichment rotation?** The plan (D3) derived
   the position from `users.enriched_at`. That overloads one column with two
   incompatible contracts.
4. **What does reproject replay of the person layer need from `Settings`?**

## Options considered

- **A. Profile richness in `peers`** (extend the stub table). Rejected: ties
  every field to the #38/#39 lattice and the min-merge rules.
- **B. A `users` current-state table + `user_snapshots` append-only log**,
  mirroring `channels`/`channel_snapshots`; `peers` untouched. Chosen.
- **Tri-state:** (a) a `not_set | present | hidden_from_you` enum as the spec
  drafted it; (b) `present | absent | hidden_from_you`. Chosen: (b) — a plain
  wire absence can never honestly be called "not set" (that is the very "no
  photo" claim the constitution forbids); `not_set` is unprovable.
- **Rotation key:** (a) `users.enriched_at` (D3 as drafted); (b) a distinct
  `profile_attempts` table written where budget is spent. Chosen: (b).

## Decision

1. **`users` / `user_snapshots` / `user_photos`, never `peers`**
   (`0004_people.sql`). Profile richness — identity, tri-state field states,
   the enrichment benchmark, dated avatar history — lives in the new tables;
   `peers` stays the min-provenance stub table and its `upsert_peer` SQL is
   never touched by this layer. The full `User` objects the roster/profile
   RPCs return are still projected into `peers` (via `upsert_full_peer`,
   which preserves any stored `(seen_in_chat, seen_in_msg)` provenance), so a
   `min` stub is healed without the identity ever being lost.

2. **Tri-state is `present | absent | hidden_from_you`** in
   `users.field_states_json` (keys `phone, photo, status, about, birthday,
   forwards, stories`). `hidden_from_you` is written only with a
   machine-readable proof (`fallback_photo` present while `profile_photo` is
   absent; `private_forward_name` present); a `userStatus*` keeps
   `present` with `granularity`, and a coarse bucket's `coarse_cause` is
   `self_privacy` when `by_me` is set — *our* account's privacy degrading the
   data, not the target's opsec. Only a real `photo`/`Photo` constructor
   counts as a photo (a `PhotoEmpty`/personal-photo shadow is `absent`). The
   collecting account's own privacy posture is recorded once per run
   (`account.getPrivacy` for phone/lastseen/photo) so a `by_me` observation
   is attributable to us.

3. **One SELF/REL chokepoint.** Every `users` column, including the
   level-keyed `bot_json` (`{"user": …, "full": …}`), is derived from
   `target_user_facts` / `target_full_facts`, which strip every fact-about-us
   (`contact`, `bot_can_edit`, `blocked`, `common_chats_count`, `note`, …).
   The guardrail has a single enforcement point rather than a set plus a
   prefix heuristic (leg-1 review).

4. **`users.enriched_at` is a provenance benchmark; `profile_attempts` is the
   scheduling key (D3 as amended).** `enriched_at` moves only when full
   columns were actually applied — so it cannot also be the round-robin
   cursor, or a user whose `getFullUser` permanently fails sits at the head
   of every run and starves the rest (reproduced twice in the leg-2 review:
   first in the refresh pass, then, with `enriched_at IS NOT NULL` as the
   leading sort term, for a never-enriched permanent failure). The rotation
   is `profile_attempts (uri PK, attempted_at, outcome, detail)`, written at
   the ONE point a budget slot is spent (`attempted`, before the RPC) and
   replaced by whichever arm finishes it (`skipped`/`malformed`/
   `not_projected`/`enriched`) — structurally unskippable. `_enrichment_
   candidates` orders never-attempted first, then everyone strictly
   least-recently-attempted, so a permanent failure costs one slot per lap
   and can never block the refresh wrap. `profile_attempts` is bookkeeping
   like `sync_state` — excluded from round-trip identity.

5. **Synthetic raw kinds for observations with no TL payload** (precedent:
   `MediaDownload`): `RosterWalled` (a walled roster is a first-class stored
   outcome, never a silent zero), `UserNotParticipant` (the oracle's
   definitive negative), `AvatarDownload` (a downloaded avatar's sha/path).
   RPC-result envelopes are stamped with their TL namespace
   (`users.UserFull`, `channels.ChannelParticipants`, …) because Telethon's
   `to_dict()` emits the bare class name and the envelope collides with its
   inner object.

6. **New edge predicate `reacted_to`** (user → message), from the zero-RPC
   `recent_reactions` sample and the bounded `messages.getMessageReactionsList`
   vector (groups only; a broadcast answers `BROADCAST_FORBIDDEN`).
   `member_of`/`admin_of`/`invited_by`/`added_by` are as the design reserved.

7. **A structural wall is terminal; a hidden-member wall is bulk-only (leg-3
   review).** `_roster_wall_reason` returns `(reason, terminal)`: a broadcast
   peer is terminal (the per-user oracle is `CHAT_ADMIN_REQUIRED` too), but a
   `participants_hidden` / `can_view_participants=false` group is walled for
   BULK enumeration only — the `getParticipant` oracle and the reaction-list
   vector are exactly the designated fallback there (spec §5/§6.2), so the
   roster is still built and those vectors still run.

8. **Reproject replays the person layer per run with lifted budgets and
   `allow_join`/`unsafe` (D6 as amended).** Per historical run,
   `enrich_profiles` is set from whether that run recorded a `users.UserFull`;
   `unsafe=True` (the session-age gate has no live RPC to protect on replay);
   `allow_join=True` (a `--join` source's sweep must not be skipped); and
   `profile_budget` / `participant_oracle_budget` / `participant_reactions_budget`
   are all lifted, because the live budget already bounded *what was recorded*
   and a smaller replay budget would drop recorded observations. The five
   person tables round-trip identically (collect → reproject); `profile_attempts`
   and `sync_state` are excluded as bookkeeping.

## Consequences

- The `#38`/`#39` `peers` lattice is untouched; profile richness has its own
  home and its own, simpler recency/richness rules in `store/users.py`.
- "no photo" is never recorded; a Datasette reader sees `absent` vs
  `hidden_from_you` (with the proof) vs `present`, and a coarse status with
  its cause.
- The enrichment sweep converges across runs and cannot be starved by a
  permanently failing user; the choice is replay-deterministic.
- Reproject rebuilds the person layer like every other phase (ADR-0002
  "rebuildable from raw" holds for the new tables).
- **Residual (#50):** reproject's lifted budget and forced `allow_join`
  leave two divergences in the *reprojected* DB's bookkeeping — a phantom
  `participants`/`join` `run_events` row on a passively-enumerated source,
  and the lifted `profile_budget` written verbatim into `sync_state`. Both
  are excluded from round-trip identity (no data table, no round-trip test
  affected); tracked for a clean fix or an accepted-residual note.
- The default collector set is now `channel, history, discussion,
  participants, profiles, graph`; `participants` and `profiles`-triage are
  read-only and bounded, so both are default-on. Full profile enrichment
  (`getFullUser`/photos/avatars) stays behind `--profiles`.

## Notes

- Spec: `docs/superpowers/specs/2026-08-26-person-layer-design.md`.
- Plan (with the D3/D6 amendments): `docs/superpowers/plans/2026-08-27-person-layer.md`.
- Feature doc + DoD smoke: `docs/features/person-layer.md`.
- Builds on ADR-0002 (raw-first), ADR-0003 (guardrails/budget), ADR-0005
  (run structure / reproject).
- Reproject-fidelity residual: [#50](https://github.com/SpencerNorris/paperboy/issues/50).
