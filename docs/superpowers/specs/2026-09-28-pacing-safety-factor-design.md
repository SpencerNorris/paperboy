# Pacing safety factor — every assumed wait, doubled

**Status:** draft for Gate A, 2026-09-28. **Tracking:** issue #69.
**Batch:** 2, before #68 (list fetch depends on it). Touches `budget.py`,
`config.py`, the web collector's pacing and `app.py` — no overlap with
batch 1.
**ADR:** amendment to ADR-0003 (guardrails are a settled decision).

## 1. Policy (operator decision, 2026-09-28)

Be conservative with Telegram and never push an API harder than we believe
it tolerates. Apply an engineering tolerance: **whatever we assume the
minimum wait is, wait twice that.** This covers our own pacing *and* the
waits servers tell us to take.

## 2. Today

- `Budget` (all Telegram RPCs) enforces a per-method minimum interval
  between two calls to the same method — `DEFAULT_MIN_INTERVAL_SECONDS =
  1.0`, overridden per method by `--profile-interval`.
- A `FLOOD_WAIT` ≤ `flood_sleep_threshold` (60 s) is slept for exactly the
  server's `seconds`, then retried once; a longer one persists a cooldown of
  exactly `seconds` in `flood_log` and stops the phase.
- Telethon's own flood auto-sleep is disabled (`flood_sleep_threshold: 0` in
  `app.py`), so every Telegram wait passes through `Budget`. Keep it that way.
- The web collector has its own `_DEFAULT_MIN_INTERVAL_SECONDS = 1.0`.

## 3. Change

- New setting `pacing_safety_factor: float = Field(default=2.0, ge=1.0)`
  (`PAPERBOY_PACING_SAFETY_FACTOR`). There is intentionally no CLI flag to
  lower it per run; raising it is allowed via env.
- **Our pacing:** every interval `Budget` enforces is `base × factor`, where
  `base` is the default, a per-method override, or `--profile-interval`.
  Same for the web collector's interval (t.me, web.archive.org).
- **Server-mandated waits:** a `FLOOD_WAIT` of `s` seconds is treated as
  `s × factor` everywhere — the in-call sleep, the persisted `flood_log`
  cooldown (`until = now + s × factor`; keep the raw `seconds` column as the
  server's value and add an `applied_seconds` column via migration so the log
  shows both), and the threshold comparison (sleep-and-retry only if
  `s × factor ≤ flood_sleep_threshold`, else phase stop). The net effect is
  that floods stop phases sooner and cooldowns last longer.
- **Base intervals for the media path** (assumptions, stated as such in the
  ADR; the factor then doubles them):
  - `contacts.resolveUsername`: base 5 s (one of Telegram's most
    flood-limited methods) → effective 10 s.
  - `channels.getMessages`, `upload.getFile` (one call per file): the
    default 1 s → effective 2 s.
- A single INFO line at gateway construction lists the effective interval of
  every method with an override, and the factor.
- Out of scope: pacing *inside* one file download (Telethon requests a file's
  chunks back to back on the media DC; that is normal client behaviour and
  one `Budget` call).

## 4. Tests (write first, see them fail)

- Default factor 2.0: two consecutive calls to one method sleep ≥ 2 × base
  (fake clock).
- Per-method override and `--profile-interval` are both doubled.
- `FLOOD_WAIT(20)` with threshold 60: sleeps 40 s, retries once; `flood_log`
  records `seconds=20, applied_seconds=40`.
- `FLOOD_WAIT(40)` with threshold 60: 80 s > 60 → `PhaseStop`, cooldown
  `until = now + 80 s`.
- Factor 1.0 reproduces today's behaviour exactly (regression guard for the
  existing budget tests — run them parameterized at 1.0).
- `pacing_safety_factor = 0.5` rejected at settings load.
- Web collector interval doubled.

## 5. Definition of done (smoke)

Live, read-only: `paperboy collect @<small public channel> --phases channel,history`
with `--max-rpc` small, logging at INFO — paste the effective-interval line
and timestamps showing ≥ 2 s between same-method calls. Then
`sqlite3 … "select method, seconds, applied_seconds from flood_log order by
rowid desc limit 5"` (empty is fine if no flood occurred — say so).
