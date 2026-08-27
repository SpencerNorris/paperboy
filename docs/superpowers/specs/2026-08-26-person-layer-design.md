# The person layer — `participants` + `profiles` collectors

**Status:** approved 2026-08-27. Realizes the entity-graph thesis (CLAUDE.md §1:
"architected as an entity graph so later recipes — user dossier, phone lookup,
watchlists — are thin additions") by filling in the *people*: discovering the
linked group's roster where the walls permit, and enriching every discovered
peer from a `min` stub into a full person.
**Tracking:** issue #41 (Gate A anchor); part of the Phase-2 umbrella #13.
**Execution:** one `single-feature-run` on `feat/person-layer`. See §14 for the
implementing session's context.
**Depends on:** PR #40 (reproject) — **merged to `main` (2026-08-27)**. §10
(reproject replay support) extends `RawReplayGateway`, which #40 shipped; the
collectors themselves do not depend on it, only §10 does.

## 1. Goal

Turn the `min` peer stubs paperboy already collects into full people, and
discover the members it has not yet seen — within Telegram's hard walls, and
without abandoning passive-by-default collection. Two collectors:

- **`participants`** — the discovery front-end: enumerate the linked discussion
  group's roster where permitted, record per-member roster facts (join date,
  rank, status), emit membership edges, and opportunistically write the full
  `User` objects the roster RPCs return for free.
- **`profiles`** — the single enrichment authority: sweep *every* discovered
  peer, batch-triage with `users.getUsers`, then fetch full profiles
  (`users.getFullUser`, `photos.getUserPhotos`) on a priority order within
  budget, writing tri-state profile state to new tables.

Recipe slot (already committed in the 2026-08-20 design §6):
`channel → history → discussion → **participants → profiles** → graph → web`.
Both new collectors join the **default set** (`channel, history, discussion,
participants, profiles, graph`; `media`/`web` stay opt-in). `participants` runs
fully by default — it is read-only and bounded. `profiles` runs its **cheap
`getUsers` triage by default** (everyone gets basic identity), but performs the
**expensive full enrichment** (`getFullUser`/photos/…, up to ~33 min) **only
under `--profiles`**; without that flag a warning names the un-run enrichment so
a plain `collect` never silently becomes a long job (§7, §7.2).

## 2. The person-vector reality (why this shape)

For a **broadcast channel** — paperboy's primary target — a non-admin account
can enumerate *nothing* about subscribers, and **joining buys nothing**: the
member list, admin list, reactors, and poll voters are all `CHAT_ADMIN_REQUIRED`
/ `BROADCAST_FORBIDDEN` for anyone below admin, a tier paperboy will never hold
(`docs/research/sources/mtproto-participants-users.md` §1.3; settled empirically
in `telegram-extraction-surface.md` §7 item 5). The people live in the channel's
**linked discussion supergroup**, which is exactly where the shipped `discussion`
collector already sweeps.

So the discovery of the *active* population — everyone who has posted or
commented — **has already happened**: those people sit in `peers` right now as
`min` stubs with `(seen_in_chat, seen_in_msg)` provenance, from
`history`/`discussion`/`recent_repliers`. The person layer's two remaining jobs
are therefore:

1. **Enrich** those stubs into full people (`profiles`) — passive-safe, no join,
   no new tier, high yield.
2. **Discover the lurkers** — members who never posted (`participants`) — which
   is member-tier, server-capped, and needs `--join` to go past the free
   vectors.

## 3. Enrichment vs. discovery is per-vector

The "enrich during or after discovery" question has two answers, by vector:

- **Roster RPCs enrich *during*.** `channels.getParticipants` /
  `channels.getParticipant` return a `users: Vector<User>` of *full* objects
  alongside the participant list — discovery hands enrichment over for free.
  `participants` writes those straight into `users`.
- **Message-discovered stubs enrich *after*.** A `min` author carries no
  resolvable `access_hash`; it can only be enriched via
  `inputUserFromMessage{peer, msg_id, user_id}` built from its stored
  `(seen_in_chat, seen_in_msg)` provenance. That is a separate `getFullUser`
  call, done by `profiles`.

`profiles` remains the *single* enrichment authority (it sweeps everyone,
including whoever `participants` already enriched — idempotent); `participants`
only opportunistically populates what its own RPCs return.

