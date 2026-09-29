# Conservative pacing and patient flood handling (#69)

**Status:** draft for Gate A, 2026-09-28 (revised the same day after operator
review). **Tracking:** issue #69.
**Batch:** can run in the first overnight batch — touches `budget.py`,
`config.py`, `cli.py` (flags), the web collector's pacing, one migration; no
overlap with #62/#64 beyond adding fields to `Settings`.
**ADR:** amendment to ADR-0003 (guardrails are a settled decision).

## 1. Policy (operator decisions, 2026-09-28)

- Be conservative with Telegram: never push an API harder than we believe it
  tolerates. Where **we** assume a minimum interval, use twice that.
- A server-mandated `FLOOD_WAIT` is not a guess — honour it with a small
  margin, not a multiplier.
- **Don't kill a phase just because a wait is long.** Sleep through it, up to
  an operator-set ceiling; only beyond the ceiling stop the phase (resumable
  next run).

## 2. Today

- `Budget` enforces a per-method minimum interval between calls to the same
  method: `DEFAULT_MIN_INTERVAL_SECONDS = 1.0`, overridden by
  `--profile-interval` for the profile RPCs.
- A `FLOOD_WAIT` ≤ `flood_sleep_threshold` (60 s) is slept for exactly
  `seconds`, then retried **once**; a second consecutive wait, or any wait
  > 60 s, stops the phase with a persisted `flood_log` cooldown.
- Transient `ConnectionError`/`TimeoutError`/`OSError` classify as RETRY and
  are retried once with no pause.
- Telethon's own flood auto-sleep is disabled (`flood_sleep_threshold: 0` in
  `app.py`), so every Telegram wait passes through `Budget`. Keep it so.
- The web collector has its own `_DEFAULT_MIN_INTERVAL_SECONDS = 1.0`.

## 3. Change

### 3.1 Our own pacing: a safety factor, as a flag

- `pacing_factor: float = Field(default=2.0, ge=1.0)` — CLI `--pacing-factor`
  on every command that talks to Telegram or the web (`collect`,
  `fetch-media`, `doctor`, `auth`), env `PAPERBOY_PACING_FACTOR`.
- Every interval `Budget` enforces is `base × pacing_factor` (default, each
  per-method base, `--profile-interval`). Same for the web collector.
- Base intervals — **assumptions**, stated as such in the ADR:
  - `contacts.resolveUsername`: 5 s (one of Telegram's most flood-limited
    methods) → 10 s effective at the default factor.
  - everything else, including `channels.getMessages` and the per-file
    `upload.getFile` call: 1 s → 2 s.
- One INFO line at gateway construction lists the factor and every
  method's effective interval that differs from the default.

### 3.2 Server-mandated waits: a margin, not a multiplier

- A `FLOOD_WAIT` of `s` seconds is waited as
  `applied = ceil(s × 1.1) + 5` seconds (constants in `budget.py`, named and
  commented: the margin exists so we never re-call at the exact instant the
  server's window closes).
- `flood_log` gains `applied_seconds` (migration) alongside the server's
  `seconds`; the persisted cooldown uses `applied`.

### 3.3 Patience: sleep through long waits, up to a ceiling

- Reuse the existing `flood_sleep_threshold` setting as the ceiling, raise
  its default from 60 s to **3600 s**, and expose it as
  `--max-flood-sleep SECONDS`. A wait with `applied ≤ ceiling` is slept
  through; `applied > ceiling` persists the cooldown and stops the phase
  (next run waits it out via the existing `_active_cooldown_seconds`).
- While sleeping any wait ≥ 60 s, log an INFO heartbeat every 60 s
  (`method, remaining, total`) and update the progress display, so a long
  sleep never looks like a hang.
- **Retries:** up to 3 consecutive flood waits on the same call are each
  slept and retried; a 4th stops the phase. Transient network errors retry up
  to 3 times with a backoff of 5 s, 10 s, 20 s (× `pacing_factor`); a 4th
  failure goes through `classify` as today. Every retry is logged at WARNING
  with the attempt number.
- Retrying re-invokes the call's factory — callers that stream (the #64
  media sink) already reset per attempt; add a regression test here too.
- Unchanged: `PEER_FLOOD` / `FROZEN_METHOD_INVALID` remain hard stops;
  `max_rpc_per_run` still counts every attempt.

## 4. Tests (write first, see them fail)

- Default factor: two consecutive calls to one method are ≥ 2 × base apart
  (fake clock); `resolveUsername` ≥ 10 s apart.
- `--pacing-factor 1.0` reproduces today's intervals; `0.5` rejected at
  settings load; flag and env both reach `Settings`.
- `FLOOD_WAIT(20)`: sleeps `ceil(22) + 5 = 27` s, retries; `flood_log`
  has `seconds=20, applied_seconds=27`.
- `FLOOD_WAIT(1800)` with default ceiling: slept through (heartbeat logged
  30+ times on the fake clock), call succeeds — **no phase stop**.
- `FLOOD_WAIT(7200)`: `applied > 3600` → `PhaseStop`, cooldown `now + applied`.
- Three consecutive floods then success → success; four → `PhaseStop`.
- `ConnectionError` ×3 then success → success, with 5/10/20 s × factor
  backoff; ×4 → classified as today.
- Web collector interval scales with the factor.

## 5. Definition of done (smoke)

Live, read-only, small: `paperboy collect @<small public channel> --phases
channel,history --max-rpc 30` at INFO — paste the effective-interval line and
log timestamps showing ≥ 2 s between same-method calls. Then
`select method, seconds, applied_seconds from flood_log order by rowid desc
limit 5` (empty is fine — say so).
