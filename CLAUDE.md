# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**paperboy** — a local, read-only CLI that collects everything obtainable about a
Telegram channel or supergroup (metadata, full message history with edits and
deletions, media, comment threads, discoverable people and their profiles, the
forward/mention/recommendation graph, web-archive snapshots) into one SQLite
database for OSINT / investigative-journalism use. Architected as an entity
graph so later recipes (user dossier, phone lookup, watchlists) are thin
additions. See `README.md` for the user-facing summary and disclaimer.

**Status (2026-08-26):** Phase 1 (core) shipped on `feat/core` — `paperboy
collect`/`status`/`export`/`doctor`/`auth` work end to end against live
Telegram (see `docs/features/collect-channel.md` for the DoD smoke
transcript). `watch` and `lookup` are Phase 2 stubs. `paperboy reproject`
(rebuild every projection from `raw_records`, offline, zero network/
credentials — see `docs/features/reproject.md`) shipped on `feat/reproject`;
its run-structure redesign (`raw_records.run_id`, replay once per historical
run — see `docs/adr/0005-run-structure.md`) has landed on top of it.
Phase 2 collectors: `discussion`/`graph`/`media`/`web`/`participants`/`profiles`
are implemented — the person layer shipped on `feat/person-layer` (#41): the
`participants` roster collector and the `profiles` enrichment collector, both
default-on (`profiles` full enrichment behind `--profiles`), with the `users`/
`user_snapshots`/`user_photos`/`participants`/`participant_snapshots` tables
(migration `0004_people.sql`) and reproject-replay support. See
`docs/features/person-layer.md` and `docs/adr/0006-person-layer-storage.md`.

**In progress on `dev/gcs-pull` (2026-10-06, not yet on `main`):** the
`dev/media-storage` chain below has merged into it. #63 per-run media stores is on
`feat/media-stores` (PR pending → `dev/gcs-pull`; ADR-0008,
`docs/features/media-stores.md`): each run's media goes to the local profile folder
or a write-once GCS bucket (`--media-store`, `PAPERBOY_MEDIA_STORE[_BUCKETS]`), no
local copy, create-only uploads, no delete path, per-store dedup, `custody_log.store`
(migration 0007), reproject reads bucket receipts read-only. #91 `fetch-from-list`
is next and builds on `MediaStore`. Earlier in the chain: #69
pacing (`--pacing-factor`, `--max-flood-sleep`; migration 0005) and #62
profile-relative media keys (ADR-0007; migration 0006) have merged. #64 media streaming is on `feat/media-streaming`
(PR pending; `docs/features/media-streaming.md`): streamed downloads,
`size_mismatch`, `--media-min-free-gb` (#53), replay leaves the source
untouched. #75 replay lookup performance is on
`perf/replay-lookup` (PR pending; per-run in-memory raw index, no migration —
`docs/features/reproject.md`). #70 profile split is on `feat/profile-split`
(PR pending; `reproject --include-target/--exclude-target/--out-profile`,
`scripts/unreferenced_media.py`, replay now verifies each media sha; no
migration — `docs/features/reproject.md` "Splitting a mixed profile"). #84 collect
by id is merged (PR #86; `docs/features/collect-channel.md`). #68
`fetch-media LIST` is on `feat/fetch-media-by-id` (PR pending →
`dev/media-storage`; `docs/features/fetch-media.md`): ordered cross-channel media
pull, each channel reached by id through the standard `channel` phase,
`--exclude-target`, `MediaSelection` names the channel so reproject walks the same
rows; no migration. Order and protocol: `docs/superpowers/specs/2026-09-28-media-storage-overview.md`.

## Read these first

- `docs/how-it-works.md` — plain-language map of the system (raw vs.
  projections, replay/reproject, profiles, media keys). Keep it current.
- `docs/research/telegram-extraction-surface.md` — what the Telegram API does
  and does not expose, by access tier, with the hard walls. Cited raw
  sub-reports in `docs/research/sources/`.
- `docs/superpowers/specs/2026-08-20-paperboy-design.md` — the approved design.
- `docs/superpowers/plans/` — implementation plans (one per workflow run).
- `docs/adr/` — decisions (library, storage, guardrails, raw-first persistence).
- `docs/opsec.md` — operator runbook for the collecting account (human steps).

## Settled decisions (do not re-litigate without an ADR)

- Python ≥3.12 (dev on 3.14), `uv`, Telethon 1.44.x behind a thin
  `TelegramGateway` seam, Typer CLI, stdlib `sqlite3` (WAL) with explicit
  migrations, pytest + pytest-asyncio, ruff + pyright.
- **Raw first:** every TL object the API returns is appended verbatim
  (`to_dict()` JSON) to the `raw_records` table before any normalisation;
  normalised tables are a projection that can be rebuilt from raw.
- **SQLite is the system of record**, Datasette-friendly (plain columns, JSON
  text for raw, FTS5, `metadata.json`); `edges` is triple-shaped
  `(subject, predicate, object, observed_at, tier, source_raw_id)` with
  URI-style ids (`tg:user:123`) so RDF/GraphML export is a projection.
  JSONL/CSV/HTML/RDF are `export` views, never primary stores.
- **`pts` is the sync primitive** (`updates.getChannelDifference`), not
  `last_message_id`; messages are versioned (`message_revisions`), deletions
  are tombstones, counters are time series.
- Profile richness lives in `users`/`user_snapshots`, **never** in `peers`
  (keeps the `upsert_peer` #38/#39 lattice untouched); tri-state fields are
  `present | absent | hidden_from_you` in `field_states_json`, and "no photo"
  is never recorded as a fact (ADR-0006).
- Media locations (`media.path`, `custody_log.path`, `MediaDownload`/
  `AvatarDownload` payloads) are profile-relative keys
  `media/<sha[:2]>/<sha><ext>` (ADR-0007), never absolute or cwd-relative
  paths; construct with `media_keys.media_key`, resolve at read time.
- A key lives under a **store root**: the profile folder (`local`) or a bucket
  `gs://<bucket>/<prefix>` (ADR-0008). One store per run, selected by
  `media_store`; `custody_log.store` and a bucket run's `"store"` receipt key say
  which (a missing key means local). `paperboy.media_store.MediaStore` is the only
  seam; bucket writes are create-only with **no delete or overwrite path**.
- `min` peers are stored with `(seen_in_chat, seen_in_msg)` provenance and
  fetched via `inputUserFromMessage`; optional user fields are tri-state
  (present / not-set / hidden-from-you) — never record "no photo".

## Non-negotiable guardrails (product requirements, not style)

- Read-only. The tool never sends, reacts, votes, types, marks read, joins
  without `--join`, or calls `users.suggestBirthday`. Passive (un-joined)
  collection is the default.
- One MTProto session per auth key; parallelism only on media DCs. Honour
  `FLOOD_WAIT` per method; `PEER_FLOOD` / `FROZEN_METHOD_INVALID` are hard
  stops. All RPCs go through the budget/guardrail module — no collector calls
  the gateway raw.
- Excluded outright: `contacts.getLocated`, poll-voter collection, any
  add-member/invite capability, AI-training export. Flag-gated + budgeted:
  phone lookup (`importContacts` → snapshot → `deleteContacts`), `--join`,
  private-invite joins (operator asserts authorisation).
- Outbound HTTP only to an allow-list (`t.me`, `web.archive.org`), via the
  configured proxy; never fetch URLs found inside collected content. The one
  addition (ADR-0003 amendment, #63): `storage.googleapis.com`, only for a
  configured, allow-listed media bucket (ADC only, no proxy); reproject only reads.
- Credentials (phone, `api_hash`, session, login codes) never in logs or the
  repo; logs reference targets by id. Exports scrub the collecting account.

## Commands

`uv sync`; `uv run pytest -q`; `uv run ruff check`; `uv run pyright`;
`uv run paperboy --help`. The CLI: `auth`, `doctor`, `collect TARGET
[--phases channel,history] [--unsafe] [--pacing-factor F] [--max-flood-sleep S] [--media-store gs://B/P]`
(also on `doctor`; defaults 2.0 / 3600 — `docs/features/pacing.md`), `fetch-media LIST [--dry-run] [--report OUT.csv] [--exclude-target T …] [--media-store gs://B/P]` (ordered cross-channel
media pull, #68 — `docs/features/fetch-media.md`; per-run stores, #63 — `docs/features/media-stores.md`), `status [TARGET]`, `export TARGET
--format jsonl --out DIR` — all read `api_id`/`api_hash`/session for
`--profile` (default `default`) from the OS keychain via `keyring` (macOS/Windows/Linux; tested on macOS — see issue #10) (`scripts/store_api.py`,
`scripts/login.py`, or `paperboy auth`). `reproject [--profile P] [--out
PATH | --out-profile NAME] [--include-target T | --exclude-target T] [--phases a,b,c]`
needs none of that — it never touches Telegram or the keychain, only a
source `paperboy.sqlite`'s `raw_records` (plus, for a bucket run's receipts, read-only
GETs to an allow-listed GCS bucket with ADC, #63; a local-only source does no network
I/O at all) (the target flags split a mixed
profile, #70; `scripts/unreferenced_media.py --profile P` lists orphaned media). `watch`/
`lookup` exit 1 with a "Phase 2" message — not implemented yet. `TARGET` for
`collect`/`status`/`export` is a handle or a channel id (`123`, `-100123` after
`--`, `t.me/c/123`; #84) — by id the account must already have been shown the
channel.

## Workflow

Global `~/.claude/CLAUDE.md` applies (DoD with smoke transcript, no-shed,
GitHub Issues as the only tracker, branch-tier: `main` is protected, work on
`feat/`/`fix/`/`chore/` branches via PR). Implementation runs use
`single-feature-run` (core) and `federated-run` (independent collectors, 2–3
per batch); Sonnet implements, Opus reviews. Keep sub-agent fan-out small —
this user's session quota is a real constraint.