## 4. New tables (migration `0004_people.sql`)

Current-state + append-only-snapshot split, mirroring `channels`/
`channel_snapshots`. Same conventions as every existing table: `uri` PK where
applicable, `source_raw_id`, `first_seen`, `last_seen`, JSON text for structured
sub-objects.

### `users` — current profile state

One row per user. Profile richness lives **here, never in `peers`** — `peers`
stays the min-provenance stub table, so this design never touches the
`upsert_peer` merge lattice or its open bugs (#38/#39).

| Column | Notes |
|---|---|
| `uri` PK, `id`, `access_hash`, `tier`, `is_min` | `tier ∈ stranger\|member\|contact\|admin\|self` (spec §5); `is_min` true only if we have *never* seen a non-min object for this user |
| `username`, `usernames_json` | `usernames_json` keeps the full `[{username, editable, active}]` structure — the Fragment/collectible signal `primary_username()` discards today |
| `first_name`, `last_name`, `phone`, `about` | `phone` only when Telegram's privacy rules actually reveal it (a real third-party fact); the *collecting* account's own phone is still stripped (#12 precedent) |
| `birthday`, `emoji_status`, `color_json` | |
| `status_kind`, `status_value` | `userStatus*` discriminator + its timestamp/bucket |
| `photo_ref`, `restriction_json`, `bot_json` | photo id + dc for the avatar-download path; `bot_json` folds the bot-only fields |
| `field_states_json` | the tri-state map — §4.3 |
| `source_raw_id`, `first_seen`, `last_seen` | |

### `user_snapshots` — append-only observation log

One row per `getUsers`/`getFullUser` observation: `uri`, `observed_at`, `tier`,
`content_hash` (of the observed bundle, to dedupe no-change re-observations like
`message_revisions` does), the observed field bundle as JSON, `source_raw_id`.
This is the time series; `users` is its current-state projection.

### `participants` — roster membership facts

Keyed `(group_id, user_id)`. `uri` (the user), `group_id`, `status`
(`member`/`admin`/`creator`/`banned`/`left`), `join_date` (from
`channelParticipant.date` — the prize: a join timestamp per enumerable member;
absent on `Creator`, and on `Banned` it is the *ban* date, so store per-status,
never blindly), `rank` (custom title — leaks org role / real name), `subscription_until_date`
(identifies paying members), `inviter_id` (populated only for self — stored when
present, never inferred), `source_raw_id`, `first_seen`, `last_seen`.

### `participant_snapshots` — append-only membership observations

Rosters rotate and drift; this counts membership over time. Each run appends the
enumerated set plus the run's `enumerated / true_count` accounting (§6.3).

### 4.3 Tri-state as a storage shape (not a convention)

CLAUDE.md's "never record 'no photo'" MUST becomes a real shape. Each optional
field carries a state in `field_states_json`:

`state ∈ present | not_set | hidden_from_you`, plus the **disambiguator** that
proved `hidden_from_you`. Absence alone is ambiguous; only these machine-readable
signals promote it to a positive "hidden" (research §1.6):

- `fallback_photo` present + `profile_photo` absent → **photo hidden_from_you**.
- `by_me` set on a `userStatus*` → the target is *not* hiding; *our* account's
  `privacyKeyStatusTimestamp` (or lack of Premium) is degrading the data.
- `private_forward_name` present → forwards privacy on.
- `phone` flag set with **empty string** → a real `min` wire state; test
  non-empty, never presence.

Each run also records the **collecting account's own privacy posture** (from
`account.getPrivacy` for `phone`/`lastseen`/`photo` — `doctor` already reads
exactly these three keys), stored once per run, so a `by_me`-degraded
observation is attributable to *us*, never misread as target opsec. Fields that
are facts about *us* not them (`common_chats_count`, `blocked`, `personal_photo`,
`note`) are never ingested as target data.

## 5. Gateway methods (5 new + the InputUser builder)

Added to the `Gateway` Protocol, `TelethonGateway`, and `FakeGateway` (dict-in/
dict-out seam preserved; every method routed through `Budget.call` in the real
gateway). `FakeGateway` gets matching fixture keys, and — per its established
convention — a fixture value may be a `BaseException` to exercise the
`CHAT_ADMIN_REQUIRED`/`BROADCAST_FORBIDDEN` skip paths without a live failure;
`FakeGateway.calls` asserts the **zero-RPC** broadcast branch.

| Method | TL | Notes |
|---|---|---|
| `get_participants(input_channel, filter, offset, limit, hash)` | `channels.getParticipants` | Page size 200 (Telegram's, not ours — §6.3); surface `channelParticipantsNotModified` |
| `get_participant(input_channel, participant)` | `channels.getParticipant` | The per-user oracle; works in hidden-member groups where bulk fails |
| `get_users(ids)` | `users.getUsers` | Batched triage |
| `get_full_user(input_user)` | `users.getFullUser` | Returns **both** `full_user` and a `users` vector — parse both (disjoint data). Telethon plumbing already half-exists: `get_self` calls `GetFullUserRequest(InputUserSelf())`; only the arbitrary-`InputUser` case is new |
| `get_user_photos(input_user, offset, max_id, limit)` | `photos.getUserPhotos` | limit ≤ 100; excludes personal/fallback photos; retrospective avatar history with dates |

**`_input_user` builder — the load-bearing new plumbing.** Three cases:

1. non-`min`, valid `access_hash` → `InputUser{user_id, access_hash}`.
2. `min` → `InputUserFromMessage{peer=InputPeerChannel(seen_in_chat, <that
   channel's access_hash from peers>), msg_id=seen_in_msg, user_id}`. The
   seen-in channel's hash lives in `peers`, so the gateway receives it in the
   dict: `{"user_id", "from_msg": {"channel_id", "access_hash", "msg_id"}}`.
3. unresolvable (no non-min object and no usable provenance) → skip, recorded.

Without case 2 every `min` stub in the store is permanently unenrichable, so
this is the single most valuable unit to build first (`_input_channel` /
`_input_peer_channel` already exist; `_input_user` is the gap).

## 6. `participants` collector

### 6.1 Preflight

Fetch `get_full_channel(linked_group)` first — the branch flags
`can_view_participants` / `participants_hidden` live on the *group's*
`channelFull`, which paperboy has only ever fetched for the *target*, not the
linked group. Add a **per-phase session-age gate**: refuse enumeration on a
session younger than `min_session_age_days` without `--unsafe` (the spec MUST is
currently enforced only run-level by `doctor`).

### 6.2 Branch by what the tier permits

- **The broadcast channel's OWN subscriber roster is never enumerable (§2) —
  skip *it*, not the collector.** Record `participants_count` and the precise
  reason (`participants_hidden` / `can_view_participants` false /
  `CHAT_ADMIN_REQUIRED`) to `run_events` and `participant_snapshots`, with
  **zero enumeration RPC against the channel** — a walled roster is a
  first-class stored outcome, never a silent zero. This is emphatically **not**
  a skip of the whole phase: if the broadcast target has a linked discussion
  group (the usual case), `participants` proceeds to enumerate *that group* via
  the next bullet. A **full** phase skip happens only for a target with **no
  linked group at all** (no comment section ⇒ no person vector, §2).
- **Linked supergroup, un-joined (passive default):** **bulk `Recent`
  enumeration is a real vector here, not a fallback** — the §13 probe confirmed
  a *public* linked group's roster IS enumerable un-joined and non-admin
  (`getParticipants(Recent)` on NRM Chat returned `count=307` with participants,
  no join, no error). So this branch pages `Recent` to the server's depth (§6.3)
  and unions it with: the `get_participant` **oracle** — confirmed callable
  un-joined/non-admin **on the group** for arbitrary users via
  `InputPeerUserFromMessage` (join date + status). **The oracle is bounded, not
  a blanket per-user sweep:** in the normal enumerable case the `Recent` roster
  already carries each member's join date, so the oracle is reserved for (i)
  `participants_hidden`/capped groups where bulk enumeration is reduced, and
  (ii) specific known user_ids absent from the enumerated set — and is capped by
  the run's RPC budget, never one call per known commenter (a large group's
  thousands of commenters would otherwise be thousands of RPCs and a flood
  risk). Also unioned: `channelParticipantsMentions(top_msg_id)`
  per thread root, group reaction lists (`{peer_id, date, reaction}` — groups-only,
  read-only), and **join/leave service messages already in the captured history**
  (§8, zero new RPC). Only if bulk enumeration is *walled* here
  (`CHAT_ADMIN_REQUIRED`/`CHANNEL_PRIVATE` — a private or `join_to_send` group)
  is it a recorded skip that triggers the §6.4 warning. Note the roster (current
  members) and the discussion-discovered commenters are **different
  populations** — the probe's arbitrary commenter was `UserNotParticipant` (left
  the group) — which is exactly why both collectors are needed.
