# Feature: conservative pacing and patient flood handling

**Status:** implemented on `feat/pacing-factor` (#69). **Spec:**
`docs/superpowers/specs/2026-09-28-pacing-safety-factor-design.md`. **Plan:**
`docs/superpowers/plans/2026-09-28-pacing-safety-factor.md`. **ADR:**
ADR-0003 amendment (2026-09-28).

## Purpose

Never push Telegram harder than we believe it tolerates, honour server-mandated
waits instead of abandoning a phase because a wait is long, and keep every
phase resumable.

## Inputs

- `--pacing-factor F` / `PAPERBOY_PACING_FACTOR` (default `2.0`, minimum `1.0`;
  values below 1 are rejected at settings load / CLI parse, exit 2). On
  `collect` and `doctor`. (`auth` never builds a `Budget`, so it has no flag;
  `fetch-media` inherits via `Settings`.)
- `--max-flood-sleep S` / `PAPERBOY_FLOOD_SLEEP_THRESHOLD` (default `3600`,
  `0` = stop on every flood). On `collect` and `doctor`.

## Behaviour

- **Our assumptions get the factor.** Every interval `Budget` enforces is
  `base x factor`: default base 1 s, `contacts.resolveUsername` 5 s,
  `--profile-interval` (a base) and the web collector's inter-request pause.
  At the default factor: 2 s and 10 s. `--pacing-factor 1` reproduces the
  pre-#69 intervals (with resolveUsername at 5 s).
- **Server waits get a margin, not a multiplier.** A `FLOOD_WAIT` of `s`
  seconds is waited as `applied = ceil(s x 1.1) + 5` (20 -> 27, 1800 -> 1985).
  `flood_log` stores `seconds` (server) and `applied_seconds` (migration
  `0005_flood_applied.sql`; NULL on older rows); the persisted cooldown uses
  `applied`.
- **Patience.** `applied <= ceiling` is slept through, in chunks of at most 60 s
  with an INFO heartbeat (`method, remaining, total`) per chunk for waits of 60 s
  or more. `applied > ceiling` persists the cooldown and raises `PhaseStop`;
  the next run waits it out. The ceiling is compared to `applied`, not `s`.
- **Retries.** Up to 3 consecutive floods per call are slept and retried (a 4th
  is a `PhaseStop`). Transient `ConnectionError`/`TimeoutError`/`OSError` retry
  3 times at 5/10/20 s x factor (a 4th is a `PhaseStop`, cause chained), never
  touching `flood_log`. Each retry is a WARNING with its attempt number, re-invokes
  the call's factory, and counts toward `max_rpc_per_run`.
- Unchanged: `PEER_FLOOD` / `FROZEN_METHOD_INVALID` are hard stops; Telethon's own
  flood auto-sleep stays off so every wait passes through `Budget`.
- One INFO line at gateway construction: `pacing: factor=... default=...s
  <method>=...s; flood ceiling=...s`.

## Known limitations

- The base intervals are assumptions, not measured limits.
- No progress-display hook inside `Budget`: the existing 5 s `Progress`
  heartbeat and Budget's 60 s INFO line cover a long sleep.

## Definition-of-done smoke transcript

Offline and live results are recorded below (redacted per the run rules).

_Pending: filled in by the DoD smoke step._
