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
  `fetch-media` takes both flags too.)
- `--max-flood-sleep S` / `PAPERBOY_FLOOD_SLEEP_THRESHOLD` (default `3600`,
  `0` = stop on every flood). On `collect`, `doctor` and `fetch-media`.

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

Pasted tool output, redacted (`@<channel>`, `<scratch>` = scratch data dir on
a copy of the store made with `sqlite3 .backup`; the real store was never
written). Unredacted logs: `smoke-69-doctor.log`, `smoke-69.log`,
`smoke-69b.log` in the scratch dir. Live route: VPN (`utun4`) verified
before each of the 3 live invocations (cap 5), `PAPERBOY_REQUIRE_PROXY=false`,
no `--unsafe`/`--join`/`--profiles`, `--max-flood-sleep 60`, no media.

### Gates

```
651 passed in 65.01s (0:01:05)
All checks passed!
0 errors, 0 warnings, 0 informations
```

### Offline

```
$ paperboy collect --help | grep -E "pacing-factor|max-flood-sleep"
│ --pacing-factor               <float range> [x>=1.0]  Multiply every request │
│ --max-flood-sleep             <int range> [x>=0]      Longest single         │
$ paperboy doctor --help | grep -E "pacing-factor|max-flood-sleep"
│ --pacing-factor          <float range> [x>=1.0]  Multiply every request      │
│ --max-flood-sleep        <int range> [x>=0]      Longest single FLOOD_WAIT   │
$ paperboy collect @x --pacing-factor 0.5
│ Invalid value for '--pacing-factor': 0.5 is not in the range x>=1.0.         │
exit=2
$ pragma table_info(flood_log)   # scratch store, after migration 0005
(4, 'recorded_at', 'TEXT', 1, None, 0), (5, 'applied_seconds', 'INTEGER', 0, None, 0)
```

### Live (VPN route check passed before each: both DCs -> `utun4`)

`doctor --profile default --max-flood-sleep 60`: every check `ok`, final line `PASS`.

`collect @<channel> --profile default --phases channel,history --max-rpc 30
--max-flood-sleep 60` (first run, 3 messages, then a second run on another
channel already in the store):

```
INFO     pacing: factor=2.0 default=2.0s contacts.resolveUsername=10.0s; flood ceiling=60s
INFO     ✓ channel · channels=1 peers=2 · 2s
INFO     ✓ history · messages=3 revisions=0 tombstones=2 edges=0 · 1s
```

Consecutive same-method calls from the second run's `paperboy.log` (DEBUG
`rpc <method> attempt` lines; `users.getFullUser` is the collect preflight's
self-check, then the channel phase's own call):

```
2026-09-29T04:12:11.206314+00:00 rpc users.getFullUser attempt 1 (run call #1)
2026-09-29T04:12:13.208717+00:00 rpc users.getFullUser attempt 1 (run call #7)
```

Gap 2.002 s (>= 2 s = 1 s base x factor 2.0). No FLOOD_WAIT occurred; no
retry was exercised live.

```
$ select method, seconds, applied_seconds from flood_log order by rowid desc limit 5
channels.getMessages|29|
channels.getMessages|24|
channels.getMessages|30|
channels.getMessages|30|
channels.getMessages|29|
```

These five rows are pre-#69 history copied from the real store
(`applied_seconds` is NULL); this run added none (`count(*)` 17,
`recorded_at` after this run: 0). Long-wait and retry behaviour is proven by
the fake-clock tests, not by provoking real floods.