- **`--join` given:** join the linked group (the one write; audited in
  `run_events` as `active: True`, logged as a warning — the existing
  `discussion._join_or_skip` machinery), then `channelParticipantsRecent` ∪
  `Admins` ∪ `Bots` (the latter two work even when members are hidden).

### 6.3 Roster accounting — no silent ceiling

`getParticipants` returns 200 users *per request*; the collector pages
(`offset += 200`) until the server stops returning new users, deduping by id.
**200 is not a total cap.** The real ceiling is Telegram's: a non-admin's offset
paging is server-limited far below the true membership (a 78k-member group
returned 12), and the historical prefix-`Search` workaround is dead upstream
(Telethon v1.25.1, 2022). So enumeration is **always best-effort and labeled
partial**: `channelParticipants.count` (the true total, reported even when
hidden) is stored alongside the number actually enumerated. Every run records
`enumerated / true_count`; a shortfall is never presented as completeness.

### 6.4 The `--join` shortfall warning

When the un-joined roster comes back walled or partial (`enumerated <
true_count`, or a hard skip), emit a warning — *"enumerated N of M members; the
full roster requires membership — re-run with `--join` to join and enumerate
(an active, audited write)"* — as a console warning, a `run_events` row, and the
phase `stopped` reason, mirroring `discussion`'s existing "re-run with `--join`"
idiom. paperboy **never auto-joins** to close the gap: it names the gap and its
cost, and leaves the choice to the operator.

