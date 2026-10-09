# Feature: `collect-channel`

**Status:** shipped (Phase 1 / core, Tasks 1–17). **Spec:**
`docs/superpowers/specs/2026-08-20-paperboy-design.md` §4–§10. **Plan:**
`docs/superpowers/plans/2026-08-20-paperboy-core.md`.

## Purpose

`paperboy collect <target>` archives a Telegram channel or supergroup's
metadata and full message history — with edit revisions, deletion
tombstones, and counter time series — into one local SQLite database,
read-only and passively (no join, no send/react/vote), behind a budget gate
that paces every RPC and classifies every error per spec §8.

## Inputs

- `TARGET` (`targets.py`), either a **handle** (`@name`, `name`, `t.me/name`,
  `t.me/name/123`) or a channel **id** (#84): bare `123`, marked `-100123`,
  or `t.me/c/123[/456]` (the message part is ignored). All id forms normalise
  to the bare id. A leading `-` needs `--` on the command line
  (`paperboy collect -- -100123`). A negative id without `-100` is a basic
  group or user id and is rejected. Collecting by id requires that this
  account has already been shown the channel (see Step A below); invite
  hashes still parse but aren't resolvable.
- `--profile`: selects the session/database compartment (`config.profile_dir`).
- `--phases channel,history`: restricts which collectors run (default: both).
- `--unsafe`: skips the `doctor` preflight gate.
- `--profile-budget` / `--max-rpc`: override `Settings` for this run.
- Credentials: `api_hash` + session from the OS keychain (`secrets.py`);
  `api_id` from `PAPERBOY_API_ID` or the same keychain entry
  `scripts/store_api.py` writes.

## Outputs

- `<data_dir>/<profile>/paperboy.sqlite`: `channels` (+ `channel_snapshots`),
  `peers`, `messages` (+ `message_revisions`, `message_metrics`,
  `message_tombstones`), `edges`, `sync_state`/`sync_ranges`, `raw_records`
  (every TL object as received, before any projection).
- `paperboy status [TARGET]`: row counts for one channel or the whole profile.
- `paperboy export TARGET --format jsonl --out DIR`: `channel.jsonl`,
  `messages.jsonl` (current state + inline `revisions` array), `edges.jsonl`
  — scrubbed of the collecting account's own messages/edges.

## How it works

`cli.py` → `recipes.collect_channel` runs `ChannelCollector` (identify
`self` → **Step A, get access** → `getFullChannel` → upsert channel +
snapshot + `linked_group` edge → seed `pts` → upsert peers), then
`HistoryCollector`: pages
`getHistory` newest→oldest into `sync_ranges`, probes every id in the swept
span that `getHistory` didn't return via `getMessages` (chunks of ≤200),
tombstones any `messageEmpty` result (`evidence="empty"`), then immediately
runs `catch_up()` (`updates.getChannelDifference` from the stored `pts`) so
the channel's sync state is current as of *now*. Every Telegram RPC goes
through `Budget.call` (per-method pacing, persisted flood cooldowns, a
per-run cap) — no collector or gateway method calls Telethon directly.

### Step A: getting access to the channel (#84)

Every channel request needs the id plus a per-account `access_hash` that
Telegram only hands to an account when it shows it the channel. Step A
obtains one, never guessing, by the first route that works (spec
`docs/superpowers/specs/2026-09-30-collect-by-id-design.md` §2):

1. **saved key**: a full (non-`min`) `access_hash` in `peers`.
2. **from message**: a `min` peer with `(seen_in_chat, seen_in_msg)`
   provenance into a chat whose own key is full, used as
   `inputChannelFromMessage`. A `min` hash is never used as a key.
3. **handle**: the stored username, resolved with `contacts.resolveUsername`
   and accepted only if it resolves to the requested id (else
   `the stored handle for channel N now belongs to channel M`, and the phase
   is skipped). A handle target is this route without the verification.
4. Nothing worked: the phase is skipped with an error naming what would
   make each route work.

"Works" means `getFullChannel` accepts the key. If it rejects a saved key, a
from-message reference or a verified handle with an access error
(`CHANNEL_INVALID`, `CHANNEL_PRIVATE`, `MSG_ID_INVALID`, i.e. what
`Budget.call` turns into `SkipAndRecord` for that call), that attempt is
recorded as a `ChannelAccess` receipt with `granted: false` and the `error`
name, and the next route is tried (1, then 2, then 3, then the route-4 failure,
which lists every route tried and its error). Floods, hard stops and phase
stops are not access errors: they propagate and never trigger a fallback. A
handle target has the one route, so its rejection is recorded (`granted: false` plus the error) and then propagates.

Step A always appends a `ChannelAccess` raw record (`via`, `input_channel`,
`granted`, ...; see `docs/data-model.md`) before `getFullChannel`, so replay
serves Step A from the receipt instead of recomputing it from the output
store. `getFullChannel`'s identity check is the verification for every route;
afterwards the run uses the full key it returns, so a from-message start is a
saved key next time. `status` and `export` also accept the id forms,
offline, via `channels.id`. An id beyond SQLite's signed 64-bit range is
rejected at parse time with a one-line message (exit 1, no traceback).

On reproject, the refused attempts and the final one are replayed from the
recorded receipts in order. A run recorded before #84 has no receipt: it
replays exactly as it did then (handle path) and writes no `ChannelAccess` row
(see `docs/features/reproject.md`).

## Edge cases handled

- **Interrupted backfill**: a per-page `sync_state('history', ...)`
  `offset_id` cursor means Ctrl-C mid-run loses at most one page; a re-run
  resumes from the cursor rather than restarting (verified live, below).
- **Deleted/never-existed messages**: an id inside the swept `[min, max]`
  span that `getHistory` never returned is probed via `getMessages`; a
  `messageEmpty` result gets a tombstone (`evidence="empty"`); a `pts`
  catch-up delete event gets `evidence="update"` (spec §7's ranking).
  `deleted_at` is set for both, never for a plain `gap`.
- **Edited messages**: a changed `content_hash` appends a
  `message_revisions` row and updates current state; identical content only
  advances `last_seen`. The hash ignores Telegram's rotating `file_reference`
  tokens, so re-fetching an unchanged photo/document message is not an edit (#96;
  [data model](../data-model.md#message_revisions--edit-history)).
- **Multi-username channels/peers** (Fragment-purchased extra handles):
  Telegram reports the legacy `username` field as `null` and lists every
  handle in `usernames[]` instead; `ids.primary_username` falls back to the
  `editable: true` entry. Found live against `@durov` (6 usernames).
- **`CHAT_ADMIN_REQUIRED`**: classified `SkipAndRecord`; confirmed live
  against `@durov`'s admin list (below).
- **Doctor-blocked account**: `collect` refuses to run (exit 1) unless
  `--unsafe`; confirmed live (below).

## Known limitations (v1 core scope)

- Invite-hash targets parse (`Target.is_channel_like`) but aren't
  resolvable yet. An id target is only reachable when this account has
  already been shown the channel (a saved key, a referencing message, or a
  known handle; Step A route 4 otherwise).
- `fetch-from-list` (#68, renamed in #91) reaches every list channel through
  this same Step A (id target, standard `channel` collector); a route-4
  channel's rows are `no_access` there. A handle row for a channel the store
  has never seen goes through the `handle` route (#91;
  `docs/features/fetch-from-list.md`).
- When Step A finds no route (route 4) no `ChannelAccess` receipt is written,
  so that run has no target for `reproject`, which logs its existing "no
  resolve records" warning and drops it.
- A backfill resumed after an interruption only marks the *resumed* span
  `[1, cursor_at_interruption]` as a verified `sync_range` — the portion
  collected *before* the interruption isn't retroactively gap-probed by that
  run. No data is lost or wrong; that upper span just isn't re-verified
  until something (a future `watch`/audit pass) revisits it explicitly.
- `--join` is accepted but inert — v1 core never joins anything (by design;
  channel/history are passive-only per the Global Constraints).
- A real `FLOOD_WAIT` sleep-and-retry is exercised only in `tests/test_budget.py`
  (`FakeFlood`), not in the live smoke below — deliberately: inducing a real
  one against Telegram means abusive request volume, which contradicts the
  tool's own pacing/opsec purpose. `CHAT_ADMIN_REQUIRED` classification
  *is* confirmed against a real Telegram error (below), exercising the same
  `Budget.call` → `classify()` path.

## Definition-of-Done smoke transcript (2026-08-21, profile `default`)

Research account already in the macOS Keychain (`service=paperboy,
profile=default`); read-only throughout, nothing joined, nothing sent.

### 1. `doctor` — FAIL blocks `collect`

```
$ paperboy doctor --profile default
                      paperboy doctor — profile 'default'
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ check            ┃ status ┃ detail                                           ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ proxy            │ fail   │ require_proxy is set but no proxy is configured  │
│ session_age      │ fail   │ session is 0.1 days old, below                   │
│                  │        │ min_session_age_days=7                          │
│ two_factor_auth  │ fail   │ no 2FA password set                              │
│ privacy_phone    │ ok     │ phone privacy is restricted                      │
│ privacy_lastseen │ fail   │ lastseen privacy is Everyone (AllowAll)          │
│ privacy_photo    │ fail   │ photo privacy is Everyone (AllowAll)             │
│ minimal_profile  │ ok     │ self profile is minimal                         │
└──────────────────┴────────┴──────────────────────────────────────────────────┘
BLOCKED: collect refuses to run without --unsafe.
$ echo $?
1
```

```
$ paperboy collect @durov --profile default --phases channel
doctor preflight failed — refusing to collect. Run `paperboy doctor` for
details, or pass --unsafe to override.
$ echo $?
1
```

(Every check here is real: this is a genuinely fresh research account —
0.1 days old, no proxy configured in this sandbox, no 2FA yet. A production
investigation account should clear all of these before real use per
`docs/opsec.md`; `--unsafe` below is a deliberate, scoped override for this
smoke test only.)

### 2. `collect` un-joined against a live public channel

```
$ paperboy collect @durov --profile default --phases channel,history --unsafe
                                 collect @durov
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ phase   ┃ counts                                                   ┃ stopped ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ channel │ {'channels': 1, 'peers': 2}                              │ -       │
│ history │ {'messages': 476, 'revisions': 476, 'tombstones': 67,    │ -       │
│         │ 'edges': 1}                                              │         │
└─────────┴──────────────────────────────────────────────────────────┴─────────┘

$ paperboy status @durov --profile default
  paperboy status — @durov
┏━━━━━━━━━━━━┳━━━━━━━┓
┃ metric     ┃ count ┃
┡━━━━━━━━━━━━╇━━━━━━━┩
│ messages   │ 476   │
│ revisions  │ 476   │
│ tombstones │ 67    │
└────────────┴───────┘
```

`@durov`'s username resolved via `contacts.resolveUsername` un-joined
(spec §13.2/.7 confirmed on a real account, not just the Phase-0 spike);
476 messages backfilled with edit revisions and 67 gap-probed tombstones,
1 `forwarded_from` edge.

### 3. Interrupt mid-backfill (Ctrl-C) and resume

Against `@nytimes` (a larger channel, for a wider interruption window):

```
$ paperboy collect @nytimes --profile default --phases channel,history --unsafe &
[running...]
$ # after ~5s, sent SIGINT (Ctrl-C) mid-backfill
```

State at the moment of interruption:

```
messages: 1500
sync_state: history/1606432449 -> {"offset_id": 2087}
min/max msg_id stored: 2087 .. 3616
```

Re-run, same command:

```
$ paperboy collect @nytimes --profile default --phases channel,history --unsafe
                                collect @nytimes
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ phase   ┃ counts                                                   ┃ stopped ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ channel │ {'channels': 1, 'peers': 1}                              │ -       │
│ history │ {'messages': 2043, 'revisions': 2043, 'tombstones': 43,  │ -       │
│         │ 'edges': 0}                                              │         │
└─────────┴──────────────────────────────────────────────────────────┴─────────┘
```

Resumed from `offset_id=2087` (not from 0): 1500 (pre-interrupt) + 2043
(resumed) = 3543 total messages, confirmed via `select count(*) from
messages` — no re-fetch of the already-collected span, no duplicates
(upserts are idempotent by `uri`).

### 4. `CHAT_ADMIN_REQUIRED` classified and skipped, not a crash

A direct `Budget.call` around `channels.getParticipants(filter=Admins)` on
`@durov` (a broadcast channel, and this account is not its admin):

```
CONFIRMED: Budget.call classified the real CHAT_ADMIN_REQUIRED error as
SkipAndRecord: Chat admin privileges are required to do that in the
specified chat [...] (caused by GetParticipantsRequest)
```

`classify()` mapped Telethon's real `ChatAdminRequiredError` to
`Disposition.SKIP`, and `Budget.call` raised `SkipAndRecord` rather than
propagating the raw RPC error — exactly spec §8's "skip-and-record" path,
confirmed against a genuine Telegram response, not a test double.

### 5. `export --format jsonl`

```
$ paperboy export @durov --format jsonl --out /tmp/paperboy_smoke_export --profile default
    export @durov -> /tmp/paperboy_smoke_export
┏━━━━━━━━━━━━━━━━┳━━━━━━┓
┃ file           ┃ rows ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━┩
│ channel.jsonl  │ 1    │
│ messages.jsonl │ 476  │
│ edges.jsonl    │ 1    │
└────────────────┴──────┘
$ wc -l /tmp/paperboy_smoke_export/*.jsonl
       1 channel.jsonl
       1 edges.jsonl
     476 messages.jsonl
```

Row counts match `status` exactly. The first exported message (id 1, the
channel-creation service message) correctly carries `"is_service": 1` and
an `action_json` of `MessageActionChannelCreate` — confirming the
PascalCase-discriminator fix (below) round-trips correctly end to end.

## Bugs found and fixed by this smoke test (no-shed)

The unit suite (115 tests, `FakeGateway` fixtures I authored myself) was
green throughout implementation but never caught these — they only show up
against real Telethon objects:

1. **Wrong TL discriminator casing.** Telethon's `to_dict()` uses the
   PascalCase Python class name (`"Channel"`, `"PeerUser"`,
   `"MessageEmpty"`, `"ChannelDifferenceTooLong"`, ...) as the `"_"` key —
   not the lowercase TL constructor name every fixture and discriminator
   check in this codebase assumed. `@durov`'s channel object came back as
   `{"_": "Channel", ...}`; `_pick_channel`'s `.startswith("channel")`
   check never matched, and `ChannelCollector.collect` raised. Fixed by
   matching case-insensitively everywhere a discriminator is checked
   (`store/peers.py`, `store/messages.py`, `ids.peer_ref_uri`,
   `collectors/channel.py`, `collectors/history.py`, `doctor.py`).
2. **Multi-username accounts store a null username.** `@durov`'s channel
   has six usernames (a Fragment-era feature); Telegram reports the legacy
   `username` field as `null` for it and lists every handle in
   `usernames[]` instead. `channels.username`/`peers.username` came back
   `NULL`, breaking `status`/`export`'s lookup-by-username entirely. Fixed
   via `ids.primary_username`, which falls back to the `editable: true`
   entry in `usernames[]`.
3. **`NameError` in `TelethonGateway.get_channel_difference`.** `cast(TLObject, ...)`
   referenced a name only imported under `TYPE_CHECKING` — invisible to
   pyright (annotations are lazy strings under `from __future__ import
   annotations`, so it never actually resolves `TLObject` at check time)
   but a real `NameError` the moment `cast()`'s first argument is
   evaluated at runtime. Fixed with a real (non-guarded) local import,
   matching every other method in the file.

All three are fixed in `fix: match Telethon's real PascalCase TL
discriminators, not lowercase`; the full local suite (115 tests, ruff,
pyright) stayed green throughout, and every scenario above was re-run
against live Telegram after the fix to confirm it.

## VALIDATE-phase findings (`feat/core-fixes`, second pass)

Two more rounds of bugs surfaced after the transcript above, none of them
caught by the (still-green) unit suite because every existing `collect`/
`doctor` test monkeypatches `composition.build_gateway` — no test exercised
the real credential-resolution or the real recipe-level exception handling.

### Correctness-review fixes (`fix: correctness-review findings — SkipAndRecord, history guards, handler redaction`)

1. **`SkipAndRecord` propagated uncaught out of `collect_channel`.**
   `Budget.call` already classified `CHAT_ADMIN_REQUIRED`/`ChannelPrivateError`/
   etc. into `SkipAndRecord` (confirmed live against `@durov` above via a
   direct `Budget.call`, item 4) — but `recipes.collect_channel`'s `except`
   clauses only listed `PhaseStop`/`HardStop`. Collecting any
   private/inaccessible channel crashed the whole run instead of skipping
   that phase and continuing. Fixed by adding a `except SkipAndRecord`
   branch that records a `run_events(kind='skip')` row and marks the phase
   `stopped="skip"`, matching spec §8.
2. **`HistoryCollector.collect()`/`catch_up()` raised a bare
   `AssertionError`** when `ctx.input_channel`/`channel_id` weren't set —
   which happens whenever `channel` stops early (e.g. finding #1's
   `SkipAndRecord`, or a long-`FLOOD_WAIT` `PhaseStop`) but `history` still
   runs next in the same pass. Fixed by raising a handled `PhaseStop`
   instead.
3. **`RedactionFilter` was attached to the `paperboy` logger, not its
   handlers.** The app logs exclusively through child loggers
   (`paperboy.cli`, the `log` threaded into recipes/collectors);
   `Logger.callHandlers` never re-checks an ancestor logger's own filters
   for a record bubbling up from a child, so secrets logged through any
   child logger reached the log file unmasked. Fixed by attaching one
   shared `RedactionFilter` instance to each handler instead.
4. **`Budget._record_flood` wrote spurious `flood_log` rows for transient
   network errors.** `ConnectionError`/`TimeoutError`/`OSError` classify as
   `Disposition.RETRY` too, but carry no `.seconds`; `getattr(exc,
   "seconds", 0)` silently fell back to 0, writing a `(seconds=0)` row into
   `flood_log` on every ordinary transient-network retry. Fixed by only
   recording when `seconds > 0`.

Re-validated without live Telegram (this session's sandbox denied
keychain/credential access — see below): a script written independently of
the implementer's own smoke test drives finding #1 end-to-end through the
*real* `ChannelCollector`+`HistoryCollector` (not stub collectors), with a
genuine `telethon.errors.ChatAdminRequiredError` routed through a real
`Budget` instance — confirming `channel` is marked `stopped="skip"` and
`history` (which runs next by default) hits guard #2 and is marked
`stopped="phase_stop"`, never an uncaught exception. Findings #3 and #4 were
re-confirmed directly: a secret registered via `register_secret` and logged
through `logging.getLogger("paperboy.cli")` (and a grandchild logger) comes
back masked in the file handler's JSON output; `ConnectionError`/
`TimeoutError`/`OSError` retries leave `flood_log` empty while a genuine
`telethon.errors.FloodWaitError` (not the `FakeFlood` test double) still
records normally. Full transcript in the DoD report for this validation
pass.

Also included in `feat/core-fixes`: `fix: reject --phases history without
channel instead of crashing` — `--phases history` alone now exits 1 with an
actionable message from `cli.py`, before any RPC/doctor/store setup runs,
instead of reaching `HistoryCollector`'s (now-fixed) guard. Re-confirmed via
the real CLI in this validation pass (see the DoD report).

### Missing-credentials failure mode (found and fixed during VALIDATE)

`doctor`, `collect`, and `auth` all reach `composition.build_client`, which
raises `app.ConfigError` — a deliberately actionable exception ("No api_id
configured for profile ...: set PAPERBOY_API_ID or run ...") — when no
`api_id`/`api_hash` is configured for the profile. Nothing in `cli.py`
caught it: it reached the user as a raw Rich-rendered Python traceback
instead of `ConfigError`'s own message. No test caught this either, for the
same reason as the four findings above — every `collect`/`doctor` test
monkeypatches `build_gateway` entirely, bypassing credential resolution.

Fixed in `cli.py`: a `_run_async_or_exit` helper (used by `doctor`/
`collect`) and a direct `try/except` in `auth` now catch `ConfigError` and
exit 1 with its message, no traceback. Three new regression tests
(`test_doctor_missing_credentials_exits_cleanly`,
`test_collect_missing_credentials_exits_cleanly`,
`test_auth_missing_credentials_exits_cleanly`) monkeypatch
`build_gateway`/`build_client` to raise `ConfigError` and assert a clean
exit + no `"Traceback"` in stdout. Re-confirmed live against the real CLI
with a profile that genuinely has no stored credentials (`doctor`,
`collect`, and `auth` each now print the actionable message and exit 1) —
transcript in the DoD report.

## Live-Telegram scenarios not re-run in this validation pass

This VALIDATE session ran in a sandbox where keychain/credential access was
denied by the harness's own permission policy (a deliberate safety boundary
against an autonomous agent placing unsupervised live calls against a real
Telegram account) — so the live-network portions of the DoD checklist
(`CHAT_ADMIN_REQUIRED` skip via a real `paperboy collect` run against a
genuinely inaccessible channel, a real `FLOOD_WAIT` sleep against Telegram,
Ctrl-C-resume against live network, `doctor`-FAIL-blocks-`collect` against a
real non-compliant account) were not re-executed here. They were already
demonstrated live in the transcript above, prior to the fixes in this
section; the specific defect finding #1 fixes (`SkipAndRecord` crashing
`collect_channel`) was never actually exercised end-to-end live even in that
earlier pass — the "CHAT_ADMIN_REQUIRED" item there drove `Budget.call`
directly, not the full `collect_channel` recipe. A live confirmation of
finding #1 (`paperboy collect <a private/no-admin channel> --unsafe` no
longer crashing) is recommended as a follow-up by whoever next has
interactive keychain access — see the DoD report for the exact command.

## Collect-by-id smoke (#84)

Offline evidence (all green on this branch): `pytest` 949 passed, `ruff check`
clean, `pyright` 0 errors; the parity golden diff is only the added
`ChannelAccess` raw row and the raw-id shifts it causes (no projected-table
content change); id-target routes 1-4, replay of the receipt (output-store
`peers` deliberately wrong, receipt still served) and `--exclude-target`
by id are pinned by tests in `tests/test_collector_channel.py` and
`tests/test_profile_split.py`.

Offline smoke on a scratch `.backup` of the store (redacted; the real data
dir is read-only). Output is verbatim apart from `<id>` substitutions; the
rejection is a clean one-line message with exit 1 and no traceback (an earlier
revision of this doc showed only the tail of a Rich traceback; fixed in
`_parse_target_or_exit`, pinned by `test_negative_non_channel_id_is_rejected_without_a_traceback`):

```
$ paperboy status --profile default -- <id>          # bare id
  messages 8400 / revisions 8400 / tombstones 304
$ paperboy status --profile default -- -100<id>      # marked form, same channel
  paperboy status —
    -100<id>
┏━━━━━━━━━━━━┳━━━━━━━┓
┃ metric     ┃ count ┃
┡━━━━━━━━━━━━╇━━━━━━━┩
│ messages   │ 8400  │
│ revisions  │ 8400  │
│ tombstones │ 304   │
└────────────┴───────┘
$ paperboy status --profile default -- -123
'-123' is a basic group or user id (channel ids look like -100<id>); collecting 
non-channel peers is out of scope
exit=1
$ paperboy collect --profile default --phases channel -- -123
'-123' is a basic group or user id (channel ids look like -100<id>); collecting 
non-channel peers is out of scope
exit=1
$ paperboy status --profile default -- 999
No local data for '999' yet — run `collect` first.
exit=1
```

Reproject of the post-live scratch store (offline, a fresh `.backup` copy,
`--phases channel`). Source `ChannelAccess` rows by `via`: `from_message` 1,
`handle` 1, `saved_key` 2 (the four live runs; nothing earlier carries a
receipt). `--exclude-target -100<minid>` (the min channel):

```
$ paperboy reproject --profile default --phases channel --exclude-target -100<minid>
reproject: run=<run1> target=<fullid> channel_id=<fullid> decision=included       # live 1, saved_key
reproject: run=<run2> target=<minid> channel_id=<minid> decision=excluded         # live 2, from_message
reproject: run=<run3> target=<minid> channel_id=<minid> decision=excluded         # live 3, saved_key
reproject: run=<run4> target=@<fullhandle> channel_id=<fullid> decision=included  # live 4, handle
(whole store: 62 runs included, 2 excluded)
output ChannelAccess rows by via: handle 1, saved_key 1
```

The two id-started runs of the min channel are excluded by its id; the id run
of the full-key channel and the handle run are kept. (Re-run after the legacy
fix: the 60 pre-feature handle runs in the store have no receipt, so they replay
as before and write none; the two output rows are the live `saved_key` run and
the live `handle` run. An earlier revision of this doc showed `handle 60` because
replay minted a receipt for every legacy run, which spec 2.6 forbids.)
`--include-target <minid>` instead keeps exactly those two runs and
reproduces the `from_message` receipt:

```
$ paperboy reproject --profile default --phases channel --include-target <minid>
reproject: run=<run2> target=<minid> channel_id=<minid> decision=included
reproject: run=<run3> target=<minid> channel_id=<minid> decision=included
channel access: id=<minid> via=from_message granted=True
channel access: id=<minid> via=saved_key granted=True
output ChannelAccess rows: from_message 1 (input_channel carries from_msg with msg_id)
```

Live smoke (`--phases channel`, scratch `.backup` data dir, VPN egress,
`--max-rpc 20 --max-flood-sleep 60`, no `--media`/`--join`/`--unsafe`/
`--profiles`): **4 of 5 live calls used, all exit 0, no FLOOD_WAIT, no stop
condition.** Before every call: no stop flag, live-call counter below 5, and
the VPN route check

```
149.154.167.51 -> utun4
91.108.56.130 -> utun4
```

Redacted log lines (`<id>`/`@<channel>`; unredacted transcripts are kept in
the scratch dir as `live-1.txt` .. `live-4.txt`):

```
1. paperboy collect <id>          (full key, previously collected)
   INFO channel access: id=<id> via=saved_key granted=True
   channel | {'channels': 1, 'peers': 1}
2. paperboy collect <id>          (min peer, seen via a message in a full-key chat)
   INFO channel access: id=<id> via=from_message granted=True
   channel | {'channels': 1, 'peers': 1}
3. paperboy collect <id>          (same id again)
   INFO channel access: id=<id> via=saved_key granted=True
   channel | {'channels': 1, 'peers': 1}
4. paperboy collect @<channel>    (handle of the call-1 channel)
   INFO channel access: id=<id> via=handle granted=True
   channel | {'channels': 1, 'peers': 1}
```

SQL on the scratch store afterwards (run ids elided, ids redacted):

```
ChannelAccess rows, newest first:  handle | saved_key | from_message | saved_key
call 1: ChannelAccess(saved_key) then ChatFull; no ResolvedPeer in that run
call 2: ChannelAccess(from_message) then ChatFull; no ResolvedPeer in that run
        receipt input_channel = {"channel_id": <id>, "from_msg": {"access_hash": <n>, "channel_id": <n>, "msg_id": <id>}}
        peers row for the min channel afterwards:  is_min=0 | access_hash present
call 3: ChannelAccess(saved_key) then ChatFull; no ResolvedPeer
call 4: ResolvedPeer, then ChannelAccess(handle), then ChatFull
```

So every route behaved as specified: route 1 and route 2 make no `resolve`
call, the `getFullChannel` answer upgrades the `min` row to a full key (so
the repeat is route 1), and the handle path is unchanged apart from the
added receipt. No `CHANNEL_PRIVATE` refusal occurred, so the spare fifth call
was not used.
