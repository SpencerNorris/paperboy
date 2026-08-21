# paperboy — design spec

**Date:** 2026-08-20 · **Status:** approved in conversation; awaiting written
sign-off · **Research basis:** `docs/research/telegram-extraction-surface.md`

## 1. Purpose and scope

paperboy is a local, read-only CLI that collects everything obtainable about a
Telegram **channel or supergroup** into one SQLite database for open-source
investigation, and is architected as an **entity graph** so that later
recipes (user dossier, phone lookup, watchlists, network maps) are thin
additions rather than rewrites.

**v1 (this spec's plans):** the `collect-channel` recipe end to end —
channel metadata with snapshots, full message history with edit revisions,
deletion tombstones and counter time series, `pts`-based incremental sync,
a passive watch loop, media, linked-discussion comments, people discovery
and budgeted full profiles, graph edges, `t.me/s/` + Wayback snapshots,
JSONL/CSV/RDF export, and an operational-security preflight. v1 is delivered
in phases (§11); the **core** (Phase 1) is channel + history + sync + export +
doctor; the remaining collectors are Phase 2.

**vNext (designed for, not built):** `dossier-user`, `lookup phone`,
hashtag/geo story search, multi-seed network maps, watchlists, third-party
indexer enrichment.

**Out of scope permanently:** anything that acts on Telegram (send, react,
vote, invite, add members), `contacts.getLocated`, phone *enumeration*,
AI-training export, circumvention of privacy settings.

## 2. Constraints and guardrails

**Posture.** A read-only, rate-respecting third-party client reading public
content as the user's own account. Telegram's Content Licensing Terms make
non-ordinary use a ToS gray zone; the realistic sanction is account
limitation. Guardrails are product requirements enforced in code, not
documentation:

| Rule | Level |
|---|---|
| Read-only: never send, react, vote, type, mark read, request to join, `suggestBirthday` | MUST |
| Passive by default: no joining; `--join` is explicit and prints what it exposes | MUST |
| One MTProto session per auth key; parallelism only on media DCs | MUST |
| Every RPC passes through `Budget` (per-method pacing, persisted cooldowns, daily cap) | MUST |
| `FLOOD_WAIT` ≤ threshold → sleep; > threshold → record + stop phase; `PEER_FLOOD`, `FROZEN_METHOD_INVALID`, `AUTH_KEY_DUPLICATED` → hard stop | MUST |
| Participant sweeps refused on sessions younger than `min_session_age_days` (7) without `--unsafe` | MUST |
| Outbound HTTP only to allow-listed hosts (`t.me`, `web.archive.org`), through the configured proxy; never fetch URLs found in collected content | MUST |
| Credentials never logged; logs reference targets by id; exports scrub the collecting account | MUST |
| Optional user fields are tri-state (present / not-set / hidden-from-you) | MUST |
| Admin-only methods detected via rights and skipped, not attempted | SHOULD |
| Phone lookup (`importContacts` → snapshot at stranger tier → `deleteContacts`), single numbers, budgeted | flag-gated |
| Joining via private invite link | flag-gated + operator asserts authorisation |
| Poll-voter collection (requires casting a vote), `contacts.getLocated`, add-member/invite, AI export | EXCLUDED |

## 3. Operational security

Threat model: hide the investigator from **targets** (fully achievable —
reading is invisible; joining is the exposure event), be **pseudonymous to
Telegram** (phone + IP are the controllable surface; Telegram retains IP and
device info ~12 months and discloses phone + IP on valid legal requests), and
leak nothing to **third parties**.

Tool-side controls: `paperboy doctor` preflight (privacy keys via
`account.getPrivacy`, 2FA via `account.getPassword`, minimal profile, session
age via `account.getAuthorizations`, proxy presence, stable device identity);
`require_proxy`; SOCKS5/MTProxy support; stable generic `device_model` /
`system_version` / `app_version` (never impersonating an official client);
session + `api_hash` in the macOS Keychain via `keyring`; named **profiles**
(one account per investigation compartment, separate DB and media); no read
acknowledgements ever; network allow-list; no OSINT-bot integrations;
redacted logs; self-scrubbed exports. Human-side steps (number acquisition —
Fragment +888 or cash SIM; registering through the proxy; aging; privacy
checklist) live in `docs/opsec.md`. Data lives on an encrypted volume;
SQLCipher is not used (Datasette compatibility not investigated — ADR-0002).

## 4. Architecture

```
 CLI (Typer)
  └─ Recipe (collect-channel): ordered Collectors + Budget + resume cursors
       ├─ Collector.collect(ctx) ──► Gateway (Telethon behind a Protocol; returns to_dict() dicts)
       │                              └─ Budget.call(method, fn) — pacing, cooldowns, hard stops
       ├─ WebClient (allow-listed httpx) ──► t.me/s, web.archive.org CDX
       └─ Store (SQLite, WAL): RawLog ─► projections (entities / history / edges) + SyncState
  └─ Exporters: views over Store (jsonl, csv, rdf/turtle, datasette metadata.json)
  └─ Doctor: preflight over Gateway + Config
```

- **Targets** (`targets.py`): parse `@name`, `t.me/name`, `t.me/+hash`,
  `t.me/joinchat/…`, `t.me/name/123`, `t.me/c/123/456`, numeric ids,
  `+phone`, `#hashtag` into a typed `Target`. v1 acts on channel/group
  targets only; others parse but raise `UnsupportedTarget`.
- **Gateway** (`gateway.py`): `Gateway` Protocol + `TelethonGateway`. Every
  method returns plain dicts (`TLObject.to_dict()`), never Telethon types, so
  collectors are testable with `FakeGateway` fed by recorded JSON fixtures.
  `TelethonGateway` routes every RPC through `Budget.call`.
- **Budget** (`budget.py`): `Policy` (flags + limits), per-method minimum
  interval, `flood_log`-persisted cooldowns, per-run RPC cap, hard-stop
  mapping. Raises `HardStop` / `PhaseStop`.
- **Collectors** (`collectors/`): `Collector` Protocol —
  `name: str`, `applies_to(kind) -> bool`, `async collect(ctx) -> CollectResult`.
  Idempotent and resumable; read/write cursors via `SyncState`.
- **Recipe** (`recipes.py`): `collect_channel` — `channel → history →
  discussion → participants → profiles → media → graph → web`; `watch` runs
  `history.catch_up()` in a loop. Phases are selectable; each commits at
  segment boundaries.
- **Store** (`store/`): see §5.
- **Exporters** (`export/`), **Doctor** (`doctor.py`), **Config**
  (`config.py`: pydantic-settings, `~/.config/paperboy/<profile>/config.toml`,
  `PAPERBOY_*` env), **Secrets** (`secrets.py`: keyring), **Logging**
  (`logging_setup.py`: JSON file + rich console, `RedactionFilter`).

## 5. Data model

One SQLite file per profile: `<data_dir>/<profile>/paperboy.sqlite`; media at
`<data_dir>/<profile>/media/<sha256[:2]>/<sha256><ext>`. WAL mode, foreign
keys on, explicit `migrations/NNNN_*.sql` tracked in `schema_migrations`.
All timestamps are ISO-8601 UTC text. Entity ids are URI strings:
`tg:user:<id>`, `tg:channel:<id>`, `tg:chat:<id>`, `tg:msg:<channel_id>/<msg_id>`.
Visibility `tier` ∈ `stranger | member | contact | admin | self`.

**Raw (system of record):** `raw_records(id, kind, observed_at, tier,
context_json, payload_json)` — every TL object as received. Projections
carry `source_raw_id` and can be rebuilt from raw after a layer bump.

**Entities (current state):** `channels(id, uri, username, title, about,
kind[broadcast|megagroup|gigagroup|forum], created_at, linked_chat_id,
participants_count, flags_json, restriction_json, source_raw_id,
first_seen, last_seen)`; `peers(uri PK, kind, id, access_hash, is_min,
seen_in_chat, seen_in_msg, username, first_name, last_name, flags_json,
source_raw_id, first_seen, last_seen)`; `messages(uri PK, channel_id, msg_id,
date, edit_date, from_uri, post_author, text, entities_json, media_kind,
media_json, fwd_json, reply_to_msg_id, reply_to_top_id, grouped_id,
via_bot_id, is_service, action_json, content_hash, deleted_at,
source_raw_id, first_seen, last_seen)`; `media(sha256 PK, message_uri,
kind[photo|document], mime_type, size, file_name, attributes_json, path,
downloaded_at, exif_json)`; `users(...)` and `participants(...)` arrive in
Phase 2 with the same conventions.

**History (append-only):** `channel_snapshots(channel_id, observed_at,
participants_count, online_count, title, username, about_hash, source_raw_id)`;
`message_revisions(message_uri, observed_at, edit_date, content_hash, text,
entities_json, media_json, source_raw_id)`; `message_metrics(message_uri,
observed_at, views, forwards, replies, reactions_json)`;
`message_tombstones(message_uri, observed_at, evidence[update|gap|empty])`;
`user_snapshots`, `participant_snapshots` (Phase 2).

**Edges (triple-shaped):** `edges(subject_uri, predicate, object_uri,
observed_at, tier, source_raw_id, evidence_json)` with predicates
`forwarded_from, linked_group, mentions, replied_to, member_of, admin_of,
commented_on, recommended_with, invited_via, gifted_to`.

**Sync:** `sync_state(scope, key, value_json)` for `pts` and phase cursors;
`sync_ranges(channel_id, lo, hi)` — verified-complete message-id ranges (gaps
= candidates for `channels.getMessages` probing); `flood_log(method, until,
seconds, recorded_at)`.

**Search & Datasette:** `messages_fts` (FTS5, external-content on
`messages.text`, triggers maintain it); `metadata.json` emitted by `export
--format datasette` with labels and facets (`channel_id`, `date`,
`media_kind`, `from_uri`). **Custody:** `custody_log(path, sha256,
recorded_at, source_message_uri)` for every downloaded file.

## 6. Collectors

| Collector | Phase | What it does | Key methods |
|---|---|---|---|
| `channel` | 1 | Resolve target; `getFullChannel`; upsert channel + snapshot; record linked group, `participants_hidden`, `can_view_participants`, admin rights of self | `contacts.resolveUsername`, `channels.getFullChannel`, `channels.getParticipant(self)` |
| `history` | 1 | Backfill newest→oldest in 100-message pages into `sync_ranges`; probe gaps with `channels.getMessages` (≤200 ids) → tombstones `evidence=empty`; `catch_up()` via `updates.getChannelDifference` from stored `pts` → new messages, revisions, tombstones `evidence=update`; metrics row on every observation; `min` peers recorded with provenance; `forwarded_from` edges | `messages.getHistory`, `channels.getMessages`, `updates.getChannelDifference`, `messages.getMessagesViews` |
| `discussion` | 2 | Bulk `getHistory` on `linked_chat_id`, bucket by `reply_to_top_id`; `commented_on` edges; `recent_repliers` | `messages.getDiscussionMessage`, `messages.getHistory` |
| `participants` | 2 | Branch on `participants_hidden`; `Recent ∪ Search(prefix sweep, multi-charset) ∪ Admins ∪ Bots ∪ Mentions`; `channelParticipant.date`, `rank`; `member_of`/`admin_of` edges; snapshots | `channels.getParticipants`, `channels.getParticipant` |
| `profiles` | 2 | Batched `getUsers` triage for all discovered peers; full profile (`getFullUser`, `getUserPhotos`, displayed gifts, pinned stories, common chats) prioritised admins → authors → commenters → others, bounded by `profile_budget` (default 2000/run); tri-state fields; `user_snapshots` | `users.getUsers`, `users.getFullUser`, `photos.getUserPhotos`, `payments.getSavedStarGifts`, `stories.getPinnedStories` |
| `media` | 2 | Download documents byte-exact (EXIF pass), photos flagged re-encoded, dedup by sha256, custody log, refetch on expired `file_reference`, parallel on media DCs only | `upload.getFile` via Telethon |
| `graph` | 2 | `recommended_with` (similar channels + true `count`), `mentions` from entities, invite-link previews without joining, giveaway co-sponsors, sponsored messages (`sponsor_info`) | `channels.getChannelRecommendations`, `messages.checkChatInvite`, `messages.getSponsoredMessages` |
| `web` | 2 | `t.me/s/<name>` pages (paged by `?before=`) and Wayback CDX enumeration + snapshot fetch into `web_snapshots`; diff against `messages` for deleted-post recovery; polite pacing | HTTPS only, allow-listed |
| `watch` | 2 | Loop `history.catch_up()` + periodic `channel` snapshot + `getMessagesViews` refresh; passive (no join); ≤10 channels | as `history` |

## 7. Sync and resume semantics

- `pts` seeded from `channelFull.pts`; `catch_up` applies `channelDifference`
  (`new_messages`, `other_updates`: edit/delete/pin) and handles
  `channelDifferenceTooLong` by re-seeding `pts` and scheduling a gap probe.
- A message observed with a new `content_hash` appends a `message_revisions`
  row and updates the entity; identical content updates `last_seen` only.
- Deletion evidence ranks: `update` (unambiguous) > `empty` (`messageEmpty`
  for an id inside a previously verified-complete range) > `gap` (never
  observed; may be hidden/service). `deleted_at` is set only for `update` and
  `empty`.
- Every collector commits at segment boundaries and records a cursor in
  `sync_state`; Ctrl-C is safe; re-running continues.

## 8. Error handling

RPC errors map to: **retry** (transient network, `FLOOD_WAIT` ≤ threshold),
**skip-and-record** (`CHAT_ADMIN_REQUIRED`, `CHANNEL_PRIVATE`,
`MSG_ID_INVALID`, `PREMIUM_ACCOUNT_REQUIRED`, `BROADCAST_FORBIDDEN` → logged
with method + target, phase continues), **phase stop** (`FLOOD_WAIT` >
threshold → cooldown persisted, other phases continue), **hard stop**
(`PEER_FLOOD`, `FROZEN_METHOD_INVALID`, `AUTH_KEY_DUPLICATED`,
`SESSION_REVOKED` → run ends, message points at @SpamBot / re-auth). Every
error is written to `run_events` with context. No exception is swallowed;
nothing is cast to None to proceed.

## 9. CLI and configuration

```
paperboy auth     [--profile P]                       # login → session in Keychain
paperboy doctor   [--profile P] [--strict]            # opsec preflight; FAIL blocks collect unless --unsafe
paperboy collect  TARGET [--profile P] [--phases a,b] [--join] [--profile-budget N] [--max-rpc N] [--unsafe]
paperboy watch    TARGET [--profile P] [--interval S]
paperboy status   [TARGET] [--profile P]
paperboy export   TARGET --format jsonl|csv|rdf|datasette [--out PATH] [--profile P]
paperboy lookup   phone +E164 [--profile P] --i-understand-the-risk     # Phase 2, flag-gated
```
Config precedence: CLI > env (`PAPERBOY_*`) > `config.toml` > defaults.
Settings: `api_id`, `data_dir`, `proxy` (`socks5://…` | `mtproxy://…`),
`require_proxy` (default true), `device` (model/system/app strings),
`min_session_age_days` (7), `flood_sleep_threshold` (60), `max_rpc_per_run`
(20000), `profile_budget` (2000), `allow_join` (false), `allow_phone_lookup`
(false). Secrets (`api_hash`, session) only in keyring.

## 10. Testing and Definition of Done

- **Unit**: pure logic (targets, ids, tri-state, content hash, pts arithmetic,
  range/gap math, redaction, allow-list, policy decisions) — fast, no I/O.
- **Store**: real SQLite in `tmp_path`; migrations apply from empty; projections
  round-trip recorded fixtures; revisions/tombstones/metrics behave per §7.
- **Collectors**: `FakeGateway` replaying `tests/fixtures/tl/*.json`
  (recorded `to_dict()` output from the Phase 0 spike, scrubbed of the
  collecting account).
- **CLI**: Typer `CliRunner` for `--help`, error paths, `status`.
- **DoD smoke** (per `~/.claude/reference/definition-of-done.md`): the real
  CLI against a live public channel the operator chooses, transcript in the
  report, covering happy path, `--help`, resume after Ctrl-C, a
  `CHAT_ADMIN_REQUIRED` skip, a `FLOOD_WAIT` sleep, doctor FAIL → collect
  refused, and the §13 unverified items relevant to the phase.

## 11. Phasing and workflow mapping

| Phase | Content | Execution | Model tiers |
|---|---|---|---|
| 0 | Scaffold (uv, ruff, pyright, pytest, CI), ADRs, `docs/opsec.md`, **throwaway spike** (`scripts/spike.py`) that settles §13 and records fixtures | interactive (this session) | — |
| 1 | **Core**: ids, targets, config/secrets/logging, store + migrations + raw + projections + sync, budget, gateway, `channel` + `history` collectors, recipe, jsonl export, doctor, CLI | `single-feature-run` (one feature: coupled) | Sonnet implements; Opus reviewers (adversarial + correctness; security-reviewer opted in) |
| 2 | `discussion`, `participants`+`profiles`, `media`, `graph`, `web`, `watch`, csv/rdf/datasette export, `lookup phone` | `federated-run`, **2–3 features per batch**, each its own issue/branch/plan | same |

Branch-tier is active: `main` protected; Phase 0 lands via `chore/bootstrap`
PR; Phase 1 on `feat/core`; Phase 2 features on `feat/<collector>`.

## 12. ADRs to record (Phase 0)

0001 library — Telethon 1.44.x behind a gateway seam (vs Kurigram, TDLib, gotd).
0002 storage — SQLite primary, raw-first, triple-shaped edges, URI ids,
graph/RDF as export; encryption by volume (SQLCipher not investigated).
0003 guardrails & opsec policy — the §2/§3 rules as requirements.
0004 sync — `pts`-based incremental sync, revisions, tombstone evidence ranks.

## 13. Unverified — settled by the Phase 0 spike

1. ~~Telethon 1.44.0 wheel layer vs 228~~ — SETTLED 2026-08-20: installed wheel is **layer 227**; all core raw methods present (getChannelRecommendations, getChannelDifference, getSponsoredMessages, premium.getBoostsList, channels.searchPosts). · 2. `getHistory` on a public channel
un-joined · 3. `getParticipants(Admins)` on a broadcast channel as subscriber
· 4. passive `getChannelDifference` un-joined delivers delete events · 5.
`Recent` yield on a mid-size visible supergroup · 6. `channelParticipantsMentions`
returns non-participant commenters · 7. `photos.getUserPhotos` on a restricted
non-contact · 8. `stories.getPeerStories` as non-contact · 9.
`fragment.getCollectibleInfo` on a stranger's collectible · 10. `FLOOD_WAIT`
onset for sequential `getFullUser` at 1 req/s.

## 14. Follow-ups to file as GitHub issues once the remote exists

- Canary URLs and phone-home documents: document the threat, enforce the
  allow-list, add a "open collected documents offline/sandboxed" note to
  `docs/opsec.md`, consider a `--strip-remote-content` pass for office files.
- Datasette/analytics export interop (generic).
- Third-party indexer enrichment (tgstat/telemetr) — research not completed.