### 6.5 Projection

For every enumerated participant: upsert the `participants` row (per-status join
date, rank, subscription), emit `member_of` / `admin_of` edges, and upsert the
free full `User` object into `users` (and `peers`). Admin-only sub-methods
(boosts, invite importers, admin log, banned/kicked) are **detected via rights
and skipped, never attempted** (spec SHOULD).

## 7. `profiles` collector

The universal enrichment sweep — passive, no join, no new tier. It is
**default-on for its cheap half and flag-gated for its expensive half**: steps
1–2 (triage) always run; steps 3–5 (**full enrichment**) run only under
`--profiles`. When `--profiles` is absent, the collector completes the triage
and emits a warning — *"triaged N people (basic names/handles); full enrichment
(bios, photos, last-seen, …) not run — pass `--profiles` to enrich them (~1
`getFullUser`/s, bounded by `--profile-budget`, default 2000/run ≈ 33 min)"* —
as a console warning and a `run_events` row, the same "here is what you did not
get, and how to get it" idiom as §6.4's `--join` warning.

1. **Gather** every `kind='user'` peer from the store (all vectors). *(always)*
2. **Triage** — batched `get_users(ids)` for cheap fields (names, usernames,
   verified/scam/fake/deleted/premium, `emoji_status`, `color`, stripped thumb).
   Write to `users` + `user_snapshots`. Cheap enough to run for everyone. *(always)*
3. **Full profile** *(only under `--profiles`)* — `get_full_user`
   (+ `get_user_photos`, displayed gifts, pinned stories, common chats) on the
   spec's committed priority order
   **admins → authors → commenters → others**, bounded by `profile_budget`
   (default 2000/run; at ~1 `getFullUser`/s this is ~33 min, so the triage/full
   split is a runtime necessity, not polish). Parse **both** `full_user` and its
   `users` vector.
4. **Resolve `min` stubs** via `_input_user` case 2 — the capability that makes
   a stub enrichable at all.
5. **Tri-state** every optional field (§4.3).

`min`-merge rules for the `peers` side follow research §8.7 (never apply
`contact`/`mutual_contact`/`stories_*` from a `min`; apply `first_name`/
`last_name`/`username`/`phone` only if incoming is non-min or cached is min;
`photo` only with `apply_min_photo`; `status` if cached is min/empty). Full
profile state lands in `users`/`user_snapshots`, keeping `peers` untouched.

