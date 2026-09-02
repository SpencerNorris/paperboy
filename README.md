# paperboy

A local, read-only command-line tool that collects everything obtainable about a
Telegram **channel or supergroup** into a single SQLite database — for
open-source investigation and journalism. Everything runs on your machine;
nothing is uploaded.

paperboy is built as an *entity graph* (peers, messages, edges) so that today's
channel collection and tomorrow's user dossiers, phone lookups, and watchlists
share one store and one set of guardrails.

## Status

**Phase 1 (core) works end to end against live Telegram:**

| Capability | State |
|---|---|
| Channel/supergroup metadata + snapshots | ✅ |
| Full message history (un-joined for public channels) | ✅ |
| Edit revisions + deletion tombstones (gap-detected) | ✅ |
| `pts`-based incremental sync (edits & deletions) | ✅ |
| Raw-TL preservation + normalized projections + FTS5 | ✅ |
| JSONL export | ✅ |
| Opsec preflight (`paperboy doctor`) | ✅ |
| Comment threads, people & profiles (the person layer), forward/mention/similar-channel graph, media download, web-archive snapshots | ✅ Phase 2 |
| `watch`, phone `lookup` | 🔜 Phase 2 (not yet implemented) |

## Requirements

- **Python ≥ 3.12** and [**uv**](https://docs.astral.sh/uv/).
- **macOS, Windows, or Linux.** Credentials are stored in your OS's native
  encrypted keychain via the [`keyring`](https://pypi.org/project/keyring/)
  library — macOS Keychain, Windows Credential Manager, or the Linux Secret
  Service (GNOME Keyring / KWallet). Currently tested on macOS. Headless Linux
  and WSL boxes without a Secret Service need extra setup — tracked in
  [issue #10](https://github.com/SpencerNorris/paperboy/issues/10).
- A **dedicated Telegram account** for research (not your personal one — see
  [Safety](#safety--guardrails)) and its `api_id` / `api_hash` from
  [my.telegram.org](https://my.telegram.org).

## Install

```bash
git clone https://github.com/SpencerNorris/paperboy.git
cd paperboy
uv sync
uv run paperboy --help
```

`uv run paperboy …` runs the CLI inside the project environment. (Not yet
published to PyPI.)

## Quick start

### 1. Get your API credentials

Log in at [my.telegram.org](https://my.telegram.org) with your **research
account's** phone number (the code arrives in your Telegram app), open **API
development tools**, and create an app to obtain an `api_id` (integer) and
`api_hash` (32-hex string).

### 2. Store the credentials and log in

Credentials go in your OS keychain — never a file, never the repo. Two ways:

```bash
# a) guided helper scripts (run them yourself; hidden input, nothing printed)
uv run python scripts/store_api.py        # paste api_id, then api_hash
uv run python scripts/login.py            # enter phone + the login code

# b) or the built-in interactive command
uv run paperboy auth                      # prompts for phone + code, saves session
```

Everything is stored under the **profile** name `default`. Use
`--profile <name>` throughout to keep separate accounts and databases (one per
investigation you want kept unlinkable).

### 3. Run the opsec preflight

```bash
uv run paperboy doctor
```

`doctor` checks your account's posture — proxy configured, session age, 2FA,
privacy settings, minimal profile — and **blocks `collect` if any check fails**.
Fix the account (see [`docs/opsec.md`](docs/opsec.md)); `--unsafe` overrides the
gate for throwaway testing only.

### 4. Collect a channel

```bash
uv run paperboy collect @durov
```

This resolves the channel, pulls its full metadata and message history
(**without joining** — public channels are readable un-joined), records edit
revisions, and detects deletions as tombstones. It prints a per-phase summary:

```
 phase    counts                                                   stopped
 channel  {'channels': 1, 'peers': 2}                              -
 history  {'messages': 476, 'revisions': 476, 'tombstones': 67, …} -
```

Useful flags: `--phases channel,history` (run a subset), `--profile <name>`,
and `--unsafe` (bypass the doctor gate — only when you accept the posture).
`collect` is idempotent and resumable: re-running continues sync and captures
new edits/deletions.

### 5. Inspect what you collected

```bash
uv run paperboy status @durov          # counts for one channel
uv run paperboy status                 # everything in the profile
```

The store is a plain SQLite file with an FTS index, so you can also browse it
with [Datasette](https://datasette.io):

```bash
uvx datasette ./data/default/paperboy.sqlite
```

### 6. Export

```bash
uv run paperboy export @durov --format jsonl --out ./out
# writes channel.jsonl, messages.jsonl (with a revisions[] array), edges.jsonl
```

The collecting account's own record is scrubbed from exports.

## Command reference

| Command | Purpose |
|---|---|
| `paperboy auth` | Interactive login; saves the session to the Keychain. |
| `paperboy doctor` | Opsec preflight; blocks `collect` on failure unless `--unsafe`. |
| `paperboy collect TARGET [--phases …] [--unsafe] [--profile P]` | Collect channel metadata, history, the linked group's roster, and per-user profiles. |
| `paperboy collect TARGET --profiles [--profile-budget N]` | Also run full profile enrichment (`getFullUser`, photo history, avatars) — the expensive opt-in on top of the always-on `getUsers` triage. |
| `paperboy status [TARGET] [--profile P]` | Summarize stored data. |
| `paperboy export TARGET --format jsonl --out DIR [--profile P]` | Export to JSONL. |
| `paperboy reproject [--out PATH] [--phases …] [--profile P]` | Rebuild every projection from `raw_records` into a fresh DB — offline, no network, no credentials. |
| `paperboy watch` / `paperboy lookup` | Phase 2 — not implemented (exit with a notice). |

`TARGET` accepts `@username`, `t.me/name`, `t.me/name/123`, an invite link, or a
numeric peer id.

## Configuration

Settings resolve **CLI flag > `PAPERBOY_*` env var > `config.toml` > default**.
Secrets (`api_hash`, session) live only in the Keychain. Key settings:

| Setting (`PAPERBOY_…`) | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | Root for `<data_dir>/<profile>/paperboy.sqlite` + media (repo-relative). |
| `PROXY` | *(unset)* | `socks5://…` or `mtproxy://…` to route Telegram traffic. |
| `REQUIRE_PROXY` | `true` | `doctor` fails (and `collect` refuses) without a proxy. |
| `MIN_SESSION_AGE_DAYS` | `7` | Guards bulk work on fresh accounts. |
| `FLOOD_SLEEP_THRESHOLD` | `60` | Sleep through `FLOOD_WAIT`s ≤ this; stop the phase above it. |
| `MAX_RPC_PER_RUN` | `20000` | Hard per-run request cap. |
| `ALLOW_JOIN` / `ALLOW_PHONE_LOOKUP` | `false` | Off-by-default flag-gated behaviors (Phase 2). |

## Where your data lives

```
./data/<profile>/        # in the repo dir by default; gitignored
  paperboy.sqlite     # system of record: raw_records + normalized tables + FTS5
  paperboy.log        # credential-redacted JSON log
  media/              # (Phase 2) downloaded files, content-addressed
```

The default data dir is `./data` (relative to where you run `paperboy`), so
collected data lands next to the code and is gitignored. Override it with
`PAPERBOY_DATA_DIR`. Keep it on an **encrypted volume** — FileVault on macOS,
LUKS on Linux, BitLocker/VeraCrypt on Windows. Every TL object is stored raw in
`raw_records` before it is projected into the normalized tables, so a Telegram
schema bump can be re-parsed rather than re-scraped.

## What's in the database

The store is one SQLite file with an FTS index — browse it with
[Datasette](https://datasette.io) (`uvx datasette ./data/default/paperboy.sqlite`)
or query it with `sqlite3`. The tables you'll actually read:

| Table | What it holds |
|---|---|
| `channels` | the channel itself — id, username, title, `participants_count`, about, flags |
| `messages` | current state of every message — `text`, `date`, `from_uri`, `media_kind`, `deleted_at`, `content_hash` |
| `message_revisions` | append-only edit history — every version of a message you've observed |
| `message_tombstones` | deleted messages, with how the deletion was detected (`update`/`empty`/`gap`) |
| `message_metrics` | time series of `views` / `forwards` / `replies` per message |
| `edges` | the graph — `(subject, predicate, object)`, e.g. `forwarded_from` |
| `peers` | every user/channel seen, with `min`-provenance |
| `raw_records` | every raw Telegram object as received — the system of record |
| `messages_fts` | full-text index over message text |

A few useful queries:

```sql
-- recent posts with view counts
SELECT m.msg_id, substr(m.date,1,10) AS date, m.text, mm.views
FROM messages m LEFT JOIN message_metrics mm ON mm.message_uri = m.uri
WHERE m.text <> '' ORDER BY m.msg_id DESC LIMIT 20;

-- full-text search
SELECT m.msg_id, m.text
FROM messages_fts f JOIN messages m ON m.rowid = f.rowid
WHERE messages_fts MATCH 'your query' ORDER BY m.msg_id DESC;

-- what got deleted
SELECT message_uri, evidence, observed_at FROM message_tombstones;
```

**Full column-by-column reference: [`docs/data-model.md`](docs/data-model.md).**
Note that message ids are sequential per channel but only non-deleted,
non-service messages carry text — gaps are deletions (see `message_tombstones`),
not missing data.

## Safety & guardrails

paperboy is deliberately constrained (enforced in code, not just docs):

- **Read-only.** It never sends, reacts, votes, joins (without an explicit
  `--join`, Phase 2), or otherwise mutates the account. Reading a public channel
  is invisible to its admins.
- **Rate-respecting.** Every request passes a budget gate; `FLOOD_WAIT` is
  honored per method and `PEER_FLOOD` is a hard stop.
- **Credential-safe.** Secrets stay in the Keychain and are redacted from logs.
- **Opsec-aware.** `doctor` refuses to collect from an unhardened account.

Read [`docs/opsec.md`](docs/opsec.md) before your first real collection — it
covers account acquisition, proxies, handling collected files (which can phone
home when opened), and compartmentalization.

## Development

```bash
uv run pytest -q          # unit + integration tests
uv run ruff check         # lint
uv run pyright            # type-check
```

## Documentation

- [`docs/opsec.md`](docs/opsec.md) — operator security runbook.
- [`docs/data-model.md`](docs/data-model.md) — the database codebook (every table and column).
- [`docs/features/collect-channel.md`](docs/features/collect-channel.md) — the core feature, with the live smoke transcript.
- [`docs/features/reproject.md`](docs/features/reproject.md) — rebuild projections from raw, offline, with the real-archive smoke transcript.
- [`docs/research/telegram-extraction-surface.md`](docs/research/telegram-extraction-surface.md) — what the Telegram API does and does not expose, by access tier.
- [`docs/superpowers/specs/2026-08-20-paperboy-design.md`](docs/superpowers/specs/2026-08-20-paperboy-design.md) — the design.
- [`docs/adr/`](docs/adr/) — architecture decisions (library, storage, guardrails, sync).

## Disclaimer

paperboy is an unofficial third-party client built on the Telegram API. It is
not affiliated with or endorsed by Telegram. It only reads data the logged-in
account is already permitted to see; it does not circumvent privacy settings.
Using it is subject to Telegram's Terms of Service, API Terms of Service and
Content Licensing Terms — in particular, data collected with it must not be
used to train, fine-tune or evaluate AI/ML systems. Automated use of a Telegram
account can result in that account being limited or banned. You are
responsible for ensuring your use is lawful in your jurisdiction and
proportionate to a legitimate purpose, and for handling any personal data you
collect accordingly.
