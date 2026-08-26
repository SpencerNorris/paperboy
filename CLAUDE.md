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
credentials — see `docs/features/reproject.md`) shipped on `feat/reproject`.
Phase 2 collectors: `discussion`/`graph`/`media`/`web` are implemented;
`participants`/`profiles` are not started.

## Read these first

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
  configured proxy; never fetch URLs found inside collected content.
- Credentials (phone, `api_hash`, session, login codes) never in logs or the
  repo; logs reference targets by id. Exports scrub the collecting account.

## Commands

`uv sync`; `uv run pytest -q`; `uv run ruff check`; `uv run pyright`;
`uv run paperboy --help`. The CLI: `auth`, `doctor`, `collect TARGET
[--phases channel,history] [--unsafe]`, `status [TARGET]`, `export TARGET
--format jsonl --out DIR` — all read `api_id`/`api_hash`/session for
`--profile` (default `default`) from the OS keychain via `keyring` (macOS/Windows/Linux; tested on macOS — see issue #10) (`scripts/store_api.py`,
`scripts/login.py`, or `paperboy auth`). `reproject [--profile P] [--out
PATH] [--phases a,b,c]` needs none of that — it never touches the network or
the keychain, only a source `paperboy.sqlite`'s `raw_records`. `watch`/
`lookup` exit 1 with a "Phase 2" message — not implemented yet.

## Workflow

Global `~/.claude/CLAUDE.md` applies (DoD with smoke transcript, no-shed,
GitHub Issues as the only tracker, branch-tier: `main` is protected, work on
`feat/`/`fix/`/`chore/` branches via PR). Implementation runs use
`single-feature-run` (core) and `federated-run` (independent collectors, 2–3
per batch); Sonnet implements, Opus reviews. Keep sub-agent fan-out small —
this user's session quota is a real constraint.