### 7.1 Resume to convergence — enrich *everyone*, across runs

`profile_budget` caps the **expensive** `getFullUser` fetch per run, not the
population; the cheap batched `getUsers` triage (step 2) always covers everyone.
So a group larger than the budget is fully triaged in one run but only its
top-priority slice is deep-enriched. To converge on the whole group over
repeated runs rather than re-enriching the same high-priority head every time,
`profiles` keeps a **resume cursor** (like `history`'s `offset_id`), in
`sync_state('profiles', <channel_id>)`:

- Each run spends its `getFullUser` budget on users **not yet fully enriched**,
  in the §7 priority order, then advances the cursor past them.
- When every discovered user has been fully enriched once, the cursor wraps to a
  **refresh** pass — re-enriching the **stalest** users first (oldest
  `users.last_seen` among fully-enriched), so profile drift (a renamed account,
  a new photo, a changed bio) is picked up as a fresh `user_snapshots` row
  without ever starving newly-discovered users of their first enrichment. A
  configurable **staleness floor** (`--profile-refresh-after`, default off)
  suppresses re-enriching anyone seen more recently than that, so a
  never-ending watch loop does not burn budget re-fetching unchanged profiles.

Convergence is therefore: run repeatedly (or once with a raised budget) and
every discovered user gets a full profile; keep running and they stay fresh.
Because enrichment writes to append-only `user_snapshots`, re-enrichment is
always safe (a new observation, never a clobber) and reproject-faithful.

### 7.2 Parameterizing the enrichment pass

Every knob is a `Settings` field with a CLI override, so an operator tunes the
run to the target and their own risk posture without touching code:

| Knob | CLI | Default | Controls |
|---|---|---|---|
| **Enrich** | `--profiles` | off (triage-only) | the master switch for **full** enrichment (steps 3–5). Off = triage-only + the §7 warning; on = full profiles. |
| Budget | `--profile-budget N` | 2000/run | how many `getFullUser` fetches one `--profiles` run spends |
| Wait | `--profile-interval SECONDS` | inherits Budget's `min_interval` (1.0s) | the pace between full-profile RPCs — raise it to stay quieter / dodge flood onset, lower it (carefully) to go faster |
| Refresh floor | `--profile-refresh-after DURATION` | off | skip re-enriching users seen more recently than this (§7.1) |

Triage-only is the *default* precisely because the cheap pass is the
passive-safe posture for a first, low-footprint look at an unfamiliar target;
`--profiles` is the deliberate step up to the expensive full sweep. Pacing is
enforced through the existing `Budget` module (the one chokepoint all RPCs
already route through), so `--profile-interval` composes with — never bypasses —
flood-wait handling and the per-run RPC cap.

## 8. Edges

Predicates (reusing the reserved vocabulary, all via `add_edge_once` — set-like
facts):

- `member_of`, `admin_of` — from `participants` (§6.5).
- `invited_by` / `added_by` — **from join service messages already in captured
  history**, zero new RPC: `messageActionChatJoinedByLink` carries `inviter_id`;
  `messageActionChatAddUser`'s adder is the message `from_id`. Partial by nature
  (silent joins leave no trace; channel subscriptions never emit one) — always
  labeled as trace-only, never a claim of the full invite graph.
- **#11 fix (folded in, no-shed):** `profiles` walks the forward-origin path, so
  it upserts `forwarded_from` edge-target users into `peers` — they currently
  exist as edge endpoints with no peer row, so they were invisible to the very
  enrichment sweep that should reach them.

## 9. Guardrails

- **Forbidden, no flag, never implemented:** `contacts.getLocated`; poll-voter /
  reactor collection on broadcasts (`BROADCAST_FORBIDDEN`); any add-member/invite
  capability; **`users.suggestBirthday`** (it *notifies the target* — never
  invoked from a collection account, which also means "no birthday" vs "hidden"
  is permanently indistinguishable and we do not pretend otherwise); bulk phone
  enumeration; circumvention of privacy settings (no inferring around omitted
  fields, no third-party OSINT-bot integration).
- **Gated + budgeted:** roster enumeration behind `--join` (§6.2); per-phase
  session-age gate behind `--unsafe` (§6.1). Phone *lookup* is **out of scope**
  — its own flag-gated `lookup` command, its own spec (§12).
- **Free (read-only, budget-only):** everything in §5, plus `messages.getOnlines`
  aggregate and the zero-RPC service-message / reaction vectors.
- **Tri-state MUST** is now structural (§4.3), not a convention.
- **Third-party `phone`** that the privacy rules *do* reveal is a legitimate
  fact about the target and is stored; only the *collecting* account's phone is
  stripped (#12 precedent). Profile photos route through the existing
  media/custody path and honor the "don't download `porno`/illegal-flagged by
  default" rule.
- `CHAT_ADMIN_REQUIRED` and `BROADCAST_FORBIDDEN` are already classified
  skip-and-record in `errors.py`, so both walls degrade gracefully today.

## 10. Reproject replay support (depends on #40)

Raw-first means the 5 new RPC responses are appended to `raw_records`
automatically — but a `reproject` rebuilds the person layer only if
`RawReplayGateway` can *serve* those kinds back. This section adds the 5 replay
methods (serving `getUsers`/`getFullUser`/`getUserPhotos`/`getParticipants`/
`getParticipant` records by context, in run scope — the same pattern as the
existing replay methods), plus their `detect_phases` raw-kind detection, so
`participants`/`profiles` reproject like every other phase. Without this, a
reproject of a person-layer archive would silently skip the new tables —
breaking the ADR-0002 "rebuildable from raw" invariant for exactly the new data.
Presumes #40 is on `main`; if #40 is deferred, §10 splits off as the immediate
fast-follow (the raw is captured regardless — only replay *serving* is missing).

## 11. Testing

- **Round-trip identity** extends to the person layer (via §10): collect →
  reproject → `users`/`participants`/snapshots identical.
- **`FakeGateway` fixtures** for all 5 methods; **`BaseException` fixtures** for
  the `CHAT_ADMIN_REQUIRED`/`BROADCAST_FORBIDDEN` walls; a **zero-RPC assertion**
  (`FakeGateway.calls == []` for the enumeration RPCs) on the broadcast-skip
  branch.
- **`_input_user` unit tests** — all three cases, especially case 2's
  `InputUserFromMessage` construction from stored provenance.
- **Tri-state disambiguator tests** — `fallback_photo` → `hidden_from_you`;
  `by_me` → self-degraded, not target opsec; empty-string `phone` flag.
- **Per-phase session-age gate** test (refuse without `--unsafe`).
- **Roster accounting** test — `enumerated / true_count` recorded; the §6.4
  warning fires on shortfall.
- **`invited_by`/`added_by`** projection from service-message fixtures.
- **Resume-to-convergence** test (§7.1): a discovered population larger than the
  budget, run twice, deep-enriches a different (next-priority) slice each run and
  reaches *everyone* across runs — no user starved, no head re-enriched before
  the tail is reached; then a third run wraps to the stalest-first refresh pass.
- **Default-set + `--profiles` gating** tests: a plain `collect` runs
  `participants` and `profiles`-triage (both default-on) but makes **zero**
  `getFullUser` calls and emits the §7 warning (assert via `FakeGateway.calls`
  and a captured `run_events`/log record); `--profiles` enables the full sweep.
- **Parameterization** tests (§7.2): `--profile-interval` routes through
  `Budget` (pacing composes with flood handling, never bypasses it);
  `--profile-refresh-after` skips a recently-seen user; `--profile-budget`
  bounds the `getFullUser` count.
- **The two empirical gates are RESOLVED** (§13, live probe 2026-08-27): tests
  assert the confirmed behavior — a public linked group's `Recent` roster
  enumerates un-joined; `get_participant` answers arbitrary group members
  un-joined/non-admin but is `ChatAdminRequiredError` for an arbitrary user on
  the broadcast channel. Fixtures encode both the success and the
  `ChatAdminRequiredError`/`UserNotParticipantError` shapes.
- **DoD smoke** against the real archive's linked discussion group.

## 12. Scope and follow-ups

- **In scope:** the two collectors (`participants` default-on; `profiles` triage
  default-on, full enrichment behind `--profiles` — §1/§7), 4 tables, 5 gateway
  methods + `_input_user`, tri-state storage, the membership/invite edges, the
  #11 fix, reproject replay support (§10), the profiles resume-to-convergence
  cursor (§7.1), and the enrichment-pass parameters (§7.2).
- **Out of scope (own specs):** phone `lookup` (reverse-direction, server-
  mutating, its own flag); basic-group inviter enumeration (targets are
  channel-typed); `peerSettings`/registration-month (requires the target to have
  DM'd *you* — unavailable to a collection account).
- **vNext (thin recipes over this layer's output):** user **dossier** export,
  **watchlists**, multi-seed network maps, hashtag/geo story search — exporters
  and recipes over `users` + `participants` + `edges` + snapshots, not
  collectors, which is why they follow the person layer.

## 13. The two empirical gates — RESOLVED (2026-08-27, live probe)

Both were answered by a read-only probe against the real `default` archive
(`@national_resistance_movement`, a broadcast channel, and its linked group
*NRM Chat*, 307 members). No join, no writes. Answers, now folded into §6.2:

1. **`channels.getParticipant` on a broadcast non-admin** — the oracle's home is
   the **linked group**, not the channel. On the group it works un-joined /
   non-admin for **arbitrary** users (built via `InputPeerUserFromMessage`),
   returning a definitive per-user answer (`ChannelParticipant` with join date,
   or a clean `UserNotParticipantError`). On the **broadcast channel** it works
   for **self** only; an arbitrary user is `ChatAdminRequiredError`. So the
   oracle is a genuine un-joined capability for group members (and the fallback
   where a group's bulk roster is capped/hidden), but it cannot enumerate the
   broadcast channel's own subscribers — consistent with the §2 wall.
2. **`channels.getParticipants` `Recent` un-joined** — **works** on a public
   linked group: NRM Chat returned `ChannelParticipants(count=307, …)` with no
   join and no error. So the passive branch enumerates the roster directly;
   `--join` is the escalation only for a group that *walls* it (private /
   `join_to_send`), and `count` (307) is the true-total denominator for §6.3
   accounting.

**Two refinements left to verify in implementation** (not gates — the design
holds regardless): the server's real paging *depth* on a larger roster (NRM Chat
at 307 is small; the 78k→12 ceiling bites only huge groups — page and record
`enumerated / count`), and `get_participant`/`getParticipants` behavior on a
`participants_hidden` group (the oracle is expected to survive where bulk is
reduced to admins+bots). The throwaway probe used to answer these is not part of
the deliverable and does not need to be reproduced.

## 14. Context for the implementing session

Everything a fresh session needs that is not design content:

- **Branch:** `feat/person-layer` (already exists, has this spec committed, and
  descends from the reproject work so `clock.py`/`replay.py`/`reproject.py` and
  migration `0003_run_id.sql` are present). Do the work here.
- **Gate A issue:** **#41** (this feature); umbrella **#13** (Phase 2). Reference
  #41 in the `single-feature-run`.
- **Reproject is merged** to `main` (#40, 2026-08-27), so §10 (replay support) is
  buildable now, not deferred. Read `docs/features/reproject.md` +
  `docs/adr/0005-run-structure.md` before touching `replay.py`/`reproject.py`.
- **The must-read grounding** beyond this spec: the two research docs
  `docs/research/telegram-extraction-surface.md` and
  `docs/research/sources/mtproto-participants-users.md` (the hard walls and the
  `min`-merge / tri-state field rules — the §-number citations in this spec
  point there; the actual rules are also inlined in §4.3 and §7); the reproject
  plan `docs/superpowers/plans/2026-08-26-reproject.md` as the *format* model for
  the implementation plan; and the existing collectors (`discussion.py` for the
  linked-group + `--join` machinery, `history.py` for the `sync_state` resume
  idiom, `media.py` for a store-walking collector).
- **DoD smoke target:** the real `default` archive — `@national_resistance_movement`
  and its linked group **NRM Chat** (307 members). Live Telegram runs must be on
  the **main thread** (Keychain access; sandboxed workflow agents cannot reach
  it), so the DoD live step is a human/main-thread action, not a subagent's.
- **Process:** brainstorming is complete and this spec is approved — go straight
  to `superpowers:writing-plans` to author the implementation plan, then execute
  via `single-feature-run` (plan pre-approved). Do not re-open the design.
