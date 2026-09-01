# Person layer (`participants` + `profiles`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.


**Goal:** Turn the `min` peer stubs paperboy already collects into full people and discover the linked group's roster — two new default-on collectors (`participants`, `profiles`), five new tables, seven new gateway methods + the `_input_user` builder, tri-state profile storage, membership/invite/reaction edges, the #11 forward-origin fix, and reproject replay support — all read-only and passive by default.

**Architecture:** `participants` (roster discovery: `channels.getParticipants` `Recent` paging on the linked group with `enumerated/true_count` accounting, the bounded `channels.getParticipant` oracle, zero-RPC join/leave service messages and reaction samples, bounded `messages.getMessageReactionsList`) and `profiles` (the single enrichment authority: batched `users.getUsers` triage for every discovered user always; `users.getFullUser` + `photos.getUserPhotos` + avatar download behind `--profiles`, priority-ordered, budgeted, converging across runs via `users.enriched_at`). Profile richness lives in the new `users`/`user_snapshots`/`user_photos` tables — `peers` stays the min-provenance stub table and its `upsert_peer` lattice (#38/#39) is never modified. Every RPC goes through the existing `Gateway` seam + `Budget`; every response is raw-first (`raw_records`) so `RawReplayGateway` can serve it back and `reproject` rebuilds the person layer like every other phase.

**Tech Stack:** Python ≥3.12 (dev 3.14), `uv`, Telethon 1.44.0 (layer 227 — every TL constructor below was verified against the installed wheel), stdlib `sqlite3`, Typer, pydantic-settings, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff + pyright. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-26-person-layer-design.md` (approved 2026-08-27). Gate A issue **#41**; umbrella #13. Branch `feat/person-layer`.

## Global Constraints

- **Read-only, passive by default.** The only write is `channels.joinChannel` under an explicit `--join` (reuses `discussion`'s audited `join_or_skip`). Never `users.suggestBirthday`, never `contacts.getLocated`, never poll voters, never reactors on a broadcast (`BROADCAST_FORBIDDEN` is a recorded skip).
- **Raw first.** Every RPC response is `add_raw`'d before projection, with the per-record `observed_at` from `ctx.clock.for_payload(...)` (reproject seam D1/D2). Zero-RPC derived rows stamp from the row they derive from (D5 below).
- **Every RPC through `Budget.call`** (ADR-0003) with a real TL method name; `--profile-interval` composes with — never bypasses — flood handling.
- **Profile richness never in `peers`.** `upsert_peer`'s SQL is not modified by this plan. `users`/`user_snapshots` hold profile state.
- **Tri-state is structural:** `users.field_states_json` with `state ∈ present | absent | hidden_from_you` (user decision, 2026-08-27 — see D2). "no photo" is never recorded as a fact.
- **The collecting account is never a subject:** `upsert_user` returns `None` for self (mirrors `upsert_peer`); `add_edge` already drops self endpoints; the self `phone` stays stripped (#12).
- **Credentials never in logs.** Third-party `phone` values are stored (a target fact, spec §9) but never logged.
- **Session-age gate** (spec §6.1): enumeration refused on a session younger than `min_session_age_days` unless `--unsafe`.
- **Test command (this machine):** `TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest`, plus `uv run ruff check` and `uv run pyright` — all green after every task.
- **Live smoke** (Task 12) runs on the **main thread** (Keychain) against the real `default` archive (`@national_resistance_movement` + NRM Chat, 307 members) — never inside a sub-agent.

## Locked design decisions (read before implementing anything)

These resolve every question the spec leaves open or that implementation-level facts force. They were confirmed with the user on 2026-08-27 where marked **[user]**; the rest follow from verified code/Telethon facts. Do not re-derive them.

**D1 — Raw kinds are stamped explicitly for the new envelopes.** Verified: Telethon's `to_dict()` emits the *bare* class name, so the `users.UserFull` envelope and the inner `UserFull` both serialise as `"_": "UserFull"`, and `channels.ChannelParticipant` (envelope) collides with `ChannelParticipant` (inner). Collectors therefore call `add_raw` with an explicit namespaced kind — `channels.ChannelParticipants`, `channels.ChannelParticipantsNotModified`, `channels.ChannelParticipant`, `users.UserFull`, `photos.Photos` / `photos.PhotosSlice`, `messages.MessageReactionsList`, `account.PrivacyRules` — via the helper `namespaced_kind(ns, payload)` (Task 1). Per-user `getUsers` results are stored one record each with kind `payload["_"]` (`User`/`UserEmpty`) and context `{"channel_id", "method": "users.getUsers", "user_id"}`. Three **synthetic** raw kinds (precedent: `MediaDownload`) record observations that have no TL payload: `RosterWalled` (a walled roster is a first-class stored outcome, spec §6.2), `UserNotParticipant` (the oracle's definitive negative), `AvatarDownload` (a downloaded avatar's sha/path). `replay._kind_clause` already tolerates the dotted prefix.

**D2 — Tri-state encoding is `present | absent | hidden_from_you`. [user]** Telegram enforces privacy by omission, so plain absence never proves "not set"; `absent` means "not on the wire and no disambiguator". `hidden_from_you` only with the §4.3 proofs: `fallback_photo` present + `profile_photo` absent → photo; `private_forward_name` present → forwards. Status keeps `state: present` with `granularity: exact|coarse` and, for coarse buckets, `coarse_cause: self_privacy` (when `by_me` is set — our account degrades it) or `target_privacy`. A triage-level `absent` never overwrites a stored `hidden_from_you` (a proof persists until a newer *full* observation revises it). Tracked keys: `phone, photo, status, about, birthday, forwards, stories`.

**D3 — Resume-to-convergence is driven by `users.enriched_at`, not `last_seen`.** Spec §7.1 says "oldest `users.last_seen` among fully-enriched", but the always-on triage bumps *every* user's `last_seen` every run, which makes that ordering meaningless. `users.enriched_at` (set on every successful `getFullUser` observation) is the honest cursor: a run spends its `getFullUser` budget on `enriched_at IS NULL` users in priority order (admins → authors → commenters → others, then `uri`), then wraps to a refresh pass ordered `enriched_at ASC`. `--profile-refresh-after` compares `enriched_at` against `ctx.clock.now()` (a new `Clock` method, Task 1, so replay is deterministic). `sync_state('profiles', <channel_id>)` stores the run's convergence summary (`pass`, `population`, `fully_enriched`, `enriched_this_run`, `budget`) — the *position* is derived from `enriched_at`, which is what makes an interrupted run resumable with no cursor to corrupt.

**D3 — amended 2026-08-31 (Leg 2 review, root-cause diagnosis).** Deriving the *position* from `enriched_at` alone overloaded one column with two incompatible contracts: as a provenance benchmark it must move only when full columns were applied; as a rotation cursor it must move on every attempt, or a user whose `getFullUser` permanently fails sits at the head of every run and starves everyone else (spec §7.1's "no user starved" — the spec is silent on a spent-but-failed slot; this is the one honest reading). The rotation key is therefore a distinct table, `profile_attempts (uri PK, attempted_at, outcome, detail)` in `0004_people.sql`, written at the ONE point where a budget slot is spent (`outcome='attempted'`, before the RPC answers) and replaced by whichever arm finishes the attempt — so recording is structurally unskippable. `_enrichment_candidates` orders: never-enriched & never-attempted (rank, uri) → never-enriched & attempted (least recent attempt first) → enriched (least recent attempt first — with no failures exactly "stalest enrichment first"). `enriched_at` stays a pure provenance benchmark and the refresh floor's input. `profile_attempts` is bookkeeping like `sync_state` (excluded from round-trip identity). Recorded as ADR-0006 (d) in Task 12.

**D4 — `USER_NOT_PARTICIPANT` and `USER_ID_INVALID` handling.** Neither is in `errors.classify`'s tables today, so both would propagate verbatim and crash a phase. `UserNotParticipantError` is a *result*, not a failure: `TelethonGateway.get_participant` catches it and returns `None`; the collector then writes a synthetic `UserNotParticipant` raw and a `participants` row with `status='left'` (spec §13 itself reads it as "left the group"; the CHECK constraint keeps the spec's five statuses). `UserIdInvalidError` is added to `_skip_error_classes()` (a per-user skip, safe globally); `ChannelInvalidError` is deliberately NOT — `classify` has no per-method scope and a `CHANNEL_INVALID` on the target's own `getFullChannel`/`getChannelDifference` must keep surfacing as a real failure, so `TelethonGateway.get_users`/`get_full_user` catch it locally and raise `SkipAndRecord` (as shipped in Leg 1).

**D5 — Zero-RPC derived rows stamp from their source row (reproject plan D3, extended).** `RosterWalled` for the broadcast target ← the target's `channels.last_seen` / `source_raw_id` (the ChatFull observation that established `participants_count` and the flags); join/leave service-message facts ← the message's own `date` (the fact's time; roster observations, stamped later, then correctly win over an old join) with `source_raw_id` = the message's; the #11 forward/mention peers ← the message's `first_seen`; `recent_reactions` ← the message raw's `observed_at`. Never `utc_now_iso()` in a collector.

**D6 — Replay settings are per run.** For each historical run `reproject` uses `settings.model_copy(update={"allow_join": True, "unsafe": True, "enrich_profiles": <run has any users.UserFull raw>, "profile_budget": 10**9, "participant_oracle_budget": 10**9, "participant_reactions_budget": 10**9})`: the session-age gate has no RPC to protect on replay (D4.3 precedent); a `--profiles` original replays its `UserFull` records while a triage-only original replays triage-only (its "enrichment off" `run_events` warning included); and the three budgets are lifted because the live budget already bounded *what was recorded* — replay walks the same deterministic candidate order, serves every recorded observation, and an unrecorded candidate is a cheap offline `SkipAndRecord` that projects nothing. A smaller replay budget than the original's would silently drop recorded observations past the cut.

**D7 — Schema beyond the spec's column lists (all additive).** `users.flags_json` (verified/scam/fake/deleted/premium/support/restricted… — `bot_json` only folds bot fields and the spec gives these no other home), `users.enriched_at` (D3), `user_snapshots.method` (per-method hash dedupe, so triage and full bundles do not alternate forever), and a fifth table **`user_photos`** (dated avatar history + the downloaded file's `sha256`) because avatar download is in scope **[user]**. Avatars go through the existing `media`/`custody_log` path with `message_uri`/`source_message_uri` NULL and `media.kind='avatar'`; `user_photos.sha256` is the link.

**D8 — Reactions in, Mentions out. [user]** `get_message_reactions_list` (`messages.getMessageReactionsList`) is the 7th gateway method; it is groups-only (a broadcast answers `BROADCAST_FORBIDDEN` → recorded skip) and bounded by `Settings.participant_reactions_budget` (default 200 messages/run, newest first; the done-set is derived from `raw_records` so repeated runs converge). The zero-RPC `MessageReactions.recent_reactions` sample already inside stored message payloads is projected first (like `recent_repliers`). New predicate `reacted_to` (user → message, evidence `{reaction, date, source}`) extends the reserved vocabulary — recorded in ADR-0006. The `channelParticipantsMentions` per-thread-root union (spec §6.2) is **skipped** and filed as a follow-up: its identities are redundant with a completed discussion sweep; the `filter` dict on `get_participants` is generic so it slots in later with no gateway change.

**D9 — The oracle is bounded and conditional.** `channels.getParticipant` runs only when a roster came back walled or partial (`enumerated < true_count`), over candidates = users referenced in that group (message authors, `peers.seen_in_chat`) minus this run's enumerated set minus users that already have a `participants` row for the group, capped by `Settings.participant_oracle_budget` (default 100). `CHAT_ADMIN_REQUIRED` on the oracle ends the oracle loop for that group (it is the wall, not a per-user condition).

**D10 — `Settings.unsafe`.** `--unsafe` sets `Settings.unsafe = True` (env `PAPERBOY_UNSAFE` is the same operator override); `cli._run_collect` reads it for the doctor skip and `participants` reads it for the per-phase session-age gate (which otherwise costs one `account.getAuthorizations`).

**D11 — `--join` in `participants` joins only when not already a member** (`Channel.left` is true or membership unknown), via `join_or_skip` extracted from `discussion.py` (shared, audited, `run_events` kind `join`, `active: True`). Then `Recent ∪ Admins ∪ Bots`. Un-joined: `Recent` only (+ oracle, service messages, reactions).

**D12 — Avatar download policy.** Under `--profiles` only; every photo on the first `getUserPhotos` page (limit 100) that has no `user_photos.sha256` yet; skipped (counted `restricted_skipped`) when the user carries any `restriction_reason` — the "don't download porno/illegal-flagged by default" rule; sequential (one MTProto session; no media-DC parallelism in this pass); bytes content-addressed at `<data_dir>/<profile>/media/<sha[:2]>/<sha>.jpg`.

**D13 — Batching + resilience for `getUsers`.** ≤100 refs per call. A `SkipAndRecord` on a multi-ref batch (one stale `inputUserFromMessage` provenance → `MSG_ID_INVALID` for the whole vector) bisects the batch; a failing singleton is counted `skipped` and logged. Worst case 2n calls, typical +log n.

**D14 — Replay scoping for per-user records.** The gateway seam carries no target on `get_full_user`/`get_user_photos`/`get_users`, so replay serves those by `user_id` (+ `method` for `getUsers`) within the run; a multi-target run's profiles phase can therefore serve a sibling target's observation of the same user in the same run — same data, idempotent projection (documented in `docs/features/person-layer.md` Known limitations). The live side has the same shape by spec design: `profiles` gathers EVERY `kind='user'` peer in the profile DB ("all vectors", spec §7 step 1), so in a multi-target profile target A's run also triages target B's people (priority-ranked as "others", context `channel_id` = A) — documented alongside.

**File structure** (new files):

| File | Responsibility |
|---|---|
| `src/paperboy/store/migrations/0004_people.sql` | `users`, `user_snapshots`, `user_photos`, `participants`, `participant_snapshots` |
| `src/paperboy/store/users.py` | `field_states`, `target_user_facts`, `target_full_facts`, `upsert_user`, `add_user_snapshot`, `upsert_user_photo`, `set_user_photo_sha` |
| `src/paperboy/store/participants.py` | `participant_row`, `write_participant`, `upsert_participant`, `add_participant_snapshot`, `add_roster_snapshot`, `project_join_service_messages` |
| `src/paperboy/store/message_peers.py` | `backfill_message_referenced_peers` (#11: forward origins + `MentionName` users) |
| `src/paperboy/store/reactions.py` | `backfill_recent_reactions`, `reacted_message_ids`, `fetched_reaction_lists` |
| `src/paperboy/collectors/profiles.py` | `ProfilesCollector` |
| `src/paperboy/collectors/participants.py` | `ParticipantsCollector` |
| `docs/features/person-layer.md`, `docs/adr/0006-person-layer-storage.md` | feature doc + DoD transcript; the users/peers split, tri-state shape, `reacted_to` predicate |
| `tests/test_store_users.py`, `tests/test_store_participants.py`, `tests/test_store_message_peers.py`, `tests/test_store_reactions.py`, `tests/test_input_user.py`, `tests/test_collector_profiles.py`, `tests/test_collector_participants.py`, `tests/test_replay_people.py`, `tests/test_reproject_people.py`, `tests/test_integration_people.py` | unit / collector / replay / round-trip / integration suites |

Modified: `ids.py` (`iso_or_none`, `peer_stub`, `namespaced_kind`), `clock.py` (`now()`), `store/messages.py` + `store/repliers.py` + `collectors/history.py` (use the shared helpers), `store/peers.py` (`input_user_ref`), `gateway.py` (Protocol + `TelethonGateway` + `FakeGateway`: 7 methods, `_input_user`/`_input_peer_user`, per-id `full_channel_by_id`, tolerant `get_privacy`), `errors.py` (skip classes), `config.py` (knobs, `parse_duration`), `budget.py` (`method_intervals`), `app.py` (Budget wiring), `doctor.py` (`session_age_days` public), `collectors/discussion.py` (`linked_group`/`join_or_skip` extracted), `recipes.py` (default set), `cli.py` (flags, dependent phases, `status`), `progress.py`, `replay.py` (7 replay methods + `get_privacy` serving + `has_context_value`), `reproject.py` (`detect_phases`, tables, replay settings), tests: `test_recipe.py`, `test_integration_discussion.py`, `test_reproject.py`, `test_reproject_parity.py` (+ regenerated golden), `test_gateway_fake.py`, `test_budget.py`, `test_config.py`, `test_cli.py`, `test_clock.py`, `test_store_migrations.py`.

**Task map vs. the requested dependency order** (schema → store writers → gateway + `_input_user` → profiles → participants → edges incl. #11 → §10 replay → wiring/DoD): the edge producers (`invited_by`/`added_by`/`member_of` from service messages, #11's peer backfill, `reacted_to`) are store-layer walkers with no gateway dependency, so they are built in Task 3 alongside the participants writers and *consumed* by Tasks 6–9; everything else follows the requested order exactly.

---

### Task 0: Land the plan in the repo

**Files:**
- Create: `docs/superpowers/plans/2026-08-27-person-layer.md` (verbatim copy of this file, minus this plan-mode note)

- [ ] **Step 1: Copy and commit**

```bash
cp ~/.claude/plans/linked-purring-curry.md docs/superpowers/plans/2026-08-27-person-layer.md
git add docs/superpowers/plans/2026-08-27-person-layer.md
git commit -m "docs(plan): person layer implementation plan (#41)"
```

---

### Task 1: Migration `0004_people.sql` + shared helpers (`iso_or_none`, `peer_stub`, `namespaced_kind`, `Clock.now`)

**Files:**
- Create: `src/paperboy/store/migrations/0004_people.sql`
- Modify: `src/paperboy/ids.py` (add three helpers), `src/paperboy/store/messages.py` (use `iso_or_none`), `src/paperboy/collectors/history.py` + `src/paperboy/store/repliers.py` (use `peer_stub`), `src/paperboy/clock.py` (`now()`)
- Test: `tests/test_store_migrations.py`, `tests/test_ids.py`, `tests/test_clock.py`

**Interfaces:**
- Produces: `ids.iso_or_none(value: int | str | datetime | None) -> str | None`; `ids.peer_stub(ref: dict | None) -> dict | None` (a `min` `User`/`Channel` stub for a `Peer*`/`InputPeer*` reference, else `None`); `ids.namespaced_kind(namespace: str, payload: dict, default: str) -> str` (`"users.UserFull"` from `{"_": "UserFull"}`; a payload whose `_` is already dotted is returned as-is); `Clock.now() -> str` on `LiveClock` (wall clock) and `ReplayClock` (the last served record's stamp; `ReplayClockError` if nothing served); `Store.run_id -> str | None` (read-only property: the current pass's id from `begin_run`, `None` before one begins — the once-per-run guard for the privacy-posture record, Task 6).
- Tables (every later task depends on these exact columns):

```sql
-- 0004_people: the person layer (spec §4). Profile richness lives HERE, never
-- in `peers` (which stays the min-provenance stub table). Every CREATE is IF
-- NOT EXISTS for the same re-runnable-migration reason as 0001_init.

-- current profile state — one row per user (spec §4 `users`)
CREATE TABLE IF NOT EXISTS users (
    uri               TEXT PRIMARY KEY,
    id                INTEGER NOT NULL,
    access_hash       INTEGER,
    tier              TEXT NOT NULL,
    is_min            INTEGER NOT NULL DEFAULT 0,
    username          TEXT,
    usernames_json    TEXT,
    first_name        TEXT,
    last_name         TEXT,
    phone             TEXT,
    about             TEXT,
    birthday          TEXT,
    emoji_status      TEXT,
    color_json        TEXT,
    status_kind       TEXT,
    status_value      TEXT,
    photo_ref         TEXT,
    restriction_json  TEXT,
    bot_json          TEXT,
    flags_json        TEXT,
    field_states_json TEXT,
    enriched_at       TEXT,
    source_raw_id     INTEGER REFERENCES raw_records(id),
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_enriched ON users(enriched_at);

-- append-only observation log — one row per (user, method) observation whose
-- bundle hash changed (spec §4 `user_snapshots`)
CREATE TABLE IF NOT EXISTS user_snapshots (
    id             INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    tier           TEXT NOT NULL,
    method         TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    fields_json    TEXT NOT NULL,
    source_raw_id  INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_user_snapshots_uri ON user_snapshots(uri, observed_at);

-- dated avatar history (photos.getUserPhotos) + the downloaded file, if any
CREATE TABLE IF NOT EXISTS user_photos (
    id             INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    photo_id       INTEGER NOT NULL,
    date           TEXT,
    dc_id          INTEGER,
    has_video      INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT REFERENCES media(sha256),
    observed_at    TEXT NOT NULL,
    source_raw_id  INTEGER REFERENCES raw_records(id),
    UNIQUE (uri, photo_id)
);

-- roster membership facts, keyed (group, user) (spec §4 `participants`).
-- `left` covers both channelParticipantLeft and the oracle's
-- USER_NOT_PARTICIPANT — Telegram does not distinguish "left" from "never
-- joined" for a non-admin. `join_date` is stored only where `date` MEANS
-- join (member/admin/self); a Banned `date` is the ban date and stays in raw.
CREATE TABLE IF NOT EXISTS participants (
    group_id                 INTEGER NOT NULL,
    uri                      TEXT NOT NULL,
    status                   TEXT NOT NULL
        CHECK (status IN ('member', 'admin', 'creator', 'banned', 'left')),
    join_date                TEXT,
    rank                     TEXT,
    subscription_until_date  TEXT,
    inviter_id               INTEGER,
    source_raw_id            INTEGER REFERENCES raw_records(id),
    first_seen               TEXT NOT NULL,
    last_seen                TEXT NOT NULL,
    PRIMARY KEY (group_id, uri)
);
CREATE INDEX IF NOT EXISTS idx_participants_uri ON participants(uri);

-- append-only membership observations: one row per enumerated member per
-- run (uri set) PLUS one roster-level accounting row per (group, run) with
-- uri NULL carrying `enumerated / true_count` and, when walled, `reason`.
CREATE TABLE IF NOT EXISTS participant_snapshots (
    id                       INTEGER PRIMARY KEY,
    group_id                 INTEGER NOT NULL,
    observed_at              TEXT NOT NULL,
    uri                      TEXT,
    status                   TEXT,
    join_date                TEXT,
    rank                     TEXT,
    subscription_until_date  TEXT,
    enumerated               INTEGER,
    true_count               INTEGER,
    reason                   TEXT,
    source_raw_id            INTEGER REFERENCES raw_records(id)
);
CREATE INDEX IF NOT EXISTS idx_participant_snapshots_group
    ON participant_snapshots(group_id, observed_at);
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store_migrations.py`:

```python
def test_0004_people_tables_exist(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        applied = {r["name"] for r in st.conn.execute("select name from schema_migrations")}
        assert "0004_people" in applied
        names = {
            r["name"]
            for r in st.conn.execute("select name from sqlite_master where type='table'")
        }
        for expected in (
            "users", "user_snapshots", "user_photos", "participants", "participant_snapshots",
        ):
            assert expected in names, f"missing table {expected}"


def test_participants_status_is_constrained(tmp_path):
    import sqlite3

    with Store.open(tmp_path / "p.sqlite") as st:
        with pytest.raises(sqlite3.IntegrityError):
            st.conn.execute(
                "insert into participants (group_id, uri, status, first_seen, last_seen) "
                "values (1, 'tg:user:1', 'lurker', 'now', 'now')"
            )


def test_user_photos_unique_per_user_and_photo(tmp_path):
    import sqlite3

    with Store.open(tmp_path / "p.sqlite") as st:
        st.conn.execute(
            "insert into user_photos (uri, photo_id, observed_at) values ('tg:user:1', 7, 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            st.conn.execute(
                "insert into user_photos (uri, photo_id, observed_at) "
                "values ('tg:user:1', 7, 'later')"
            )


def test_run_id_property_reflects_begin_run(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        assert st.run_id is None
        assert st.begin_run("r1") == "r1" and st.run_id == "r1"
```

(add `import pytest` at the top of that file.)

Append these tests to `tests/test_ids.py`, adding `UTC, datetime` (from `datetime`) and `iso_or_none, namespaced_kind, peer_stub` (from `paperboy.ids`) to the file's EXISTING top-of-file import block — never as a second import block below existing code (`ruff` selects `E`: E402/F811 would fail every "green" step; this applies to every "append to an existing test file" step in this plan):

```python
def test_iso_or_none_accepts_int_str_datetime_and_none():
    assert iso_or_none(None) is None
    assert iso_or_none("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    assert iso_or_none(0) == "1970-01-01T00:00:00+00:00"
    # Telethon's to_dict() hands back aware datetimes for TL `date` fields.
    assert iso_or_none(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"


def test_peer_stub_maps_peer_references_to_min_stubs():
    assert peer_stub({"_": "PeerUser", "user_id": 5}) == {"_": "User", "id": 5, "min": True}
    assert peer_stub({"_": "peerChannel", "channel_id": 9}) == {
        "_": "Channel", "id": 9, "min": True,
    }
    assert peer_stub({"_": "PeerChat", "chat_id": 3}) is None  # basic groups: not a stub kind
    assert peer_stub(None) is None
    assert peer_stub({"_": "PeerUser"}) is None  # id-less reference


def test_namespaced_kind_prefixes_bare_class_names_only():
    assert namespaced_kind("users", {"_": "UserFull"}, "UserFull") == "users.UserFull"
    assert namespaced_kind("users", {"_": "users.UserFull"}, "UserFull") == "users.UserFull"
    assert namespaced_kind("users", {}, "UserFull") == "users.UserFull"
```

Append to `tests/test_clock.py`:

```python
def test_live_clock_now_is_iso_utc():
    assert LiveClock().now().endswith("+00:00")


def test_replay_clock_now_is_the_last_served_stamp():
    clock = ReplayClock()
    with pytest.raises(ReplayClockError):
        clock.now()
    clock.serve("2026-01-01T00:00:07+00:00", {"_": "a"})
    assert clock.now() == "2026-01-01T00:00:07+00:00"
```

- [ ] **Step 2: Run to verify failure**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_store_migrations.py tests/test_ids.py tests/test_clock.py -q --basetemp=/Volumes/Storage/tmp/pytest`
Expected: FAIL — `0004_people` not applied; `ImportError` on `iso_or_none`/`peer_stub`/`namespaced_kind`; `AttributeError: now`.

- [ ] **Step 3: Write the migration** at `src/paperboy/store/migrations/0004_people.sql` — exactly the SQL in **Interfaces** above.

- [ ] **Step 4: Add the helpers to `src/paperboy/ids.py`**

```python
def iso_or_none(value: datetime | int | str | None) -> str | None:
    """`to_iso` for the three shapes a TL `date` reaches a projection in:
    an aware `datetime` (Telethon's `to_dict()`), epoch seconds (hand-authored
    fixtures), or already-ISO text (a replayed raw record). `None` passes
    through — absence is data here (spec §4.3), never an error."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return to_iso(value)


# `Peer*`/`InputPeer*` discriminator -> (stub constructor tag, id field).
# Basic groups (`PeerChat`) are deliberately absent: paperboy targets
# channel-typed peers and a `Chat` stub has no min/provenance story.
_PEER_STUB_KIND = {
    "peeruser": ("User", "user_id"), "inputpeeruser": ("User", "user_id"),
    "peerchannel": ("Channel", "channel_id"), "inputpeerchannel": ("Channel", "channel_id"),
}


def peer_stub(ref: dict | None) -> dict | None:
    """A minimal `min` peer object for a bare `Peer*` reference (a message's
    `from_id`, a forward header's `from_id`, a `recent_repliers` entry, a
    reaction's `peer_id`), or `None` for anything else. One mapping shared by
    every producer of message-referenced stubs so they all agree."""
    if not ref:
        return None
    mapped = _PEER_STUB_KIND.get((ref.get("_") or "").lower())
    if mapped is None:
        return None
    tag, id_field = mapped
    peer_id = ref.get(id_field)
    return None if peer_id is None else {"_": tag, "id": peer_id, "min": True}


def namespaced_kind(namespace: str, payload: dict, default: str) -> str:
    """The raw-record kind for an RPC-result ENVELOPE. Telethon's `to_dict()`
    emits the bare class name, so `users.UserFull` (the envelope) and
    `UserFull` (its inner object) both serialise as `"UserFull"` — replay
    keys off `kind`, so envelopes are stamped with their TL namespace
    explicitly. A payload whose `_` is already dotted is returned as-is."""
    kind = payload.get("_") or default
    return kind if "." in kind else f"{namespace}.{kind}"
```

`datetime` is already imported in `ids.py`.

Then in `src/paperboy/store/messages.py` delete `_iso_or_none` and its `to_iso` import use, `from paperboy.ids import iso_or_none, msg_uri, peer_ref_uri`, and replace the two call sites (`date`, `edit_date`) with `iso_or_none(...)`. In `src/paperboy/collectors/history.py` delete `_author_stub`, import `peer_stub` from `paperboy.ids`, and replace `stub = _author_stub(m.get("from_id"))` with `stub = peer_stub(m.get("from_id"))`. In `src/paperboy/store/repliers.py` delete `_PEER_STUB_KIND` and `_peer_stub`, import `peer_stub`, and replace `stub = _peer_stub(peer)` with `stub = peer_stub(peer)`.

- [ ] **Step 5: `Clock.now()` in `src/paperboy/clock.py`, `Store.run_id` in `src/paperboy/store/db.py`**

```python
class Clock(Protocol):
    def for_payload(self, payload: dict) -> str: ...

    def now(self) -> str:
        """"Now" for a decision that has no payload of its own (e.g. the
        profiles refresh floor): the wall clock live, the last served record's
        stamp on replay — so the decision is reproducible from raw."""
        ...


class LiveClock:
    def for_payload(self, payload: dict) -> str: ...  # unchanged

    def now(self) -> str:
        return utc_now_iso()


class ReplayClock:
    ...
    def now(self) -> str:
        if self._current is None:
            raise ReplayClockError("ReplayClock.now before any record was served")
        return self._current
```

and on `Store` (after `begin_run`):

```python
    @property
    def run_id(self) -> str | None:
        """The current collect pass's id (`begin_run`), or `None` before one
        begins — lets a collector do something exactly once per run."""
        return self._run_id
```

- [ ] **Step 6: Run the three test files, then the full suite + lint/type**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest && uv run ruff check && uv run pyright`
Expected: all green (the parity golden is unaffected: no projection changed).

- [ ] **Step 7: Commit**

```bash
git add src/paperboy/store/migrations/0004_people.sql src/paperboy/ids.py src/paperboy/clock.py \
  src/paperboy/store/db.py src/paperboy/store/messages.py src/paperboy/store/repliers.py src/paperboy/collectors/history.py \
  tests/test_store_migrations.py tests/test_ids.py tests/test_clock.py
git commit -m "feat(store): 0004_people migration + shared iso/peer-stub/kind helpers, Clock.now (#41)"
```

---
### Task 2: `store/users.py` — `users`, `user_snapshots`, `user_photos` writers + the tri-state shape

**Files:**
- Create: `src/paperboy/store/users.py`
- Test: `tests/test_store_users.py`

**Interfaces:**
- Consumes: Task 1's tables; `ids.iso_or_none`, `ids.primary_username`, `ids.user_uri`; `store.sync.is_self`; `store.db.dumps`.
- Produces (every later task uses these exact names):
  - `FIELD_STATE_KEYS: tuple[str, ...]` = `("phone", "photo", "status", "about", "birthday", "forwards", "stories")`
  - `field_states(user: dict, full_user: dict | None = None) -> dict[str, dict]` — the D2 tri-state map.
  - `merge_field_states(existing: dict, incoming: dict, *, full: bool) -> dict` — a triage-level (`full=False`) `absent` never overwrites a stored `hidden_from_you`.
  - `target_user_facts(user: dict) -> dict` / `target_full_facts(full_user: dict) -> dict` — the observed bundle minus every fact-about-us field (spec §4.3) and minus empty values.
  - `upsert_user(store, user: dict, source_raw_id: int, observed_at: str, tier: str, *, full_user: dict | None = None) -> str | None` — returns the user URI, or `None` for the collecting account. Sets `enriched_at` iff `full_user` is given.
  - `add_user_snapshot(store, uri: str, observed_at: str, tier: str, method: str, bundle: dict, source_raw_id: int) -> bool` — appends iff the bundle hash differs from the latest snapshot for `(uri, method)`.
  - `upsert_user_photo(store, uri: str, photo: dict, observed_at: str, source_raw_id: int) -> None`; `user_photo_sha(store, uri: str, photo_id: int) -> str | None`; `set_user_photo_sha(store, uri: str, photo_id: int, sha256: str) -> None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_store_users.py`

```python
"""`users` / `user_snapshots` / `user_photos` projection + the tri-state shape (spec §4, §4.3)."""

from __future__ import annotations

import json

import pytest

from paperboy.store.db import Store
from paperboy.store.sync import set_state
from paperboy.store.users import (
    add_user_snapshot,
    field_states,
    merge_field_states,
    set_user_photo_sha,
    target_full_facts,
    target_user_facts,
    upsert_user,
    upsert_user_photo,
    user_photo_sha,
)

T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"
T3 = "2026-01-03T00:00:00+00:00"


def _full_user_obj(**extra) -> dict:
    return {
        "_": "User", "id": 9, "access_hash": 111, "first_name": "Real", "last_name": "Person",
        "username": None,
        "usernames": [
            {"_": "Username", "username": "bought", "editable": False, "active": True},
            {"_": "Username", "username": "chosen", "editable": True, "active": True},
        ],
        "phone": "+15550001111", "verified": True, "premium": True, "scam": None,
        "photo": {"_": "UserProfilePhoto", "photo_id": 77, "dc_id": 2, "has_video": False,
                  "personal": None, "stripped_thumb": None},
        "status": {"_": "UserStatusOffline", "was_online": 1767322445},
        "emoji_status": {"_": "EmojiStatus", "document_id": 5, "until": None},
        "restriction_reason": [],
        **extra,
    }


def _store(tmp_path) -> Store:
    return Store.open(tmp_path / "p.sqlite")


def _row(st, uri="tg:user:9"):
    return st.conn.execute("select * from users where uri=?", (uri,)).fetchone()


def test_upsert_user_projects_triage_columns(tmp_path):
    with _store(tmp_path) as st:
        u = _full_user_obj()
        rid = st.add_raw("User", u, "stranger", None)
        assert upsert_user(st, u, rid, T1, "stranger") == "tg:user:9"
        row = _row(st)
        assert row["username"] == "chosen"  # the editable (self-chosen) handle wins
        # the full multi-username structure survives — `editable: False` = Fragment/collectible
        assert [e["username"] for e in json.loads(row["usernames_json"])] == ["bought", "chosen"]
        assert row["phone"] == "+15550001111"
        assert row["status_kind"] == "offline"
        assert row["status_value"] == "2026-01-02T02:54:05+00:00"  # 1767322445
        assert json.loads(row["photo_ref"])["photo_id"] == 77
        assert json.loads(row["flags_json"]) == {"verified": True, "premium": True}
        assert json.loads(row["emoji_status"])["document_id"] == 5
        assert row["is_min"] == 0 and row["enriched_at"] is None
        assert row["first_seen"] == row["last_seen"] == T1
        states = json.loads(row["field_states_json"])
        assert states["phone"] == {"state": "present"}
        assert states["photo"] == {"state": "present"}
        assert states["status"] == {"state": "present", "granularity": "exact"}
        assert "about" not in states  # triage cannot observe full-only fields


def test_collecting_account_is_never_written(tmp_path):
    with _store(tmp_path) as st:
        set_state(st, "account", "self", {"uri": "tg:user:9", "id": 9})
        u = _full_user_obj()
        rid = st.add_raw("User", u, "self", None)
        assert upsert_user(st, u, rid, T1, "self") is None
        assert _row(st) is None


def test_min_never_clobbers_a_full_row_but_widens_the_window(tmp_path):
    with _store(tmp_path) as st:
        full = _full_user_obj()
        r1 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r1, T1, "stranger")
        mn = {"_": "User", "id": 9, "min": True, "first_name": "Min", "phone": "",
              "status": {"_": "UserStatusRecently", "by_me": None}}
        r2 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r2, T2, "stranger")
        row = _row(st)
        assert row["first_name"] == "Real" and row["phone"] == "+15550001111"
        assert row["is_min"] == 0
        assert row["status_kind"] == "offline"  # cached status is not empty -> min status ignored
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)


def test_min_first_then_full_applies_even_when_the_full_is_older(tmp_path):
    # Richness beats recency (ADR-0005 §6's min<-full cell, applied here too).
    with _store(tmp_path) as st:
        mn = {"_": "User", "id": 9, "min": True, "first_name": "Min"}
        r1 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r1, T2, "stranger")
        full = _full_user_obj()
        r2 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r2, T1, "stranger")
        row = _row(st)
        assert row["first_name"] == "Real" and row["is_min"] == 0
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)


def test_triage_after_full_keeps_about_birthday_and_enriched_at(tmp_path):
    with _store(tmp_path) as st:
        user = _full_user_obj()
        full_user = {"_": "UserFull", "id": 9, "about": "bio text",
                     "birthday": {"_": "Birthday", "day": 4, "month": 7, "year": None},
                     "profile_photo": {"_": "Photo", "id": 77}}
        r1 = st.add_raw("users.UserFull", {"full_user": full_user, "users": [user]}, "stranger", None)
        upsert_user(st, user, r1, T1, "stranger", full_user=full_user)
        row = _row(st)
        assert row["about"] == "bio text"
        assert json.loads(row["birthday"]) == {"day": 4, "month": 7, "year": None}
        assert row["enriched_at"] == T1
        states = json.loads(row["field_states_json"])
        assert states["about"] == {"state": "present"} and states["birthday"] == {"state": "present"}

        r2 = st.add_raw("User", user, "stranger", None)
        upsert_user(st, {**user, "first_name": "Renamed"}, r2, T2, "stranger")
        row = _row(st)
        assert row["first_name"] == "Renamed"
        assert row["about"] == "bio text" and row["enriched_at"] == T1  # triage never blanks these
        assert json.loads(row["field_states_json"])["about"] == {"state": "present"}


def test_field_states_phone_empty_string_is_the_min_wire_state():
    states = field_states({"_": "User", "id": 1, "min": True, "phone": ""})
    assert states["phone"] == {"state": "absent", "why": "min_empty_string"}
    assert field_states({"_": "User", "id": 1, "phone": None})["phone"] == {"state": "absent"}
    assert field_states({"_": "User", "id": 1, "phone": "+1"})["phone"] == {"state": "present"}


def test_field_states_fallback_photo_proves_hidden_from_you():
    user = {"_": "User", "id": 1, "photo": None}
    full = {"_": "UserFull", "id": 1, "profile_photo": None,
            "fallback_photo": {"_": "Photo", "id": 3}}
    assert field_states(user, full)["photo"] == {"state": "hidden_from_you", "why": "fallback_photo"}
    # Absence alone stays ambiguous — never "not set".
    assert field_states(user, {"_": "UserFull", "id": 1})["photo"] == {"state": "absent"}
    assert field_states(user)["photo"] == {"state": "absent"}


def test_field_states_by_me_is_self_privacy_not_target_opsec():
    ours = field_states({"_": "User", "id": 1, "status": {"_": "UserStatusRecently", "by_me": True}})
    theirs = field_states({"_": "User", "id": 1, "status": {"_": "UserStatusLastWeek", "by_me": None}})
    assert ours["status"] == {"state": "present", "granularity": "coarse", "coarse_cause": "self_privacy"}
    assert theirs["status"] == {"state": "present", "granularity": "coarse", "coarse_cause": "target_privacy"}
    assert field_states({"_": "User", "id": 1, "status": {"_": "UserStatusEmpty"}})["status"] == {"state": "absent"}


def test_field_states_forwards_and_stories():
    full = {"_": "UserFull", "id": 1, "private_forward_name": "Anon", "stories": None}
    s = field_states({"_": "User", "id": 1, "stories_unavailable": True}, full)
    assert s["forwards"] == {"state": "hidden_from_you", "why": "private_forward_name"}
    assert s["stories"] == {"state": "absent", "why": "stories_unavailable"}


def test_triage_absent_never_downgrades_a_hidden_proof():
    existing = {"photo": {"state": "hidden_from_you", "why": "fallback_photo"}}
    triage = {"photo": {"state": "absent"}}
    assert merge_field_states(existing, triage, full=False)["photo"]["state"] == "hidden_from_you"
    assert merge_field_states(existing, triage, full=True)["photo"] == {"state": "absent"}
    assert merge_field_states(existing, {"photo": {"state": "present"}}, full=False)["photo"] == {"state": "present"}


def test_personal_photo_is_never_ingested_as_target_data(tmp_path):
    with _store(tmp_path) as st:
        u = _full_user_obj(photo={"_": "UserProfilePhoto", "photo_id": 5, "dc_id": 1,
                                  "has_video": False, "personal": True, "stripped_thumb": None})
        rid = st.add_raw("User", u, "stranger", None)
        upsert_user(st, u, rid, T1, "stranger")
        row = _row(st)
        assert row["photo_ref"] is None
        assert json.loads(row["field_states_json"])["photo"] == {"state": "absent", "why": "personal_photo_shadows"}


def test_target_facts_strip_facts_about_us_and_empties():
    full = {"_": "UserFull", "id": 9, "about": "bio", "common_chats_count": 3, "blocked": True,
            "personal_photo": {"_": "Photo", "id": 1}, "note": {"text": "mine"},
            "settings": {"_": "PeerSettings"}, "notify_settings": {"_": "PeerNotifySettings"},
            "personal_channel_id": 42, "birthday": None}
    assert target_full_facts(full) == {"id": 9, "about": "bio", "personal_channel_id": 42}
    user = {"_": "User", "id": 9, "contact": True, "mutual_contact": None, "first_name": "R",
            "restriction_reason": [], "usernames": [], "phone": "", "min": True, "premium": True}
    assert target_user_facts(user) == {"id": 9, "first_name": "R", "premium": True}


def test_snapshot_dedupes_by_uri_and_method_hash(tmp_path):
    with _store(tmp_path) as st:
        rid = st.add_raw("User", {"_": "User", "id": 9}, "stranger", None)
        assert add_user_snapshot(st, "tg:user:9", T1, "stranger", "users.getUsers", {"a": 1}, rid)
        assert not add_user_snapshot(st, "tg:user:9", T2, "stranger", "users.getUsers", {"a": 1}, rid)
        # a different METHOD with the same bundle is a distinct observation stream
        assert add_user_snapshot(st, "tg:user:9", T2, "stranger", "users.getFullUser", {"a": 1}, rid)
        assert add_user_snapshot(st, "tg:user:9", T3, "stranger", "users.getUsers", {"a": 2}, rid)
        n = st.conn.execute("select count(*) from user_snapshots").fetchone()[0]
        assert n == 3


def test_user_photos_upsert_and_sha_link(tmp_path):
    with _store(tmp_path) as st:
        rid = st.add_raw("photos.Photos", {"_": "Photos"}, "stranger", None)
        photo = {"_": "Photo", "id": 77, "access_hash": 1, "date": 1767322445, "dc_id": 2,
                 "video_sizes": None, "sizes": []}
        upsert_user_photo(st, "tg:user:9", photo, T1, rid)
        upsert_user_photo(st, "tg:user:9", photo, T2, rid)  # idempotent, observed_at widens
        row = st.conn.execute("select * from user_photos where uri='tg:user:9'").fetchone()
        assert row["photo_id"] == 77 and row["date"] == "2026-01-02T02:54:05+00:00"
        assert row["observed_at"] == T2 and row["has_video"] == 0
        assert user_photo_sha(st, "tg:user:9", 77) is None
        st.conn.execute(
            "insert into media (sha256, kind, mime_type, size, path, downloaded_at) "
            "values ('ab', 'avatar', 'image/jpeg', 1, '/x', ?)", (T1,)
        )
        set_user_photo_sha(st, "tg:user:9", 77, "ab")
        assert user_photo_sha(st, "tg:user:9", 77) == "ab"


def test_upsert_user_rejects_non_user_objects(tmp_path):
    with _store(tmp_path) as st:
        with pytest.raises(ValueError):
            upsert_user(st, {"_": "Channel", "id": 1}, 1, T1, "stranger")
```

- [ ] **Step 2: Run to verify failure**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_store_users.py -q --basetemp=/Volumes/Storage/tmp/pytest`
Expected: FAIL with `ModuleNotFoundError: paperboy.store.users`.

- [ ] **Step 3: Implement `src/paperboy/store/users.py`**

```python
"""Person projection: current profile state (`users`) + append-only
observation log (`user_snapshots`) + dated avatar history (`user_photos`).

Profile richness lives HERE, never in `peers` (spec §4): `peers` stays the
min-provenance stub table with its own merge lattice (ADR-0005 §6, open
residuals #38/#39), and this module never touches it.

Tri-state (spec §4.3, encoded per plan D2): every privacy-gated field carries
`{"state": present | absent | hidden_from_you, ...}` in `field_states_json`.
Telegram enforces privacy by OMISSION — there is no privacy-denial error —
so plain absence never proves "not set": `absent` is the honest record for
"not on the wire, no disambiguator". `hidden_from_you` is written only with
a machine-readable proof (`fallback_photo` present while `profile_photo` is
absent; `private_forward_name` present). A user status keeps `present` with
its granularity; a coarse bucket's `coarse_cause` is `self_privacy` when
`by_me` is set — OUR account's privacy is degrading the data, not the
target's opsec (research §6).

Merge rules follow research §8.7 for `min` objects (a min observation never
clobbers full identity; status only if the cached status is empty; photo only
with `apply_min_photo`) and ADR-0005 §6's composed richness ∘ recency lattice
for full ones, done in Python rather than one giant CASE statement so the
cells stay readable.
"""

from __future__ import annotations

import hashlib
import json

from paperboy.ids import iso_or_none, primary_username, user_uri
from paperboy.store.db import Store, dumps
from paperboy.store.sync import is_self

FIELD_STATE_KEYS = ("phone", "photo", "status", "about", "birthday", "forwards", "stories")

# `User` flags that are facts about the TARGET (kept in `users.flags_json`).
_TARGET_FLAG_KEYS = (
    "deleted", "bot", "verified", "restricted", "support", "scam", "fake", "premium",
    "stories_unavailable", "contact_require_premium",
)
# `User` fields that describe US or our relationship, never the target
# (research §2a/§2b, §8.7): never ingested.
_USER_SELF_FACTS = frozenset({
    "is_self", "contact", "mutual_contact", "attach_menu_enabled", "bot_can_edit",
    "close_friend", "stories_hidden", "min", "apply_min_photo",
})
# `UserFull` fields that describe OUR side (spec §4.3's four, plus every
# other SELF/REL field in research §3a/§3b): never ingested as target data.
_FULL_SELF_FACTS = frozenset({
    "common_chats_count", "blocked", "personal_photo", "note", "settings",
    "notify_settings", "folder_id", "pinned_msg_id", "ttl_period", "theme", "wallpaper",
    "wallpaper_overridden", "translations_disabled", "blocked_my_stories_from",
    "has_scheduled", "can_pin_message", "sponsored_enabled", "stars_my_pending_rating",
    "stars_my_pending_rating_date", "noforwards_my_enabled", "display_gifts_button",
    "business_greeting_message", "business_away_message", "can_view_revenue",
    "bot_can_manage_emoji_status",
})
_STATUS_KINDS = {
    "userstatusonline": "online", "userstatusoffline": "offline",
    "userstatusrecently": "recently", "userstatuslastweek": "last_week",
    "userstatuslastmonth": "last_month", "userstatusempty": "empty",
}
_COARSE_STATUS = {"recently", "last_week", "last_month"}
_FULL_ONLY_COLUMNS = ("about", "birthday")


def _kind(obj: dict | None) -> str:
    return ((obj or {}).get("_") or "").lower()


def _facts(obj: dict, exclude: frozenset[str]) -> dict:
    """`obj` minus the discriminator, the excluded keys, and empty values
    (`None`, `""`, `[]`) — absence is recorded by `field_states`, not by a
    sea of nulls in the snapshot bundle."""
    return {
        k: v for k, v in obj.items()
        if k != "_" and k not in exclude and v is not None and v != "" and v != []
    }


def target_user_facts(user: dict) -> dict:
    return _facts(user, _USER_SELF_FACTS)


def target_full_facts(full_user: dict) -> dict:
    return _facts(full_user, _FULL_SELF_FACTS)


def field_states(user: dict, full_user: dict | None = None) -> dict[str, dict]:
    """The tri-state map for one observation. Keys observable at triage level
    (a bare `User`): `phone`, `photo`, `status`. With a `UserFull`: also
    `about`, `birthday`, `forwards`, `stories`, and `photo` gains its
    disambiguator."""
    states: dict[str, dict] = {}

    phone = user.get("phone")
    if phone:
        states["phone"] = {"state": "present"}
    elif user.get("min") and phone == "":
        # flags.4 set with "" is a real min wire state (research §8.2):
        # test non-empty, never presence.
        states["phone"] = {"state": "absent", "why": "min_empty_string"}
    else:
        states["phone"] = {"state": "absent"}

    if full_user is not None:
        if full_user.get("profile_photo"):
            states["photo"] = {"state": "present"}
        elif full_user.get("fallback_photo"):
            states["photo"] = {"state": "hidden_from_you", "why": "fallback_photo"}
        else:
            states["photo"] = {"state": "absent"}
    else:
        photo = user.get("photo") or {}
        if _kind(photo) == "userprofilephoto" and not photo.get("personal"):
            states["photo"] = {"state": "present"}
        elif photo.get("personal"):
            # A photo WE set for them shadows their real avatar in `user.photo`
            # (research §8.5) — zero information about the target.
            states["photo"] = {"state": "absent", "why": "personal_photo_shadows"}
        else:
            states["photo"] = {"state": "absent"}

    status = user.get("status") or {}
    status_kind = _STATUS_KINDS.get(_kind(status))
    if status_kind in ("online", "offline"):
        states["status"] = {"state": "present", "granularity": "exact"}
    elif status_kind in _COARSE_STATUS:
        states["status"] = {
            "state": "present", "granularity": "coarse",
            "coarse_cause": "self_privacy" if status.get("by_me") else "target_privacy",
        }
    else:
        states["status"] = {"state": "absent"}

    if full_user is not None:
        states["about"] = {"state": "present" if full_user.get("about") else "absent"}
        states["birthday"] = {"state": "present" if full_user.get("birthday") else "absent"}
        if full_user.get("private_forward_name"):
            states["forwards"] = {"state": "hidden_from_you", "why": "private_forward_name"}
        else:
            states["forwards"] = {"state": "absent"}
        if full_user.get("stories"):
            states["stories"] = {"state": "present"}
        elif user.get("stories_unavailable"):
            states["stories"] = {"state": "absent", "why": "stories_unavailable"}
        else:
            states["stories"] = {"state": "absent"}
    return states


def merge_field_states(existing: dict, incoming: dict, *, full: bool) -> dict:
    """Newest observation wins per key it can observe — except that a
    triage-level `absent` (no disambiguating capability) never overwrites a
    stored `hidden_from_you` proof; only a newer FULL observation revises it."""
    merged = dict(existing)
    for key, state in incoming.items():
        if (
            not full
            and state.get("state") == "absent"
            and merged.get(key, {}).get("state") == "hidden_from_you"
        ):
            continue
        merged[key] = state
    return merged


def _triage_columns(user: dict) -> dict:
    status = user.get("status") or {}
    status_kind = _STATUS_KINDS.get(_kind(status))
    if status_kind == "online":
        status_value = iso_or_none(status.get("expires"))
    elif status_kind == "offline":
        status_value = iso_or_none(status.get("was_online"))
    else:
        status_value = None

    photo = user.get("photo") or {}
    photo_ref = None
    if _kind(photo) == "userprofilephoto" and not photo.get("personal"):
        photo_ref = dumps({k: photo.get(k) for k in ("photo_id", "dc_id", "has_video", "stripped_thumb")})

    emoji = user.get("emoji_status") or {}
    emoji_status = dumps(emoji) if emoji and _kind(emoji) != "emojistatusempty" else None
    color = {k: user[k] for k in ("color", "profile_color") if user.get(k)}
    usernames = [
        {k: e.get(k) for k in ("username", "editable", "active")}
        for e in (user.get("usernames") or [])
    ]
    bot = {}
    if user.get("bot"):
        bot = {k: v for k, v in user.items() if k.startswith("bot_") and v not in (None, False, "", [])}
    flags = {k: user[k] for k in _TARGET_FLAG_KEYS if user.get(k)}
    restriction = user.get("restriction_reason") or None
    return {
        "id": user["id"],
        "access_hash": user.get("access_hash"),
        "username": primary_username(user),
        "usernames_json": dumps(usernames) if usernames else None,
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "phone": user.get("phone") or None,
        "emoji_status": emoji_status,
        "color_json": dumps(color) if color else None,
        "status_kind": status_kind,
        "status_value": status_value,
        "photo_ref": photo_ref,
        "restriction_json": dumps(restriction) if restriction else None,
        "bot_json": dumps(bot) if bot else None,
        "flags_json": dumps(flags) if flags else None,
    }


def _full_columns(user: dict, full_user: dict, bot_json: str | None) -> dict:
    birthday = full_user.get("birthday") or None
    cols = {
        "about": full_user.get("about") or None,
        "birthday": dumps({k: birthday.get(k) for k in ("day", "month", "year")}) if birthday else None,
    }
    if user.get("bot"):
        # `UserFull` carries the rest of the bot-only surface (bot_info,
        # bot_group_admin_rights, bot_verification, bot_manager_id, ...).
        bot = json.loads(bot_json) if bot_json else {}
        bot.update({k: v for k, v in full_user.items() if k.startswith("bot_") and v not in (None, False, "", [])})
        cols["bot_json"] = dumps(bot) if bot else None
    return cols


def upsert_user(
    store: Store,
    user: dict,
    source_raw_id: int,
    observed_at: str,
    tier: str,
    *,
    full_user: dict | None = None,
) -> str | None:
    """Project one `User` (plus its `UserFull`, when this observation came
    from `users.getFullUser`) into `users`; returns the URI, or `None` for the
    collecting account (never a subject, #12). `enriched_at` moves only when
    `full_user` is given, so a triage pass never looks like an enrichment."""
    kind = _kind(user)
    if not kind.startswith("user") or kind == "userempty":
        raise ValueError(f"not a User object: {user.get('_')!r}")
    uri = user_uri(user["id"])
    if is_self(store, uri):
        return None
    incoming_min = bool(user.get("min"))
    cols = _triage_columns(user)
    if full_user is not None:
        cols.update(_full_columns(user, full_user, cols["bot_json"]))
    states = field_states(user, full_user)

    existing = store.conn.execute("SELECT * FROM users WHERE uri=?", (uri,)).fetchone()
    if existing is None:
        cols.update({
            "uri": uri, "tier": tier, "is_min": int(incoming_min),
            "field_states_json": dumps(states),
            "enriched_at": observed_at if full_user is not None else None,
            "source_raw_id": source_raw_id, "first_seen": observed_at, "last_seen": observed_at,
        })
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        store.conn.execute(f"INSERT INTO users ({names}) VALUES ({marks})", tuple(cols.values()))
        return uri

    newer = observed_at >= existing["last_seen"]
    updates: dict = {}
    if incoming_min and not existing["is_min"]:
        # research §8.7: a min object never clobbers a full row's identity.
        # Status applies only if the cached status is empty; photo only with
        # `apply_min_photo`. Both still gated on recency.
        if newer and existing["status_kind"] in (None, "empty") and cols["status_kind"]:
            updates["status_kind"] = cols["status_kind"]
            updates["status_value"] = cols["status_value"]
        if newer and user.get("apply_min_photo") and cols["photo_ref"]:
            updates["photo_ref"] = cols["photo_ref"]
    elif newer or (existing["is_min"] and not incoming_min):
        # full<-full and min<-min on recency; min<-full always (richness).
        updates.update(cols)
        if full_user is None:
            for column in _FULL_ONLY_COLUMNS:
                updates.pop(column, None)  # triage never blanks full-only columns
        updates["is_min"] = int(incoming_min)
        updates["tier"] = tier
        updates["source_raw_id"] = source_raw_id
        merged = merge_field_states(
            json.loads(existing["field_states_json"] or "{}"), states, full=full_user is not None
        )
        updates["field_states_json"] = dumps(merged)
    if full_user is not None:
        updates["enriched_at"] = max(existing["enriched_at"] or "", observed_at)
    updates["first_seen"] = min(existing["first_seen"], observed_at)
    updates["last_seen"] = max(existing["last_seen"], observed_at)
    assignments = ", ".join(f"{column} = ?" for column in updates)
    store.conn.execute(f"UPDATE users SET {assignments} WHERE uri = ?", (*updates.values(), uri))
    return uri


def add_user_snapshot(
    store: Store,
    uri: str,
    observed_at: str,
    tier: str,
    method: str,
    bundle: dict,
    source_raw_id: int,
) -> bool:
    """Append one observation iff its bundle differs from the latest snapshot
    of the same `(uri, method)` — like `message_revisions`, a no-change
    re-observation is not a new row. Returns whether a row was written."""
    fields_json = dumps(bundle)
    content_hash = hashlib.sha256(fields_json.encode("utf-8")).hexdigest()
    latest = store.conn.execute(
        "SELECT content_hash FROM user_snapshots WHERE uri=? AND method=? "
        "ORDER BY observed_at DESC, id DESC LIMIT 1",
        (uri, method),
    ).fetchone()
    if latest is not None and latest["content_hash"] == content_hash:
        return False
    store.conn.execute(
        "INSERT INTO user_snapshots (uri, observed_at, tier, method, content_hash, fields_json, "
        "source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uri, observed_at, tier, method, content_hash, fields_json, source_raw_id),
    )
    return True


def upsert_user_photo(store: Store, uri: str, photo: dict, observed_at: str, source_raw_id: int) -> None:
    store.conn.execute(
        "INSERT INTO user_photos (uri, photo_id, date, dc_id, has_video, observed_at, source_raw_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uri, photo_id) DO UPDATE SET "
        "observed_at = MAX(user_photos.observed_at, excluded.observed_at), "
        "date = COALESCE(user_photos.date, excluded.date), "
        "source_raw_id = CASE WHEN excluded.observed_at >= user_photos.observed_at "
        "THEN excluded.source_raw_id ELSE user_photos.source_raw_id END",
        (
            uri, photo["id"], iso_or_none(photo.get("date")), photo.get("dc_id"),
            int(bool(photo.get("video_sizes"))), observed_at, source_raw_id,
        ),
    )


def user_photo_sha(store: Store, uri: str, photo_id: int) -> str | None:
    row = store.conn.execute(
        "SELECT sha256 FROM user_photos WHERE uri=? AND photo_id=?", (uri, photo_id)
    ).fetchone()
    return row["sha256"] if row else None


def set_user_photo_sha(store: Store, uri: str, photo_id: int, sha256: str) -> None:
    store.conn.execute(
        "UPDATE user_photos SET sha256=? WHERE uri=? AND photo_id=?", (sha256, uri, photo_id)
    )
```

- [ ] **Step 4: Run tests to green, then full suite + lint/type**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_store_users.py -q --basetemp=/Volumes/Storage/tmp/pytest && TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest && uv run ruff check && uv run pyright`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/paperboy/store/users.py tests/test_store_users.py
git commit -m "feat(store): users/user_snapshots/user_photos writers with tri-state field states (#41)"
```

---
### Task 3: `store/participants.py`, `store/message_peers.py` (#11), `store/reactions.py` — roster writers and the zero-RPC edge producers

**Files:**
- Create: `src/paperboy/store/participants.py`, `src/paperboy/store/message_peers.py`, `src/paperboy/store/reactions.py`
- Modify: `src/paperboy/store/messages.py` (no-shed: a `message_metrics` row is also written when *only* `reactions` is present — group messages carry no `views`, so their reactions never reached the metrics time series)
- Test: `tests/test_store_participants.py`, `tests/test_store_message_peers.py`, `tests/test_store_reactions.py`, `tests/test_store_messages.py` (one added test)

**Interfaces:**
- Consumes: Task 1 tables + `ids.peer_stub`/`iso_or_none`; `store.peers.upsert_peer`; `store.edges.add_edge_once`; `store.messages.upsert_message` (tests).
- Produces:
  - `participants.PARTICIPANT_STATUSES`, `participants.MEMBER_STATUSES = ("member", "admin", "creator")`
  - `@dataclass(frozen=True) ParticipantFacts(uri: str, status: str, join_date: str | None, rank: str | None, subscription_until_date: str | None, inviter_id: int | None)`
  - `participant_row(participant: dict) -> ParticipantFacts | None` — parses the six `channelParticipant*` constructors (unknown → `None`); `join_date` only from `channelParticipant`/`Self`/`Admin` (`Creator` has none; `Banned.date` is the ban date and stays in raw).
  - `write_participant(store, group_id: int, facts: ParticipantFacts, source_raw_id: int, observed_at: str) -> str | None` — newest-observation-wins upsert keyed `(group_id, uri)`; a known `join_date` is never blanked by a newer observation that lacks one; `None` for self.
  - `upsert_participant(store, group_id, participant: dict, source_raw_id, observed_at) -> ParticipantFacts | None` — `participant_row` + `write_participant`.
  - `add_participant_snapshot(store, group_id, facts: ParticipantFacts, observed_at, source_raw_id, *, once: bool = False) -> bool`
  - `add_roster_snapshot(store, group_id, observed_at, *, enumerated: int, true_count: int | None, reason: str | None, source_raw_id: int | None) -> None`
  - `membership_edges(store, group_id, facts, observed_at, tier, source_raw_id, evidence: dict) -> int` — `member_of` for `MEMBER_STATUSES`, plus `admin_of` for admin/creator; returns edges written.
  - `project_join_service_messages(store, group_id, tier) -> dict[str, int]` — counts `joins`, `leaves`, `edges`; idempotent.
  - `message_peers.backfill_message_referenced_peers(store, channel_id) -> int` — distinct peers upserted from `fwd_from.from_id` and `MessageEntityMentionName`.
  - `reactions.backfill_recent_reactions(store, channel_id, tier) -> int`; `reactions.reacted_message_ids(store, channel_id) -> list[int]` (newest first, from raw message payloads); `reactions.fetched_reaction_lists(store, channel_id) -> set[int]`; predicate constant `reactions.REACTED_TO = "reacted_to"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_store_participants.py`:

```python
"""`participants` / `participant_snapshots` writers + the zero-RPC join/leave service-message vector."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.participants import (
    ParticipantFacts,
    add_participant_snapshot,
    add_roster_snapshot,
    membership_edges,
    participant_row,
    project_join_service_messages,
    upsert_participant,
    write_participant,
)
from paperboy.store.sync import set_state

GROUP_ID = 77
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"
JOINED = 1735689600  # 2025-01-01T00:00:00+00:00


def test_participant_row_parses_every_constructor():
    member = participant_row({"_": "ChannelParticipant", "user_id": 1, "date": JOINED,
                              "subscription_until_date": None, "rank": "scout"})
    assert member == ParticipantFacts("tg:user:1", "member", "2025-01-01T00:00:00+00:00", "scout", None, None)
    me = participant_row({"_": "ChannelParticipantSelf", "user_id": 2, "inviter_id": 9, "date": JOINED,
                          "via_request": True, "subscription_until_date": None, "rank": None})
    assert me is not None and (me.status, me.inviter_id) == ("member", 9)
    creator = participant_row({"_": "ChannelParticipantCreator", "user_id": 3,
                               "admin_rights": {"_": "ChatAdminRights"}, "rank": "founder"})
    assert creator == ParticipantFacts("tg:user:3", "creator", None, "founder", None, None)
    admin = participant_row({"_": "ChannelParticipantAdmin", "user_id": 4, "promoted_by": 3, "date": JOINED,
                             "admin_rights": {"_": "ChatAdminRights"}, "can_edit": None, "is_self": None,
                             "inviter_id": None, "rank": None})
    assert admin is not None and (admin.status, admin.join_date) == ("admin", "2025-01-01T00:00:00+00:00")
    banned = participant_row({"_": "ChannelParticipantBanned", "peer": {"_": "PeerChannel", "channel_id": 5},
                              "kicked_by": 3, "date": JOINED, "banned_rights": {}, "left": True, "rank": None})
    assert banned == ParticipantFacts("tg:channel:5", "banned", None, None, None, None)  # ban date != join
    left = participant_row({"_": "ChannelParticipantLeft", "peer": {"_": "PeerUser", "user_id": 6}})
    assert left == ParticipantFacts("tg:user:6", "left", None, None, None, None)
    assert participant_row({"_": "SomethingElse", "user_id": 7}) is None


def test_upsert_writes_current_state_and_a_newer_observation_wins_keeping_join_date(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r1 = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        facts = upsert_participant(
            st, GROUP_ID, {"_": "ChannelParticipant", "user_id": 1, "date": JOINED, "rank": "scout"}, r1, T1
        )
        assert facts is not None and facts.uri == "tg:user:1"
        r2 = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        upsert_participant(
            st, GROUP_ID,
            {"_": "ChannelParticipantBanned", "peer": {"_": "PeerUser", "user_id": 1}, "kicked_by": 3,
             "date": JOINED + 86400, "banned_rights": {}, "rank": None},
            r2, T2,
        )
        row = st.conn.execute("select * from participants where uri='tg:user:1'").fetchone()
        assert row["status"] == "banned"
        assert row["join_date"] == "2025-01-01T00:00:00+00:00"  # a known join date is never blanked
        assert row["rank"] is None  # rank DID move: the newer observation carries none
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)
        assert row["source_raw_id"] == r2


def test_older_observation_never_overrides_status(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        write_participant(st, GROUP_ID, ParticipantFacts("tg:user:1", "left", None, None, None, None), r, T2)
        write_participant(
            st, GROUP_ID, ParticipantFacts("tg:user:1", "member", "2025-01-01T00:00:00+00:00", None, None, None), r, T1
        )
        row = st.conn.execute("select status, join_date, first_seen from participants").fetchone()
        assert (row["status"], row["join_date"], row["first_seen"]) == ("left", "2025-01-01T00:00:00+00:00", T1)


def test_collecting_account_is_never_a_participant_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:2", "id": 2})
        r = st.add_raw("channels.ChannelParticipants", {}, "member", None)
        assert upsert_participant(
            st, GROUP_ID, {"_": "ChannelParticipantSelf", "user_id": 2, "inviter_id": 9, "date": JOINED}, r, T1
        ) is None
        assert st.conn.execute("select count(*) from participants").fetchone()[0] == 0


def test_snapshots_member_rows_and_roster_accounting_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        facts = ParticipantFacts("tg:user:1", "member", "2025-01-01T00:00:00+00:00", None, None, None)
        assert add_participant_snapshot(st, GROUP_ID, facts, T1, r)
        assert add_participant_snapshot(st, GROUP_ID, facts, T1, r)  # append-only by default
        assert not add_participant_snapshot(st, GROUP_ID, facts, T1, r, once=True)
        add_roster_snapshot(st, GROUP_ID, T1, enumerated=1, true_count=307, reason=None, source_raw_id=r)
        rows = st.conn.execute(
            "select uri, enumerated, true_count from participant_snapshots order by id"
        ).fetchall()
        assert [tuple(x) for x in rows] == [("tg:user:1", None, None), ("tg:user:1", None, None), (None, 1, 307)]


def test_membership_edges_only_for_member_statuses(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        admin = ParticipantFacts("tg:user:1", "admin", None, None, None, None)
        assert membership_edges(st, GROUP_ID, admin, T1, "stranger", r, {"source": "roster"}) == 2
        assert membership_edges(st, GROUP_ID, admin, T1, "stranger", r, {"source": "roster"}) == 0  # once
        banned = ParticipantFacts("tg:user:2", "banned", None, None, None, None)
        assert membership_edges(st, GROUP_ID, banned, T1, "stranger", r, {}) == 0
        preds = sorted(
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        )
        assert preds == [("tg:user:1", "admin_of", "tg:channel:77"), ("tg:user:1", "member_of", "tg:channel:77")]


def _service(msg_id: int, action: dict, from_user: int | None, date: int) -> dict:
    m = {"_": "MessageService", "id": msg_id, "date": date, "action": action,
         "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if from_user is not None:
        m["from_id"] = {"_": "PeerUser", "user_id": from_user}
    return m


def _seed(st: Store, m: dict) -> None:
    raw_id = st.add_raw(m["_"], m, "stranger", {"channel_id": GROUP_ID})
    upsert_message(st, GROUP_ID, m, raw_id, T1, "stranger")


def test_join_service_messages_project_membership_and_invite_edges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _service(10, {"_": "MessageActionChatAddUser", "users": [5, 6]}, 4, JOINED))
        _seed(st, _service(11, {"_": "MessageActionChatJoinedByLink", "inviter_id": 4}, 7, JOINED + 60))
        _seed(st, _service(12, {"_": "MessageActionChatJoinedByRequest"}, 8, JOINED + 120))
        _seed(st, _service(13, {"_": "MessageActionChatDeleteUser", "user_id": 6}, 6, JOINED + 180))
        _seed(st, _service(14, {"_": "MessageActionPinMessage"}, 4, JOINED + 240))  # not a membership fact
        counts = project_join_service_messages(st, GROUP_ID, "stranger")
        # 4 member_of (5, 6, 7, 8) + 2 added_by (5, 6 <- 4) + 1 invited_by (7 <- 4)
        assert counts == {"joins": 4, "leaves": 1, "edges": 7}
        rows = {
            r["uri"]: (r["status"], r["join_date"], r["inviter_id"])
            for r in st.conn.execute("select uri, status, join_date, inviter_id from participants")
        }
        assert rows["tg:user:5"] == ("member", "2025-01-01T00:00:00+00:00", None)
        assert rows["tg:user:6"] == ("left", "2025-01-01T00:00:00+00:00", None)  # joined, then left
        assert rows["tg:user:7"] == ("member", "2025-01-01T00:01:00+00:00", 4)
        assert rows["tg:user:8"] == ("member", "2025-01-01T00:02:00+00:00", None)
        edges = {
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }
        assert ("tg:user:5", "added_by", "tg:user:4") in edges
        assert ("tg:user:6", "added_by", "tg:user:4") in edges
        assert ("tg:user:7", "invited_by", "tg:user:4") in edges
        assert ("tg:user:5", "member_of", "tg:channel:77") in edges
        assert ("tg:user:8", "member_of", "tg:channel:77") in edges
        # the membership observation is stamped with the FACT's time (the message
        # date), so a later roster observation correctly wins over it (plan D5)
        snap = st.conn.execute(
            "select observed_at from participant_snapshots where uri='tg:user:7'"
        ).fetchone()
        assert snap["observed_at"] == "2025-01-01T00:01:00+00:00"

        # idempotent: a re-run adds nothing
        again = project_join_service_messages(st, GROUP_ID, "stranger")
        assert again["edges"] == 0
        assert st.conn.execute("select count(*) from participant_snapshots").fetchone()[0] == 5
        assert st.conn.execute("select count(*) from edges").fetchone()[0] == 7
```

`tests/test_store_message_peers.py`:

```python
"""#11: forward origins and mention-name users become `peers` rows with provenance."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.messages import upsert_message
from paperboy.store.peers import upsert_peer

CHANNEL_ID = 5
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"


def _seed(st, m):
    raw_id = st.add_raw("Message", m, "stranger", {"channel_id": CHANNEL_ID})
    upsert_message(st, CHANNEL_ID, m, raw_id, T1, "stranger")


def test_forward_origins_and_mentioned_users_get_provenance_rows(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, {"_": "Message", "id": 1, "message": "fwd", "date": 1767322445,
                   "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerChannel", "channel_id": 1003099698}}})
        _seed(st, {"_": "Message", "id": 2, "message": "hi @x", "date": 1767322445,
                   "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}},
                   "entities": [{"_": "MessageEntityMentionName", "offset": 3, "length": 2, "user_id": 43},
                                {"_": "MessageEntityBold", "offset": 0, "length": 2}]})
        _seed(st, {"_": "Message", "id": 3, "message": "plain", "date": 1767322445})
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 3
        rows = {
            r["uri"]: (r["kind"], r["is_min"], r["seen_in_chat"], r["seen_in_msg"], r["first_seen"])
            for r in st.conn.execute("select * from peers")
        }
        assert rows["tg:channel:1003099698"] == ("channel", 1, CHANNEL_ID, 1, T1)
        assert rows["tg:user:42"] == ("user", 1, CHANNEL_ID, 2, T1)
        assert rows["tg:user:43"] == ("user", 1, CHANNEL_ID, 2, T1)


def test_backfill_is_idempotent_and_never_clobbers_a_full_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        full = {"_": "User", "id": 42, "access_hash": 7, "username": "real", "first_name": "R"}
        rid = st.add_raw("User", full, "stranger", None)
        upsert_peer(st, full, rid, T2, seen_in_chat=None, seen_in_msg=None)
        _seed(st, {"_": "Message", "id": 2, "message": "x", "date": 1767322445,
                   "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}}})
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 1
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 1
        row = st.conn.execute("select username, is_min, seen_in_msg from peers where uri='tg:user:42'").fetchone()
        assert (row["username"], row["is_min"]) == ("real", 0)
        assert row["seen_in_msg"] is None  # the older min reference does not move newer provenance
        assert st.conn.execute("select count(*) from peers").fetchone()[0] == 1
```

`tests/test_store_reactions.py`:

```python
"""Reaction vectors on a group: the zero-RPC `recent_reactions` sample + bookkeeping for the bounded RPC."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.reactions import (
    backfill_recent_reactions,
    fetched_reaction_lists,
    reacted_message_ids,
)

GROUP_ID = 77


def _raw_message(st, msg_id: int, reactions: dict | None):
    payload = {"_": "Message", "id": msg_id, "message": "m", "date": 1767322445,
               "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if reactions is not None:
        payload["reactions"] = reactions
    return st.add_raw("Message", payload, "stranger", {"channel_id": GROUP_ID},
                      observed_at="2026-01-01T00:00:00+00:00")


def test_recent_reactions_project_min_peers_and_reacted_to_edges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _raw_message(st, 10, {
            "_": "MessageReactions",
            "results": [{"_": "ReactionCount", "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}, "count": 2}],
            "recent_reactions": [
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 5}, "date": 1767322500,
                 "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}},
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 6}, "date": 1767322501,
                 "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}},
            ],
        })
        _raw_message(st, 11, None)
        assert backfill_recent_reactions(st, GROUP_ID, "stranger") == 2
        assert backfill_recent_reactions(st, GROUP_ID, "stranger") == 2  # idempotent
        peer = st.conn.execute("select is_min, seen_in_chat, seen_in_msg from peers where uri='tg:user:5'").fetchone()
        assert (peer["is_min"], peer["seen_in_chat"], peer["seen_in_msg"]) == (1, GROUP_ID, 10)
        edges = st.conn.execute(
            "select subject_uri, predicate, object_uri, evidence_json from edges order by subject_uri"
        ).fetchall()
        assert [(e["subject_uri"], e["predicate"], e["object_uri"]) for e in edges] == [
            ("tg:user:5", "reacted_to", "tg:msg:77/10"), ("tg:user:6", "reacted_to", "tg:msg:77/10"),
        ]
        assert '"source": "recent_reactions"' in edges[0]["evidence_json"]
        assert '"emoticon": "👍"' in edges[0]["evidence_json"]


def test_reacted_message_ids_newest_first_and_fetched_set(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _raw_message(st, 10, {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}]})
        _raw_message(st, 12, {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 3, "reaction": {}}]})
        _raw_message(st, 11, {"_": "MessageReactions", "results": []})
        _raw_message(st, 13, None)
        assert reacted_message_ids(st, GROUP_ID) == [12, 10]
        assert fetched_reaction_lists(st, GROUP_ID) == set()
        st.add_raw("messages.MessageReactionsList", {"_": "MessageReactionsList", "count": 3, "reactions": []},
                   "stranger", {"channel_id": GROUP_ID, "msg_id": 12, "offset": ""})
        assert fetched_reaction_lists(st, GROUP_ID) == {12}
```

Append to `tests/test_store_messages.py`:

```python
def test_metrics_row_is_written_when_only_reactions_are_present(tmp_path):
    # Group messages carry no `views`/`forwards`; before this fix their
    # reactions never reached `message_metrics` at all (found building the
    # reaction-candidate query in the person layer, no-shed).
    with Store.open(tmp_path / "p.sqlite") as st:
        m = {"_": "Message", "id": 1, "message": "m", "date": 1767322445,
             "reactions": {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2}]}}
        rid = st.add_raw("Message", m, "stranger", {"channel_id": 77})
        upsert_message(st, 77, m, rid, "2026-01-01T00:00:00+00:00", "stranger")
        row = st.conn.execute("select views, reactions_json from message_metrics").fetchone()
        assert row is not None and row["views"] is None and '"count": 2' in row["reactions_json"]
```

- [ ] **Step 2: Run to verify failure**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_store_participants.py tests/test_store_message_peers.py tests/test_store_reactions.py tests/test_store_messages.py -q --basetemp=/Volumes/Storage/tmp/pytest`
Expected: `ModuleNotFoundError` ×3; the metrics test fails with `row is None`.

- [ ] **Step 3: `messages.py` fix** — in `upsert_message`, change the metrics guard to

```python
    if views is not None or forwards is not None or replies is not None or reactions:
```

- [ ] **Step 4: Implement `src/paperboy/store/participants.py`**

```python
"""Roster projection: current membership facts (`participants`) + append-only
observations (`participant_snapshots`), the membership edges, and the
zero-RPC join/leave service-message vector (spec §6.2, §8).

`join_date` is stored only where the constructor's `date` MEANS "joined"
(`channelParticipant`, `Self`, `Admin` — research §1.6); `Creator` carries no
date and `Banned.date` is the BAN date, which stays in raw. Membership facts
are newest-observation-wins keyed `(group_id, uri)`, except that a known
`join_date` is never blanked by a later observation that lacks one (a member
who is later banned still joined when they joined). `inviter_id` is populated
only for self (spec §4) — stored when present, never inferred.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from paperboy.ids import channel_uri, iso_or_none, peer_ref_uri, user_uri
from paperboy.store.db import Store
from paperboy.store.edges import add_edge_once
from paperboy.store.sync import is_self

PARTICIPANT_STATUSES = ("member", "admin", "creator", "banned", "left")
MEMBER_STATUSES = ("member", "admin", "creator")
_ADMIN_STATUSES = ("admin", "creator")
MEMBER_OF = "member_of"
ADMIN_OF = "admin_of"
INVITED_BY = "invited_by"
ADDED_BY = "added_by"


@dataclass(frozen=True)
class ParticipantFacts:
    uri: str
    status: str
    join_date: str | None
    rank: str | None
    subscription_until_date: str | None
    inviter_id: int | None


def participant_row(participant: dict) -> ParticipantFacts | None:
    """Parse one `channelParticipant*` dict (Telethon `to_dict()`, PascalCase
    `_`, matched case-insensitively) into the facts we store; `None` for an
    unknown constructor — never guessed at."""
    kind = (participant.get("_") or "").lower()
    rank = participant.get("rank") or None
    sub = iso_or_none(participant.get("subscription_until_date"))
    if kind == "channelparticipant":
        return ParticipantFacts(
            user_uri(participant["user_id"]), "member", iso_or_none(participant.get("date")), rank, sub, None
        )
    if kind == "channelparticipantself":
        return ParticipantFacts(
            user_uri(participant["user_id"]), "member", iso_or_none(participant.get("date")), rank, sub,
            participant.get("inviter_id"),
        )
    if kind == "channelparticipantcreator":
        return ParticipantFacts(user_uri(participant["user_id"]), "creator", None, rank, None, None)
    if kind == "channelparticipantadmin":
        # `inviter_id` shares flag bit 1 with `self` — only ever set for us.
        inviter = participant.get("inviter_id") if participant.get("is_self") else None
        return ParticipantFacts(
            user_uri(participant["user_id"]), "admin", iso_or_none(participant.get("date")), rank, None, inviter
        )
    if kind in ("channelparticipantbanned", "channelparticipantleft"):
        uri = peer_ref_uri(participant.get("peer"))
        if uri is None:
            return None
        status = "banned" if kind == "channelparticipantbanned" else "left"
        return ParticipantFacts(uri, status, None, rank if status == "banned" else None, None, None)
    return None


def write_participant(
    store: Store, group_id: int, facts: ParticipantFacts, source_raw_id: int, observed_at: str
) -> str | None:
    if is_self(store, facts.uri):
        return None
    store.conn.execute(
        """
        INSERT INTO participants (
            group_id, uri, status, join_date, rank, subscription_until_date, inviter_id,
            source_raw_id, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, uri) DO UPDATE SET
            -- newest-observation-wins (ADR-0005 §6, recency only: no `min`
            -- concept here); a known join_date survives an observation
            -- without one (Creator, Banned, Left, USER_NOT_PARTICIPANT).
            status = CASE WHEN excluded.last_seen >= participants.last_seen
                          THEN excluded.status ELSE participants.status END,
            join_date = CASE WHEN excluded.last_seen >= participants.last_seen
                             THEN COALESCE(excluded.join_date, participants.join_date)
                             ELSE COALESCE(participants.join_date, excluded.join_date) END,
            rank = CASE WHEN excluded.last_seen >= participants.last_seen
                        THEN excluded.rank ELSE participants.rank END,
            subscription_until_date = CASE WHEN excluded.last_seen >= participants.last_seen
                                           THEN excluded.subscription_until_date
                                           ELSE participants.subscription_until_date END,
            inviter_id = CASE WHEN excluded.last_seen >= participants.last_seen
                              THEN COALESCE(excluded.inviter_id, participants.inviter_id)
                              ELSE COALESCE(participants.inviter_id, excluded.inviter_id) END,
            source_raw_id = CASE WHEN excluded.last_seen >= participants.last_seen
                                 THEN excluded.source_raw_id ELSE participants.source_raw_id END,
            first_seen = MIN(participants.first_seen, excluded.first_seen),
            last_seen = MAX(participants.last_seen, excluded.last_seen)
        """,
        (
            group_id, facts.uri, facts.status, facts.join_date, facts.rank,
            facts.subscription_until_date, facts.inviter_id, source_raw_id, observed_at, observed_at,
        ),
    )
    return facts.uri


def upsert_participant(
    store: Store, group_id: int, participant: dict, source_raw_id: int, observed_at: str
) -> ParticipantFacts | None:
    facts = participant_row(participant)
    if facts is None:
        return None
    return facts if write_participant(store, group_id, facts, source_raw_id, observed_at) else None


def add_participant_snapshot(
    store: Store,
    group_id: int,
    facts: ParticipantFacts,
    observed_at: str,
    source_raw_id: int | None,
    *,
    once: bool = False,
) -> bool:
    """Append one membership observation. `once=True` is for producers that
    re-scan stored rows every run (service messages): the same observation
    (same source record, same stamp) is never appended twice."""
    if once:
        dup = store.conn.execute(
            "SELECT 1 FROM participant_snapshots WHERE group_id=? AND uri=? AND observed_at=? "
            "AND source_raw_id IS ? LIMIT 1",
            (group_id, facts.uri, observed_at, source_raw_id),
        ).fetchone()
        if dup is not None:
            return False
    store.conn.execute(
        "INSERT INTO participant_snapshots (group_id, observed_at, uri, status, join_date, rank, "
        "subscription_until_date, source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id, observed_at, facts.uri, facts.status, facts.join_date, facts.rank,
            facts.subscription_until_date, source_raw_id,
        ),
    )
    return True


def add_roster_snapshot(
    store: Store,
    group_id: int,
    observed_at: str,
    *,
    enumerated: int,
    true_count: int | None,
    reason: str | None,
    source_raw_id: int | None,
) -> None:
    """The roster-level accounting row (spec §6.3): `enumerated / true_count`
    for this run, and `reason` when the roster was walled. A shortfall is
    never presented as completeness — it is stored as exactly that."""
    store.conn.execute(
        "INSERT INTO participant_snapshots (group_id, observed_at, uri, enumerated, true_count, "
        "reason, source_raw_id) VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (group_id, observed_at, enumerated, true_count, reason, source_raw_id),
    )


def membership_edges(
    store: Store,
    group_id: int,
    facts: ParticipantFacts,
    observed_at: str,
    tier: str,
    source_raw_id: int | None,
    evidence: dict,
) -> int:
    if facts.status not in MEMBER_STATUSES:
        return 0
    group = channel_uri(group_id)
    written = 0
    if add_edge_once(store, facts.uri, MEMBER_OF, group, observed_at, tier, source_raw_id, evidence):
        written += 1
    if facts.status in _ADMIN_STATUSES and add_edge_once(
        store, facts.uri, ADMIN_OF, group, observed_at, tier, source_raw_id, evidence
    ):
        written += 1
    return written


def project_join_service_messages(store: Store, group_id: int, tier: str) -> dict[str, int]:
    """Membership + invite facts from join/leave service messages already in
    the captured history — zero RPC (spec §8). Partial by nature (silent
    joins leave no trace; channel subscriptions never emit one), so every
    edge is evidenced `source: service_message` and the roster remains the
    authority: each fact is stamped with the MESSAGE date (when it was true),
    so a later roster observation of the same member correctly wins.
    Idempotent — re-scans every run."""
    counts = {"joins": 0, "leaves": 0, "edges": 0}
    rows = store.conn.execute(
        "SELECT uri, msg_id, from_uri, date, action_json, source_raw_id, first_seen FROM messages "
        "WHERE channel_id=? AND is_service=1 AND action_json IS NOT NULL ORDER BY msg_id",
        (group_id,),
    ).fetchall()
    for row in rows:
        action = json.loads(row["action_json"])
        kind = (action.get("_") or "").lower()
        observed_at = row["date"] or row["first_seen"]
        evidence = {"source": "service_message", "msg_uri": row["uri"]}
        joined: list[tuple[str, int | None]] = []  # (uri, inviter_id)
        if kind == "messageactionchatadduser":
            for user_id in action.get("users") or []:
                joined.append((user_uri(user_id), None))
        elif kind == "messageactionchatjoinedbylink" and row["from_uri"]:
            joined.append((row["from_uri"], action.get("inviter_id")))
        elif kind == "messageactionchatjoinedbyrequest" and row["from_uri"]:
            joined.append((row["from_uri"], None))
        elif kind == "messageactionchatdeleteuser" and action.get("user_id") is not None:
            facts = ParticipantFacts(user_uri(action["user_id"]), "left", None, None, None, None)
            if write_participant(store, group_id, facts, row["source_raw_id"], observed_at):
                counts["leaves"] += 1
                add_participant_snapshot(store, group_id, facts, observed_at, row["source_raw_id"], once=True)
            continue
        else:
            continue
        for uri, inviter_id in joined:
            facts = ParticipantFacts(uri, "member", observed_at, None, None, inviter_id)
            if write_participant(store, group_id, facts, row["source_raw_id"], observed_at) is None:
                continue
            counts["joins"] += 1
            add_participant_snapshot(store, group_id, facts, observed_at, row["source_raw_id"], once=True)
            counts["edges"] += membership_edges(
                store, group_id, facts, observed_at, tier, row["source_raw_id"], evidence
            )
            if inviter_id is not None and add_edge_once(
                store, uri, INVITED_BY, user_uri(inviter_id), observed_at, tier, row["source_raw_id"], evidence
            ):
                counts["edges"] += 1
            elif (
                kind == "messageactionchatadduser" and row["from_uri"] and row["from_uri"] != uri
                and add_edge_once(
                    store, uri, ADDED_BY, row["from_uri"], observed_at, tier, row["source_raw_id"], evidence
                )
            ):
                counts["edges"] += 1
    return counts
```

- [ ] **Step 5: Implement `src/paperboy/store/message_peers.py`**

```python
"""Issue #11: every peer a stored message REFERENCES — the forward origin in
`fwd_from.from_id` and users named by `MessageEntityMentionName` — gets a
`peers` row with the `(chat, msg_id)` provenance `inputPeerFromMessage`
needs (research §8.7 explicitly sanctions both as FromMessage contexts). Until
now those existed only as edge endpoints, invisible to the very enrichment
sweep that should reach them. Zero RPC: walks `messages` only, stamps each
stub with the message's own `first_seen` (a derived row, reproject plan D3)."""

from __future__ import annotations

import json

from paperboy.ids import peer_stub
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer


def backfill_message_referenced_peers(store: Store, channel_id: int) -> int:
    """Returns the number of DISTINCT peers upserted. Idempotent: the min
    stub's stamp is the message's `first_seen`, so a re-run re-asserts the
    same provenance and a fuller, newer row is never touched (`upsert_peer`'s
    full<-min cell)."""
    rows = store.conn.execute(
        "SELECT msg_id, fwd_json, entities_json, source_raw_id, first_seen FROM messages "
        "WHERE channel_id=? AND (fwd_json IS NOT NULL OR entities_json IS NOT NULL)",
        (channel_id,),
    ).fetchall()
    seen: set[str] = set()
    for row in rows:
        stubs: list[dict] = []
        if row["fwd_json"]:
            stub = peer_stub(json.loads(row["fwd_json"]).get("from_id"))
            if stub is not None:
                stubs.append(stub)
        if row["entities_json"]:
            for entity in json.loads(row["entities_json"]) or []:
                if (entity.get("_") or "").lower() == "messageentitymentionname":
                    user_id = entity.get("user_id")
                    if user_id is not None:
                        stubs.append({"_": "User", "id": user_id, "min": True})
        for stub in stubs:
            uri = upsert_peer(
                store, stub, row["source_raw_id"], row["first_seen"],
                seen_in_chat=channel_id, seen_in_msg=row["msg_id"],
            )
            if uri is not None:
                seen.add(uri)
    return len(seen)
```

- [ ] **Step 6: Implement `src/paperboy/store/reactions.py`**

```python
"""Reaction vectors on a GROUP (spec §6.2; plan D8): the zero-RPC
`MessageReactions.recent_reactions` sample Telegram inlines in every reacted
message (a handful of `{peer_id, date, reaction}` per message — projected
from stored raw payloads exactly like `recent_repliers`), plus the
bookkeeping the `participants` collector needs to spend its bounded
`messages.getMessageReactionsList` budget without re-fetching a message it
already listed. Reactors on a BROADCAST are `BROADCAST_FORBIDDEN` and never
requested (guardrail)."""

from __future__ import annotations

import json

from paperboy.ids import iso_or_none, msg_uri, peer_ref_uri, peer_stub
from paperboy.store.db import Store
from paperboy.store.edges import add_edge_once
from paperboy.store.peers import upsert_peer

REACTED_TO = "reacted_to"


def _raw_messages(store: Store, channel_id: int):
    return store.conn.execute(
        "SELECT id, observed_at, payload_json FROM raw_records "
        "WHERE lower(kind) = 'message' AND json_extract(context_json, '$.channel_id') = ? "
        "ORDER BY id",
        (channel_id,),
    ).fetchall()


def backfill_recent_reactions(store: Store, channel_id: int, tier: str) -> int:
    """Project every `recent_reactions` reactor into `peers` (min, with the
    message as provenance) and a `reacted_to` edge. Returns DISTINCT
    reactors. Idempotent (`add_edge_once`; the stub's stamp is the raw
    record's `observed_at`)."""
    seen: set[str] = set()
    for row in _raw_messages(store, channel_id):
        payload = json.loads(row["payload_json"])
        sample = (payload.get("reactions") or {}).get("recent_reactions") or []
        msg_id = payload.get("id")
        if not sample or msg_id is None:
            continue
        for reaction in sample:
            stub = peer_stub(reaction.get("peer_id"))
            uri = peer_ref_uri(reaction.get("peer_id"))
            if stub is None or uri is None:
                continue
            if upsert_peer(
                store, stub, row["id"], row["observed_at"], seen_in_chat=channel_id, seen_in_msg=msg_id
            ) is None:
                continue  # the collecting account reacting (#12)
            add_edge_once(
                store, uri, REACTED_TO, msg_uri(channel_id, msg_id), row["observed_at"], tier, row["id"],
                {"source": "recent_reactions", "reaction": reaction.get("reaction"),
                 "date": iso_or_none(reaction.get("date"))},
            )
            seen.add(uri)
    return len(seen)


def reacted_message_ids(store: Store, channel_id: int) -> list[int]:
    """Message ids that carry at least one reaction in their stored raw
    payload (raw, not `message_metrics`: archives captured before the
    reactions-only metrics fix have them only in raw), newest first."""
    ids: set[int] = set()
    for row in _raw_messages(store, channel_id):
        payload = json.loads(row["payload_json"])
        results = (payload.get("reactions") or {}).get("results") or []
        if any((r.get("count") or 0) > 0 for r in results) and payload.get("id") is not None:
            ids.add(payload["id"])
    return sorted(ids, reverse=True)


def fetched_reaction_lists(store: Store, channel_id: int) -> set[int]:
    """Message ids whose full reactor list was already fetched (any run) —
    derived from the raw log so repeated runs converge instead of re-spending."""
    rows = store.conn.execute(
        "SELECT DISTINCT json_extract(context_json, '$.msg_id') AS m FROM raw_records "
        "WHERE lower(kind) LIKE '%messagereactionslist' "
        "AND json_extract(context_json, '$.channel_id') = ?",
        (channel_id,),
    ).fetchall()
    return {int(r["m"]) for r in rows if r["m"] is not None}
```

- [ ] **Step 7: Run the four test files, then the full suite + lint/type**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest && uv run ruff check && uv run pyright`
Expected: PASS. (The parity golden is unaffected: its fixtures carry no reactions, so the `messages.py` guard change writes nothing new.)

- [ ] **Step 8: Commit**

```bash
git add src/paperboy/store/participants.py src/paperboy/store/message_peers.py src/paperboy/store/reactions.py \
  src/paperboy/store/messages.py tests/test_store_participants.py tests/test_store_message_peers.py \
  tests/test_store_reactions.py tests/test_store_messages.py
git commit -m "feat(store): participants writers, service-message/#11/reaction zero-RPC producers (#41)"
```

---
### Task 4: Gateway seam — 7 methods, `_input_user` / `_input_peer_user`, `input_user_ref`, fixtures, error classes

**Files:**
- Modify: `src/paperboy/gateway.py` (Protocol, `TelethonGateway`, `FakeGateway`), `src/paperboy/errors.py`, `src/paperboy/store/peers.py` (`input_user_ref`), `src/paperboy/doctor.py` (`session_age_days` made public)
- Test: `tests/test_input_user.py` (new), `tests/test_gateway_fake.py`, `tests/test_budget.py`

**Interfaces:**
- Consumes: Task 1–2 tables (`users`, `peers`); `ids.parse_uri`, `ids.channel_uri`.
- Produces (the seam every collector and the replay gateway implement):

```python
# Gateway Protocol additions (dict-in / dict-out; `ref` = an input-user ref dict, see below)
async def get_participants(self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0) -> dict
async def get_participant(self, input_channel: dict, participant: dict) -> dict | None   # None == USER_NOT_PARTICIPANT
async def get_users(self, refs: list[dict]) -> list[dict]
async def get_full_user(self, ref: dict) -> dict
async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict
async def download_user_photo(self, photo: dict) -> bytes | None
async def get_message_reactions_list(self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int) -> dict
```

  - **Input-user ref dict** (spec §5): case 1 `{"user_id": int, "access_hash": int}`; case 2 `{"user_id": int, "from_msg": {"channel_id": int, "access_hash": int, "msg_id": int}}`. `gateway._input_user(ref) -> InputUser | InputUserFromMessage`; `gateway._input_peer_user(ref) -> InputPeerUser | InputPeerUserFromMessage`.
  - **Filter dict**: `{"_": "channelParticipantsRecent"}` / `"channelParticipantsAdmins"` / `"channelParticipantsBots"` / `{"_": "channelParticipantsMentions", "q": str | None, "top_msg_id": int | None}` (matched case-insensitively). Module constants `FILTER_RECENT`, `FILTER_ADMINS`, `FILTER_BOTS`.
  - `store.peers.input_user_ref(store, uri: str) -> dict | None` — case 3 (unresolvable) is `None`.
  - `doctor.session_age_days(authorizations: dict) -> float | None` (was `_session_age_days`; the old name stays as an alias).
  - `FakeGateway` fixture keys: `full_channel_by_id: {channel_id: dict | BaseException}`, `participants: {channel_id: {filter_name: list[dict | BaseException] | BaseException}}` (pages consumed in call order per `(channel_id, filter)`; past the end → an empty page), `participant: {channel_id: {user_id: dict | None | BaseException}}` (missing → `None`), `users: {user_id: dict | BaseException}` (missing → `UserEmpty`; any exception value fails the whole batch), `full_user: {user_id: dict | BaseException}` (missing → `SkipAndRecord`), `user_photos: {user_id: dict | BaseException}` (missing → empty `Photos`), `avatar: {photo_id: bytes | None | BaseException}` (missing → `None`), `reactions: {channel_id: {msg_id: dict | list[dict] | BaseException}}` (missing → empty list dict; a list is consumed per `offset` call order), `privacy` (missing key → `SkipAndRecord`, was `KeyError`). Introspection lists: `participants_calls: list[tuple[int, str, int]]`, `participant_calls: list[tuple[int, int]]`, `users_calls: list[list[int]]`, `full_user_calls: list[int]`, `user_photos_calls: list[int]`, `avatar_calls: list[int]`, `reactions_calls: list[tuple[int, int, str | None]]`; `calls` gains the seven method names.
  - `errors._skip_error_classes()` gains `UserIdInvalidError` only; `ChannelInvalidError` is caught locally in `TelethonGateway.get_users`/`get_full_user` → `SkipAndRecord` (see D4).

- [ ] **Step 1: Write the failing tests**

`tests/test_input_user.py`:

```python
"""Spec §5's load-bearing plumbing: the store-side ref builder (three cases)
and the gateway-side TL builders — verified against the installed Telethon."""

from __future__ import annotations

import base64

from telethon.tl.types import (
    ChannelParticipantsAdmins,
    ChannelParticipantsBots,
    ChannelParticipantsMentions,
    ChannelParticipantsRecent,
    InputPeerChannel,
    InputPeerUser,
    InputPeerUserFromMessage,
    InputUser,
    InputUserFromMessage,
)

from paperboy.gateway import (
    FILTER_ADMINS,
    FILTER_BOTS,
    FILTER_RECENT,
    _file_reference,
    _input_peer_user,
    _input_user,
    _largest_photo_size,
    _participants_filter,
)
from paperboy.store.db import Store
from paperboy.store.peers import input_user_ref, upsert_peer
from paperboy.store.users import upsert_user

T = "2026-01-01T00:00:00+00:00"
GROUP_ID = 77


def test_case_1_full_access_hash_builds_input_user():
    ref = {"user_id": 5, "access_hash": 99}
    built = _input_user(ref)
    assert isinstance(built, InputUser) and (built.user_id, built.access_hash) == (5, 99)
    peer = _input_peer_user(ref)
    assert isinstance(peer, InputPeerUser) and peer.access_hash == 99


def test_case_2_min_provenance_builds_from_message():
    ref = {"user_id": 5, "from_msg": {"channel_id": GROUP_ID, "access_hash": 4242, "msg_id": 200}}
    built = _input_user(ref)
    assert isinstance(built, InputUserFromMessage)
    assert isinstance(built.peer, InputPeerChannel)
    assert (built.peer.channel_id, built.peer.access_hash, built.msg_id, built.user_id) == (
        GROUP_ID, 4242, 200, 5,
    )
    peer = _input_peer_user(ref)
    assert isinstance(peer, InputPeerUserFromMessage) and peer.msg_id == 200


def test_store_ref_prefers_a_full_users_row_then_a_full_peer_then_provenance(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        group = {"_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "G", "megagroup": True}
        r = st.add_raw("Channel", group, "stranger", None)
        upsert_peer(st, group, r, T, seen_in_chat=None, seen_in_msg=None)

        # case 2: a min stub with provenance into a channel whose hash we know
        stub = {"_": "User", "id": 5, "min": True}
        r2 = st.add_raw("User", stub, "stranger", None)
        upsert_peer(st, stub, r2, T, seen_in_chat=GROUP_ID, seen_in_msg=200)
        assert input_user_ref(st, "tg:user:5") == {
            "user_id": 5, "from_msg": {"channel_id": GROUP_ID, "access_hash": 4242, "msg_id": 200},
        }

        # case 1 via peers: a full peer object
        full = {"_": "User", "id": 6, "access_hash": 66, "first_name": "F"}
        r3 = st.add_raw("User", full, "stranger", None)
        upsert_peer(st, full, r3, T, seen_in_chat=None, seen_in_msg=None)
        assert input_user_ref(st, "tg:user:6") == {"user_id": 6, "access_hash": 66}

        # case 1 via users: a triaged user outranks its own min peer stub
        triaged = {"_": "User", "id": 5, "access_hash": 55, "first_name": "T"}
        r4 = st.add_raw("User", triaged, "stranger", None)
        upsert_user(st, triaged, r4, T, "stranger")
        assert input_user_ref(st, "tg:user:5") == {"user_id": 5, "access_hash": 55}


def test_store_ref_case_3_unresolvable(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        stub = {"_": "User", "id": 7, "min": True}
        r = st.add_raw("User", stub, "stranger", None)
        # provenance into a channel we have NO hash for
        upsert_peer(st, stub, r, T, seen_in_chat=999, seen_in_msg=1)
        assert input_user_ref(st, "tg:user:7") is None
        # no provenance at all
        stub2 = {"_": "User", "id": 8, "min": True, "access_hash": 123}  # a min hash is not usable
        r2 = st.add_raw("User", stub2, "stranger", None)
        upsert_peer(st, stub2, r2, T, seen_in_chat=None, seen_in_msg=None)
        assert input_user_ref(st, "tg:user:8") is None
        assert input_user_ref(st, "tg:user:404") is None


def test_participants_filter_mapping():
    assert isinstance(_participants_filter(FILTER_RECENT), ChannelParticipantsRecent)
    assert isinstance(_participants_filter(FILTER_ADMINS), ChannelParticipantsAdmins)
    assert isinstance(_participants_filter(FILTER_BOTS), ChannelParticipantsBots)
    mentions = _participants_filter({"_": "channelParticipantsMentions", "top_msg_id": 3, "q": None})
    assert isinstance(mentions, ChannelParticipantsMentions) and mentions.top_msg_id == 3


def test_photo_download_helpers():
    sizes = [
        {"_": "PhotoStrippedSize", "type": "i", "bytes": b""},
        {"_": "PhotoSize", "type": "m", "w": 320, "h": 320, "size": 1},
        {"_": "PhotoSizeProgressive", "type": "x", "w": 800, "h": 800, "sizes": [1, 2]},
    ]
    assert _largest_photo_size(sizes) == "x"
    assert _largest_photo_size([]) == "x"
    assert _file_reference({"file_reference": b"\x01\x02"}) == b"\x01\x02"
    # a replayed/stored photo dict carries base64 text (store.db.dumps)
    assert _file_reference({"file_reference": base64.b64encode(b"\x01\x02").decode()}) == b"\x01\x02"
```

Append to `tests/test_gateway_fake.py` (merge `SkipAndRecord` from `paperboy.budget` and `FILTER_RECENT` from `paperboy.gateway` into the file's existing top-of-file imports; `pytest`/`FakeGateway` are already imported there — no second import block, no redefinition):

```python
@pytest.mark.asyncio
async def test_fake_person_layer_methods_record_calls_and_default_benignly():
    gw = FakeGateway({})
    ic = {"channel_id": 77, "access_hash": 1}
    page = await gw.get_participants(ic, FILTER_RECENT, 0, 200)
    assert page == {"_": "ChannelParticipants", "count": 0, "participants": [], "chats": [], "users": []}
    assert await gw.get_participant(ic, {"user_id": 5, "access_hash": 1}) is None
    users = await gw.get_users([{"user_id": 5, "access_hash": 1}, {"user_id": 6, "access_hash": 1}])
    assert users == [{"_": "UserEmpty", "id": 5}, {"_": "UserEmpty", "id": 6}]
    with pytest.raises(SkipAndRecord):
        await gw.get_full_user({"user_id": 5, "access_hash": 1})
    photos = await gw.get_user_photos({"user_id": 5, "access_hash": 1}, offset=0, max_id=0, limit=100)
    assert photos == {"_": "Photos", "photos": [], "users": []}
    assert await gw.download_user_photo({"id": 9}) is None
    reactions = await gw.get_message_reactions_list(ic, 10, offset=None, limit=100)
    assert reactions["reactions"] == [] and reactions["next_offset"] is None
    with pytest.raises(SkipAndRecord):
        await gw.get_privacy("phone")
    assert gw.calls == [
        "get_participants", "get_participant", "get_users", "get_full_user", "get_user_photos",
        "download_user_photo", "get_message_reactions_list", "get_privacy",
    ]
    assert gw.participants_calls == [(77, "channelParticipantsRecent", 0)]
    assert gw.participant_calls == [(77, 5)]
    assert gw.users_calls == [[5, 6]]
    assert gw.full_user_calls == [5] and gw.user_photos_calls == [5] and gw.avatar_calls == [9]
    assert gw.reactions_calls == [(77, 10, None)]


@pytest.mark.asyncio
async def test_fake_participants_pages_are_consumed_in_order_then_empty():
    p1 = {"_": "ChannelParticipants", "count": 3, "participants": [{"_": "ChannelParticipant", "user_id": 1}],
          "chats": [], "users": []}
    p2 = {"_": "ChannelParticipants", "count": 3, "participants": [{"_": "ChannelParticipant", "user_id": 2}],
          "chats": [], "users": []}
    gw = FakeGateway({"participants": {77: {"channelParticipantsRecent": [p1, p2]}}})
    ic = {"channel_id": 77, "access_hash": 1}
    assert await gw.get_participants(ic, FILTER_RECENT, 0, 200) is p1
    assert await gw.get_participants(ic, FILTER_RECENT, 1, 200) is p2
    assert (await gw.get_participants(ic, FILTER_RECENT, 2, 200))["participants"] == []
    # a different channel has its own page sequence
    assert (await gw.get_participants({"channel_id": 78, "access_hash": 1}, FILTER_RECENT, 0, 200))["count"] == 0


@pytest.mark.asyncio
async def test_fake_exception_fixtures_raise_for_walls_and_batches():
    wall = SkipAndRecord("CHAT_ADMIN_REQUIRED")
    gw = FakeGateway({
        "participants": {77: {"channelParticipantsRecent": wall}},
        "participant": {77: {5: wall, 6: {"_": "ChannelParticipant", "participant": {}, "users": []}}},
        "users": {5: {"_": "User", "id": 5}, 6: SkipAndRecord("MSG_ID_INVALID")},
        "full_channel_by_id": {77: {"full_chat": {"id": 77}}},
        "full_channel": {"full_chat": {"id": 5}},
    })
    ic = {"channel_id": 77, "access_hash": 1}
    with pytest.raises(SkipAndRecord):
        await gw.get_participants(ic, FILTER_RECENT, 0, 200)
    with pytest.raises(SkipAndRecord):
        await gw.get_participant(ic, {"user_id": 5, "access_hash": 1})
    assert (await gw.get_participant(ic, {"user_id": 6, "access_hash": 1}))["_"] == "ChannelParticipant"
    with pytest.raises(SkipAndRecord):  # one bad ref fails the whole vector, like the real RPC
        await gw.get_users([{"user_id": 5, "access_hash": 1}, {"user_id": 6, "access_hash": 1}])
    assert (await gw.get_users([{"user_id": 5, "access_hash": 1}]))[0]["id"] == 5
    assert (await gw.get_full_channel(ic))["full_chat"]["id"] == 77
    assert (await gw.get_full_channel({"channel_id": 5, "access_hash": 1}))["full_chat"]["id"] == 5
```

Append to `tests/test_budget.py`:

```python
def test_user_id_invalid_and_channel_invalid_classify_as_skip():
    # `users.getFullUser` on a stale `inputUserFromMessage` provenance answers
    # USER_ID_INVALID / CHANNEL_INVALID (research Part 2 §1): one user's
    # enrichment is skipped, the sweep continues — never a raw crash.
    from telethon.errors import ChannelInvalidError, UserIdInvalidError

    for exc in (UserIdInvalidError, ChannelInvalidError):
        assert classify(exc(None)) == Disposition.SKIP
```

- [ ] **Step 2: Run to verify failure**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_input_user.py tests/test_gateway_fake.py tests/test_budget.py -q --basetemp=/Volumes/Storage/tmp/pytest`
Expected: `ImportError` (`FILTER_RECENT`, `_input_user`, `input_user_ref`, …); the classify test fails because `classify` re-raises `UserIdInvalidError`.

- [ ] **Step 3: `errors.py`** — add `ChannelInvalidError, UserIdInvalidError` to the `from telethon.errors import (...)` block in `_skip_error_classes()` and to its returned tuple, with the comment:

```python
        # `users.getFullUser`/`users.getUsers` on an `inputUserFromMessage`
        # whose provenance went stale (message deleted, hash rotated) answer
        # USER_ID_INVALID / CHANNEL_INVALID: skip that one user, the profiles
        # sweep continues (person layer, spec §5 case 2).
        UserIdInvalidError,
        ChannelInvalidError,
```

- [ ] **Step 4: `doctor.py`** — rename `_session_age_days` → `session_age_days` (public; the `participants` collector's per-phase gate reuses it) and keep `_session_age_days = session_age_days` for the existing tests.

- [ ] **Step 5: `store/peers.py` — `input_user_ref`**

```python
def input_user_ref(store: Store, uri: str) -> dict | None:
    """The store side of spec §5's `_input_user` builder: the dict the gateway
    turns into an `InputUser`/`InputPeerUser`.

    1. A non-`min` row with a real `access_hash` — in `users` (a triaged/
       enriched person) first, else `peers` (seen in a full `users` vector) —
       → `{"user_id", "access_hash"}`.
    2. Else a `min` stub with `(seen_in_chat, seen_in_msg)` provenance into a
       channel whose own hash `peers` knows → `{"user_id", "from_msg": {...}}`
       for `inputUserFromMessage` (research §1.9/§8.7 — the ONLY way a
       message-discovered stub is ever enrichable).
    3. Else `None`: unresolvable (a `min` hash is only good for photo
       downloads and is never offered here).
    """
    kind, ids = parse_uri(uri)
    if kind != "user":
        raise ValueError(f"input_user_ref expects a user URI, got {uri!r}")
    user_id = ids[0]
    user = store.conn.execute("SELECT is_min, access_hash FROM users WHERE uri=?", (uri,)).fetchone()
    if user is not None and not user["is_min"] and user["access_hash"]:
        return {"user_id": user_id, "access_hash": user["access_hash"]}
    peer = store.conn.execute(
        "SELECT is_min, access_hash, seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
    ).fetchone()
    if peer is None:
        return None
    if not peer["is_min"] and peer["access_hash"]:
        return {"user_id": user_id, "access_hash": peer["access_hash"]}
    if peer["seen_in_chat"] and peer["seen_in_msg"]:
        chan = store.conn.execute(
            "SELECT access_hash FROM peers WHERE uri=?", (channel_uri(peer["seen_in_chat"]),)
        ).fetchone()
        if chan is not None and chan["access_hash"]:
            return {
                "user_id": user_id,
                "from_msg": {
                    "channel_id": peer["seen_in_chat"], "access_hash": chan["access_hash"],
                    "msg_id": peer["seen_in_msg"],
                },
            }
    return None
```

(add `parse_uri` to the `paperboy.ids` import in `peers.py`.)

- [ ] **Step 6: `gateway.py` — Protocol, builders, `TelethonGateway`, `FakeGateway`**

Protocol additions (after `get_sponsored_messages`):

```python
    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        """`channels.getParticipants` — a `channels.ChannelParticipants` dict
        (`count` is the TRUE total even when paging is server-capped, spec
        §6.3) or `channels.ChannelParticipantsNotModified`. `filter` is a
        `channelParticipants*` dict (`FILTER_RECENT`/`FILTER_ADMINS`/
        `FILTER_BOTS`, or a `channelParticipantsMentions` dict). Page size
        is Telegram's 200. May raise `SkipAndRecord` (`CHAT_ADMIN_REQUIRED`,
        `CHANNEL_PRIVATE` — a walled roster)."""
        ...

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        """`channels.getParticipant` — the per-user membership oracle. Returns
        a `channels.ChannelParticipant` dict (`participant` + `users`), or
        `None` for a definitive `USER_NOT_PARTICIPANT` (a RESULT, not a
        failure — plan D4). `participant` is an input-user ref dict (see
        `_input_user`). May raise `SkipAndRecord` (`CHAT_ADMIN_REQUIRED` —
        e.g. an arbitrary user on a broadcast channel, spec §13)."""
        ...

    async def get_users(self, refs: list[dict]) -> list[dict]:
        """`users.getUsers` — batched triage; one `User`/`UserEmpty` dict per
        ref, in order. `refs` are input-user ref dicts; a single stale
        `from_msg` provenance fails the WHOLE vector (`MSG_ID_INVALID` →
        `SkipAndRecord`), which is why callers bisect (plan D13)."""
        ...

    async def get_full_user(self, ref: dict) -> dict:
        """`users.getFullUser` for an arbitrary input-user ref — a
        `users.UserFull` dict: parse BOTH `full_user` and the `users` vector
        (disjoint data, research Part 2 §1). May raise `SkipAndRecord`
        (`USER_ID_INVALID`, `CHANNEL_INVALID`, `MSG_ID_INVALID`)."""
        ...

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        """`photos.getUserPhotos` (limit ≤ 100) — a `photos.Photos` /
        `photos.PhotosSlice` dict: the target's own dated avatar history,
        newest first; personal/fallback photos are never included."""
        ...

    async def download_user_photo(self, photo: dict) -> bytes | None:
        """Download one `Photo` from `get_user_photos` (largest size) as raw
        bytes via `upload.getFile`; `None` if gone. Raises `SkipAndRecord`
        when its `file_reference` has expired. Read-only."""
        ...

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        """`messages.getMessageReactionsList` — `{count, reactions: [{peer_id,
        date, reaction}], users, chats, next_offset}`. GROUPS ONLY: a broadcast
        answers `BROADCAST_FORBIDDEN` (`SkipAndRecord`) and must never be
        asked (guardrail)."""
        ...
```

Module-level builders (next to `_input_channel`):

```python
FILTER_RECENT = {"_": "channelParticipantsRecent"}
FILTER_ADMINS = {"_": "channelParticipantsAdmins"}
FILTER_BOTS = {"_": "channelParticipantsBots"}


def _input_user(ref: dict) -> TypeInputUser:
    """Spec §5's `_input_user` builder (gateway side). Case 1: a real
    `access_hash` → `InputUser`. Case 2: `from_msg` provenance →
    `InputUserFromMessage` (the server re-derives the hash from the message
    the user was seen in). Case 3 never reaches here — `store.peers
    .input_user_ref` returns `None` for it and callers skip."""
    from telethon.tl.types import InputUser, InputUserFromMessage

    from_msg = ref.get("from_msg")
    if from_msg is not None:
        return InputUserFromMessage(
            peer=_input_peer_channel(from_msg), msg_id=from_msg["msg_id"], user_id=ref["user_id"]
        )
    return InputUser(user_id=ref["user_id"], access_hash=ref["access_hash"])


def _input_peer_user(ref: dict) -> TypeInputPeer:
    from telethon.tl.types import InputPeerUser, InputPeerUserFromMessage

    from_msg = ref.get("from_msg")
    if from_msg is not None:
        return InputPeerUserFromMessage(
            peer=_input_peer_channel(from_msg), msg_id=from_msg["msg_id"], user_id=ref["user_id"]
        )
    return InputPeerUser(user_id=ref["user_id"], access_hash=ref["access_hash"])


def _participants_filter(filter: dict) -> TypeChannelParticipantsFilter:
    from telethon.tl.types import (
        ChannelParticipantsAdmins,
        ChannelParticipantsBots,
        ChannelParticipantsMentions,
        ChannelParticipantsRecent,
    )

    kind = (filter.get("_") or "").lower()
    if kind == "channelparticipantsrecent":
        return ChannelParticipantsRecent()
    if kind == "channelparticipantsadmins":
        return ChannelParticipantsAdmins()
    if kind == "channelparticipantsbots":
        return ChannelParticipantsBots()
    if kind == "channelparticipantsmentions":
        return ChannelParticipantsMentions(q=filter.get("q"), top_msg_id=filter.get("top_msg_id"))
    raise ValueError(f"unsupported participants filter: {filter.get('_')!r}")


def _largest_photo_size(sizes: list[dict]) -> str:
    """The `thumb_size` type letter of the largest real size (`photoSize` /
    `photoSizeProgressive` carry `w`/`h`; stripped/cached thumbs do not).
    Telegram's largest avatar size is conventionally `x`, the fallback."""
    best: tuple[int, str] | None = None
    for size in sizes:
        w, h, type_ = size.get("w"), size.get("h"), size.get("type")
        if isinstance(w, int) and isinstance(h, int) and isinstance(type_, str):
            if best is None or w * h > best[0]:
                best = (w * h, type_)
    return best[1] if best else "x"


def _file_reference(photo: dict) -> bytes:
    """`file_reference` arrives as `bytes` live (`to_dict()`) and as base64
    text from a stored/replayed record (`store.db.dumps`)."""
    ref = photo.get("file_reference") or b""
    return ref if isinstance(ref, bytes) else base64.b64decode(ref)
```

(add `import base64` and, under `TYPE_CHECKING`, `from telethon.tl.types import TypeChannelParticipantsFilter, TypeInputPeer, TypeInputUser`.)

`TelethonGateway` methods (after `join_channel`):

```python
    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        tl_filter = _participants_filter(filter)
        result = cast(
            TLObject,
            await self.budget.call(
                "channels.getParticipants",
                lambda: self.client(
                    GetParticipantsRequest(
                        channel=channel, filter=tl_filter, offset=offset, limit=limit, hash=hash_
                    )
                ),
            ),
        )
        return result.to_dict()

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        # USER_NOT_PARTICIPANT is a definitive ANSWER (spec §13), not a failure:
        # `errors.classify` deliberately leaves it unclassified so it reaches
        # here verbatim, and it becomes `None` for the collector.
        from telethon.errors import UserNotParticipantError
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.tlobject import TLObject

        channel = _input_channel(input_channel)
        peer = _input_peer_user(participant)
        try:
            result = cast(
                TLObject,
                await self.budget.call(
                    "channels.getParticipant",
                    lambda: self.client(GetParticipantRequest(channel=channel, participant=peer)),
                ),
            )
        except UserNotParticipantError:
            return None
        return result.to_dict()

    async def get_users(self, refs: list[dict]) -> list[dict]:
        from telethon.tl.functions.users import GetUsersRequest
        from telethon.tl.tlobject import TLObject

        ids = [_input_user(r) for r in refs]
        result = cast(
            list[TLObject],
            await self.budget.call("users.getUsers", lambda: self.client(GetUsersRequest(id=ids))),
        )
        return [u.to_dict() for u in result]

    async def get_full_user(self, ref: dict) -> dict:
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types.users import UserFull

        user = _input_user(ref)
        result = cast(
            UserFull,
            await self.budget.call(
                "users.getFullUser", lambda: self.client(GetFullUserRequest(id=user))
            ),
        )
        return result.to_dict()

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        from telethon.tl.functions.photos import GetUserPhotosRequest
        from telethon.tl.tlobject import TLObject

        user = _input_user(ref)
        result = cast(
            TLObject,
            await self.budget.call(
                "photos.getUserPhotos",
                lambda: self.client(
                    GetUserPhotosRequest(user_id=user, offset=offset, max_id=max_id, limit=limit)
                ),
            ),
        )
        return result.to_dict()

    async def download_user_photo(self, photo: dict) -> bytes | None:
        from telethon.errors import FileReferenceExpiredError
        from telethon.tl.types import InputPhotoFileLocation

        from paperboy.budget import SkipAndRecord

        location = InputPhotoFileLocation(
            id=photo["id"], access_hash=photo["access_hash"],
            file_reference=_file_reference(photo), thumb_size=_largest_photo_size(photo.get("sizes") or []),
        )
        try:
            # `file=bytes` is Telethon's documented "return the bytes" idiom
            # (same `Any` cast as `download_media`, a stub gap).
            return cast(
                bytes | None,
                await self.budget.call(
                    "upload.getFile",
                    lambda: self.client.download_file(
                        location, file=cast(Any, bytes), dc_id=photo.get("dc_id")
                    ),
                ),
            )
        except FileReferenceExpiredError as exc:
            # The reference came from THIS run's getUserPhotos; a second fetch
            # would rarely help within the same pass. Skip this one avatar.
            raise SkipAndRecord(
                f"avatar download skipped: file_reference expired for photo {photo['id']}"
            ) from exc

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        from telethon.tl.functions.messages import GetMessageReactionsListRequest
        from telethon.tl.tlobject import TLObject

        peer = _input_peer_channel(input_channel)
        result = cast(
            TLObject,
            await self.budget.call(
                "messages.getMessageReactionsList",
                lambda: self.client(
                    GetMessageReactionsListRequest(peer=peer, id=msg_id, limit=limit, offset=offset)
                ),
            ),
        )
        return result.to_dict()
```

`FakeGateway` — extend `__init__` and add methods; also make `get_full_channel` and `get_privacy` per-key tolerant:

```python
        # Person-layer introspection (same rationale as `calls` above).
        self.participants_calls: list[tuple[int, str, int]] = []
        self.participant_calls: list[tuple[int, int]] = []
        self.users_calls: list[list[int]] = []
        self.full_user_calls: list[int] = []
        self.user_photos_calls: list[int] = []
        self.avatar_calls: list[int] = []
        self.reactions_calls: list[tuple[int, int, str | None]] = []

    async def get_full_channel(self, input_channel: dict) -> dict:
        self.calls.append("get_full_channel")
        # `full_channel_by_id` lets a test answer the LINKED GROUP's ChatFull
        # (participants preflight) differently from the target's.
        by_id: dict[int, object] = self._fx.get("full_channel_by_id", {})
        value = by_id.get(input_channel["channel_id"], self._fx.get("full_channel"))
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise KeyError("full_channel")
        return cast(dict, value)

    async def get_privacy(self, key: str) -> dict:
        self.calls.append("get_privacy")
        table = self._fx.get("privacy") or {}
        if key not in table:
            from paperboy.budget import SkipAndRecord

            raise SkipAndRecord(f"fake: no privacy fixture for {key!r}")
        return table[key]

    @staticmethod
    def _raise_if_exc(value: object) -> None:
        if isinstance(value, BaseException):
            raise value

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        self.calls.append("get_participants")
        del limit, hash_
        cid = input_channel["channel_id"]
        name = filter.get("_", "")
        prior = sum(1 for c in self.participants_calls if c[0] == cid and c[1] == name)
        self.participants_calls.append((cid, name, offset))
        pages = self._fx.get("participants", {}).get(cid, {}).get(name)
        self._raise_if_exc(pages)
        empty = {"_": "ChannelParticipants", "count": 0, "participants": [], "chats": [], "users": []}
        if not pages:
            return empty
        if prior >= len(pages):
            # Past the recorded pages: an empty page ends the collector's loop
            # (never repeat the last page — that would spin forever).
            return {**empty, "count": pages[-1].get("count", 0)}
        page = pages[prior]
        self._raise_if_exc(page)
        return page

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        self.calls.append("get_participant")
        cid, uid = input_channel["channel_id"], participant["user_id"]
        self.participant_calls.append((cid, uid))
        value = self._fx.get("participant", {}).get(cid, {}).get(uid)
        self._raise_if_exc(value)
        return value

    async def get_users(self, refs: list[dict]) -> list[dict]:
        self.calls.append("get_users")
        ids = [r["user_id"] for r in refs]
        self.users_calls.append(ids)
        table = self._fx.get("users", {})
        for uid in ids:
            self._raise_if_exc(table.get(uid))  # one bad ref fails the vector
        return [table.get(uid, {"_": "UserEmpty", "id": uid}) for uid in ids]

    async def get_full_user(self, ref: dict) -> dict:
        self.calls.append("get_full_user")
        self.full_user_calls.append(ref["user_id"])
        value = self._fx.get("full_user", {}).get(ref["user_id"])
        self._raise_if_exc(value)
        if value is None:
            from paperboy.budget import SkipAndRecord

            raise SkipAndRecord(f"fake: no full_user fixture for {ref['user_id']}")
        return value

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        self.calls.append("get_user_photos")
        del offset, max_id, limit
        self.user_photos_calls.append(ref["user_id"])
        value = self._fx.get("user_photos", {}).get(ref["user_id"])
        self._raise_if_exc(value)
        return value if value is not None else {"_": "Photos", "photos": [], "users": []}

    async def download_user_photo(self, photo: dict) -> bytes | None:
        self.calls.append("download_user_photo")
        self.avatar_calls.append(photo["id"])
        value = self._fx.get("avatar", {}).get(photo["id"])
        self._raise_if_exc(value)
        return value

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        self.calls.append("get_message_reactions_list")
        del limit
        cid = input_channel["channel_id"]
        prior = sum(1 for c in self.reactions_calls if c[0] == cid and c[1] == msg_id)
        self.reactions_calls.append((cid, msg_id, offset))
        value = self._fx.get("reactions", {}).get(cid, {}).get(msg_id)
        self._raise_if_exc(value)
        empty = {"_": "MessageReactionsList", "count": 0, "reactions": [], "chats": [], "users": [],
                 "next_offset": None}
        if value is None:
            return empty
        if isinstance(value, list):
            page = value[prior] if prior < len(value) else empty
            self._raise_if_exc(page)
            return page
        return value
```

Update the `FakeGateway` class docstring's fixture-key list to include the new keys (exact wording from **Interfaces**).

- [ ] **Step 7: Run tests to green; full suite + lint/type**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest && uv run ruff check && uv run pyright`
Expected: PASS. If pyright objects to `cast(list[TLObject], ...)`, use `cast(list[Any], ...)`.

- [ ] **Step 8: Commit**

```bash
git add src/paperboy/gateway.py src/paperboy/errors.py src/paperboy/store/peers.py src/paperboy/doctor.py \
  tests/test_input_user.py tests/test_gateway_fake.py tests/test_budget.py
git commit -m "feat(gateway): person-layer methods, _input_user builders, input_user_ref, fake fixtures (#41)"
```

---

### Task 5: Settings knobs, `parse_duration`, per-method Budget pacing, composition

**Files:**
- Modify: `src/paperboy/config.py`, `src/paperboy/budget.py`, `src/paperboy/app.py`
- Test: `tests/test_config.py`, `tests/test_budget.py`

**Interfaces:**
- Produces: `Settings.unsafe: bool = False`; `Settings.enrich_profiles: bool = False`; `Settings.profile_interval: float | None = None` (seconds, `ge=0`); `Settings.profile_refresh_after: int | None = None` (seconds, `ge=0`); `Settings.participant_oracle_budget: int = 100` (`ge=0`); `Settings.participant_reactions_budget: int = 200` (`ge=0`); `config.parse_duration(text: str) -> int` (`"7d" | "12h" | "30m" | "45s" | "3600"` → seconds; `ValueError` otherwise); `Budget(..., method_intervals: dict[str, float] | None = None)`; `app.PROFILE_PACED_METHODS = ("users.getFullUser", "photos.getUserPhotos")`; `app.profile_method_intervals(settings) -> dict[str, float]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py` (add `parse_duration` to the file's existing `from paperboy.config import ...` line and `import pytest` to its top block only if absent):

```python
def test_person_layer_defaults():
    s = load_settings("default", {})
    assert s.unsafe is False
    assert s.enrich_profiles is False
    assert s.profile_interval is None
    assert s.profile_refresh_after is None
    assert s.profile_budget == 2000
    assert s.participant_oracle_budget == 100
    assert s.participant_reactions_budget == 200


def test_parse_duration_units():
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("12h") == 12 * 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("45s") == 45
    assert parse_duration("3600") == 3600
    for bad in ("", "7x", "-1d", "d"):
        with pytest.raises(ValueError):
            parse_duration(bad)
```

Append to `tests/test_budget.py`:

```python
@pytest.mark.asyncio
async def test_per_method_interval_paces_only_that_method(tmp_path):
    class Clock:
        t = 1000.0

        def time(self):
            return self.t

    clock = Clock()
    slept: list[float] = []
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, clock=clock, sleeper=lambda x: slept.append(x), min_interval=1.0,
                   method_intervals={"users.getFullUser": 2.5})

        async def ok():
            return 1

        await b.call("users.getFullUser", ok)
        clock.t += 0.5
        await b.call("users.getFullUser", ok)  # 0.5s since last -> sleep 2.0 (the METHOD interval)
        await b.call("messages.getHistory", ok)  # first call of that method: no sleep
        clock.t += 0.2
        await b.call("messages.getHistory", ok)  # default 1.0 interval -> sleep 0.8
        assert slept == [2.0, pytest.approx(0.8)]


@pytest.mark.asyncio
async def test_per_method_interval_composes_with_flood_handling(tmp_path):
    # `--profile-interval` never bypasses flood handling: a short FLOOD_WAIT on
    # a paced method is still recorded and slept, then retried once.
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        slept: list[float] = []
        b = Budget(s, st, sleeper=lambda x: slept.append(x), method_intervals={"users.getFullUser": 2.0})
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeFlood(3)
            return "ok"

        assert await b.call("users.getFullUser", flaky) == "ok"
        assert 3 in slept
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure** — `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_config.py tests/test_budget.py -q --basetemp=/Volumes/Storage/tmp/pytest` → `ImportError: parse_duration`, `AttributeError`s, `TypeError: unexpected keyword 'method_intervals'`.

- [ ] **Step 3: `config.py`**

```python
import re
...
_DURATION_RE = re.compile(r"^(\d+)([dhms]?)$")
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 1}


def parse_duration(text: str) -> int:
    """`7d` / `12h` / `30m` / `45s` / bare seconds → seconds. Used by the
    `--profile-refresh-after` CLI flag (spec §7.2)."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (expected e.g. 7d, 12h, 30m, 45s)")
    value, unit = match.groups()
    return int(value) * _DURATION_UNITS[unit]
```

and on `Settings`, after `allow_phone_lookup`:

```python
    # --unsafe: skip the doctor preflight AND the per-phase session-age gate
    # on roster enumeration (spec §6.1). Env `PAPERBOY_UNSAFE` is the same
    # operator override.
    unsafe: bool = False
    # Person layer (spec §7.2). `profile_budget` above bounds getFullUser
    # fetches per run; these parameterize the rest of the enrichment pass.
    enrich_profiles: bool = False
    profile_interval: float | None = Field(default=None, ge=0)
    profile_refresh_after: int | None = Field(default=None, ge=0)  # seconds
    participant_oracle_budget: int = Field(default=100, ge=0)
    participant_reactions_budget: int = Field(default=200, ge=0)
```

- [ ] **Step 4: `budget.py`** — `Budget.__init__` gains `method_intervals: dict[str, float] | None = None` → `self._method_intervals: dict[str, float] = dict(method_intervals or {})`; in `call`, replace the `delta < self._min_interval` check with

```python
        interval = self._method_intervals.get(method, self._min_interval)
        last = self._last_call.get(method)
        if last is not None:
            delta = self._clock.time() - last
            if delta < interval:
                await self._sleep(interval - delta)
```

and extend the class docstring: "`method_intervals` overrides the pace for specific methods (`--profile-interval` → `users.getFullUser`/`photos.getUserPhotos`); flood cooldowns and the run cap apply regardless."

- [ ] **Step 5: `app.py`**

```python
PROFILE_PACED_METHODS = ("users.getFullUser", "photos.getUserPhotos")


def profile_method_intervals(settings: Settings) -> dict[str, float]:
    """`--profile-interval` (spec §7.2) as `Budget.method_intervals`: it paces
    the two per-user profile RPCs only, THROUGH the budget chokepoint."""
    if settings.profile_interval is None:
        return {}
    return dict.fromkeys(PROFILE_PACED_METHODS, settings.profile_interval)
```

and in `build_gateway`: `budget = Budget(settings, store, method_intervals=profile_method_intervals(settings))`. (`Settings` must be a runtime import for the annotation → move it out of `TYPE_CHECKING`, or keep `from __future__ import annotations` semantics — it already is, so the string annotation is fine.)

- [ ] **Step 6: Green + full suite + lint/type; commit**

```bash
git add src/paperboy/config.py src/paperboy/budget.py src/paperboy/app.py tests/test_config.py tests/test_budget.py
git commit -m "feat(config): person-layer knobs, parse_duration, per-method Budget pacing (#41)"
```

---
### Task 6: `profiles` collector — the always-on half (#11 backfill, privacy posture, gather, batched triage, the `--profiles`-off warning)

**Files:**
- Create: `src/paperboy/collectors/profiles.py`, `src/paperboy/collectors/posture.py`
- Test: `tests/test_collector_profiles.py`

**Interfaces:**
- Consumes: Tasks 2–5 (`upsert_user`, `add_user_snapshot`, `target_*_facts`, `backfill_message_referenced_peers`, `input_user_ref`, `upsert_peer`, `gateway.get_users`/`get_privacy`, `Settings.enrich_profiles`/`profile_budget`); `ids.namespaced_kind`; `store.events.record_run_event`; `store.sync.set_state`.
- Produces: `posture.record_privacy_posture(ctx: CollectContext, phase: str) -> bool` (shared by `profiles` and `participants`; records the account's `phone`/`lastseen`/`photo` rules once per run — guarded by `Store.run_id` — and returns whether this call recorded them); `ProfilesCollector` (`name = "profiles"`); module constants `METHOD_GET_USERS = "users.getUsers"`, `METHOD_GET_FULL_USER = "users.getFullUser"`, `METHOD_GET_USER_PHOTOS = "photos.getUserPhotos"`, `ENRICHMENT_OFF_WARNING`; `run_events` kinds `warning` (detail `{"code": "profiles_enrichment_off", "triaged": n, "hint": "--profiles"}`) and `privacy_posture`; raw kinds `User`/`UserEmpty` (context `{"channel_id", "method": "users.getUsers", "user_id"}`) and `account.PrivacyRules` (tier `self`, context `{"key"}`); `sync_state('profiles', <channel_id>)` = `{"pass", "population", "fully_enriched", "enriched_this_run", "budget"}`. Counts: `backfilled_peers, gathered, unresolvable, triaged, empty, skipped, snapshots, enriched, refreshed, fresh_skipped, photos, avatars, restricted_skipped`.

- [ ] **Step 1: Write the failing tests** — `tests/test_collector_profiles.py`

```python
"""The `profiles` collector, spec §7: triage always; full enrichment behind --profiles."""

from __future__ import annotations  # noqa: I001

import json
import logging

import pytest
from paperboy.collectors.profiles import ProfilesCollector

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.participants import ParticipantFacts, write_participant
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import get_state, set_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77
T0 = "2026-01-01T00:00:00+00:00"


def _settings(tmp_path, **over):
    return load_settings("default", {"data_dir": tmp_path, **over})


def _ctx(st, gw, settings, tier="stranger"):
    return CollectContext(
        gw, st, settings, parse_target("@x"),
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, tier, logging.getLogger("t"), "p",
    )


def _seed_channel(st: Store, linked: int | None = GROUP_ID) -> None:
    raw_id = st.add_raw("ChatFull", {"_": "ChatFull"}, "stranger", None)
    upsert_channel(
        st, {"_": "channelFull", "id": CHANNEL_ID, "pts": 1, "linked_chat_id": linked, "participants_count": 10},
        {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C", "username": "c", "broadcast": True},
        raw_id, T0,
    )
    if linked:
        upsert_peer(st, {"_": "Channel", "id": linked, "access_hash": 4242, "title": "G", "megagroup": True},
                    raw_id, T0, seen_in_chat=None, seen_in_msg=None)


def _seed_stub(st: Store, user_id: int, *, chat: int | None = GROUP_ID, msg: int | None = 200) -> None:
    raw_id = st.add_raw("Message", {"_": "Message", "id": msg or 0}, "stranger", {"channel_id": chat})
    upsert_peer(st, {"_": "User", "id": user_id, "min": True}, raw_id, T0, seen_in_chat=chat, seen_in_msg=msg)


def _user(user_id: int, **extra) -> dict:
    return {"_": "User", "id": user_id, "access_hash": user_id * 10, "first_name": f"U{user_id}",
            "username": f"u{user_id}", "phone": None, "photo": None, "status": None,
            "restriction_reason": [], "usernames": [], **extra}


def _gw(users: dict[int, dict], **more) -> FakeGateway:
    return FakeGateway({"users": users, **more})


@pytest.mark.asyncio
async def test_triage_resolves_min_stubs_via_from_message_and_writes_users(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        _seed_stub(st, 12, msg=201)
        gw = _gw({11: _user(11), 12: _user(12, phone="+15550002222")})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.users_calls == [[11, 12]]
        # case-2 refs: built from the stub's stored provenance + the group's hash
        assert gw.calls.count("get_users") == 1
        rows = {r["uri"]: r for r in st.conn.execute("select * from users")}
        assert rows["tg:user:11"]["first_name"] == "U11" and rows["tg:user:11"]["enriched_at"] is None
        assert rows["tg:user:12"]["phone"] == "+15550002222"
        assert json.loads(rows["tg:user:12"]["field_states_json"])["phone"] == {"state": "present"}
        peer = st.conn.execute(
            "select is_min, access_hash, seen_in_chat, seen_in_msg from peers where uri='tg:user:11'"
        ).fetchone()
        assert (peer["is_min"], peer["access_hash"]) == (0, 110)  # now a full peer with a real hash
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 200)  # provenance preserved
        raw = st.conn.execute(
            "select kind, context_json from raw_records where json_extract(context_json, '$.method')='users.getUsers' "
            "order by id"
        ).fetchall()
        assert [r["kind"] for r in raw] == ["User", "User"]
        assert json.loads(raw[0]["context_json"]) == {"channel_id": CHANNEL_ID, "method": "users.getUsers", "user_id": 11}
        snaps = st.conn.execute("select uri, method from user_snapshots order by uri").fetchall()
        assert [(s["uri"], s["method"]) for s in snaps] == [("tg:user:11", "users.getUsers"), ("tg:user:12", "users.getUsers")]
        assert res.counts["gathered"] == 2 and res.counts["triaged"] == 2 and res.counts["snapshots"] == 2
        assert res.counts["enriched"] == 0 and gw.full_user_calls == []


@pytest.mark.asyncio
async def test_triage_batches_at_most_100_refs_per_call(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for i in range(1, 231):
            _seed_stub(st, i, msg=i)
        gw = _gw({i: _user(i) for i in range(1, 231)})
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert [len(c) for c in gw.users_calls] == [100, 100, 30]


@pytest.mark.asyncio
async def test_a_failed_batch_is_bisected_to_isolate_the_stale_ref(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for i in (1, 2, 3, 4):
            _seed_stub(st, i, msg=i)
        gw = _gw({1: _user(1), 2: _user(2), 3: SkipAndRecord("MSG_ID_INVALID"), 4: _user(4)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        # [1,2,3,4] fails -> [1,2] ok -> [3,4] fails -> [3] fails (skipped) -> [4] ok
        assert gw.users_calls == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]
        assert res.counts["triaged"] == 3 and res.counts["skipped"] == 1
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_unresolvable_stubs_are_counted_and_never_sent(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1, chat=None, msg=None)  # no provenance at all
        _seed_stub(st, 2, chat=999, msg=5)  # provenance into a channel with no known hash
        _seed_stub(st, 3)
        gw = _gw({3: _user(3)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.users_calls == [[3]]
        assert res.counts["unresolvable"] == 2 and res.counts["gathered"] == 3


@pytest.mark.asyncio
async def test_forward_origin_users_are_backfilled_then_triaged(tmp_path):
    # Issue #11: the forwarded_from endpoint had no peers row, so no sweep
    # could ever reach it. Now it is backfilled (provenance = the message)
    # and triaged in the same pass.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        m = {"_": "Message", "id": 300, "message": "fwd", "date": 1767322445,
             "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}}}
        raw_id = st.add_raw("Message", m, "stranger", {"channel_id": CHANNEL_ID})
        upsert_message(st, CHANNEL_ID, m, raw_id, T0, "stranger")
        gw = _gw({42: _user(42)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["backfilled_peers"] == 1
        assert gw.users_calls == [[42]]
        assert st.conn.execute("select first_name from users where uri='tg:user:42'").fetchone()[0] == "U42"


@pytest.mark.asyncio
async def test_without_profiles_flag_no_full_user_call_and_a_warning_is_recorded(tmp_path, caplog):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        gw = _gw({11: _user(11)}, full_user={11: {"full_user": {"_": "UserFull", "id": 11}, "users": [_user(11)]}})
        with caplog.at_level(logging.WARNING):
            res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.full_user_calls == [] and gw.user_photos_calls == []
        assert res.stopped is None  # triage-only is the documented default, not a stop
        event = st.conn.execute(
            "select detail_json from run_events where phase='profiles' and kind='warning'"
        ).fetchone()
        detail = json.loads(event["detail_json"])
        assert detail["code"] == "profiles_enrichment_off" and detail["triaged"] == 1
        assert any("--profiles" in r.getMessage() and "triaged 1" in r.getMessage() for r in caplog.records)
        assert get_state(st, "profiles", str(CHANNEL_ID))["pass"] == "triage_only"


@pytest.mark.asyncio
async def test_privacy_posture_is_recorded_once_per_run(tmp_path):
    rules = {"_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}], "chats": [], "users": []}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw({}, privacy={"phone": rules, "lastseen": rules})  # `photo` deliberately missing
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.calls.count("get_privacy") == 3
        raw = st.conn.execute(
            "select kind, tier, context_json from raw_records where kind like '%PrivacyRules' order by id"
        ).fetchall()
        assert [(r["kind"], r["tier"]) for r in raw] == [("account.PrivacyRules", "self")] * 2
        assert json.loads(raw[0]["context_json"]) == {"key": "phone"}
        posture = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='privacy_posture'"
        ).fetchone()["detail_json"])
        assert posture["phone"] == ["PrivacyValueAllowContacts"]
        assert "unavailable" in posture["photo"]


@pytest.mark.asyncio
async def test_user_empty_is_recorded_raw_but_never_projected(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        res = await ProfilesCollector().collect(_ctx(st, _gw({}), _settings(tmp_path)))
        assert res.counts["empty"] == 1 and res.counts["triaged"] == 0
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 0
        assert st.conn.execute("select count(*) from raw_records where kind='UserEmpty'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_collecting_account_in_a_users_vector_is_never_projected(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:1", "id": 1})
        _seed_channel(st)
        # a peer row for self cannot exist (#12), but a fixture may still answer it
        _seed_stub(st, 11)
        gw = _gw({11: _user(1, is_self=True)})  # fake answers the WRONG user: self
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_phase_stop_when_channel_context_is_missing(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _ctx(st, _gw({}), _settings(tmp_path))
        ctx.channel_id = None
        with pytest.raises(PhaseStop):
            await ProfilesCollector().collect(ctx)


def test_applies_to_channel_like_targets():
    assert ProfilesCollector().applies_to(parse_target("@durov"))
    assert not ProfilesCollector().applies_to(parse_target("#osint"))
```

- [ ] **Step 2: Run to verify failure** — `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_collector_profiles.py -q --basetemp=/Volumes/Storage/tmp/pytest` → `ModuleNotFoundError: paperboy.collectors.profiles`.

- [ ] **Step 3: Implement `src/paperboy/collectors/profiles.py`** (the triage half; Task 7 fills in `_enrich`, `_enrichment_candidates`, `_photos`, `_download_avatar` — leave those three methods out entirely for now; `collect` returns after the warning branch when `enrich_profiles` is false, and for this task the `else` branch simply calls `self._record_summary(ctx, counts, pass_="initial")`)

```python
"""The `profiles` collector: the person layer's single enrichment authority
(spec §7). Sweeps EVERY discovered user peer — from every vector — and turns
`min` stubs into people:

1. Zero-RPC first (issue #11): forward origins and mention-name users
   referenced by stored messages get `peers` rows with provenance, so this
   very sweep can reach them.
2. Record the collecting account's own privacy posture for the run
   (`account.getPrivacy` for phone/lastseen/photo — the three keys `doctor`
   reads), so a `by_me`-degraded status is attributable to US, never misread
   as target opsec (spec §4.3).
3. Gather every `kind='user'` peer; build each one's input-user ref
   (`store.peers.input_user_ref`) — a `min` stub is reachable ONLY via
   `inputUserFromMessage` from its stored `(seen_in_chat, seen_in_msg)`.
4. Triage — batched `users.getUsers` (≤100/call, bisected on failure — plan
   D13): cheap identity for everyone, always. Writes `users` +
   `user_snapshots`, and the full object into `peers` with the stub's
   provenance preserved.
5. Full enrichment — ONLY under `--profiles`: `users.getFullUser` +
   `photos.getUserPhotos` + avatar download per user, priority admins →
   authors → commenters → others, bounded by `profile_budget`, converging
   across runs via `users.enriched_at` (plan D3). Without the flag the run
   ends after triage with a warning naming exactly what was not fetched.

Profile richness lands in `users`/`user_snapshots`/`user_photos`; `peers`
is only ever written through `upsert_peer` (never modified here).
"""

from __future__ import annotations

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.posture import record_privacy_posture
from paperboy.ids import channel_uri, user_uri
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.peers import input_user_ref, upsert_peer
from paperboy.store.sync import set_state
from paperboy.store.users import add_user_snapshot, target_full_facts, target_user_facts, upsert_user
from paperboy.targets import Target

_GET_USERS_BATCH = 100
METHOD_GET_USERS = "users.getUsers"
METHOD_GET_FULL_USER = "users.getFullUser"

ENRICHMENT_OFF_WARNING = (
    "profiles: triaged {n} people (basic names/handles); full enrichment (bios, photos, "
    "last-seen, …) not run — pass --profiles to enrich them (~1 getFullUser/s, bounded by "
    "--profile-budget, default {budget}/run ≈ {minutes} min)"
)


class ProfilesCollector:
    name = "profiles"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None:
            raise PhaseStop(
                "profiles skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "backfilled_peers": 0, "gathered": 0, "unresolvable": 0, "triaged": 0, "empty": 0,
            "skipped": 0, "snapshots": 0, "enriched": 0, "refreshed": 0, "fresh_skipped": 0,
            "photos": 0, "avatars": 0, "restricted_skipped": 0,
        }
        for channel_id in self._scope_channels(ctx):
            counts["backfilled_peers"] += backfill_message_referenced_peers(ctx.store, channel_id)
        await record_privacy_posture(ctx, self.name)

        refs = self._gather(ctx, counts)
        await self._triage(ctx, refs, counts)

        if not ctx.settings.enrich_profiles:
            budget = ctx.settings.profile_budget
            ctx.log.warning(
                ENRICHMENT_OFF_WARNING.format(
                    n=counts["triaged"], budget=budget, minutes=round(budget / 60)
                )
            )
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "warning",
                {"code": "profiles_enrichment_off", "triaged": counts["triaged"], "hint": "--profiles"},
            )
            self._record_summary(ctx, counts, pass_="triage_only")
            return CollectResult(name=self.name, counts=counts)

        await self._enrich(ctx, counts)
        return CollectResult(name=self.name, counts=counts)

    # ---- zero-RPC preamble ---------------------------------------------------

    @staticmethod
    def _scope_channels(ctx: CollectContext) -> list[int]:
        """The target and its linked group (if any): the channels whose stored
        messages reference the people this sweep must reach."""
        assert ctx.channel_id is not None
        row = ctx.store.conn.execute(
            "SELECT linked_chat_id FROM channels WHERE id=?", (ctx.channel_id,)
        ).fetchone()
        linked = row["linked_chat_id"] if row else None
        return [ctx.channel_id] + ([linked] if linked else [])

    # ---- gather + triage -------------------------------------------------------

    def _gather(self, ctx: CollectContext, counts: dict[str, int]) -> list[tuple[str, dict]]:
        rows = ctx.store.conn.execute(
            "SELECT uri FROM peers WHERE kind='user' ORDER BY uri"
        ).fetchall()
        refs: list[tuple[str, dict]] = []
        for row in rows:
            counts["gathered"] += 1
            ref = input_user_ref(ctx.store, row["uri"])
            if ref is None:
                counts["unresolvable"] += 1  # spec §5 case 3: recorded, never guessed
                continue
            refs.append((row["uri"], ref))
        if counts["unresolvable"]:
            ctx.log.info(
                "profiles: %d of %d users unresolvable (no full object, no usable provenance)",
                counts["unresolvable"], counts["gathered"],
            )
        return refs

    async def _triage(
        self, ctx: CollectContext, refs: list[tuple[str, dict]], counts: dict[str, int]
    ) -> None:
        for start in range(0, len(refs), _GET_USERS_BATCH):
            batch = refs[start:start + _GET_USERS_BATCH]
            for user in await self._get_users_resilient(ctx, batch, counts):
                self._project_triaged(ctx, user, counts)

    async def _get_users_resilient(
        self, ctx: CollectContext, batch: list[tuple[str, dict]], counts: dict[str, int]
    ) -> list[dict]:
        """One stale `from_msg` provenance fails the whole `getUsers` vector;
        bisect to isolate it rather than losing the batch (plan D13)."""
        try:
            return await ctx.gateway.get_users([ref for _, ref in batch])
        except SkipAndRecord as exc:
            if len(batch) == 1:
                ctx.log.warning("profiles: triage skipped for %s: %s", batch[0][0], exc)
                counts["skipped"] += 1
                return []
            mid = len(batch) // 2
            head = await self._get_users_resilient(ctx, batch[:mid], counts)
            tail = await self._get_users_resilient(ctx, batch[mid:], counts)
            return head + tail

    def _project_triaged(self, ctx: CollectContext, user: dict, counts: dict[str, int]) -> None:
        kind = (user.get("_") or "").lower()
        if kind not in ("user", "userempty"):
            # `ReplayUnknownUser`: the original run never observed this id —
            # nothing to project (reproject D4.1's analogue).
            return
        observed_at = ctx.clock.for_payload(user)
        raw_id = ctx.store.add_raw(
            user.get("_", "User"), user, ctx.tier,
            {"channel_id": ctx.channel_id, "method": METHOD_GET_USERS, "user_id": user["id"]},
            observed_at=observed_at,
        )
        if kind == "userempty":
            counts["empty"] += 1
            return
        if self._project_user(ctx, user, raw_id, observed_at, METHOD_GET_USERS, counts) is not None:
            counts["triaged"] += 1

    def _project_user(
        self,
        ctx: CollectContext,
        user: dict,
        raw_id: int,
        observed_at: str,
        method: str,
        counts: dict[str, int],
        *,
        full_user: dict | None = None,
    ) -> str | None:
        """`users` + `user_snapshots` (+ the full object into `peers`, keeping
        the stub's provenance). `None` for the collecting account."""
        self._upsert_peer_keeping_provenance(ctx, user, raw_id, observed_at)
        uri = upsert_user(ctx.store, user, raw_id, observed_at, ctx.tier, full_user=full_user)
        if uri is None:
            return None
        bundle: dict = {"user": target_user_facts(user)}
        if full_user is not None:
            bundle["full_user"] = target_full_facts(full_user)
        if add_user_snapshot(ctx.store, uri, observed_at, ctx.tier, method, bundle, raw_id):
            counts["snapshots"] += 1
        return uri

    @staticmethod
    def _upsert_peer_keeping_provenance(
        ctx: CollectContext, obj: dict, raw_id: int, observed_at: str
    ) -> str | None:
        """`upsert_peer` for a FULL object returned by a profile RPC. A full
        observation carrying no provenance of its own would — correctly, by
        the recency rule — overwrite the stub's `seen_in_chat`/`seen_in_msg`
        with NULLs, losing the only path back to `inputUserFromMessage`. Pass
        the stored provenance through instead."""
        kind = (obj.get("_") or "").lower()
        uri = user_uri(obj["id"]) if kind.startswith("user") else channel_uri(obj["id"])
        row = ctx.store.conn.execute(
            "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
        ).fetchone()
        return upsert_peer(
            ctx.store, obj, raw_id, observed_at,
            seen_in_chat=row["seen_in_chat"] if row else None,
            seen_in_msg=row["seen_in_msg"] if row else None,
        )

    def _record_summary(self, ctx: CollectContext, counts: dict[str, int], *, pass_: str) -> None:
        """The run's convergence summary (spec §7.1). The enrichment POSITION
        is derived from `users.enriched_at`, not stored here — an interrupted
        run has enriched exactly those it wrote, so there is no cursor to
        corrupt (plan D3)."""
        population = ctx.store.conn.execute(
            "SELECT count(*) FROM peers WHERE kind='user'"
        ).fetchone()[0]
        fully = ctx.store.conn.execute(
            "SELECT count(*) FROM users WHERE enriched_at IS NOT NULL"
        ).fetchone()[0]
        set_state(ctx.store, "profiles", str(ctx.channel_id), {
            "pass": pass_, "population": population, "fully_enriched": fully,
            "enriched_this_run": counts["enriched"] + counts["refreshed"],
            "budget": ctx.settings.profile_budget,
        })
```

(For this task, `_enrich` is a two-line stub: `async def _enrich(self, ctx: CollectContext, counts: dict[str, int]) -> None: self._record_summary(ctx, counts, pass_="initial")` — Task 7 replaces it. The import block above is exactly Task 6's; Task 7 Step 3 lists the imports, constants and helper it adds.)

- [ ] **Step 3a: Implement `src/paperboy/collectors/posture.py`** (the once-per-run privacy record, spec §4.3, shared with `participants` in Task 8 because the roster's free `users` vectors carry `userStatus*` too)

```python
"""The collecting account's own privacy posture, recorded ONCE per run (spec
§4.3): `account.getPrivacy` for `phone`/`lastseen`/`photo` — the three keys
`doctor` reads. It is what makes a `by_me`-degraded `userStatus*` (a coarse
bucket because OUR privacy hides exact status) attributable to us, never
misread as target opsec. Both person-layer collectors call it; the first one
in a run records it (raw `account.PrivacyRules` per key + a `run_events`
`privacy_posture` row), later calls in the same run are no-ops."""

from __future__ import annotations

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.ids import namespaced_kind
from paperboy.store.events import record_run_event
from paperboy.store.sync import get_state, set_state

PRIVACY_KEYS = ("phone", "lastseen", "photo")


async def record_privacy_posture(ctx: CollectContext, phase: str) -> bool:
    """Returns whether THIS call recorded the posture (False: already
    recorded earlier in the same run)."""
    marker = get_state(ctx.store, "account", "privacy_posture") or {}
    if ctx.store.run_id is not None and marker.get("run_id") == ctx.store.run_id:
        return False
    posture: dict[str, object] = {}
    for key in PRIVACY_KEYS:
        try:
            rules = await ctx.gateway.get_privacy(key)
        except SkipAndRecord as exc:
            posture[key] = {"unavailable": str(exc)}
            continue
        observed_at = ctx.clock.for_payload(rules)
        ctx.store.add_raw(
            namespaced_kind("account", rules, "PrivacyRules"), rules, "self", {"key": key},
            observed_at=observed_at,
        )
        posture[key] = [(rule.get("_") or "") for rule in rules.get("rules") or []]
    record_run_event(ctx.store, ctx.channel_id, phase, "privacy_posture", posture)
    set_state(ctx.store, "account", "privacy_posture", {"run_id": ctx.store.run_id, "posture": posture})
    return True
```

- [ ] **Step 4: Run tests to green; full suite + lint/type**

Run: `TMPDIR=/Volumes/Storage/tmp uv run pytest -q --basetemp=/Volumes/Storage/tmp/pytest && uv run ruff check && uv run pyright`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/paperboy/collectors/profiles.py src/paperboy/collectors/posture.py tests/test_collector_profiles.py
git commit -m "feat(profiles): batched getUsers triage, #11 backfill, privacy posture, --profiles-off warning (#41)"
```

---

### Task 7: `profiles` collector — full enrichment under `--profiles` (priority order, budget, resume-to-convergence, photos + avatars)

**Files:**
- Modify: `src/paperboy/collectors/profiles.py`
- Test: `tests/test_collector_profiles.py` (append)

**Interfaces:**
- Consumes: Task 6's module; `gateway.get_full_user`/`get_user_photos`/`download_user_photo`; `Settings.profile_budget`/`profile_refresh_after`; `Clock.now()`; `store.users.upsert_user_photo`/`user_photo_sha`/`set_user_photo_sha`; `config.profile_dir`.
- Produces: raw kinds `users.UserFull` (context `{"channel_id", "user_id", "method": "users.getFullUser"}`), `photos.Photos`/`photos.PhotosSlice` (context `{"channel_id", "user_id", "method": "photos.getUserPhotos"}`), `AvatarDownload` (payload `{"sha256", "path", "size", "user_uri", "photo_id"}`, context `{"channel_id", "user_id", "photo_id"}`); `media` rows with `kind='avatar'`, `message_uri NULL`; `custody_log` rows with `source_message_uri NULL`; `sync_state('profiles', …)["pass"] ∈ {"triage_only", "initial", "refresh"}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_collector_profiles.py`

```python
def _full(user_id: int, **full_extra) -> dict:
    return {
        "_": "UserFull",
        "full_user": {"_": "UserFull", "id": user_id, "about": f"bio {user_id}", "common_chats_count": 0,
                      "blocked": None, "profile_photo": None, "fallback_photo": None, **full_extra},
        "chats": [], "users": [_user(user_id)],
    }


def _photos(*photo_ids: int) -> dict:
    return {"_": "Photos", "users": [], "photos": [
        {"_": "Photo", "id": pid, "access_hash": 1, "file_reference": "AQ==", "date": 1767322445,
         "dc_id": 2, "sizes": [{"_": "PhotoSize", "type": "x", "w": 640, "h": 640, "size": 1}],
         "video_sizes": None}
        for pid in photo_ids
    ]}


def _seed_population(st: Store) -> None:
    """Five discovered users with distinct priorities: 1 admin, 2 author (posted
    in the channel), 3 commenter (posted in the group), 4 and 5 others."""
    _seed_channel(st)
    for uid in (1, 2, 3, 4, 5):
        _seed_stub(st, uid, msg=uid)
    rid = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
    write_participant(st, GROUP_ID, ParticipantFacts("tg:user:1", "admin", None, None, None, None), rid, T0)
    post = {"_": "Message", "id": 900, "message": "post", "date": 1767322445,
            "from_id": {"_": "PeerUser", "user_id": 2}}
    upsert_message(st, CHANNEL_ID, post, st.add_raw("Message", post, "stranger", {"channel_id": CHANNEL_ID}), T0, "stranger")
    comment = {"_": "Message", "id": 901, "message": "comment", "date": 1767322445,
               "from_id": {"_": "PeerUser", "user_id": 3}}
    upsert_message(st, GROUP_ID, comment, st.add_raw("Message", comment, "stranger", {"channel_id": GROUP_ID}), T0, "stranger")


def _enrich_gw(ids=(1, 2, 3, 4, 5), **more) -> FakeGateway:
    return FakeGateway({
        "users": {i: _user(i) for i in ids},
        "full_user": {i: _full(i) for i in ids},
        **more,
    })


@pytest.mark.asyncio
async def test_profiles_flag_enriches_in_priority_order_within_budget(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        gw = _enrich_gw()
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=2))
        )
        assert gw.full_user_calls == [1, 2]  # admin, then author — budget 2
        assert gw.user_photos_calls == [1, 2]
        assert res.counts["enriched"] == 2 and res.counts["triaged"] == 5
        row = st.conn.execute("select about, enriched_at from users where uri='tg:user:1'").fetchone()
        assert row["about"] == "bio 1" and row["enriched_at"] is not None
        assert st.conn.execute("select enriched_at from users where uri='tg:user:3'").fetchone()[0] is None
        kinds = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records where json_extract(context_json, '$.user_id')=1 order by id")]
        assert kinds == ["User", "users.UserFull", "photos.Photos"]
        snaps = [s["method"] for s in st.conn.execute(
            "select method from user_snapshots where uri='tg:user:1' order by id")]
        assert snaps == ["users.getUsers", "users.getFullUser"]
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary["pass"] == "initial" and summary["fully_enriched"] == 2 and summary["population"] == 5


@pytest.mark.asyncio
async def test_resume_to_convergence_then_refresh_wraps_stalest_first(tmp_path):
    # Spec §11: budget 2 over 5 people — run 1 enriches [1,2], run 2 [3,4],
    # run 3 enriches the tail [5] and THEN wraps to refresh the stalest
    # already-enriched user (1). No head re-enriched before the tail is reached.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        settings = _settings(tmp_path, enrich_profiles=True, profile_budget=2)
        seen: list[list[int]] = []
        for _ in range(3):
            gw = _enrich_gw()
            await ProfilesCollector().collect(_ctx(st, gw, settings))
            seen.append(gw.full_user_calls)
        assert seen == [[1, 2], [3, 4], [5, 1]]
        assert get_state(st, "profiles", str(CHANNEL_ID))["pass"] == "refresh"
        assert st.conn.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 5


@pytest.mark.asyncio
async def test_refresh_floor_skips_recently_enriched_users(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        first = _enrich_gw()
        await ProfilesCollector().collect(_ctx(st, first, _settings(tmp_path, enrich_profiles=True, profile_budget=5)))
        assert first.full_user_calls == [1, 2, 3, 4, 5]
        second = _enrich_gw()
        res = await ProfilesCollector().collect(_ctx(
            st, second, _settings(tmp_path, enrich_profiles=True, profile_budget=5, profile_refresh_after=7 * 86400)
        ))
        assert second.full_user_calls == []
        assert res.counts["fresh_skipped"] == 5 and res.counts["refreshed"] == 0


@pytest.mark.asyncio
async def test_photo_history_and_avatar_download_are_content_addressed(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,), user_photos={1: _photos(701, 702)}, avatar={701: b"jpeg-1", 702: b"jpeg-2"})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        assert gw.avatar_calls == [701, 702]
        assert res.counts["photos"] == 2 and res.counts["avatars"] == 2
        rows = st.conn.execute("select photo_id, date, sha256 from user_photos order by photo_id").fetchall()
        assert [r["photo_id"] for r in rows] == [701, 702]
        assert all(r["sha256"] for r in rows) and rows[0]["date"] == "2026-01-02T02:54:05+00:00"
        media = st.conn.execute("select kind, message_uri, path, mime_type from media").fetchall()
        assert {m["kind"] for m in media} == {"avatar"} and all(m["message_uri"] is None for m in media)
        assert all(m["path"].startswith(str(tmp_path / "p" / "media")) and m["path"].endswith(".jpg") for m in media)
        assert st.conn.execute("select count(*) from custody_log where source_message_uri is null").fetchone()[0] == 2
        assert st.conn.execute("select count(*) from raw_records where kind='AvatarDownload'").fetchone()[0] == 2
        # a second run re-lists the history but never re-downloads a known photo
        again = _enrich_gw(ids=(1,), user_photos={1: _photos(701, 702)}, avatar={701: b"jpeg-1", 702: b"jpeg-2"})
        await ProfilesCollector().collect(_ctx(st, again, _settings(tmp_path, enrich_profiles=True)))
        assert again.avatar_calls == []
        assert st.conn.execute("select count(*) from custody_log").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_restricted_users_avatars_are_listed_but_not_downloaded(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        restricted = _user(1, restricted=True, restriction_reason=[
            {"_": "RestrictionReason", "platform": "all", "reason": "porn", "text": "x"}])
        gw = FakeGateway({"users": {1: restricted},
                          "full_user": {1: {**_full(1), "users": [restricted]}},
                          "user_photos": {1: _photos(701)}, "avatar": {701: b"bytes"}})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        assert gw.avatar_calls == []
        assert res.counts["photos"] == 1 and res.counts["restricted_skipped"] == 1 and res.counts["avatars"] == 0


@pytest.mark.asyncio
async def test_full_user_skip_is_counted_spends_budget_and_continues(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        gw = _enrich_gw()
        gw._fx["full_user"][1] = SkipAndRecord("USER_ID_INVALID")
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=2))
        )
        assert gw.full_user_calls == [1, 2]  # the failed attempt still spent budget
        assert res.counts["skipped"] == 1 and res.counts["enriched"] == 1


@pytest.mark.asyncio
async def test_full_profile_disambiguates_a_hidden_photo(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = _full(1, fallback_photo={"_": "Photo", "id": 5}, private_forward_name="Anon")
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        states = json.loads(st.conn.execute("select field_states_json from users where uri='tg:user:1'").fetchone()[0])
        assert states["photo"] == {"state": "hidden_from_you", "why": "fallback_photo"}
        assert states["forwards"] == {"state": "hidden_from_you", "why": "private_forward_name"}
        bundle = json.loads(st.conn.execute(
            "select fields_json from user_snapshots where method='users.getFullUser'").fetchone()[0])
        assert "common_chats_count" not in bundle["full_user"] and "blocked" not in bundle["full_user"]
```

- [ ] **Step 2: Run to verify failure** — the new tests fail (`_enrich` is the Task 6 stub; `full_user_calls == []`).

- [ ] **Step 3: Implement `_enrich`, `_enrichment_candidates`, `_photos`, `_download_avatar`** in `profiles.py`. First add what they need: `import hashlib`, `from datetime import datetime`, `from paperboy.config import profile_dir`, `from paperboy.ids import namespaced_kind` (alongside the existing `channel_uri, user_uri` — those two are removed again in Task 8), and `set_user_photo_sha, upsert_user_photo, user_photo_sha` on the `paperboy.store.users` import; the constants `_USER_PHOTOS_LIMIT = 100` and `METHOD_GET_USER_PHOTOS = "photos.getUserPhotos"`; and the module-level helper

```python
def _seconds_between(earlier: str, later: str) -> float:
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
```

Then replace the stub with:

```python
    # ---- full enrichment (--profiles) -----------------------------------------

    async def _enrich(self, ctx: CollectContext, counts: dict[str, int]) -> None:
        """Spend `profile_budget` `getFullUser` fetches on the highest-priority
        never-enriched users, then wrap to refreshing the stalest (spec §7.1).
        A failed attempt still spends budget: the RPC was made."""
        budget = ctx.settings.profile_budget
        floor = ctx.settings.profile_refresh_after
        now = ctx.clock.now()
        spent = 0
        pass_ = "initial"
        for uri, user_id, enriched_at in self._enrichment_candidates(ctx):
            if spent >= budget:
                break
            if enriched_at is not None:
                pass_ = "refresh"
                if floor is not None and _seconds_between(enriched_at, now) < floor:
                    counts["fresh_skipped"] += 1
                    continue
            ref = input_user_ref(ctx.store, uri)
            if ref is None:
                counts["unresolvable"] += 1
                continue
            spent += 1
            try:
                full = await ctx.gateway.get_full_user(ref)
            except SkipAndRecord as exc:
                ctx.log.warning("profiles: full profile skipped for %s: %s", uri, exc)
                counts["skipped"] += 1
                continue
            observed_at = ctx.clock.for_payload(full)
            raw_id = ctx.store.add_raw(
                namespaced_kind("users", full, "UserFull"), full, ctx.tier,
                {"channel_id": ctx.channel_id, "user_id": user_id, "method": METHOD_GET_FULL_USER},
                observed_at=observed_at,
            )
            full_user = full.get("full_user") or {}
            user = next((u for u in full.get("users", []) if u.get("id") == user_id), None)
            if user is None:
                # `users.UserFull` always carries the target in `users`
                # (research Part 2 §1); a response without it is recorded raw
                # but not projectable — counted, never guessed at.
                counts["skipped"] += 1
                continue
            for chat in full.get("chats", []):
                # e.g. the personal channel (`personal_channel_id`): a full
                # Channel object — a real pivot, worth a peer row.
                self._upsert_peer_keeping_provenance(ctx, chat, raw_id, observed_at)
            if self._project_user(
                ctx, user, raw_id, observed_at, METHOD_GET_FULL_USER, counts, full_user=full_user
            ) is None:
                continue
            counts["refreshed" if enriched_at is not None else "enriched"] += 1
            await self._photos(ctx, uri, user_id, ref, counts)
        if spent >= budget:
            ctx.log.info(
                "profiles: getFullUser budget (%d) spent this run; re-run to keep converging", budget
            )
        self._record_summary(ctx, counts, pass_=pass_)

    def _enrichment_candidates(self, ctx: CollectContext) -> list[tuple[str, int, str | None]]:
        """Every discovered user, in spend order (spec §7/§7.1): never-enriched
        first — admins → authors → commenters → others, then `uri` for
        determinism (replay must make the same choices) — then already-
        enriched users stalest first (the refresh wrap). A user with no
        `users` row yet (its triage batch failed) still gets a turn:
        `getFullUser` triages as a side effect."""
        assert ctx.channel_id is not None
        scope = self._scope_channels(ctx)
        group_id = scope[1] if len(scope) > 1 else None
        rows = ctx.store.conn.execute(
            """
            SELECT p.uri AS uri, p.id AS id, u.enriched_at AS enriched_at,
              CASE
                WHEN EXISTS (SELECT 1 FROM participants pa WHERE pa.uri = p.uri
                             AND pa.status IN ('admin', 'creator')) THEN 0
                WHEN EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                             AND m.channel_id = ?) THEN 1
                WHEN ? IS NOT NULL AND EXISTS (SELECT 1 FROM messages m WHERE m.from_uri = p.uri
                                               AND m.channel_id = ?) THEN 2
                ELSE 3
              END AS rank
            FROM peers p LEFT JOIN users u ON u.uri = p.uri
            WHERE p.kind = 'user'
            ORDER BY (u.enriched_at IS NOT NULL), u.enriched_at, rank, p.uri
            """,
            (ctx.channel_id, group_id, group_id),
        ).fetchall()
        return [(r["uri"], r["id"], r["enriched_at"]) for r in rows]

    async def _photos(
        self, ctx: CollectContext, uri: str, user_id: int, ref: dict, counts: dict[str, int]
    ) -> None:
        """The target's own dated avatar history (research Part 2 §5), then
        each photo's bytes through the media/custody path (plan D12)."""
        try:
            photos = await ctx.gateway.get_user_photos(
                ref, offset=0, max_id=0, limit=_USER_PHOTOS_LIMIT
            )
        except SkipAndRecord as exc:
            ctx.log.warning("profiles: photo history skipped for %s: %s", uri, exc)
            counts["skipped"] += 1
            return
        observed_at = ctx.clock.for_payload(photos)
        raw_id = ctx.store.add_raw(
            namespaced_kind("photos", photos, "Photos"), photos, ctx.tier,
            {"channel_id": ctx.channel_id, "user_id": user_id, "method": METHOD_GET_USER_PHOTOS},
            observed_at=observed_at,
        )
        row = ctx.store.conn.execute(
            "SELECT restriction_json FROM users WHERE uri=?", (uri,)
        ).fetchone()
        restricted = bool(row and row["restriction_json"])
        media_root = profile_dir(ctx.settings, ctx.profile) / "media"
        for photo in photos.get("photos") or []:
            if (photo.get("_") or "").lower() != "photo":
                continue  # PhotoEmpty
            upsert_user_photo(ctx.store, uri, photo, observed_at, raw_id)
            counts["photos"] += 1
            if restricted:
                # The "don't download porno/illegal-flagged by default" rule
                # (spec §9): the history is recorded, the bytes are not fetched.
                counts["restricted_skipped"] += 1
                continue
            if user_photo_sha(ctx.store, uri, photo["id"]) is not None:
                continue  # content-addressed and already on disk: never re-fetched
            await self._download_avatar(ctx, uri, user_id, photo, media_root, counts)

    async def _download_avatar(
        self,
        ctx: CollectContext,
        uri: str,
        user_id: int,
        photo: dict,
        media_root,
        counts: dict[str, int],
    ) -> None:
        try:
            data = await ctx.gateway.download_user_photo(photo)
        except SkipAndRecord as exc:
            ctx.log.warning("profiles: avatar %s skipped for %s: %s", photo["id"], uri, exc)
            counts["skipped"] += 1
            return
        if data is None:
            return
        sha = hashlib.sha256(data).hexdigest()
        path = media_root / sha[:2] / f"{sha}.jpg"  # Telegram re-encodes avatars as JPEG
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        raw_payload = {
            "sha256": sha, "path": str(path), "size": len(data), "user_uri": uri, "photo_id": photo["id"],
        }
        downloaded_at = ctx.clock.for_payload(raw_payload)
        ctx.store.conn.execute(
            "INSERT INTO media (sha256, message_uri, kind, mime_type, size, file_name, "
            "attributes_json, path, downloaded_at) "
            "VALUES (?, NULL, 'avatar', 'image/jpeg', ?, NULL, NULL, ?, ?) "
            "ON CONFLICT(sha256) DO NOTHING",
            (sha, len(data), str(path), downloaded_at),
        )
        ctx.store.conn.execute(
            "INSERT INTO custody_log (path, sha256, recorded_at, source_message_uri) "
            "VALUES (?, ?, ?, NULL)",
            (str(path), sha, downloaded_at),
        )
        ctx.store.add_raw(
            "AvatarDownload", raw_payload, ctx.tier,
            {"channel_id": ctx.channel_id, "user_id": user_id, "photo_id": photo["id"]},
            observed_at=downloaded_at,
        )
        set_user_photo_sha(ctx.store, uri, photo["id"], sha)
        counts["avatars"] += 1
```

- [ ] **Step 4: Run tests to green; full suite + lint/type; commit**

```bash
git add src/paperboy/collectors/profiles.py tests/test_collector_profiles.py
git commit -m "feat(profiles): --profiles full enrichment, priority/budget, resume-to-convergence, photos + avatars (#41)"
```

---
### Task 8: `participants` collector — core (walled broadcast record, linked-group preflight, session gate, `Recent` paging + accounting, projection, `--join` shortfall warning, zero-RPC vectors)

**Files:**
- Create: `src/paperboy/collectors/participants.py`
- Modify: `src/paperboy/collectors/discussion.py` (extract `linked_group(ctx)` and `join_or_skip(ctx, phase, group_id, input_channel)` as module functions; the class delegates to them), `src/paperboy/store/peers.py` (`upsert_full_peer`), `src/paperboy/collectors/profiles.py` (use `upsert_full_peer`), `src/paperboy/collectors/channel.py` (`_pick_channel` → public `pick_channel`, old name kept as alias), `src/paperboy/store/channels.py` (`_channel_flags` → public `channel_flags`, old name kept as alias)
- Test: `tests/test_collector_participants.py`

**Interfaces:**
- Consumes: Tasks 2–5 store writers/gateway; `store.channels.upsert_channel` + `channel_flags`; `store.message_peers.backfill_message_referenced_peers`; `collectors.posture.record_privacy_posture` (Task 6); `doctor.session_age_days`; `Settings.unsafe`/`min_session_age_days`/`allow_join`.
- Produces: `ParticipantsCollector` (`name = "participants"`); `discussion.linked_group(ctx) -> tuple[int, dict, bool] | str`; `discussion.join_or_skip(ctx, phase: str, group_id: int, input_channel: dict) -> str | None`; `store.peers.upsert_full_peer(store, obj, source_raw_id, observed_at) -> str | None` (a full object, stored provenance preserved); `participants.JOIN_SHORTFALL_WARNING`; raw kinds `channels.ChannelParticipants` / `channels.ChannelParticipantsNotModified` (context `{"channel_id", "filter", "offset"}`), `RosterWalled` (payload `{"_": "RosterWalled", "group_id", "reason", "participants_count", "enumerated"}`, context `{"channel_id"}`), the group's `ChatFull` (context `{"channel_id": group_id}`); `run_events` kinds `roster`, `roster_walled`, `admin_only_skipped` (spec §6.5: the admin-only sub-methods detected via rights and skipped, never attempted), `warning` (`code ∈ {"roster_partial", "session_age_gate"}`), `join`, `privacy_posture` (via the shared helper). Counts: `rosters, walled, enumerated, true_count, participants, users, edges, oracle, backfilled_peers, service_joins, service_leaves, reactors, reaction_lists, skipped`.

- [ ] **Step 1: Write the failing tests** — `tests/test_collector_participants.py`

```python
"""The `participants` collector, spec §6: roster discovery within Telegram's walls."""

from __future__ import annotations  # noqa: I001

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from paperboy.collectors.participants import ParticipantsCollector

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.peers import upsert_peer
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77
T0 = "2026-01-01T00:00:00+00:00"
JOINED = 1735689600


def _settings(**over):
    return load_settings("default", {"unsafe": True, **over})


def _ctx(st, gw, settings=None, tier="stranger"):
    return CollectContext(
        gw, st, settings or _settings(), parse_target("@x"),
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, tier, logging.getLogger("t"),
    )


def _seed_channel(st: Store, *, linked: int | None = GROUP_ID, kind: str = "broadcast") -> None:
    raw_id = st.add_raw("ChatFull", {"_": "ChatFull", "full_chat": {"id": CHANNEL_ID}}, "stranger",
                        {"channel_id": CHANNEL_ID}, observed_at=T0)
    chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C", "username": "c"}
    chan["broadcast" if kind == "broadcast" else "megagroup"] = True
    upsert_channel(st, {"_": "channelFull", "id": CHANNEL_ID, "pts": 1, "linked_chat_id": linked,
                        "participants_count": 10}, chan, raw_id, T0)
    if linked:
        upsert_peer(st, {"_": "Channel", "id": linked, "access_hash": 4242, "title": "G", "megagroup": True},
                    raw_id, T0, seen_in_chat=None, seen_in_msg=None)


def _group_full(count: int = 3, *, left: bool = True, hidden: bool = False, users=()) -> dict:
    return {
        "_": "ChatFull",
        "full_chat": {"_": "channelFull", "id": GROUP_ID, "participants_count": count, "pts": 1,
                      "can_view_participants": not hidden, "participants_hidden": hidden},
        "chats": [{"_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "G", "megagroup": True,
                   "left": left}],
        "users": list(users),
    }


def _user(uid: int, **extra) -> dict:
    return {"_": "User", "id": uid, "access_hash": uid * 10, "first_name": f"U{uid}", **extra}


def _member(uid: int, **extra) -> dict:
    return {"_": "ChannelParticipant", "user_id": uid, "date": JOINED + uid, "rank": None,
            "subscription_until_date": None, **extra}


def _page(*participants: dict, count: int | None = None, users=None) -> dict:
    return {"_": "ChannelParticipants", "count": count if count is not None else len(participants),
            "participants": list(participants), "chats": [],
            "users": users if users is not None else [_user(p["user_id"]) for p in participants if "user_id" in p]}


def _gw(pages=None, **more) -> FakeGateway:
    fx = {"full_channel_by_id": {GROUP_ID: _group_full()}}
    if pages is not None:
        fx["participants"] = {GROUP_ID: {"channelParticipantsRecent": pages}}
    fx.update(more)
    return FakeGateway(fx)


@pytest.mark.asyncio
async def test_broadcast_roster_is_a_stored_walled_outcome_and_the_group_is_enumerated(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        admin = {"_": "ChannelParticipantAdmin", "user_id": 1, "promoted_by": 9, "date": JOINED,
                 "admin_rights": {"_": "ChatAdminRights"}, "rank": "mod", "is_self": None, "inviter_id": None}
        gw = _gw([_page(admin, _member(2), _member(3, rank="vip"))])
        res = await ParticipantsCollector().collect(_ctx(st, gw))

        # 1. the broadcast channel's OWN roster: zero enumeration RPC, first-class stored outcome
        assert all(c[0] == GROUP_ID for c in gw.participants_calls)
        assert (CHANNEL_ID, "channelParticipantsRecent", 0) not in gw.participants_calls
        assert gw.calls.count("get_participants") == 1  # exactly the group's one Recent page (§11 zero-RPC)
        walled = st.conn.execute("select payload_json, observed_at from raw_records where kind='RosterWalled'").fetchone()
        payload = json.loads(walled["payload_json"])
        assert payload["group_id"] == CHANNEL_ID and payload["participants_count"] == 10
        assert "broadcast" in payload["reason"]
        assert walled["observed_at"] == T0  # stamped from the ChatFull observation, never "now" (D5)
        acct = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots where group_id=? and uri is null",
            (CHANNEL_ID,)).fetchone()
        assert (acct["enumerated"], acct["true_count"]) == (0, 10) and "broadcast" in acct["reason"]
        assert res.counts["walled"] == 1

        # 2. the linked group: preflight ChatFull recorded, roster paged, projected
        assert gw.calls.count("get_full_channel") == 1
        assert gw.participants_calls == [(GROUP_ID, "channelParticipantsRecent", 0)]
        rows = {r["uri"]: r for r in st.conn.execute("select * from participants where group_id=?", (GROUP_ID,))}
        assert rows["tg:user:1"]["status"] == "admin" and rows["tg:user:1"]["rank"] == "mod"
        assert rows["tg:user:2"]["join_date"] == "2025-01-01T00:00:02+00:00"
        assert rows["tg:user:3"]["rank"] == "vip"
        users = {r["uri"] for r in st.conn.execute("select uri from users")}
        assert users == {"tg:user:1", "tg:user:2", "tg:user:3"}  # the free full User objects
        edges = {(e["subject_uri"], e["predicate"]) for e in st.conn.execute("select subject_uri, predicate from edges")}
        assert ("tg:user:1", "admin_of") in edges and ("tg:user:2", "member_of") in edges
        roster = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"], roster["reason"]) == (3, 3, None)
        assert st.conn.execute("select count(*) from participant_snapshots where group_id=? and uri is not null",
                               (GROUP_ID,)).fetchone()[0] == 3
        group_row = st.conn.execute("select kind, participants_count from channels where id=?", (GROUP_ID,)).fetchone()
        assert (group_row["kind"], group_row["participants_count"]) == ("megagroup", 3)
        kinds = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records where json_extract(context_json,'$.channel_id')=? order by id", (GROUP_ID,))]
        assert kinds == ["ChatFull", "channels.ChannelParticipants"]
        event = json.loads(st.conn.execute("select detail_json from run_events where kind='roster'").fetchone()[0])
        assert (event["group_id"], event["enumerated"], event["true_count"], event["walled"]) == (GROUP_ID, 3, 3, None)
        assert res.stopped is None
        assert res.counts["enumerated"] == 3 and res.counts["participants"] == 3 and res.counts["users"] == 3
        # §6.5: admin-only sub-methods are detected via rights and SKIPPED — a recorded decision
        skipped = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='admin_only_skipped'").fetchone()[0])
        assert skipped["group_id"] == GROUP_ID and "channels.getAdminLog" in skipped["methods"]
        assert st.conn.execute("select count(*) from run_events where kind='privacy_posture'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_no_linked_group_records_the_walled_channel_then_skips_the_phase(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None)
        gw = _gw()
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.stopped is not None and "linked" in res.stopped
        assert gw.participants_calls == [] and gw.calls.count("get_full_channel") == 0
        assert st.conn.execute("select count(*) from raw_records where kind='RosterWalled'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_recent_pages_until_a_short_page_and_labels_the_shortfall(tmp_path):
    # The 78k->12 reality (spec §6.3): a full first page, then a short one,
    # then STOP; enumerated / true_count is recorded, never presented as complete.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        first = _page(*[_member(i) for i in range(1, 201)], count=78000)
        second = _page(*[_member(i) for i in range(201, 213)], count=78000)
        gw = _gw([first, second], full_channel_by_id={GROUP_ID: _group_full(78000)})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == [
            (GROUP_ID, "channelParticipantsRecent", 0), (GROUP_ID, "channelParticipantsRecent", 200),
        ]
        roster = st.conn.execute(
            "select enumerated, true_count from participant_snapshots where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"]) == (212, 78000)
        assert res.stopped is not None and "212 of 78000" in res.stopped and "--join" in res.stopped
        warning = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'").fetchone()[0])
        assert warning["code"] == "roster_partial" and warning["hint"] == "--join"


@pytest.mark.asyncio
async def test_a_repeating_page_ends_the_loop(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        same = _page(*[_member(i) for i in range(1, 201)], count=1000)
        gw = _gw([same, same, same])
        await ParticipantsCollector().collect(_ctx(st, gw))
        assert len(gw.participants_calls) == 2  # page 2 added nothing new -> stop


@pytest.mark.asyncio
async def test_walled_group_is_recorded_with_its_reason_and_the_join_warning(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw(SkipAndRecord("CHAT_ADMIN_REQUIRED"))
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert [w["group_id"] for w in walled] == [CHANNEL_ID, GROUP_ID]
        assert "CHAT_ADMIN_REQUIRED" in walled[1]["reason"]
        roster = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"]) == (0, 3) and "CHAT_ADMIN_REQUIRED" in roster["reason"]
        assert res.stopped is not None and "--join" in res.stopped
        assert res.counts["walled"] == 2


@pytest.mark.asyncio
async def test_session_age_gate_refuses_enumeration_without_unsafe(tmp_path):
    young = {"authorizations": [{"current": True, "date_created": datetime.now(UTC) - timedelta(days=1)}]}
    old = {"authorizations": [{"current": True, "date_created": datetime.now(UTC) - timedelta(days=30)}]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2))], authorizations=young)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.stopped is not None and "--unsafe" in res.stopped
        assert gw.participants_calls == []
        assert gw.calls.count("get_full_channel") == 0  # the gate runs BEFORE any RPC against the group (§6.1)
        assert st.conn.execute("select count(*) from run_events where kind='warning'").fetchone()[0] == 1
        assert json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'").fetchone()[0])["code"] == "session_age_gate"
    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2))], authorizations=old)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.stopped is None and gw.participants_calls != []


@pytest.mark.asyncio
async def test_zero_rpc_vectors_run_even_when_the_gate_refuses(tmp_path):
    young = {"authorizations": [{"current": True, "date_created": datetime.now(UTC)}]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        join = {"_": "MessageService", "id": 40, "date": JOINED, "from_id": {"_": "PeerUser", "user_id": 8},
                "action": {"_": "MessageActionChatJoinedByLink", "inviter_id": 4},
                "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
        upsert_message(st, GROUP_ID, join, st.add_raw("MessageService", join, "stranger", {"channel_id": GROUP_ID}), T0, "stranger")
        gw = _gw([_page(_member(2))], authorizations=young)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.counts["service_joins"] == 1
        assert st.conn.execute("select status from participants where uri='tg:user:8'").fetchone()[0] == "member"
        assert ("tg:user:8", "invited_by", "tg:user:4") in {
            (e[0], e[1], e[2]) for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")}


@pytest.mark.asyncio
async def test_privacy_posture_is_recorded_once_per_run_across_both_person_phases(tmp_path):
    from paperboy.collectors.profiles import ProfilesCollector

    rules = {"_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}], "chats": [], "users": []}
    with Store.open(tmp_path / "p.sqlite") as st:
        st.begin_run("run-1")
        _seed_channel(st)
        gw = _gw([_page(_member(2))], privacy={k: rules for k in ("phone", "lastseen", "photo")})
        ctx = _ctx(st, gw)
        await ParticipantsCollector().collect(ctx)
        await ProfilesCollector().collect(ctx)
        assert gw.calls.count("get_privacy") == 3  # participants recorded it; profiles did not repeat
        assert st.conn.execute("select count(*) from run_events where kind='privacy_posture'").fetchone()[0] == 1
        st.begin_run("run-2")
        await ProfilesCollector().collect(ctx)
        assert gw.calls.count("get_privacy") == 6  # a new run records again


@pytest.mark.asyncio
async def test_preflight_answering_for_another_channel_does_not_enumerate(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        wrong = _group_full()
        wrong["full_chat"]["id"] = 999
        gw = _gw([_page(_member(2))], full_channel_by_id={GROUP_ID: wrong})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == [] and res.counts["skipped"] == 1
        assert st.conn.execute("select count(*) from participants").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_megagroup_target_enumerates_its_own_roster_from_stored_flags(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None, kind="megagroup")
        gw = FakeGateway({"participants": {CHANNEL_ID: {"channelParticipantsRecent": [_page(_member(2), count=10)]}}})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.calls.count("get_full_channel") == 0  # the target's ChatFull is already in `channels`
        assert gw.participants_calls == [(CHANNEL_ID, "channelParticipantsRecent", 0)]
        assert st.conn.execute("select count(*) from raw_records where kind='RosterWalled'").fetchone()[0] == 0
        assert res.counts["rosters"] == 1 and res.counts["enumerated"] == 1


@pytest.mark.asyncio
async def test_phase_stop_without_channel_context(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _ctx(st, _gw())
        ctx.channel_id = None
        with pytest.raises(PhaseStop):
            await ParticipantsCollector().collect(ctx)


def test_applies_to_channel_like_targets():
    assert ParticipantsCollector().applies_to(parse_target("@durov"))
    assert not ParticipantsCollector().applies_to(parse_target("+15551234567"))
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: paperboy.collectors.participants`.

- [ ] **Step 3: Extractions** — in `discussion.py`, lift the bodies of `_linked_group` and `_join_or_skip` into module-level `linked_group(ctx)` and `join_or_skip(ctx, phase, group_id, input_channel)` (identical logic; `self.name` → `phase`), and make the two methods one-line delegates so every existing discussion test passes untouched. In `channel.py` rename `_pick_channel` → `pick_channel` (keep `_pick_channel = pick_channel`); in `store/channels.py` rename `_channel_flags` → `channel_flags` (keep `_channel_flags = channel_flags`) — no cross-module private imports. In `store/peers.py` add:

```python
def upsert_full_peer(store: Store, obj: dict, source_raw_id: int, observed_at: str) -> str | None:
    """`upsert_peer` for a FULL object returned by a profile/roster RPC. A full
    observation carrying no provenance of its own would — correctly, by the
    recency rule — overwrite the stub's `seen_in_chat`/`seen_in_msg` with
    NULLs, losing the only path back to `inputUserFromMessage`. Pass the
    stored provenance through instead."""
    kind, uri, _ = _classify(obj)
    del kind
    row = store.conn.execute(
        "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (uri,)
    ).fetchone()
    return upsert_peer(
        store, obj, source_raw_id, observed_at,
        seen_in_chat=row["seen_in_chat"] if row else None,
        seen_in_msg=row["seen_in_msg"] if row else None,
    )
```

and replace `ProfilesCollector._upsert_peer_keeping_provenance(ctx, obj, raw_id, t)` calls with `upsert_full_peer(ctx.store, obj, raw_id, t)` (delete the static method; drop the now-unused `channel_uri`/`user_uri` imports there).

- [ ] **Step 4: Implement `src/paperboy/collectors/participants.py`** (everything below is final for this task, including the `--join` `Admins ∪ Bots` branch; only the two stub methods `_oracle`/`_reactions` are replaced by Task 9)

```python
"""The `participants` collector: roster discovery for the person layer
(spec §6) — within Telegram's hard walls, passive by default.

For a BROADCAST channel a non-admin can enumerate nothing about subscribers
(research §1.3, settled live: `CHAT_ADMIN_REQUIRED` for every filter), and
joining buys nothing. That wall is a first-class stored outcome — a
`RosterWalled` raw record + a `participant_snapshots` accounting row, with
ZERO enumeration RPC against the channel — never a silent zero. The people
live in the linked discussion supergroup, whose public roster IS enumerable
un-joined (spec §13, live probe): `channels.getParticipants(Recent)` is paged
to the server's depth and `enumerated / true_count` is recorded every run
(spec §6.3 — 200 is a page size, not a total; the real ceiling is Telegram's
and a shortfall is labelled, with the `--join` escalation named, §6.4).

Unioned with zero new RPC: join/leave service messages already in the
captured history (`store.participants.project_join_service_messages`) and
the `recent_reactions` sample inside stored messages
(`store.reactions.backfill_recent_reactions`). Bounded RPC vectors (Task 9):
the `channels.getParticipant` oracle for known users a partial roster
missed, and `messages.getMessageReactionsList` on reacted group messages.

Guardrails: the per-phase session-age gate (spec §6.1) refuses enumeration
on a young session unless `--unsafe`; `--join` joins only a group we have
not joined, through the shared audited `join_or_skip`; admin-only
sub-methods (boosts, invite importers, admin log) are never attempted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.channel import pick_channel
from paperboy.collectors.discussion import join_or_skip, linked_group
from paperboy.collectors.posture import record_privacy_posture
from paperboy.doctor import session_age_days
from paperboy.gateway import FILTER_ADMINS, FILTER_BOTS, FILTER_RECENT
from paperboy.ids import namespaced_kind
from paperboy.store.channels import channel_flags, upsert_channel
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.participants import (
    add_participant_snapshot,
    add_roster_snapshot,
    membership_edges,
    project_join_service_messages,
    upsert_participant,
)
from paperboy.store.peers import upsert_full_peer, upsert_peer
from paperboy.store.reactions import backfill_recent_reactions
from paperboy.store.users import add_user_snapshot, target_user_facts, upsert_user
from paperboy.targets import Target

_PAGE_SIZE = 200  # Telegram's page size, not a total cap (spec §6.3)
METHOD_GET_PARTICIPANTS = "channels.getParticipants"
# Admin-only sub-methods: detected via rights and SKIPPED, never attempted (spec §6.5).
_ADMIN_ONLY_METHODS = (
    "channels.getAdminLog", "premium.getBoostsList", "messages.getChatInviteImporters",
    "channels.getParticipants(channelParticipantsKicked/Banned)",
)

JOIN_SHORTFALL_WARNING = (
    "participants: enumerated {enumerated} of {total} members of group {group_id}; the full "
    "roster requires membership — re-run with --join to join and enumerate (an active, "
    "audited write)"
)


@dataclass
class _Roster:
    """One enumerable group: the linked discussion group, or the target itself
    when it is a supergroup. `stamp`/`source_raw_id` are the ChatFull
    observation that established the flags — every zero-RPC row derived
    from this roster is stamped from them, never from "now" (plan D5)."""

    group_id: int
    input_channel: dict
    flags: dict
    true_count: int | None
    stamp: str
    source_raw_id: int | None
    chan: dict | None  # the group's `Channel` object (its `left` flag drives --join)


class ParticipantsCollector:
    name = "participants"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None or ctx.input_channel is None:
            raise PhaseStop(
                "participants skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "rosters": 0, "walled": 0, "enumerated": 0, "true_count": 0, "participants": 0,
            "users": 0, "edges": 0, "oracle": 0, "backfilled_peers": 0, "service_joins": 0,
            "service_leaves": 0, "reactors": 0, "reaction_lists": 0, "skipped": 0,
        }
        chan = ctx.store.conn.execute(
            "SELECT kind, participants_count, flags_json, last_seen, source_raw_id "
            "FROM channels WHERE id=?",
            (ctx.channel_id,),
        ).fetchone()
        if chan is None:
            raise PhaseStop("participants skipped: no channels row (channel phase did not complete)")
        run_stamp: str = chan["last_seen"]
        target_is_group = chan["kind"] != "broadcast"

        if not target_is_group:
            # §6.2: the broadcast channel's OWN subscriber roster is never
            # enumerable — skip IT (recorded, zero RPC), not the collector.
            self._record_walled(
                ctx, ctx.channel_id, "broadcast_channel: subscriber roster is never enumerable "
                "below admin (CHAT_ADMIN_REQUIRED for every filter)",
                chan["participants_count"], run_stamp, counts,
            )
        linked = linked_group(ctx)
        if isinstance(linked, str):
            if not target_is_group:
                # No comment section => no person vector at all (§2): the one
                # case that is a FULL phase skip.
                return CollectResult(
                    name=self.name, counts=counts,
                    stopped=f"{linked} — no person vector (a broadcast channel's subscribers "
                            "are never enumerable)",
                )
            ctx.log.info("participants: %s", linked)

        # Zero-RPC vectors first — they read only the store, so they run even
        # when the session gate below refuses enumeration.
        group_ids = ([ctx.channel_id] if target_is_group else []) + (
            [] if isinstance(linked, str) else [linked[0]]
        )
        for group_id in group_ids:
            self._zero_rpc_vectors(ctx, group_id, counts)

        # The gate comes BEFORE the first RPC against any group (spec §6.1) —
        # including the linked group's preflight getFullChannel.
        gate = await self._session_gate(ctx)
        if gate is not None:
            ctx.log.warning(gate)
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "warning",
                {"code": "session_age_gate", "message": gate},
            )
            return CollectResult(name=self.name, counts=counts, stopped=gate)
        # The roster's free `users` vectors carry `userStatus*`, so the account's
        # own posture is recorded here too (once per run — a no-op if `profiles`
        # already did it this run; spec §4.3).
        await record_privacy_posture(ctx, self.name)

        rosters: list[_Roster] = []
        if target_is_group:
            rosters.append(_Roster(
                ctx.channel_id, ctx.input_channel, json.loads(chan["flags_json"] or "{}"),
                chan["participants_count"], run_stamp, chan["source_raw_id"], None,
            ))
        if not isinstance(linked, str):
            group_id, input_channel, _needs_join = linked
            roster = await self._preflight_group(ctx, group_id, input_channel, run_stamp, counts)
            if roster is not None:
                rosters.append(roster)

        stopped: list[str] = []
        for roster in rosters:
            reason = await self._enumerate(ctx, roster, counts)
            if reason:
                stopped.append(reason)
        return CollectResult(name=self.name, counts=counts, stopped="; ".join(stopped) or None)

    # ---- preflight + gate --------------------------------------------------------

    async def _preflight_group(
        self, ctx: CollectContext, group_id: int, input_channel: dict, run_stamp: str,
        counts: dict[str, int],
    ) -> _Roster | None:
        """§6.1: `can_view_participants` / `participants_hidden` live on the
        GROUP's channelFull, which no phase has fetched before. Also projects
        the group into `channels` (+ snapshot) and its vectors into peers."""
        try:
            full = await ctx.gateway.get_full_channel(input_channel)
        except SkipAndRecord as exc:
            self._record_walled(ctx, group_id, f"preflight: {exc}", None, run_stamp, counts)
            return None
        observed_at = ctx.clock.for_payload(full)
        # A SECOND `ChatFull` in the same pass. `ReplaySource.runs()` treats
        # `chatfull` rows as opening-cluster markers only for LEGACY (NULL
        # run_id) rows; every record this collector writes is stamped, so this
        # never splits a run — keep that invariant if that branch ever changes.
        raw_id = ctx.store.add_raw(
            full.get("_", "ChatFull"), full, ctx.tier, {"channel_id": group_id}, observed_at=observed_at
        )
        full_chat = full.get("full_chat") or {}
        if full_chat.get("id") != group_id:
            ctx.log.warning(
                "participants: getFullChannel for group %s answered for %s — not enumerating",
                group_id, full_chat.get("id"),
            )
            counts["skipped"] += 1
            return None
        chats = full.get("chats") or []
        try:
            chan = pick_channel(chats, group_id) if chats else None
        except ValueError:
            chan = None
        if chan is not None:
            upsert_channel(ctx.store, full_chat, chan, raw_id, observed_at)
        if not (chan or {}).get("admin_rights") and not (chan or {}).get("creator"):
            # Spec §6.5: admin-only sub-methods (boosts, invite importers, admin
            # log, kicked/banned) are detected via rights and SKIPPED, never
            # attempted — recorded so their absence is a stored decision.
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "admin_only_skipped",
                {"group_id": group_id, "methods": list(_ADMIN_ONLY_METHODS),
                 "reason": "no admin_rights on the group"},
            )
        for obj in chats:
            upsert_peer(ctx.store, obj, raw_id, observed_at, seen_in_chat=None, seen_in_msg=None)
        self._project_users_vector(ctx, full, raw_id, observed_at, counts)
        return _Roster(
            group_id, input_channel, channel_flags(full_chat, chan or {}),
            full_chat.get("participants_count"), observed_at, raw_id, chan,
        )

    async def _session_gate(self, ctx: CollectContext) -> str | None:
        """Spec §6.1 MUST: no participant sweep on a session younger than
        `min_session_age_days` without `--unsafe` — enforced per phase here,
        not only run-level by `doctor`."""
        if ctx.settings.unsafe:
            return None
        try:
            authorizations = await ctx.gateway.get_authorizations()
        except SkipAndRecord as exc:
            return (
                f"participants: roster enumeration refused — session age unknown ({exc}); "
                "pass --unsafe to enumerate anyway"
            )
        age = session_age_days(authorizations)
        minimum = ctx.settings.min_session_age_days
        if age is None or age < minimum:
            shown = "unknown" if age is None else f"{age:.1f} days"
            return (
                f"participants: roster enumeration refused — session age {shown} is below "
                f"min_session_age_days={minimum}; pass --unsafe to enumerate anyway"
            )
        return None

    # ---- zero-RPC vectors -----------------------------------------------------------

    def _zero_rpc_vectors(self, ctx: CollectContext, group_id: int, counts: dict[str, int]) -> None:
        """Everything derivable from the store alone: #11's message-referenced
        peers (so forward origins/mentions become oracle candidates too),
        join/leave service messages, and the `recent_reactions` sample."""
        counts["backfilled_peers"] += backfill_message_referenced_peers(ctx.store, group_id)
        service = project_join_service_messages(ctx.store, group_id, ctx.tier)
        counts["service_joins"] += service["joins"]
        counts["service_leaves"] += service["leaves"]
        counts["edges"] += service["edges"]
        counts["reactors"] += backfill_recent_reactions(ctx.store, group_id, ctx.tier)

    def _record_walled(
        self, ctx: CollectContext, group_id: int, reason: str, true_count: int | None,
        observed_at: str, counts: dict[str, int], *, enumerated: int = 0,
    ) -> None:
        """A walled roster is a stored observation (§6.2): a synthetic raw
        record (so a reproject detects and reproduces it) + the accounting
        row + an audit event. Stamped from the observation that established
        the wall, never from the wall clock (plan D5)."""
        payload = {
            "_": "RosterWalled", "group_id": group_id, "reason": reason,
            "participants_count": true_count, "enumerated": enumerated,
        }
        raw_id = ctx.store.add_raw(
            "RosterWalled", payload, ctx.tier, {"channel_id": group_id}, observed_at=observed_at
        )
        add_roster_snapshot(
            ctx.store, group_id, observed_at, enumerated=enumerated, true_count=true_count,
            reason=reason, source_raw_id=raw_id,
        )
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "roster_walled",
            {"group_id": group_id, "reason": reason, "participants_count": true_count},
        )
        counts["walled"] += 1

    # ---- enumeration ----------------------------------------------------------------

    async def _enumerate(
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]
    ) -> str | None:
        """Page `Recent` (plus `Admins`/`Bots` once joined), record
        `enumerated / true_count`, then run the bounded vectors. Returns the
        §6.4 shortfall warning when the roster came back walled or partial
        and we are not a member — the phase's `stopped` reason."""
        group_id = roster.group_id
        counts["rosters"] += 1
        joined = await self._maybe_join(ctx, roster)
        enumerated: set[str] = set()
        last_stamp, last_raw = roster.stamp, roster.source_raw_id
        true_count = roster.true_count
        walled: str | None = None
        filters = [FILTER_RECENT] + ([FILTER_ADMINS, FILTER_BOTS] if joined else [])
        for filter_ in filters:
            try:
                count, last_stamp, last_raw = await self._page(
                    ctx, roster, filter_, enumerated, counts, last_stamp, last_raw
                )
            except SkipAndRecord as exc:
                if filter_ is FILTER_RECENT:
                    walled = str(exc)
                else:
                    ctx.log.warning(
                        "participants: %s skipped for group %s: %s", filter_["_"], group_id, exc
                    )
                    counts["skipped"] += 1
                continue
            if count is not None and filter_ is FILTER_RECENT:
                true_count = count  # an Admins/Bots page's `count` is only its own filter's
        if walled is not None:
            self._record_walled(
                ctx, group_id, walled, true_count, last_stamp, counts, enumerated=len(enumerated)
            )
        else:
            add_roster_snapshot(
                ctx.store, group_id, last_stamp, enumerated=len(enumerated),
                true_count=true_count, reason=None, source_raw_id=last_raw,
            )
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "roster",
            {"group_id": group_id, "enumerated": len(enumerated), "true_count": true_count,
             "walled": walled, "joined": joined},
        )
        counts["enumerated"] += len(enumerated)
        counts["true_count"] += true_count or 0

        partial = walled is not None or (true_count is not None and len(enumerated) < true_count)
        if partial:
            await self._oracle(ctx, roster, enumerated, counts)
        await self._reactions(ctx, roster, counts)
        if not partial or joined:
            return None
        warning = JOIN_SHORTFALL_WARNING.format(
            enumerated=len(enumerated), total=true_count if true_count is not None else "?",
            group_id=group_id,
        )
        ctx.log.warning(warning)
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "warning",
            {"code": "roster_partial", "group_id": group_id, "enumerated": len(enumerated),
             "true_count": true_count, "walled": walled, "hint": "--join"},
        )
        return warning

    async def _maybe_join(self, ctx: CollectContext, roster: _Roster) -> bool:
        """Under `--join`, join a group we are not a member of (`Channel.left`
        true, or membership unknown) through the shared audited path; a
        refused join falls back to the un-joined branch. Never without the
        flag (plan D11)."""
        if not ctx.settings.allow_join:
            return False
        if roster.chan is not None and roster.chan.get("left") is False:
            return True  # already a member: nothing to write
        skip = await join_or_skip(ctx, self.name, roster.group_id, roster.input_channel)
        if skip is not None:
            ctx.log.warning("participants: %s", skip)
            return False
        return True

    async def _page(
        self, ctx: CollectContext, roster: _Roster, filter_: dict, enumerated: set[str],
        counts: dict[str, int], last_stamp: str, last_raw: int | None,
    ) -> tuple[int | None, str, int | None]:
        """Page one filter until the server stops adding members: an empty
        or short page, or a page that adds nothing new (a capped server
        repeats itself). Returns `(count, stamp, raw_id)` of the last page."""
        offset = 0
        count: int | None = None
        while True:
            page = await ctx.gateway.get_participants(
                roster.input_channel, filter_, offset, _PAGE_SIZE, 0
            )
            last_stamp = ctx.clock.for_payload(page)
            last_raw = ctx.store.add_raw(
                namespaced_kind("channels", page, "ChannelParticipants"), page, ctx.tier,
                {"channel_id": roster.group_id, "filter": filter_["_"], "offset": offset},
                observed_at=last_stamp,
            )
            if (page.get("_") or "").lower().endswith("notmodified"):
                break
            if page.get("count") is not None:
                count = page["count"]
            new = self._project_page(ctx, roster.group_id, page, last_raw, last_stamp, enumerated, counts)
            got = len(page.get("participants") or [])
            if got == 0 or new == 0 or got < _PAGE_SIZE:
                break
            offset += got
        return count, last_stamp, last_raw

    def _project_page(
        self, ctx: CollectContext, group_id: int, page: dict, raw_id: int, stamp: str,
        enumerated: set[str], counts: dict[str, int],
    ) -> int:
        """Spec §6.5 for one page: participants rows + snapshots, member_of/
        admin_of edges, and the free full `User` objects. Returns how many
        participants were NEW to this run's enumerated set."""
        self._project_users_vector(ctx, page, raw_id, stamp, counts)
        for chat in page.get("chats") or []:
            upsert_peer(ctx.store, chat, raw_id, stamp, seen_in_chat=None, seen_in_msg=None)
        new = 0
        for participant in page.get("participants") or []:
            facts = upsert_participant(ctx.store, group_id, participant, raw_id, stamp)
            if facts is None:
                continue
            counts["participants"] += 1
            if facts.uri not in enumerated:
                enumerated.add(facts.uri)
                new += 1
            add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
            counts["edges"] += membership_edges(
                ctx.store, group_id, facts, stamp, ctx.tier, raw_id,
                {"source": "roster", "status": facts.status},
            )
        return new

    def _project_users_vector(
        self, ctx: CollectContext, envelope: dict, raw_id: int, stamp: str, counts: dict[str, int]
    ) -> None:
        """Roster RPCs enrich DURING discovery (spec §3): every full `User` in
        the response's `users` vector lands in `users` (+ snapshot) and
        `peers` (provenance preserved) for free."""
        for user in envelope.get("users") or []:
            if (user.get("_") or "").lower() != "user":
                continue
            upsert_full_peer(ctx.store, user, raw_id, stamp)
            uri = upsert_user(ctx.store, user, raw_id, stamp, ctx.tier)
            if uri is None:
                continue
            counts["users"] += 1
            add_user_snapshot(
                ctx.store, uri, stamp, ctx.tier, METHOD_GET_PARTICIPANTS,
                {"user": target_user_facts(user)}, raw_id,
            )

    async def _oracle(
        self, ctx: CollectContext, roster: _Roster, enumerated: set[str], counts: dict[str, int]
    ) -> None:
        return None  # Task 9

    async def _reactions(self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]) -> None:
        return None  # Task 9
```

(The import block above is exactly Task 8's — `ruff` green as shown; Task 9 Step 3 lists the imports and constants its two methods add.)

- [ ] **Step 5: Run tests to green; full suite + lint/type; commit**

```bash
git add src/paperboy/collectors/participants.py src/paperboy/collectors/discussion.py src/paperboy/collectors/channel.py \
  src/paperboy/collectors/profiles.py src/paperboy/store/peers.py src/paperboy/store/channels.py \
  tests/test_collector_participants.py
git commit -m "feat(participants): walled-broadcast record, linked-group Recent enumeration, accounting, session gate (#41)"
```

---

### Task 9: `participants` collector — the bounded oracle, `--join` (`Admins ∪ Bots`), and reaction lists

**Files:**
- Modify: `src/paperboy/collectors/participants.py`
- Test: `tests/test_collector_participants.py` (append)

**Interfaces:**
- Consumes: `gateway.get_participant` (`None` = not a participant), `gateway.join_channel` via `join_or_skip`, `gateway.get_message_reactions_list`; `Settings.participant_oracle_budget`/`participant_reactions_budget`/`allow_join`; `store.reactions.*`.
- Produces: raw kinds `channels.ChannelParticipant` + `UserNotParticipant` (context `{"channel_id", "user_id"}`), `messages.MessageReactionsList` (context `{"channel_id", "msg_id", "offset"}`); `reacted_to` edges evidenced `{"source": "reactions_list", "reaction", "date"}`; membership edges evidenced `{"source": "oracle", ...}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_collector_participants.py`

```python
def _seed_group_comment(st: Store, msg_id: int, user_id: int, *, reactions: dict | None = None) -> None:
    m = {"_": "Message", "id": msg_id, "message": "c", "date": 1767322445,
         "from_id": {"_": "PeerUser", "user_id": user_id},
         "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if reactions is not None:
        m["reactions"] = reactions
    raw_id = st.add_raw("Message", m, "stranger", {"channel_id": GROUP_ID}, observed_at=T0)
    upsert_message(st, GROUP_ID, m, raw_id, T0, "stranger")
    # what `history._observe_message` does for an author: the min stub with provenance —
    # without a `peers` row `input_user_ref` is None and the oracle has nothing to ask
    upsert_peer(st, {"_": "User", "id": user_id, "min": True}, raw_id, T0,
                seen_in_chat=GROUP_ID, seen_in_msg=msg_id)


def _answer(uid: int) -> dict:
    return {"_": "ChannelParticipant", "participant": _member(uid), "chats": [], "users": [_user(uid)]}


@pytest.mark.asyncio
async def test_oracle_runs_only_on_a_partial_roster_and_is_bounded(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302), (23, 303), (2, 304)):
            _seed_group_comment(st, mid, uid)  # 2 is also in the roster; 21-23 are not
        complete = _gw([_page(_member(2), count=1)],
                       participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}})
        await ParticipantsCollector().collect(_ctx(st, complete))
        assert complete.participant_calls == []  # complete roster: no oracle spend

    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302), (23, 303), (2, 304)):
            _seed_group_comment(st, mid, uid)
        partial = _gw([_page(_member(2), count=307)],
                      participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}})
        res = await ParticipantsCollector().collect(
            _ctx(st, partial, _settings(participant_oracle_budget=2))
        )
        assert partial.participant_calls == [(GROUP_ID, 21), (GROUP_ID, 22)]  # bounded, uri order, 2 excluded
        assert res.counts["oracle"] == 2
        rows = {r["uri"]: r["status"] for r in st.conn.execute("select uri, status from participants")}
        assert rows["tg:user:21"] == "member" and rows["tg:user:22"] == "left"
        assert "tg:user:23" not in rows
        raw = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records where json_extract(context_json,'$.user_id') in (21, 22) order by id")]
        assert raw == ["channels.ChannelParticipant", "UserNotParticipant"]
        edge = st.conn.execute(
            "select evidence_json from edges where subject_uri='tg:user:21' and predicate='member_of'").fetchone()
        assert '"source": "oracle"' in edge["evidence_json"]
        assert st.conn.execute("select count(*) from users where uri='tg:user:21'").fetchone()[0] == 1

        # a later run asks only about users still without an answer
        again = _gw([_page(_member(2), count=307)],
                    participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}})
        await ParticipantsCollector().collect(_ctx(st, again, _settings(participant_oracle_budget=2)))
        assert again.participant_calls == [(GROUP_ID, 23)]


@pytest.mark.asyncio
async def test_oracle_wall_ends_the_oracle_loop_for_that_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302)):
            _seed_group_comment(st, mid, uid)
        gw = _gw([_page(_member(2), count=307)],
                 participant={GROUP_ID: {21: SkipAndRecord("CHAT_ADMIN_REQUIRED"), 22: _answer(22)}})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participant_calls == [(GROUP_ID, 21)]
        assert res.counts["skipped"] == 1 and res.counts["oracle"] == 0


@pytest.mark.asyncio
async def test_join_flag_joins_a_left_group_then_pages_admins_and_bots(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        admins = {"_": "ChannelParticipants", "count": 1, "chats": [], "users": [_user(9)],
                  "participants": [{"_": "ChannelParticipantCreator", "user_id": 9,
                                    "admin_rights": {"_": "ChatAdminRights"}, "rank": "founder"}]}
        bots = _page(_member(30), count=1, users=[_user(30, bot=True)])
        gw = FakeGateway({
            "full_channel_by_id": {GROUP_ID: _group_full(left=True)},
            "participants": {GROUP_ID: {"channelParticipantsRecent": [_page(_member(2), count=3)],
                                        "channelParticipantsAdmins": [admins],
                                        "channelParticipantsBots": [bots]}},
            "join": {"_": "Updates", "updates": []},
        })
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1
        assert [c[1] for c in gw.participants_calls] == [
            "channelParticipantsRecent", "channelParticipantsAdmins", "channelParticipantsBots",
        ]
        assert st.conn.execute("select status from participants where uri='tg:user:9'").fetchone()[0] == "creator"
        assert st.conn.execute("select count(*) from run_events where kind='join'").fetchone()[0] == 1
        assert res.stopped is None  # joined: the shortfall is not a --join warning any more

    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = FakeGateway({
            "full_channel_by_id": {GROUP_ID: _group_full(left=False)},
            "participants": {GROUP_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]}},
        })
        await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 0  # already a member: never re-joined
        assert [c[1] for c in gw.participants_calls][1:] == ["channelParticipantsAdmins", "channelParticipantsBots"]


@pytest.mark.asyncio
async def test_join_never_fires_without_the_flag_and_a_refused_join_falls_back(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2), count=1)], join={"_": "Updates", "updates": []})
        await ParticipantsCollector().collect(_ctx(st, gw))
        assert "join_channel" not in gw.calls
    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2), count=1)], join=SkipAndRecord("INVITE_REQUEST_SENT"))
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1
        assert [c[1] for c in gw.participants_calls] == ["channelParticipantsRecent"]  # un-joined branch
        assert res.counts["enumerated"] == 1


@pytest.mark.asyncio
async def test_reaction_lists_are_bounded_newest_first_and_resumable(tmp_path):
    reacted = {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2, "reaction": {}}]}
    def _list(*uids: int, next_offset=None) -> dict:
        return {"_": "MessageReactionsList", "count": len(uids), "chats": [], "next_offset": next_offset,
                "users": [_user(u) for u in uids],
                "reactions": [{"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": u},
                               "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "🔥"}} for u in uids]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for mid in (401, 402, 403):
            _seed_group_comment(st, mid, 2, reactions=reacted)
        _seed_group_comment(st, 404, 2)  # no reactions: never asked
        gw = _gw([_page(_member(2), count=1)], reactions={GROUP_ID: {
            403: [_list(31, 32, next_offset="p2"), _list(33)], 402: _list(34), 401: _list(35),
        }})
        res = await ParticipantsCollector().collect(
            _ctx(st, gw, _settings(participant_reactions_budget=2))
        )
        assert gw.reactions_calls == [(GROUP_ID, 403, None), (GROUP_ID, 403, "p2"), (GROUP_ID, 402, None)]
        assert res.counts["reaction_lists"] == 3
        edges = {(e[0], e[2]) for e in st.conn.execute(
            "select subject_uri, predicate, object_uri from edges where predicate='reacted_to'")}
        assert edges == {("tg:user:31", "tg:msg:77/403"), ("tg:user:32", "tg:msg:77/403"),
                         ("tg:user:33", "tg:msg:77/403"), ("tg:user:34", "tg:msg:77/402")}
        assert st.conn.execute("select count(*) from users where uri in ('tg:user:31','tg:user:34')").fetchone()[0] == 2
        peer = st.conn.execute("select seen_in_chat, seen_in_msg from peers where uri='tg:user:31'").fetchone()
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 403)

        again = _gw([_page(_member(2), count=1)], reactions={GROUP_ID: {401: _list(35)}})
        await ParticipantsCollector().collect(_ctx(st, again, _settings(participant_reactions_budget=2)))
        assert again.reactions_calls == [(GROUP_ID, 401, None)]  # 403/402 already fetched: resumable


@pytest.mark.asyncio
async def test_reaction_list_wall_is_a_recorded_skip(tmp_path):
    reacted = {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2, "reaction": {}}]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 401, 2, reactions=reacted)
        _seed_group_comment(st, 402, 2, reactions=reacted)
        gw = _gw([_page(_member(2), count=1)],
                 reactions={GROUP_ID: {402: SkipAndRecord("BROADCAST_FORBIDDEN")}})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.reactions_calls == [(GROUP_ID, 402, None)]  # the wall ends the vector
        assert res.counts["skipped"] == 1 and res.stopped is None
```

- [ ] **Step 2: Run to verify failure** — the oracle/join/reaction assertions fail against the Task 8 stubs.

- [ ] **Step 3: Implement `_oracle` and `_reactions`** (replace the two stubs). First add to `participants.py`: `iso_or_none, msg_uri, peer_ref_uri, peer_stub` on the `paperboy.ids` import; `from paperboy.store.edges import add_edge_once`; `ParticipantFacts, write_participant` on the `paperboy.store.participants` import; `input_user_ref` on the `paperboy.store.peers` import; the `paperboy.store.reactions` import becomes `REACTED_TO, backfill_recent_reactions, fetched_reaction_lists, reacted_message_ids`; and the constants `_REACTIONS_PAGE = 100`, `METHOD_GET_PARTICIPANT = "channels.getParticipant"`, `METHOD_REACTIONS = "messages.getMessageReactionsList"`. Then:

```python
    async def _oracle(
        self, ctx: CollectContext, roster: _Roster, enumerated: set[str], counts: dict[str, int]
    ) -> None:
        """`channels.getParticipant` for users REFERENCED in the group (message
        authors, provenance) that a partial/walled roster did not cover and
        that have no answer yet — bounded by `participant_oracle_budget`
        (plan D9), never one call per known commenter. Confirmed un-joined /
        non-admin on the group (spec §13); a `USER_NOT_PARTICIPANT` answer is a
        definitive negative and is stored as such."""
        budget = ctx.settings.participant_oracle_budget
        if budget <= 0:
            return
        group_id = roster.group_id
        rows = ctx.store.conn.execute(
            """
            SELECT DISTINCT uri FROM (
                SELECT from_uri AS uri FROM messages WHERE channel_id = ? AND from_uri LIKE 'tg:user:%'
                UNION
                SELECT uri FROM peers WHERE kind = 'user' AND seen_in_chat = ?
            )
            WHERE uri NOT IN (SELECT uri FROM participants WHERE group_id = ?)
            ORDER BY uri
            """,
            (group_id, group_id, group_id),
        ).fetchall()
        candidates = [r["uri"] for r in rows if r["uri"] not in enumerated][:budget]
        for uri in candidates:
            ref = input_user_ref(ctx.store, uri)
            if ref is None:
                continue
            try:
                answer = await ctx.gateway.get_participant(roster.input_channel, ref)
            except SkipAndRecord as exc:
                # CHAT_ADMIN_REQUIRED here is the wall itself, not a per-user
                # condition — stop asking this group.
                ctx.log.warning("participants: oracle walled on group %s: %s", group_id, exc)
                counts["skipped"] += 1
                return
            counts["oracle"] += 1
            if answer is None:
                payload = {"_": "UserNotParticipant", "user_id": ref["user_id"]}
                stamp = ctx.clock.for_payload(payload)
                raw_id = ctx.store.add_raw(
                    "UserNotParticipant", payload, ctx.tier,
                    {"channel_id": group_id, "user_id": ref["user_id"]}, observed_at=stamp,
                )
                facts = ParticipantFacts(uri, "left", None, None, None, None)
                if write_participant(ctx.store, group_id, facts, raw_id, stamp):
                    add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
                continue
            stamp = ctx.clock.for_payload(answer)
            raw_id = ctx.store.add_raw(
                namespaced_kind("channels", answer, "ChannelParticipant"), answer, ctx.tier,
                {"channel_id": group_id, "user_id": ref["user_id"]}, observed_at=stamp,
            )
            self._project_users_vector(ctx, answer, raw_id, stamp, counts)
            facts = upsert_participant(ctx.store, group_id, answer.get("participant") or {}, raw_id, stamp)
            if facts is None:
                continue
            counts["participants"] += 1
            enumerated.add(facts.uri)
            add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
            counts["edges"] += membership_edges(
                ctx.store, group_id, facts, stamp, ctx.tier, raw_id,
                {"source": "oracle", "status": facts.status},
            )

    async def _reactions(self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]) -> None:
        """`messages.getMessageReactionsList` on reacted GROUP messages —
        newest first, bounded by `participant_reactions_budget`, resumable
        (the done-set is derived from the raw log). Reactors get a
        `reacted_to` edge, a `users` row (the response's `users` vector) and
        a `peers` row with the message as provenance. The first wall
        (`BROADCAST_FORBIDDEN` / `CHAT_ADMIN_REQUIRED`) ends the vector."""
        budget = ctx.settings.participant_reactions_budget
        if budget <= 0:
            return
        group_id = roster.group_id
        done = fetched_reaction_lists(ctx.store, group_id)
        candidates = [m for m in reacted_message_ids(ctx.store, group_id) if m not in done][:budget]
        for msg_id in candidates:
            offset: str | None = None
            while True:
                try:
                    result = await ctx.gateway.get_message_reactions_list(
                        roster.input_channel, msg_id, offset=offset, limit=_REACTIONS_PAGE
                    )
                except SkipAndRecord as exc:
                    ctx.log.warning(
                        "participants: reaction lists skipped for group %s: %s", group_id, exc
                    )
                    counts["skipped"] += 1
                    return
                stamp = ctx.clock.for_payload(result)
                raw_id = ctx.store.add_raw(
                    namespaced_kind("messages", result, "MessageReactionsList"), result, ctx.tier,
                    {"channel_id": group_id, "msg_id": msg_id, "offset": offset or ""},
                    observed_at=stamp,
                )
                counts["reaction_lists"] += 1
                self._project_users_vector(ctx, result, raw_id, stamp, counts)
                for reaction in result.get("reactions") or []:
                    subject = peer_ref_uri(reaction.get("peer_id"))
                    stub = peer_stub(reaction.get("peer_id"))
                    if subject is None or stub is None:
                        continue
                    if upsert_peer(
                        ctx.store, stub, raw_id, stamp, seen_in_chat=group_id, seen_in_msg=msg_id
                    ) is None:
                        continue  # the collecting account reacted (#12)
                    if add_edge_once(
                        ctx.store, subject, REACTED_TO, msg_uri(group_id, msg_id), stamp, ctx.tier,
                        raw_id,
                        {"source": "reactions_list", "reaction": reaction.get("reaction"),
                         "date": iso_or_none(reaction.get("date"))},
                    ):
                        counts["edges"] += 1
                offset = result.get("next_offset")
                if not offset:
                    break
```

- [ ] **Step 4: Run tests to green; full suite + lint/type; commit**

```bash
git add src/paperboy/collectors/participants.py tests/test_collector_participants.py
git commit -m "feat(participants): bounded getParticipant oracle, --join Admins/Bots, bounded reaction lists (#41)"
```

---
### Task 10: Reproject replay support (spec §10) — 7 replay methods, `get_privacy` serving, phase detection, round-trip identity

**Files:**
- Modify: `src/paperboy/replay.py`, `src/paperboy/reproject.py`, `tests/test_reproject.py` (`ROUND_TRIP_EXCLUDE`)
- Test: `tests/test_replay_people.py` (new), `tests/test_reproject_people.py` (new)

**Interfaces:**
- Consumes: the raw kinds/contexts Tasks 6–9 record (D1); `ReplaySource._kind_clause`, `RawReplayGateway._latest`/`_serve`; `reproject.detect_phases`; the collectors.
- Produces: `ReplaySource.has_context_value(run, key: str, value) -> bool` (a context KEY, bound as a `json_extract` path parameter — never interpolated); `RawReplayGateway.get_participants/get_participant/get_users/get_full_user/get_user_photos/download_user_photo/get_message_reactions_list` (served by the contexts in D1; unrecorded → `SkipAndRecord`, except `get_users` → `{"_": "ReplayUnknownUser", "id": i}` per unknown id and `download_user_photo` → `None`); `RawReplayGateway.get_privacy` serves a recorded `account.PrivacyRules` for the key, else the existing `SkipAndRecord`; `detect_phases` adds `participants` / `profiles`; `REPROJECT_TABLES` += `users, user_snapshots, user_photos, participants, participant_snapshots`; `reproject()` builds per-run replay settings (D6) and includes both new collectors; `ROUND_TRIP_EXCLUDE` += `{"users": {"source_raw_id"}, "user_snapshots": {"id", "source_raw_id"}, "user_photos": {"id", "source_raw_id"}, "participants": {"source_raw_id"}, "participant_snapshots": {"id", "source_raw_id"}}`.

- [ ] **Step 1: Write the failing replay-method tests** — `tests/test_replay_people.py`

```python
"""`RawReplayGateway` serves the person layer's raw kinds back (spec §10)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.gateway import FILTER_RECENT
from paperboy.replay import RawReplayGateway, ReplaySource
from paperboy.store.db import Store, dumps

GROUP_ID = 77
IC = {"channel_id": GROUP_ID, "access_hash": 4242}
T1 = "2026-01-01T00:00:01+00:00"
T2 = "2026-01-01T00:00:02+00:00"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "src.sqlite"
    media_root = tmp_path / "media"
    with Store.open(db) as st:
        st.begin_run("r1")
        st.add_raw("User", {"_": "User", "id": 1, "is_self": True}, "self", None, observed_at=T1)
        st.add_raw("ResolvedPeer", {"_": "ResolvedPeer", "peer": {"_": "PeerChannel", "channel_id": 5},
                                    "chats": [], "users": []}, "stranger", {"target": "@x"}, observed_at=T1)
        page = {"_": "ChannelParticipants", "count": 2, "participants": [{"_": "ChannelParticipant", "user_id": 11}],
                "chats": [], "users": []}
        st.add_raw("channels.ChannelParticipants", page, "stranger",
                   {"channel_id": GROUP_ID, "filter": "channelParticipantsRecent", "offset": 0}, observed_at=T1)
        st.add_raw("channels.ChannelParticipant", {"_": "ChannelParticipant", "participant": {"_": "ChannelParticipant", "user_id": 12},
                                                    "chats": [], "users": []}, "stranger",
                   {"channel_id": GROUP_ID, "user_id": 12}, observed_at=T1)
        st.add_raw("UserNotParticipant", {"_": "UserNotParticipant", "user_id": 13}, "stranger",
                   {"channel_id": GROUP_ID, "user_id": 13}, observed_at=T2)
        st.add_raw("User", {"_": "User", "id": 11, "first_name": "A"}, "stranger",
                   {"channel_id": 5, "method": "users.getUsers", "user_id": 11}, observed_at=T1)
        st.add_raw("UserEmpty", {"_": "UserEmpty", "id": 14}, "stranger",
                   {"channel_id": 5, "method": "users.getUsers", "user_id": 14}, observed_at=T1)
        st.add_raw("users.UserFull", {"_": "UserFull", "full_user": {"_": "UserFull", "id": 11, "about": "bio"},
                                      "chats": [], "users": [{"_": "User", "id": 11}]}, "stranger",
                   {"channel_id": 5, "user_id": 11, "method": "users.getFullUser"}, observed_at=T2)
        photos = {"_": "Photos", "photos": [{"_": "Photo", "id": 701, "access_hash": 1, "file_reference": "AQ==",
                                             "date": 1767322445, "dc_id": 2, "sizes": [], "video_sizes": None}],
                  "users": []}
        st.add_raw("photos.Photos", photos, "stranger",
                   {"channel_id": 5, "user_id": 11, "method": "photos.getUserPhotos"}, observed_at=T2)
        sha = "ab" * 32
        avatar_path = media_root / "ab" / f"{sha}.jpg"
        avatar_path.parent.mkdir(parents=True)
        avatar_path.write_bytes(b"jpeg")
        st.add_raw("AvatarDownload", {"sha256": sha, "path": str(tmp_path / "elsewhere" / f"{sha}.jpg"),
                                      "size": 4, "user_uri": "tg:user:11", "photo_id": 701}, "stranger",
                   {"channel_id": 5, "user_id": 11, "photo_id": 701}, observed_at=T2)
        st.add_raw("messages.MessageReactionsList", {"_": "MessageReactionsList", "count": 1, "reactions": [],
                                                     "chats": [], "users": [], "next_offset": "p2"}, "stranger",
                   {"channel_id": GROUP_ID, "msg_id": 40, "offset": ""}, observed_at=T1)
        st.add_raw("account.PrivacyRules", {"_": "account.PrivacyRules", "rules": [], "chats": [], "users": []},
                   "self", {"key": "phone"}, observed_at=T1)
    return db, media_root


def _gateway(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    clock = ReplayClock()
    return RawReplayGateway(src, clock, src.runs()[0]), clock


@pytest.mark.asyncio
async def test_participants_served_by_channel_filter_offset(tmp_path):
    gw, clock = _gateway(tmp_path)
    page = await gw.get_participants(IC, FILTER_RECENT, 0, 200)
    assert page["participants"][0]["user_id"] == 11
    assert clock.for_payload(page) == T1
    with pytest.raises(SkipAndRecord):
        await gw.get_participants(IC, FILTER_RECENT, 200, 200)
    with pytest.raises(SkipAndRecord):
        await gw.get_participants(IC, {"_": "channelParticipantsAdmins"}, 0, 200)


@pytest.mark.asyncio
async def test_participant_oracle_serves_answers_and_definitive_negatives(tmp_path):
    gw, clock = _gateway(tmp_path)
    answer = await gw.get_participant(IC, {"user_id": 12, "access_hash": 1})
    assert answer is not None and answer["participant"]["user_id"] == 12
    assert await gw.get_participant(IC, {"user_id": 13, "access_hash": 1}) is None
    assert clock.for_payload({"_": "UserNotParticipant", "user_id": 13}) == T2
    with pytest.raises(SkipAndRecord):
        await gw.get_participant(IC, {"user_id": 99, "access_hash": 1})


@pytest.mark.asyncio
async def test_get_users_serves_per_id_with_placeholders_for_unknown(tmp_path):
    gw, clock = _gateway(tmp_path)
    users = await gw.get_users([{"user_id": 11, "access_hash": 1}, {"user_id": 14, "access_hash": 1},
                                {"user_id": 99, "access_hash": 1}])
    assert [u["_"] for u in users] == ["User", "UserEmpty", "ReplayUnknownUser"]
    assert users[2]["id"] == 99
    assert clock.for_payload(users[0]) == T1


@pytest.mark.asyncio
async def test_full_user_photos_and_avatar_bytes(tmp_path):
    gw, clock = _gateway(tmp_path)
    full = await gw.get_full_user({"user_id": 11, "access_hash": 1})
    assert full["full_user"]["about"] == "bio" and clock.for_payload(full) == T2
    photos = await gw.get_user_photos({"user_id": 11, "access_hash": 1}, offset=0, max_id=0, limit=100)
    assert photos["photos"][0]["id"] == 701
    # bytes come from THIS source's media root even though the stored path is foreign
    assert await gw.download_user_photo(photos["photos"][0]) == b"jpeg"
    assert await gw.download_user_photo({"id": 999}) is None
    with pytest.raises(SkipAndRecord):
        await gw.get_full_user({"user_id": 99, "access_hash": 1})


@pytest.mark.asyncio
async def test_reaction_lists_by_msg_and_offset_and_privacy_by_key(tmp_path):
    gw, _ = _gateway(tmp_path)
    first = await gw.get_message_reactions_list(IC, 40, offset=None, limit=100)
    assert first["next_offset"] == "p2"
    with pytest.raises(SkipAndRecord):
        await gw.get_message_reactions_list(IC, 40, offset="p2", limit=100)
    assert (await gw.get_privacy("phone"))["rules"] == []
    with pytest.raises(SkipAndRecord):
        await gw.get_privacy("photo")


def test_has_context_value(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    run = src.runs()[0]
    assert src.has_context_value(run, "method", "users.getUsers")
    assert not src.has_context_value(run, "method", "nope")
```

- [ ] **Step 2: Write the failing round-trip tests** — `tests/test_reproject_people.py`

```python
"""Spec §11: round-trip identity extends to the person layer — collect ->
reproject -> users/participants/snapshots/photos identical, one and two runs,
--profiles and triage-only."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.participants import ParticipantsCollector
from paperboy.collectors.profiles import ProfilesCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_reproject import assert_round_trip

runner = CliRunner()
CHANNEL_ID = 5
GROUP_ID = 77
PHASES = ["channel", "history", "discussion", "participants", "profiles", "graph"]


def _user(uid: int, **extra) -> dict:
    return {"_": "User", "id": uid, "access_hash": uid * 10, "first_name": f"U{uid}", "username": f"u{uid}",
            "phone": None, "photo": None, "status": {"_": "UserStatusRecently", "by_me": None},
            "restriction_reason": [], "usernames": [], **extra}


def _photo(pid: int) -> dict:
    return {"_": "Photo", "id": pid, "access_hash": 1, "file_reference": "AQ==", "date": 1767322445, "dc_id": 2,
            "sizes": [{"_": "PhotoSize", "type": "x", "w": 640, "h": 640, "size": 1}], "video_sizes": None}


def people_fixtures() -> dict:
    chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 99, "title": "C", "username": "c", "broadcast": True}
    group = {"_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "C Chat", "megagroup": True, "left": True}
    reacted = {"_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
               "recent_reactions": [{"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 12},
                                     "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}}]}
    return {
        "self": {"_": "user", "id": 1, "self": True},
        "resolve": {"peer": {"_": "PeerChannel", "channel_id": CHANNEL_ID}, "chats": [chan], "users": []},
        "full_channel": {"full_chat": {"_": "channelFull", "id": CHANNEL_ID, "participants_count": 10, "pts": 1,
                                       "linked_chat_id": GROUP_ID}, "chats": [chan, group], "users": []},
        "full_channel_by_id": {GROUP_ID: {"full_chat": {"_": "channelFull", "id": GROUP_ID, "participants_count": 3,
                                                        "pts": 1, "can_view_participants": True,
                                                        "participants_hidden": False},
                                          "chats": [group], "users": []}},
        "history": [
            {"_": "message", "id": 3, "message": "comment", "date": 1767322445,
             "from_id": {"_": "PeerUser", "user_id": 12}, "reactions": reacted,
             "reply_to": {"_": "MessageReplyHeader", "reply_to_msg_id": 2, "reply_to_top_id": 2}},
            {"_": "message", "id": 2, "message": "", "date": 1767322445,
             "fwd_from": {"_": "MessageFwdHeader", "channel_post": 1,
                          "from_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}}},
            {"_": "message", "id": 1, "message": "post", "date": 1767322400,
             "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 15}}},
        ],
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
        "get_messages": {},
        "participants": {GROUP_ID: {"channelParticipantsRecent": [
            {"_": "ChannelParticipants", "count": 3, "chats": [], "users": [_user(11), _user(13)],
             "participants": [{"_": "ChannelParticipant", "user_id": 11, "date": 1735689600, "rank": None,
                               "subscription_until_date": None},
                              {"_": "ChannelParticipantAdmin", "user_id": 13, "promoted_by": 13, "date": 1735689600,
                               "admin_rights": {"_": "ChatAdminRights"}, "rank": "mod", "is_self": None,
                               "inviter_id": None}]},
        ]}},
        "participant": {GROUP_ID: {12: None, 15: {"_": "ChannelParticipant", "chats": [], "users": [_user(15)],
                                                  "participant": {"_": "ChannelParticipant", "user_id": 15,
                                                                  "date": 1735689700, "rank": None,
                                                                  "subscription_until_date": None}}}},
        "reactions": {GROUP_ID: {3: {"_": "MessageReactionsList", "count": 1, "chats": [], "next_offset": None,
                                     "users": [_user(12)],
                                     "reactions": [{"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 12},
                                                    "date": 1767322500,
                                                    "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}}]}}},
        "users": {11: _user(11), 12: _user(12, phone="+15550001212"), 13: _user(13), 15: _user(15)},
        "full_user": {
            11: {"_": "UserFull", "chats": [], "users": [_user(11)],
                 "full_user": {"_": "UserFull", "id": 11, "about": "bio 11", "common_chats_count": 0,
                               "fallback_photo": {"_": "Photo", "id": 5}}},
            12: {"_": "UserFull", "chats": [], "users": [_user(12, phone="+15550001212")],
                 "full_user": {"_": "UserFull", "id": 12, "about": None, "common_chats_count": 1}},
            13: {"_": "UserFull", "chats": [], "users": [_user(13)],
                 "full_user": {"_": "UserFull", "id": 13, "about": "bio 13", "common_chats_count": 0}},
            15: {"_": "UserFull", "chats": [], "users": [_user(15)],
                 "full_user": {"_": "UserFull", "id": 15, "about": None, "common_chats_count": 0}},
        },
        "user_photos": {11: {"_": "PhotosSlice", "count": 1, "photos": [_photo(701)], "users": []}},
        "avatar": {701: b"jpeg-bytes"},
        "privacy": {k: {"_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}],
                        "chats": [], "users": []} for k in ("phone", "lastseen", "photo")},
        "channel_recommendations": {"_": "messages.chats", "chats": []},
        "sponsored_messages": {"_": "messages.sponsoredMessagesEmpty"},
        "chat_invite": {},
    }


async def run_people_collect(data_dir: Path, *, enrich: bool = True, mutate=None) -> Path:
    settings = load_settings("default", {"data_dir": data_dir, "unsafe": True, "enrich_profiles": enrich})
    db = data_dir / "default" / "paperboy.sqlite"
    fixtures = people_fixtures()
    if mutate is not None:
        fixtures = mutate(fixtures)
    collectors = [ChannelCollector(), HistoryCollector(), DiscussionCollector(), ParticipantsCollector(),
                  ProfilesCollector(), GraphCollector()]
    with Store.open(db) as store:
        await collect_channel(FakeGateway(fixtures), store, settings, parse_target("@c"), phases=PHASES,
                              log=logging.getLogger("people"), collectors=collectors)
    return db


def _reproject(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    return tmp_path / "default" / "paperboy.reprojected.sqlite"


def test_people_round_trip_identity(tmp_path, monkeypatch):
    db1 = asyncio.run(run_people_collect(tmp_path))
    src = sqlite3.connect(db1)
    # the fixture really exercised every vector before we trust the identity
    assert src.execute("select count(*) from participants").fetchone()[0] == 4  # 11, 13 roster; 12 left; 15 oracle
    assert src.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 4
    assert src.execute("select count(*) from user_photos where sha256 is not null").fetchone()[0] == 1
    assert src.execute("select count(*) from edges where predicate='reacted_to'").fetchone()[0] == 1
    assert src.execute("select count(*) from raw_records where kind='RosterWalled'").fetchone()[0] == 1
    src.close()
    out = _reproject(tmp_path, monkeypatch)
    assert_round_trip(db1, out)


def test_two_run_people_round_trip(tmp_path, monkeypatch):
    asyncio.run(run_people_collect(tmp_path))

    def second(fx: dict) -> dict:
        fx = {**fx, "users": {**fx["users"], 11: _user(11, first_name="Renamed")}}
        fx["user_photos"] = {11: {"_": "PhotosSlice", "count": 2, "photos": [_photo(702), _photo(701)], "users": []}}
        fx["avatar"] = {**fx["avatar"], 702: b"new-avatar"}
        return fx

    db1 = asyncio.run(run_people_collect(tmp_path, mutate=second))
    src = sqlite3.connect(db1)
    assert src.execute("select count(*) from user_snapshots where uri='tg:user:11' and method='users.getUsers'").fetchone()[0] == 2
    assert src.execute("select count(*) from user_photos where uri='tg:user:11'").fetchone()[0] == 2
    src.close()
    assert_round_trip(db1, _reproject(tmp_path, monkeypatch))


def test_triage_only_source_reprojects_triage_only(tmp_path, monkeypatch):
    db1 = asyncio.run(run_people_collect(tmp_path, enrich=False))
    out = _reproject(tmp_path, monkeypatch)
    assert_round_trip(db1, out)
    rep = sqlite3.connect(out)
    assert rep.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 0
    assert rep.execute("select count(*) from raw_records where kind='users.UserFull'").fetchone()[0] == 0
    rep.close()


def test_detect_phases_sees_the_person_layer(tmp_path):
    db = asyncio.run(run_people_collect(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default" / "media")
    phases = detect_phases(src, src.runs()[0])
    assert phases.index("participants") == phases.index("discussion") + 1
    assert phases.index("profiles") == phases.index("participants") + 1
```

- [ ] **Step 3: Run to verify failure** — `AttributeError: 'RawReplayGateway' object has no attribute 'get_participants'`, `has_context_value`, etc.

- [ ] **Step 4: Implement in `replay.py`**

`ReplaySource`:

```python
    def has_context_value(self, run: ReplayRun, key: str, value: object) -> bool:
        """Whether any raw record in `run` carries `value` under context
        `key` (e.g. `method` = `users.getUsers`) — for phase detection of
        kinds that are NOT distinctive on their own (a `User` record is also
        the self marker). The path is a bound parameter, like every other
        query in this module."""
        return self.conn.execute(
            "SELECT 1 FROM raw_records WHERE json_extract(context_json, ?) = ? "
            "AND id BETWEEN ? AND ? LIMIT 1",
            (f"$.{key}", value, run.lo, run.hi),
        ).fetchone() is not None
```

`RawReplayGateway` (replace `get_privacy`; add the seven):

```python
    async def get_privacy(self, key: str) -> dict:
        # `profiles` records the account's own posture per run (spec §4.3);
        # doctor's own reads are still never recorded.
        row = self._latest(("account.privacyrules", "privacyrules"),
                           "json_extract(context_json, '$.key') = ?", (key,))
        if row is None:
            raise SkipAndRecord(
                "replay: privacy posture not recorded for this run; reproject never runs doctor"
            )
        return self._serve(row)

    async def get_participants(
        self, input_channel: dict, filter: dict, offset: int, limit: int, hash_: int = 0
    ) -> dict:
        del limit, hash_
        row = self._latest(
            ("channels.channelparticipants", "channels.channelparticipantsnotmodified"),
            "json_extract(context_json, '$.channel_id') = ? "
            "AND json_extract(context_json, '$.filter') = ? "
            "AND json_extract(context_json, '$.offset') = ?",
            (input_channel["channel_id"], filter.get("_"), offset),
        )
        if row is None:
            raise SkipAndRecord(
                f"replay: no {filter.get('_')} page at offset {offset} recorded for "
                f"channel {input_channel['channel_id']}"
            )
        return self._serve(row)

    async def get_participant(self, input_channel: dict, participant: dict) -> dict | None:
        row = self._latest(
            ("channels.channelparticipant", "usernotparticipant"),
            "json_extract(context_json, '$.channel_id') = ? "
            "AND json_extract(context_json, '$.user_id') = ?",
            (input_channel["channel_id"], participant["user_id"]),
        )
        if row is None:
            raise SkipAndRecord(
                f"replay: no getParticipant answer recorded for user {participant['user_id']}"
            )
        payload = self._serve(row)
        # The definitive negative was stored as a synthetic record (plan D4);
        # served so the clock has its stamp, then returned as the None it was.
        return None if (payload.get("_") or "").lower() == "usernotparticipant" else payload

    async def get_users(self, refs: list[dict]) -> list[dict]:
        self._clock.begin_batch()
        out: list[dict] = []
        for ref in refs:
            row = self._latest(
                ("user", "userempty"),
                "json_extract(context_json, '$.method') = 'users.getUsers' "
                "AND json_extract(context_json, '$.user_id') = ?",
                (ref["user_id"],),
            )
            if row is None:
                # D4.1's analogue: a placeholder the collector ignores — never
                # a synthetic UserEmpty, which would fabricate a "deleted
                # account" observation the original run never made.
                out.append({"_": "ReplayUnknownUser", "id": ref["user_id"]})
                continue
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            out.append(json.loads(row["payload_json"]))
        return out

    async def get_full_user(self, ref: dict) -> dict:
        row = self._latest(("users.userfull",),
                           "json_extract(context_json, '$.user_id') = ?", (ref["user_id"],))
        if row is None:
            raise SkipAndRecord(f"replay: no UserFull recorded for user {ref['user_id']}")
        return self._serve(row)

    async def get_user_photos(self, ref: dict, *, offset: int, max_id: int, limit: int) -> dict:
        del offset, max_id, limit
        row = self._latest(("photos.photos", "photos.photosslice"),
                           "json_extract(context_json, '$.user_id') = ?", (ref["user_id"],))
        if row is None:
            raise SkipAndRecord(f"replay: no photo history recorded for user {ref['user_id']}")
        return self._serve(row)

    async def download_user_photo(self, photo: dict) -> bytes | None:
        row = self._latest(("avatardownload",),
                           "json_extract(context_json, '$.photo_id') = ?", (photo["id"],))
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        sha = payload["sha256"]
        path = Path(payload["path"])
        if not path.exists():  # noqa: ASYNC240 — same rationale as download_media
            path = self._src.media_root / sha[:2] / f"{sha}.jpg"
        if not path.exists():
            raise SkipAndRecord(f"replay: avatar file missing for sha {sha}")
        data = path.read_bytes()
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return data

    async def get_message_reactions_list(
        self, input_channel: dict, msg_id: int, *, offset: str | None, limit: int
    ) -> dict:
        del limit
        row = self._latest(
            ("messages.messagereactionslist",),
            "json_extract(context_json, '$.channel_id') = ? "
            "AND json_extract(context_json, '$.msg_id') = ? "
            "AND json_extract(context_json, '$.offset') = ?",
            (input_channel["channel_id"], msg_id, offset or ""),
        )
        if row is None:
            raise SkipAndRecord(
                f"replay: no reaction list recorded for message {msg_id} at offset {offset!r}"
            )
        return self._serve(row)
```

- [ ] **Step 5: `reproject.py`** — import `ParticipantsCollector`/`ProfilesCollector`; extend `REPROJECT_TABLES` with `"users", "user_snapshots", "user_photos", "participants", "participant_snapshots"`; in `detect_phases` after the discussion block:

```python
    if source.has_kind(
        run, "channels.channelparticipants", "channels.channelparticipant", "rosterwalled",
        "usernotparticipant", "messages.messagereactionslist",
    ):
        phases.append("participants")
    if source.has_context_value(run, "method", "users.getUsers") or source.has_kind(run, "users.userfull"):
        phases.append("profiles")
```

(keep `graph`/`web`/`media` detection after these). In `reproject()`, replace the single `replay_settings` with a per-run copy inside the run loop (plan D6):

```python
        replay_settings = settings.model_copy(update={
            "allow_join": True, "unsafe": True,
            "enrich_profiles": source.has_kind(run, "users.userfull"),
            "profile_budget": 10**9, "participant_oracle_budget": 10**9,
            "participant_reactions_budget": 10**9,
        })
```

and add `ParticipantsCollector(), ProfilesCollector()` between `DiscussionCollector()` and `GraphCollector()` in the collectors list. Update the module docstring's phase list.

- [ ] **Step 6: `tests/test_reproject.py`** — extend `ROUND_TRIP_EXCLUDE` with the five new tables (values in **Interfaces**).

- [ ] **Step 7: Run the two new files, then the full suite + lint/type; commit**

The existing `test_round_trip_identity`/`test_two_run_round_trip_identity` (durov fixtures) must still pass: their `run_full_collect` does not select the new phases yet (Task 11 does), so nothing new is recorded or detected.

```bash
git add src/paperboy/replay.py src/paperboy/reproject.py tests/test_reproject.py tests/test_replay_people.py tests/test_reproject_people.py
git commit -m "feat(reproject): replay the person layer — 7 replay methods, phase detection, round-trip identity (#41)"
```

---

### Task 11: Recipe / CLI / progress / status wiring, default-set gating tests, parity golden regeneration

**Files:**
- Modify: `src/paperboy/recipes.py`, `src/paperboy/cli.py`, `src/paperboy/progress.py`, `tests/test_recipe.py`, `tests/test_integration_discussion.py`, `tests/test_reproject_parity.py` (+ regenerated `tests/fixtures/reproject/parity_golden.json`), `tests/test_cli.py`
- Test: `tests/test_integration_people.py` (new)

**Interfaces:**
- Produces: default collector order `channel, history, discussion, participants, profiles, graph` (+ `web`, `media` opt-in); `paperboy collect` flags `--profiles`, `--profile-interval SECONDS`, `--profile-refresh-after DURATION`; `--unsafe` also sets `Settings.unsafe`; `--phases` accepts `participants`/`profiles` (both `channel`-dependent); `paperboy status` shows `users` and `participants` counts; `progress.phase_status` for the two phases.

- [ ] **Step 1: Write the failing tests**

Replace the expected lists in `tests/test_recipe.py::test_default_collectors_web_is_opt_in`:

```python
    assert [c.name for c in _default_collectors(include_media=False, include_web=False)] == [
        "channel", "history", "discussion", "participants", "profiles", "graph",
    ]
    assert [c.name for c in _default_collectors(include_media=False, include_web=True)] == [
        "channel", "history", "discussion", "participants", "profiles", "graph", "web",
    ]
```

and fix `test_collect_channel_media_flag_opts_in`'s expected names to `["channel", "history", "discussion", "participants", "profiles", "graph", "media"]`. Add:

```python
def test_person_layer_is_default_on_right_after_discussion():
    # Design spec §6 recipe slot: channel -> history -> discussion ->
    # participants -> profiles -> graph. Both are DEFAULT-ON (person-layer spec §1).
    names = [c.name for c in _default_collectors(include_media=False, include_web=False)]
    assert names.index("participants") == names.index("discussion") + 1
    assert names.index("profiles") == names.index("participants") + 1
```

In `tests/test_integration_discussion.py` extend `_GATEWAY_READ_METHODS` with `"get_participants", "get_participant", "get_users", "get_full_user", "get_user_photos", "download_user_photo", "get_message_reactions_list"`.

`tests/test_integration_people.py`:

```python
"""Default-set + --profiles gating (spec §11), read-only guardrail, CLI wiring."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter

import pytest
from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app as cli_app
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_integration_discussion import _GATEWAY_READ_METHODS
from tests.test_reproject_people import GROUP_ID, people_fixtures

runner = CliRunner()


@pytest.mark.asyncio
async def test_plain_collect_runs_both_person_phases_but_never_getfulluser(tmp_path):
    gw = FakeGateway(people_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path, "unsafe": True}),
            parse_target("@c"), phases=None, log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == ["channel", "history", "discussion", "participants", "profiles", "graph"]
        assert gw.full_user_calls == [] and gw.user_photos_calls == [] and gw.avatar_calls == []
        assert gw.calls.count("get_users") >= 1  # triage IS default-on
        assert st.conn.execute("select count(*) from participants").fetchone()[0] >= 2
        warning = st.conn.execute(
            "select detail_json from run_events where phase='profiles' and kind='warning'").fetchone()
        assert json.loads(warning["detail_json"])["code"] == "profiles_enrichment_off"
        assert set(gw.calls) <= _GATEWAY_READ_METHODS | {"join_channel"} and "join_channel" not in gw.calls


@pytest.mark.asyncio
async def test_profiles_setting_enables_the_full_sweep(tmp_path):
    gw = FakeGateway(people_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path, "unsafe": True, "enrich_profiles": True}),
            parse_target("@c"), phases=None, log=logging.getLogger("t"),
        )
        assert Counter(gw.calls)["get_full_user"] == 4
        assert st.conn.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 4


def _patch_gateway(monkeypatch, captured: dict) -> None:
    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        captured["settings"] = settings
        return FakeGateway(people_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)


def test_cli_profiles_flags_reach_settings(tmp_path, monkeypatch):
    captured: dict = {}
    _patch_gateway(monkeypatch, captured)
    result = runner.invoke(cli_app, [
        "collect", "@c", "--profile", "people1", "--unsafe",
        "--phases", "channel,history,discussion,participants,profiles",  # people to enrich need the sweeps
        "--profiles", "--profile-budget", "3", "--profile-interval", "2.5", "--profile-refresh-after", "7d",
    ], env={"PAPERBOY_DATA_DIR": str(tmp_path)})
    assert result.exit_code == 0, result.stdout
    s = captured["settings"]
    assert (s.enrich_profiles, s.profile_budget, s.profile_interval, s.profile_refresh_after, s.unsafe) == (
        True, 3, 2.5, 7 * 86400, True,
    )
    assert "--profiles" in result.stdout  # the console names the expensive sweep it is about to run
    db = sqlite3.connect(tmp_path / "people1" / "paperboy.sqlite")
    assert db.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 3
    db.close()


def test_cli_rejects_a_bad_refresh_duration(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, {})
    result = runner.invoke(cli_app, ["collect", "@c", "--profile", "people2", "--unsafe",
                                     "--profile-refresh-after", "7x"], env={"PAPERBOY_DATA_DIR": str(tmp_path)})
    assert result.exit_code != 0 and "7x" in result.output


def test_cli_participants_alone_is_rejected_and_with_channel_works(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, {})
    alone = runner.invoke(cli_app, ["collect", "@c", "--profile", "people3", "--phases", "participants", "--unsafe"],
                          env={"PAPERBOY_DATA_DIR": str(tmp_path)})
    assert alone.exit_code == 1 and "channel" in alone.stdout.lower()
    ok = runner.invoke(cli_app, ["collect", "@c", "--profile", "people4", "--phases", "channel,participants", "--unsafe"],
                       env={"PAPERBOY_DATA_DIR": str(tmp_path)})
    assert ok.exit_code == 0, ok.stdout
    db = sqlite3.connect(tmp_path / "people4" / "paperboy.sqlite")
    assert db.execute("select count(*) from participants where group_id=?", (GROUP_ID,)).fetchone()[0] >= 2
    db.close()
    status = runner.invoke(cli_app, ["status", "--profile", "people4"], env={"PAPERBOY_DATA_DIR": str(tmp_path)})
    assert status.exit_code == 0 and "participants" in status.stdout and "users" in status.stdout
```

- [ ] **Step 2: Run to verify failure** — recipe list mismatch; unknown CLI options; `status` lacks the rows.

- [ ] **Step 3: `recipes.py`** — import both collectors; `_default_collectors` becomes `[ChannelCollector(), HistoryCollector(), DiscussionCollector(), ParticipantsCollector(), ProfilesCollector(), GraphCollector()]` (+ web/media as before); update the module docstring and the `_default_collectors` comment: "`participants` (roster) and `profiles` (cheap `getUsers` triage; full enrichment only under `--profiles`) are default-on: read-only and bounded."

- [ ] **Step 4: `cli.py`**

In `collect`, add options after `web`:

```python
    profiles: bool = typer.Option(
        False, "--profiles",
        help="Run FULL profile enrichment (getFullUser, photo history, avatar download) on top of "
             "the always-on getUsers triage — ~1 RPC/s, bounded by --profile-budget.",
    ),
    profile_interval: float = typer.Option(
        None, "--profile-interval", help="Seconds between full-profile RPCs (default: Budget's 1.0s).",
    ),
    profile_refresh_after: str = typer.Option(
        None, "--profile-refresh-after",
        help="Skip re-enriching users enriched more recently than this (e.g. 7d, 12h, 30m).",
    ),
```

and in the body:

```python
    if profiles:
        overrides["enrich_profiles"] = True
    if profile_interval is not None:
        overrides["profile_interval"] = profile_interval
    if profile_refresh_after is not None:
        try:
            overrides["profile_refresh_after"] = parse_duration(profile_refresh_after)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--profile-refresh-after") from None
    if unsafe:
        overrides["unsafe"] = True
```

and right after the existing `settings = load_settings(profile, overrides)` line (so the banner shows the effective budget, default or overridden):

```python
    if settings.enrich_profiles:
        console.print(
            "[yellow]--profiles enabled: full profile enrichment (getFullUser, photo history, "
            f"avatars) will run for up to {settings.profile_budget} users this run.[/]"
        )
```

Update `--phases` help to `channel,history,discussion,participants,profiles,graph,web,media`; add `"participants", "profiles"` to the `_dependent_phases` tuple; make `_run_collect` read `settings.unsafe` (drop its `unsafe` parameter and the argument at the call site); import `parse_duration` from `paperboy.config`. In `status`'s profile-wide branch add:

```python
            table.add_row("users", str(count("SELECT count(*) FROM users")))
            table.add_row("participants", str(count("SELECT count(*) FROM participants")))
```

- [ ] **Step 5: `progress.py`** — in `phase_status`:

```python
        if phase == "participants":
            return f"{count('SELECT count(*) FROM participants')} members"
        if phase == "profiles":
            enriched = count("SELECT count(*) FROM users WHERE enriched_at IS NOT NULL")
            return f"{count('SELECT count(*) FROM users')} users · {enriched} enriched"
```

- [ ] **Step 6: Parity golden — extend and regenerate ONCE, reviewed.** In `tests/test_reproject_parity.py` add `"users", "user_snapshots", "user_photos", "participants", "participant_snapshots"` to `PARITY_TABLES` and `"participants", "profiles"` to `run_full_collect`'s `phases` list (after `"discussion"`), then:

```bash
UPDATE_GOLDEN=1 TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_reproject_parity.py -q --basetemp=/Volumes/Storage/tmp/pytest
git diff --stat tests/fixtures/reproject/parity_golden.json
```

Review the diff before committing — it must be **purely additive**: one `RosterWalled` raw record + one `participant_snapshots` accounting row for channel 5 (the durov fixture has no linked group), `run_events` rows for `participants` (complete, stopped "no linked discussion group …"), `profiles` (complete), `roster_walled`, `privacy_posture`, `warning` (`profiles_enrichment_off`), and one `sync_state` `profiles` row. No pre-existing row may change. Then confirm the existing round trips still hold with the new phases detected: `TMPDIR=/Volumes/Storage/tmp uv run pytest tests/test_reproject.py -q --basetemp=/Volumes/Storage/tmp/pytest`.

- [ ] **Step 7: Full suite + lint/type; commit**

```bash
git add src/paperboy/recipes.py src/paperboy/cli.py src/paperboy/progress.py tests/test_recipe.py \
  tests/test_integration_discussion.py tests/test_integration_people.py tests/test_reproject_parity.py \
  tests/fixtures/reproject/parity_golden.json
git commit -m "feat(cli): participants+profiles default-on, --profiles/--profile-interval/--profile-refresh-after, status rows (#41)"
```

---
### Task 12: Documentation, follow-up issues, and the live DoD smoke (main thread)

**Files:**
- Create: `docs/features/person-layer.md`, `docs/adr/0006-person-layer-storage.md`
- Modify: `CLAUDE.md` (status line, commands, settled decisions), `README.md` (one paragraph under the phases/flags summary)

**Interfaces:**
- Produces: the DoD report for `single-feature-run` (`~/.claude/reference/definition-of-done.md` contract) with the live transcript embedded in `docs/features/person-layer.md`; two GitHub follow-up issues (referenced from the doc's Known limitations).

- [ ] **Step 1: `docs/adr/0006-person-layer-storage.md`** (template: `~/.claude/rules/adr-format.md`; status accepted 2026-08-27). Decisions recorded, each with the one-line why: (a) profile richness lives in `users`/`user_snapshots`/`user_photos`, never `peers` (keeps the #38/#39 lattice untouched; `peers` remains the min-provenance stub table); (b) tri-state is `present | absent | hidden_from_you` in `field_states_json` — absence never proves "not set" (user decision, D2), with the disambiguators listed; (c) the synthetic raw kinds `RosterWalled` / `UserNotParticipant` / `AvatarDownload` (precedent `MediaDownload`) so walled and negative observations are raw-first and replayable; (d) `users.enriched_at` is the convergence cursor (D3); (e) `reacted_to` joins the edge vocabulary (D8); (f) the seven gateway methods and why `USER_NOT_PARTICIPANT` is a `None` return, not a `SkipAndRecord` (D4); (g) replay settings are per run with lifted budgets (D6). Consequences: schema migration 0004; default-on cost profile (triage ≈ ceil(N/100) RPCs; `--profiles` ≈ 2 RPCs + downloads per user); Mentions deferred (issue link).

- [ ] **Step 2: `docs/features/person-layer.md`** — same shape as `docs/features/reproject.md`: Purpose; Inputs (`--profiles`, `--profile-budget`, `--profile-interval`, `--profile-refresh-after`, `--join`, `--unsafe`, `--phases participants,profiles`, the four `PAPERBOY_*` env knobs); Outputs (the five tables, the edge predicates, `raw_records` kinds, `run_events` kinds, `sync_state('profiles', …)`); How it works (pointers to the two collectors' module docstrings and `store/users.py`'s — no paraphrase, per the reproject doc's "single source of truth" rule); Edge cases handled (walled broadcast, walled/partial group + `--join` warning, session gate, bisected triage batch, `USER_NOT_PARTICIPANT`, `personal_photo`, `by_me`, restricted avatars, stale provenance); Known limitations: Mentions filter deferred (issue #), reactions never on broadcasts, avatar downloads sequential (no media-DC parallelism yet), multi-target replay scoping of per-user records (D14), `not_set` is never recorded (there is no honest way to), `channelParticipantsMentions`/gifts/pinned stories/common chats (spec §7 step 3 parentheticals) not collected — gifts and pinned stories have no gateway method in §5, common chats are a fact about us; then the **Definition-of-Done smoke transcript** section filled by Step 5.

- [ ] **Step 3: `CLAUDE.md` + `README.md`** — status line: "Phase 2 collectors: `discussion`/`graph`/`media`/`web`/`participants`/`profiles` are implemented (person layer shipped on `feat/person-layer`, #41)"; commands: add `--profiles [--profile-budget N] [--profile-interval S] [--profile-refresh-after D]` to `collect`; settled decisions: one bullet "Profile richness lives in `users`/`user_snapshots`, never `peers`; tri-state fields are `present | absent | hidden_from_you` (ADR-0006)". README: one paragraph naming the two phases and that `--profiles` is the expensive opt-in.

- [ ] **Step 4: Follow-up issues** (`gh issue create`, both labelled as Phase-2 follow-ups of #41): (1) "participants: `channelParticipantsMentions(top_msg_id)` per thread root — deferred; verify first whether it returns authors of comments deleted before capture (the only non-redundant yield)"; (2) "reproject: per-user person-layer records are served by user_id within a run — a multi-target run can serve a sibling target's observation (plan D14)".

- [ ] **Step 5: Live DoD smoke — MAIN THREAD ONLY (Keychain), real `default` archive.** Run by the orchestrating session after the implementer's tasks are merged into the branch, never by a sub-agent. Read-only throughout; no `--join`. Paste each command, stdout excerpt, exit code, and the observed rows into `docs/features/person-layer.md`; any bug found is fixed on the branch in the same change (no-shed) and the affected steps re-run.

```bash
# 0. preflight + baseline counts
uv run paperboy doctor --profile default
uv run paperboy status --profile default
# 1. roster: expect RosterWalled for the broadcast (zero enumeration RPC against it), NRM Chat
#    Recent enumerated (expect ~307/307 — or a labelled shortfall + the --join warning), member_of/
#    admin_of edges, join dates, the group's own `channels` row, service-message joins, reaction
#    lists bounded to 200 messages, run_events roster/roster_walled/privacy rows
uv run paperboy collect @national_resistance_movement --profile default --phases channel,participants
sqlite3 data/default/paperboy.sqlite "select group_id, enumerated, true_count, reason from participant_snapshots where uri is null order by id desc limit 3;"
sqlite3 data/default/paperboy.sqlite "select status, count(*) from participants group by status;"
sqlite3 data/default/paperboy.sqlite "select predicate, count(*) from edges where predicate in ('member_of','admin_of','invited_by','added_by','reacted_to') group by predicate;"
# 2. triage only (default): every user peer triaged in ceil(N/100) getUsers calls; ZERO getFullUser;
#    the "pass --profiles" warning on the console and in run_events; min stubs now resolvable
uv run paperboy collect @national_resistance_movement --profile default --phases channel,profiles
sqlite3 data/default/paperboy.sqlite "select count(*), sum(enriched_at is not null) from users;"
sqlite3 data/default/paperboy.sqlite "select kind, detail_json from run_events where phase='profiles' order by id desc limit 3;"
# 3. full enrichment, small budget: 5 getFullUser + 5 getUserPhotos + avatar downloads, tri-state
#    states populated (look for a hidden_from_you with its `why`), user_snapshots per method,
#    user_photos with sha256, media rows kind='avatar', custody_log rows with NULL source_message_uri
uv run paperboy collect @national_resistance_movement --profile default --phases channel,profiles --profiles --profile-budget 5 --profile-interval 1.5
sqlite3 data/default/paperboy.sqlite "select uri, status_kind, json_extract(field_states_json,'$.photo'), json_extract(field_states_json,'$.status') from users where enriched_at is not null;"
sqlite3 data/default/paperboy.sqlite "select count(*) from user_photos where sha256 is not null; select count(*) from media where kind='avatar';"
# 4. convergence: a second --profiles run enriches the NEXT 5 (different users), then a
#    --profile-refresh-after 30d run enriches nobody already fresh
uv run paperboy collect @national_resistance_movement --profile default --phases channel,profiles --profiles --profile-budget 5
uv run paperboy collect @national_resistance_movement --profile default --phases channel,profiles --profiles --profile-budget 5 --profile-refresh-after 30d
sqlite3 data/default/paperboy.sqlite "select value_json from sync_state where scope='profiles';"
# 5. error paths: participants alone rejected; bad duration rejected
uv run paperboy collect @national_resistance_movement --profile default --phases participants ; echo "exit=$?"
uv run paperboy collect @national_resistance_movement --profile default --profile-refresh-after 7x ; echo "exit=$?"
# 6. reproject the archive: row counts of the five new tables match, zero network
uv run paperboy reproject --profile default --out /Volumes/Storage/tmp/people-reprojected.sqlite
# 7. guardrails: no third-party phone in the log, no write RPC recorded, no join
grep -c '"phone"' data/default/paperboy.log ; grep -c joinChannel data/default/paperboy.log
sqlite3 data/default/paperboy.sqlite "select count(*) from run_events where kind='join';"
```

Expected observations to record explicitly: the `enumerated / true_count` numbers for NRM Chat; whether `Recent` paging depth on a 307-member group is complete (spec §13's first refinement); the oracle's spend (0 if the roster was complete); the reaction-list count; the triage batch count; the five enriched users' tri-state states; that `--profile-interval 1.5` visibly paces `users.getFullUser` in the log timestamps.

- [ ] **Step 6: DoD report** (the `single-feature-run` gate input) in the contract's structure — Changes (by file), Tests (unit/integration counts, `ruff`, `pyright`), Smoke transcript (Step 5, embedded in the feature doc), Bugs found and fixed, Deferred (the two issues). Then commit:

```bash
git add docs/features/person-layer.md docs/adr/0006-person-layer-storage.md CLAUDE.md README.md
git commit -m "docs(person-layer): feature doc with DoD smoke transcript, ADR-0006, status (#41)"
```

---

## Self-review (performed while writing)

**1. Spec coverage — every section maps to a task:**

| Spec | Task(s) |
|---|---|
| §1 recipe slot, both default-on, `profiles` triage default / full behind `--profiles` + warning | 6, 11 |
| §2/§3 enrichment during (roster `users` vectors) vs after (`inputUserFromMessage`) | 8 (`_project_users_vector`), 4 + 6 (`input_user_ref` case 2) |
| §4 `users`, `user_snapshots`, `participants`, `participant_snapshots` (+ `user_photos`, D7) | 1, 2, 3 |
| §4.3 tri-state as a storage shape, disambiguators, own privacy posture, never ingest facts-about-us | 2 (`field_states`, `target_*_facts`), 6 (`posture.record_privacy_posture`, once per run, called by both 6 and 8) |
| §5 gateway methods + `_input_user` (three cases) | 4 (7 methods per the user's decision; `input_user_ref` + `_input_user`/`_input_peer_user`) |
| §6.1 preflight on the group's `channelFull`; per-phase session-age gate | 8 |
| §6.2 broadcast walled record (zero RPC); un-joined `Recent`; bounded oracle; service messages; reactions; `--join` → `Recent ∪ Admins ∪ Bots`; full skip only with no linked group | 8, 9, 3 |
| §6.3 200 is a page size; `enumerated / true_count` every run | 8 (`_page`, `add_roster_snapshot`) |
| §6.4 `--join` shortfall warning: console + `run_events` + `stopped` | 8 |
| §6.5 projection: per-status join date, rank, subscription; `member_of`/`admin_of`; free `User` objects; admin-only sub-methods detected via rights and skipped | 3, 8 (`admin_only_skipped` `run_events` row in `_preflight_group`) |
| §7 steps 1–5, §7.1 resume-to-convergence, §7.2 knobs through `Budget` | 6, 7, 5, 11 |
| §8 edges: `member_of`/`admin_of`/`invited_by`/`added_by`; #11 fix | 3 (+ `reacted_to`, D8) |
| §9 guardrails (no `suggestBirthday`, no broadcast reactors, session gate, third-party phone stored, restricted avatars, `CHAT_ADMIN_REQUIRED`/`BROADCAST_FORBIDDEN` skips) | 4 (errors), 7 (D12), 8, 9 |
| §10 replay support + phase detection | 10 |
| §11 every listed test: round-trip, fixtures incl. `BaseException` walls, zero-RPC broadcast assertion, `_input_user` cases, tri-state disambiguators, session gate, roster accounting + warning, `invited_by`/`added_by`, resume-to-convergence (three runs), default-set + `--profiles` gating, parameterization (`--profile-interval` through `Budget`, refresh floor, budget), the §13 RPC shapes (`ChatAdminRequiredError` / `UserNotParticipantError` fixtures), DoD smoke | 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 |
| §13 resolved gates encoded in fixtures | 8 (`Recent` un-joined succeeds), 9 (`get_participant` answers + `SkipAndRecord` wall) |
| §14 branch/issue/main-thread smoke | 0, 12 |

**Spec items deliberately not built, each surfaced:** `channelParticipantsMentions` per thread root (user decision — follow-up issue); "displayed gifts, pinned stories, common chats" in §7 step 3 (no §5 method for gifts/stories; common chats are a fact about us — noted in the feature doc). The spec's `not_set` label is replaced by `absent` (user decision, D2); the spec's `last_seen`-ordered refresh is replaced by `enriched_at` (D3, unimplementable as written because triage bumps `last_seen`).

**2. Placeholder scan:** no "TBD/TODO/implement later/similar to Task N"; every code step carries its code; the two intentional deferrals inside a task ("Task 7 fills in …", "Task 9 adds …") name the exact methods and the later task's step that supplies them.

**3. Type/name consistency (checked across tasks):** `ParticipantFacts` field order `(uri, status, join_date, rank, subscription_until_date, inviter_id)` is used identically in Tasks 3, 8, 9; `input_user_ref` returns the two ref shapes `_input_user`/`_input_peer_user` accept (Task 4) and the collectors pass (6, 7, 9); raw contexts `{"channel_id", "filter", "offset"}` / `{"channel_id", "user_id"}` / `{"channel_id", "method", "user_id"}` / `{"channel_id", "msg_id", "offset"}` / `{"channel_id", "user_id", "photo_id"}` / `{"key"}` written in 6–9 are exactly what `RawReplayGateway` (10) and `detect_phases` (10) query; `FakeGateway` fixture keys in 4 match every test in 6–11; `Settings` fields added in 5 are the ones read in 6–9 and set by the CLI in 11; `upsert_full_peer` is introduced in Task 8 and the Task 6 static method it replaces is deleted there (Task 6's tests do not reference the helper by name, so they stay green); `pick_channel` (public) is introduced in Task 8 with `_pick_channel` kept as an alias for `channel.py`'s own callers.

**4. Reviewer gates:** each task ends in one commit and is independently rejectable; `single-feature-run` runs Sonnet for implementation and Opus (adversarial + correctness) for review per repo convention; the live smoke (Task 12 Step 5) is executed on the main thread by the orchestrating session, not by the implementer sub-agent.

