# The person layer — `participants` + `profiles` collectors

**Status:** design, 2026-08-26. Realizes the entity-graph thesis (CLAUDE.md §1:
"architected as an entity graph so later recipes — user dossier, phone lookup,
watchlists — are thin additions") by filling in the *people*: discovering the
linked group's roster where the walls permit, and enriching every discovered
peer from a `min` stub into a full person.
**Execution:** one `single-feature-run` on `feat/person-layer`.
**Depends on:** PR #40 (reproject) merged to `main` — §10 (reproject replay
support) extends `RawReplayGateway`, which lands with #40. The collectors
themselves do not depend on it; only §10 does.

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
The current default set is `channel, history, discussion, graph`; the two new
collectors slot between `discussion` and `graph`.

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

- **Broadcast target (or no linked group):** detect-and-skip. Record
  `participants_count` and the precise reason (`participants_hidden` /
  `can_view_participants` false / `CHAT_ADMIN_REQUIRED`) to `run_events` and
  `participant_snapshots`. **Zero enumeration RPC** — a walled roster is a
  first-class stored outcome, never a silent zero.
- **Linked supergroup, un-joined (passive default):** the free vectors only —
  `channelParticipantsMentions(top_msg_id)` per thread root (sanctioned
  non-participant commenters), the `get_participant` oracle for user_ids we
  already know (join date + status, even in hidden groups), group reaction lists
  (`{peer_id, date, reaction}` — groups-only, read-only), and **join/leave
  service messages already in the captured history** (§8, zero new RPC). Bulk
  `Recent` enumeration is attempted; on `CHAT_ADMIN_REQUIRED`/`CHANNEL_PRIVATE`
  it is a recorded skip that triggers the §6.4 warning.
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

The universal enrichment sweep — passive, no join, no new tier.

1. **Gather** every `kind='user'` peer from the store (all vectors).
2. **Triage** — batched `get_users(ids)` for cheap fields (names, usernames,
   verified/scam/fake/deleted/premium, `emoji_status`, `color`, stripped thumb).
   Write to `users` + `user_snapshots`. Cheap enough to run for everyone.
3. **Full profile** — `get_full_user` (+ `get_user_photos`, displayed gifts,
   pinned stories, common chats) on the spec's committed priority order
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
- **The two empirical probes are the FIRST implementation step** (§13), run live
  from the collecting account on the main thread; their answers are recorded
  back into this spec and shape the un-joined branch's asserted behavior.
- **DoD smoke** against the real archive's linked discussion group.

## 12. Scope and follow-ups

- **In scope:** the two collectors, 4 tables, 5 gateway methods + `_input_user`,
  tri-state storage, the membership/invite edges, the #11 fix, reproject replay
  support (§10).
- **Out of scope (own specs):** phone `lookup` (reverse-direction, server-
  mutating, its own flag); basic-group inviter enumeration (targets are
  channel-typed); `peerSettings`/registration-month (requires the target to have
  DM'd *you* — unavailable to a collection account).
- **vNext (thin recipes over this layer's output):** user **dossier** export,
  **watchlists**, multi-seed network maps, hashtag/geo story search — exporters
  and recipes over `users` + `participants` + `edges` + snapshots, not
  collectors, which is why they follow the person layer.

## 13. The two empirical gates

Both need a live RPC from the collecting account and cannot be answered from
docs; the collector structure above is correct under *either* outcome, and these
run **first** in implementation, updating this spec with the answers:

1. **`channels.getParticipant` on a broadcast channel as a non-admin** — does the
   per-user oracle survive where bulk enumeration is walled? (The single
   highest-value unknown; research §7.)
2. **`channels.getParticipants` `Recent` on a *public* supergroup, un-joined** —
   does the roster require `--join`, or is a public group's roster readable
   without joining? Determines whether the un-joined branch (§6.2) yields a real
   roster or only the free vectors.
