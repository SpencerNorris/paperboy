# paperboy Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paperboy core — an empty repo up to a working, read-only
`paperboy collect <channel>` that stores channel metadata, full message history
with edit revisions and deletion tombstones, incremental `pts` sync, and a JSONL
export, all behind the guardrail/budget layer.

**Architecture:** Typer CLI → `collect-channel` recipe → `channel` and `history`
Collectors → a `Gateway` Protocol (Telethon behind it, returning `to_dict()`
dicts so collectors test against a `FakeGateway`) → every RPC through a `Budget`
gate → a `Store` (SQLite WAL) that keeps a raw-record log as system of record and
projects it into entity / history / edge tables keyed by URI ids.

**Tech Stack:** Python ≥3.12 (dev 3.14), uv, Telethon 1.44.x, Typer, stdlib
`sqlite3`, pydantic-settings, keyring, httpx, structlog+rich, pytest +
pytest-asyncio, ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-08-20-paperboy-design.md` (read §4–§10 before starting)

## Global Constraints

- Python floor `>=3.12`; target dev interpreter 3.14. Package name `paperboy`, src layout `src/paperboy/`.
- **Read-only always.** No task may call a Telegram method that sends, reacts, votes, joins, marks read, or mutates the account. `channel`/`history` never join.
- **Every Telegram RPC goes through `Budget.call(method_name, coro_factory)`** — no collector or gateway consumer calls Telethon directly except inside `TelethonGateway`, and `TelethonGateway` wraps each call in `Budget.call`.
- **Raw first:** every TL object received is written to `raw_records` before any projection; projections carry `source_raw_id`.
- Entity ids are URI strings: `tg:user:<id>`, `tg:channel:<id>`, `tg:chat:<id>`, `tg:msg:<channel_id>/<msg_id>`. Timestamps are ISO-8601 UTC text.
- Tri-state optional user/channel fields: distinguish present / absent-not-set / absent-privacy. Never record "no photo" for an absent value.
- Credentials (`api_hash`, session, phone, login code) never appear in logs; logs reference targets by URI id. Outbound HTTP only to `t.me`, `web.archive.org`.
- Commit after every green step. Conventional-commit messages. Branch `feat/core`; `main` is protected.

---

### Task 0: Project scaffold, CI, and the guardrail smoke gate

**Files:**
- Create: `pyproject.toml`, `src/paperboy/__init__.py`, `src/paperboy/py.typed`, `tests/__init__.py`, `tests/conftest.py`, `.github/workflows/ci.yml`, `ruff.toml`, `.python-version`
- Create: `docs/adr/0001-library.md` … `0004-sync.md`, `docs/opsec.md` (stubs filled in Task 15)

**Interfaces:**
- Produces: a `uv`-managed package importable as `paperboy`; `uv run pytest`, `uv run ruff check`, `uv run pyright` all green on an empty suite; CI running the same on push/PR.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "paperboy"
version = "0.0.0"
description = "Local read-only Telegram channel OSINT collector"
requires-python = ">=3.12"
dependencies = [
  "telethon==1.44.*",
  "typer>=0.12",
  "pydantic-settings>=2.4",
  "keyring>=25",
  "httpx>=0.27",
  "structlog>=24",
  "rich>=13",
]
[project.scripts]
paperboy = "paperboy.cli:app"
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "ruff>=0.6", "pyright>=1.1"]
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["."]   # lets `from tests.fakes import ...` resolve
[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
```

- [ ] **Step 2: Create package markers and a trivial test**

`src/paperboy/__init__.py`: `__version__ = "0.0.0"`. `src/paperboy/py.typed`: empty. `tests/conftest.py`: empty for now.
`tests/test_smoke.py`:
```python
import paperboy
def test_version():
    assert paperboy.__version__ == "0.0.0"
```

- [ ] **Step 3: Sync and run the toolchain**

Run: `uv sync && uv run pytest -q && uv run ruff check && uv run pyright`
Expected: 1 passed; ruff clean; pyright 0 errors.

- [ ] **Step 4: Write CI**

`.github/workflows/ci.yml` runs on push + pull_request: checkout, `astral-sh/setup-uv`, `uv sync`, then `uv run ruff check`, `uv run pyright`, `uv run pytest -q`.

- [ ] **Step 5: Write ADR and opsec stubs**

Each ADR: problem / options / decision / consequences, seeded from spec §12. `docs/opsec.md`: heading + "TODO: filled in Task 15".

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: scaffold uv project, CI, ADR and opsec stubs"
```

---

### Task 1: URI ids and time helpers

**Files:**
- Create: `src/paperboy/ids.py`, `tests/test_ids.py`

**Interfaces:**
- Produces: `channel_uri(id:int)->str`, `chat_uri(id:int)->str`, `user_uri(id:int)->str`, `msg_uri(channel_id:int, msg_id:int)->str`, `parse_uri(uri:str)->tuple[str,tuple[int,...]]`, `utc_now_iso()->str`, `to_iso(dt: datetime|int)->str`.

- [ ] **Step 1: Write the failing test**

```python
from paperboy.ids import channel_uri, msg_uri, parse_uri, to_iso
from datetime import datetime, timezone

def test_uris():
    assert channel_uri(123) == "tg:channel:123"
    assert msg_uri(123, 45) == "tg:msg:123/45"
    assert parse_uri("tg:msg:123/45") == ("msg", (123, 45))
    assert parse_uri("tg:user:9") == ("user", (9,))

def test_to_iso_normalizes_utc():
    assert to_iso(datetime(2026,1,2,3,4,5,tzinfo=timezone.utc)) == "2026-01-02T03:04:05+00:00"
    assert to_iso(1767322445) == "2026-01-02T03:04:05+00:00"
```

- [ ] **Step 2: Run to verify fail** — Run: `uv run pytest tests/test_ids.py -q` Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `ids.py`**

`*_uri` format strings; `parse_uri` splits on `:` then `/`, returns kind and int tuple, raises `ValueError` on malformed; `to_iso` accepts aware datetime (assert tzinfo, convert to UTC, `isoformat()`) or int epoch (`datetime.fromtimestamp(x, tz=utc)`); `utc_now_iso` = `to_iso(datetime.now(utc))`.

- [ ] **Step 4: Run to verify pass** — Expected: PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: URI ids and UTC time helpers"`

---

### Task 2: Target parsing

**Files:**
- Create: `src/paperboy/targets.py`, `tests/test_targets.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TargetKind` (Enum: `USERNAME, INVITE, MSG_LINK, PEER_ID, PHONE, HASHTAG`), frozen dataclass `Target(kind, raw, value, msg_id: int|None)`, `parse_target(text:str)->Target`, exception `UnsupportedTarget(Exception)`. `Target.is_channel_like` (property) is True for USERNAME/INVITE/MSG_LINK/PEER_ID.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from paperboy.targets import parse_target, TargetKind

@pytest.mark.parametrize("text,kind,value", [
    ("@durov", TargetKind.USERNAME, "durov"),
    ("https://t.me/durov", TargetKind.USERNAME, "durov"),
    ("t.me/+AbCdEf", TargetKind.INVITE, "AbCdEf"),
    ("t.me/joinchat/AbCdEf", TargetKind.INVITE, "AbCdEf"),
    ("https://t.me/durov/1234", TargetKind.MSG_LINK, "durov"),
    ("-1001234567890", TargetKind.PEER_ID, "-1001234567890"),
    ("+15551234567", TargetKind.PHONE, "+15551234567"),
    ("#osint", TargetKind.HASHTAG, "osint"),
])
def test_parse(text, kind, value):
    t = parse_target(text)
    assert t.kind == kind and t.value == value

def test_msg_link_captures_id():
    assert parse_target("t.me/durov/1234").msg_id == 1234

def test_channel_like():
    assert parse_target("@durov").is_channel_like
    assert not parse_target("#osint").is_channel_like
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement `targets.py`** — normalize by stripping scheme + `t.me/`; order checks: invite (`+`/`joinchat/`) → msg link (`name/digits`) → username (`@`/bare handle) → phone (`^\+\d{7,15}$`) → hashtag (`#`) → peer id (`^-?\d+$`). Unknown → `UnsupportedTarget`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: parse Telegram targets"`

---

### Task 3: Config, secrets, and redacted logging

**Files:**
- Create: `src/paperboy/config.py`, `src/paperboy/secrets.py`, `src/paperboy/logging_setup.py`, `tests/test_config.py`, `tests/test_logging_redaction.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings, env prefix `PAPERBOY_`, fields per spec §9 with defaults `require_proxy=True, min_session_age_days=7, flood_sleep_threshold=60, max_rpc_per_run=20000, profile_budget=2000, allow_join=False, allow_phone_lookup=False`, `data_dir`, `proxy: str|None`, `device: DeviceIdentity`, `api_id: int|None`), `load_settings(profile:str, overrides:dict)->Settings`, `profile_dir(settings, profile)->Path`; `SecretStore` protocol + `KeyringSecrets` + `MemorySecrets(dict)` with `get_api_hash/set_api_hash/get_session/set_session`; `configure_logging(path, console:bool)->None` installing a `RedactionFilter` that masks values registered via `register_secret(s:str)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_logging_redaction.py
import logging, json
from paperboy.logging_setup import configure_logging, register_secret

def test_secret_is_masked(tmp_path, capsys):
    logf = tmp_path/"p.log"
    configure_logging(logf, console=True)
    register_secret("hunter2SECRET")
    logging.getLogger("paperboy").warning("session=%s", "hunter2SECRET")
    assert "hunter2SECRET" not in logf.read_text()
    assert "***" in logf.read_text()
```
```python
# tests/test_config.py
from paperboy.config import load_settings
def test_env_override(monkeypatch):
    monkeypatch.setenv("PAPERBOY_MAX_RPC_PER_RUN", "5")
    s = load_settings("default", {})
    assert s.max_rpc_per_run == 5
    assert s.require_proxy is True
def test_cli_override_beats_env(monkeypatch):
    monkeypatch.setenv("PAPERBOY_PROFILE_BUDGET", "10")
    s = load_settings("default", {"profile_budget": 3})
    assert s.profile_budget == 3
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement.** `Settings` via `BaseSettings`; `load_settings` merges defaults←env←overrides (overrides win) and expands `data_dir` (`~/.local/share/paperboy` default). `RedactionFilter.filter` renders `record.getMessage()`, replaces each registered secret with `***`, stores it back on `record.msg` with `record.args=()`. `KeyringSecrets` uses `keyring.get/set_password("paperboy", f"{profile}:{k}")`. `MemorySecrets` for tests.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: settings, secret store, redacted logging"`

---

### Task 4: Store — schema, migrations, raw log

**Files:**
- Create: `src/paperboy/store/__init__.py`, `src/paperboy/store/db.py`, `src/paperboy/store/migrations/0001_init.sql`, `tests/test_store_migrations.py`

**Interfaces:**
- Produces: `Store.open(path:Path)->Store` (WAL, `foreign_keys=ON`, applies pending migrations, records them in `schema_migrations`), `Store.conn` (sqlite3.Connection, `row_factory=sqlite3.Row`), `Store.add_raw(kind:str, payload:dict, tier:str, context:dict|None)->int` (returns rowid; serialises with a canonical json dumper), `Store.close()`. Context-manager support.

- [ ] **Step 1: Write the failing test**

```python
from paperboy.store.db import Store

def test_migrations_and_raw(tmp_path):
    with Store.open(tmp_path/"p.sqlite") as st:
        rid = st.add_raw("channelFull", {"id": 1, "title": "x"}, tier="stranger", context={"target": "@x"})
        assert isinstance(rid, int)
        row = st.conn.execute("select kind, payload_json, tier from raw_records where id=?", (rid,)).fetchone()
        assert row["kind"] == "channelFull"
        assert '"title"' in row["payload_json"]
        applied = [r["name"] for r in st.conn.execute("select name from schema_migrations order by name")]
        assert "0001_init" in applied
        assert st.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Write `0001_init.sql`** — all tables from spec §5 (raw_records, channels, peers, messages, media, channel_snapshots, message_revisions, message_metrics, message_tombstones, edges, sync_state, sync_ranges, flood_log, custody_log, run_events, schema_migrations, plus `messages_fts` FTS5 external-content on messages.text with insert/update/delete triggers). Use `TEXT` for timestamps and json columns.

- [ ] **Step 4: Implement `db.py`** — `Store.open` connects, sets pragmas, globs `migrations/*.sql` sorted, applies any whose stem is not in `schema_migrations`, each in a transaction, then inserts the stem. `add_raw` inserts `(kind, observed_at=utc_now_iso(), tier, context_json, payload_json)` using `json.dumps(..., sort_keys=True, ensure_ascii=False, default=_json_default)` where `_json_default` handles bytes→base64 and datetime→to_iso.

- [ ] **Step 5: Run to verify pass.**

- [ ] **Step 6: Commit** — `git commit -m "feat: SQLite store with migrations and raw-record log"`

---

### Task 5: Store — message projection, revisions, tombstones, metrics

**Files:**
- Create: `src/paperboy/store/messages.py`, `tests/test_store_messages.py`
- Modify: `src/paperboy/store/db.py` (expose helpers or mixin)

**Interfaces:**
- Consumes: `Store`, `ids.msg_uri/to_iso`.
- Produces: `content_hash(text:str, media_json:str|None)->str` (sha256 hex of `text + "\x00" + media_json`); `upsert_message(store, channel_id:int, msg:dict, source_raw_id:int, observed_at:str, tier:str)->str` returns msg_uri and: inserts/updates `messages`; if `content_hash` differs from the latest stored, appends a `message_revisions` row; always appends a `message_metrics` row when views/forwards/replies present; `mark_deleted(store, channel_id:int, msg_id:int, evidence:str, observed_at:str)->None` sets `deleted_at` (only for `update`/`empty`) and appends a tombstone.

- [ ] **Step 1: Write the failing test**

```python
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message, mark_deleted

def _msg(mid, text, views=None, edit=None):
    m = {"_": "message", "id": mid, "date": 1767322445, "message": text, "peer_id": {"channel_id": 7}}
    if views is not None: m["views"] = views
    if edit is not None: m["edit_date"] = edit
    return m

def test_edit_appends_revision(tmp_path):
    with Store.open(tmp_path/"p.sqlite") as st:
        r1 = st.add_raw("message", _msg(10, "hello"), "stranger", None)
        u = upsert_message(st, 7, _msg(10, "hello", views=5), r1, "2026-01-01T00:00:00+00:00", "stranger")
        r2 = st.add_raw("message", _msg(10, "hello EDITED", edit=1767322500), "stranger", None)
        upsert_message(st, 7, _msg(10, "hello EDITED", views=9, edit=1767322500), r2, "2026-01-02T00:00:00+00:00", "stranger")
        revs = st.conn.execute("select text from message_revisions where message_uri=? order by observed_at", (u,)).fetchall()
        assert [r["text"] for r in revs] == ["hello", "hello EDITED"]
        metrics = st.conn.execute("select views from message_metrics where message_uri=? order by observed_at", (u,)).fetchall()
        assert [m["views"] for m in metrics] == [5, 9]

def test_tombstone_only_sets_deleted_for_update(tmp_path):
    with Store.open(tmp_path/"p.sqlite") as st:
        r = st.add_raw("message", _msg(11, "x"), "stranger", None)
        u = upsert_message(st, 7, _msg(11, "x"), r, "2026-01-01T00:00:00+00:00", "stranger")
        mark_deleted(st, 7, 11, "gap", "2026-01-03T00:00:00+00:00")
        assert st.conn.execute("select deleted_at from messages where uri=?", (u,)).fetchone()["deleted_at"] is None
        mark_deleted(st, 7, 11, "update", "2026-01-04T00:00:00+00:00")
        assert st.conn.execute("select deleted_at from messages where uri=?", (u,)).fetchone()["deleted_at"] is not None
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement `messages.py`** — extract text (`msg.get("message","")`), media json (`json.dumps(msg.get("media"))` or None), entities, fwd, reply fields; compute `content_hash`; read latest revision hash; `INSERT ... ON CONFLICT(uri) DO UPDATE` on `messages`; append revision iff hash changed (including first insert); append metrics row iff any of views/forwards/replies present. `mark_deleted`: append tombstone always; set `deleted_at` only when evidence in `{"update","empty"}`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: message projection with revisions, tombstones, metrics"`

---

### Task 6: Store — peers with min-provenance, channels, edges, sync state

**Files:**
- Create: `src/paperboy/store/peers.py`, `src/paperboy/store/channels.py`, `src/paperboy/store/sync.py`, `tests/test_store_peers.py`, `tests/test_store_sync.py`

**Interfaces:**
- Produces: `upsert_peer(store, obj:dict, source_raw_id, observed_at, *, seen_in_chat:int|None, seen_in_msg:int|None)->str` — respects `min`: if incoming is `min` and a non-min row exists, updates only `last_seen` + provenance; stores `is_min`, `access_hash`. `upsert_channel(store, full:dict, chan:dict, source_raw_id, observed_at)->str` + appends `channel_snapshots`. `add_edge(store, subject, predicate, object, observed_at, tier, source_raw_id, evidence:dict|None)`. `get_state(store, scope, key)->dict|None`, `set_state(store, scope, key, value:dict)`; `add_range(store, channel_id, lo, hi)` merging adjacent/overlapping ranges; `missing_ids(store, channel_id, lo, hi)->list[int]` returning ids in `[lo,hi]` not covered by any verified range.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_sync.py
from paperboy.store.db import Store
from paperboy.store.sync import add_range, missing_ids

def test_range_merge_and_gap(tmp_path):
    with Store.open(tmp_path/"p.sqlite") as st:
        add_range(st, 7, 1, 5)
        add_range(st, 7, 6, 10)     # adjacent -> merges to 1..10
        add_range(st, 7, 20, 25)
        assert missing_ids(st, 7, 1, 25) == list(range(11, 20))
        rows = st.conn.execute("select lo,hi from sync_ranges where channel_id=7 order by lo").fetchall()
        assert [(r["lo"], r["hi"]) for r in rows] == [(1,10),(20,25)]
```
```python
# tests/test_store_peers.py
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer

def test_min_does_not_clobber_full(tmp_path):
    with Store.open(tmp_path/"p.sqlite") as st:
        full = {"_":"user","id":9,"access_hash":111,"username":"real","first_name":"Real"}
        r1 = st.add_raw("user", full, "member", None)
        upsert_peer(st, full, r1, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        mn = {"_":"user","id":9,"min":True,"first_name":"MinName"}
        r2 = st.add_raw("user", mn, "stranger", None)
        upsert_peer(st, mn, r2, "2026-01-02T00:00:00+00:00", seen_in_chat=7, seen_in_msg=34)
        row = st.conn.execute("select username, first_name, is_min, seen_in_msg from peers where uri='tg:user:9'").fetchone()
        assert row["username"] == "real" and row["first_name"] == "Real"  # not clobbered
        assert row["seen_in_msg"] == 34  # provenance updated
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement.** Peer kind from `obj["_"]`; uri via ids; merge rule per spec §5/min semantics. `add_range` reads existing ranges, inserts new, coalesces where `lo <= existing.hi+1` and `hi >= existing.lo-1`. `missing_ids` computes complement over `[lo,hi]`. `get/set_state` store json in `sync_state(scope,key,value_json)`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: peer/channel/edge projections and sync range math"`

---

### Task 7: Budget and error classification

**Files:**
- Create: `src/paperboy/budget.py`, `src/paperboy/errors.py`, `tests/test_budget.py`

**Interfaces:**
- Consumes: `Store` (for `flood_log`), `Settings`.
- Produces: exceptions `HardStop`, `PhaseStop`, `SkipAndRecord`; `Disposition` enum `RETRY|SKIP|PHASE_STOP|HARD_STOP`; `classify(exc)->Disposition` mapping Telethon errors per spec §8 (`FloodWaitError` → RETRY if `seconds<=threshold` else PHASE_STOP; `ChatAdminRequiredError`, `ChannelPrivateError`, `MsgIdInvalidError`, `BroadcastForbiddenError`, premium → SKIP; `PeerFloodError`, `FrozenMethodInvalidError`, `AuthKeyDuplicatedError` → HARD_STOP); `Budget(settings, store, clock=time)` with `async call(method:str, factory:Callable[[], Awaitable[T]])->T` enforcing per-method min interval, persisted cooldown (`flood_log`), and `max_rpc_per_run` cap (raises `HardStop` when exceeded); `sleeper` injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from paperboy.budget import Budget, classify, Disposition, HardStop
from paperboy.errors import FakeFlood, FakePeerFlood  # thin test doubles mirroring telethon errors

@pytest.mark.asyncio
async def test_flood_short_is_retryable():
    assert classify(FakeFlood(3)) == Disposition.RETRY

@pytest.mark.asyncio
async def test_rpc_cap(tmp_path):
    from paperboy.store.db import Store
    from paperboy.config import load_settings
    s = load_settings("default", {"max_rpc_per_run": 2})
    with Store.open(tmp_path/"p.sqlite") as st:
        slept=[]
        b = Budget(s, st, sleeper=lambda x: slept.append(x))
        async def ok(): return 1
        await b.call("m", ok); await b.call("m", ok)
        with pytest.raises(HardStop):
            await b.call("m", ok)
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement.** `classify` uses `isinstance` against telethon error classes imported lazily (and the test doubles by duck-typing on a `.seconds`/marker). `Budget.call`: increment counter (cap → HardStop before calling); check per-method last-call time, `await sleeper(min_interval - delta)` if needed; check `flood_log` cooldown; run factory; on exception, `d=classify(e)`: RETRY → sleep `seconds` (persist to flood_log) and retry once, else re-raise as the mapped exception type. `errors.py` holds `FakeFlood`/`FakePeerFlood` doubles used only in tests plus a `telethon_error_map`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: budget gate and RPC error classification"`

---

### Task 8: Gateway Protocol + FakeGateway + TelethonGateway

**Files:**
- Create: `src/paperboy/gateway.py`, `tests/fakes.py`, `tests/test_gateway_fake.py`

**Interfaces:**
- Consumes: `Budget`.
- Produces: `Gateway` Protocol with async methods used by core: `resolve(target_value:str)->dict` (returns `{"chats":[...], "users":[...]}` shaped like `contacts.resolveUsername.to_dict()`), `get_full_channel(input_channel:dict)->dict`, `iter_history(input_channel:dict, *, offset_id:int, limit:int)->AsyncIterator[dict]`, `get_messages(input_channel:dict, ids:list[int])->list[dict]`, `get_channel_difference(input_channel:dict, pts:int, limit:int)->dict`, `get_self()->dict`. `FakeGateway(fixtures:dict)` replaying recorded dicts. `TelethonGateway(client, budget)` implementing the Protocol, each method building the Telethon request and returning `result.to_dict()` via `budget.call`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from tests.fakes import FakeGateway

@pytest.mark.asyncio
async def test_fake_history_pages():
    fx = {"history": [ {"_":"message","id":i,"message":f"m{i}","date":1767322445} for i in (3,2,1) ]}
    gw = FakeGateway(fx)
    ids = [m["id"] async for m in gw.iter_history({"channel_id":7}, offset_id=0, limit=100)]
    assert ids == [3,2,1]
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement.** `Gateway` as `typing.Protocol`. `FakeGateway` yields from fixture lists, supports `offset_id` slicing so resume tests work, returns fixture dicts for `resolve`/`get_full_channel`/`get_self`. `TelethonGateway` wraps `telethon.tl.functions.*` requests; every call is `await self.budget.call("channels.getFullChannel", lambda: self.client(GetFullChannelRequest(...)))` then `.to_dict()`. Put `tests/fakes.py` under `tests/` (pytest `pythonpath=["."]` from Task 0 makes `from tests.fakes import ...` resolve).

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: gateway seam with fake and telethon implementations"`

---

### Task 9: CollectContext and the Collector protocol

**Files:**
- Create: `src/paperboy/collectors/__init__.py`, `src/paperboy/collectors/base.py`, `tests/test_collector_base.py`

**Interfaces:**
- Produces: dataclass `CollectContext(gateway:Gateway, store:Store, settings:Settings, target:Target, input_channel:dict|None, channel_id:int|None, tier:str, log)`; `Collector` Protocol (`name:str`, `applies_to(target:Target)->bool`, `async collect(ctx:CollectContext)->CollectResult`); dataclass `CollectResult(name:str, counts:dict[str,int], stopped:str|None)`.

- [ ] **Step 1..5:** Trivial dataclass/Protocol test (instantiate a dummy collector, assert `applies_to`, assert `CollectResult` fields). Implement. Commit `feat: collector protocol and context`.

---

### Task 10: `channel` collector

**Files:**
- Create: `src/paperboy/collectors/channel.py`, `tests/test_collector_channel.py`, `tests/fixtures/tl/resolve_durov.json`, `tests/fixtures/tl/full_channel.json`

**Interfaces:**
- Consumes: `CollectContext`, store `upsert_channel/upsert_peer`, gateway `resolve/get_full_channel/get_self`.
- Produces: `ChannelCollector` — resolves the target to a `Channel` dict (raw-logged), calls `get_full_channel`, upserts channel + snapshot, records `linked_chat_id`/`linked_group` edge, sets `ctx.input_channel` and `ctx.channel_id`, records self admin-rights into `ctx.tier`. Returns counts `{channels:1, peers:N}`.

- [ ] **Step 1: Write the failing test** (fixtures are hand-authored minimal `to_dict()` shapes until the spike replaces them):

```python
import json, pytest
from pathlib import Path
from tests.fakes import FakeGateway
from paperboy.store.db import Store
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.base import CollectContext
from paperboy.targets import parse_target
from paperboy.config import load_settings

FX = Path("tests/fixtures/tl")

@pytest.mark.asyncio
async def test_channel_collector(tmp_path):
    gw = FakeGateway({
      "resolve": json.loads((FX/"resolve_durov.json").read_text()),
      "full_channel": json.loads((FX/"full_channel.json").read_text()),
      "self": {"_":"user","id":1,"self":True},
    })
    with Store.open(tmp_path/"p.sqlite") as st:
        ctx = CollectContext(gw, st, load_settings("default", {}), parse_target("@durov"),
                             None, None, "stranger", __import__("logging").getLogger("t"))
        res = await ChannelCollector().collect(ctx)
        assert ctx.channel_id is not None
        row = st.conn.execute("select title, participants_count from channels").fetchone()
        assert row["participants_count"] >= 0
        assert res.counts["channels"] == 1
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Author minimal fixtures** — `resolve_durov.json`: `{"chats":[{"_":"channel","id":5,"access_hash":99,"title":"Durov","username":"durov","broadcast":true}],"users":[]}`. `full_channel.json`: `{"full_chat":{"_":"channelFull","id":5,"participants_count":100,"about":"x","pts":42,"linked_chat_id":0},"chats":[{"_":"channel","id":5,"access_hash":99,"title":"Durov","username":"durov"}]}`.

- [ ] **Step 4: Implement `channel.py`** — `applies_to`=`target.is_channel_like`; resolve, raw-log with tier, pick the `channel` chat, build `input_channel={"channel_id":id,"access_hash":ah}`, `get_full_channel`, raw-log, `upsert_channel`, snapshot, seed `sync_state('channel', str(id), {"pts": full_chat["pts"]})`, upsert peers from `chats`/`users`. Set ctx fields.

- [ ] **Step 5: Run to verify pass.**

- [ ] **Step 6: Commit** — `git commit -m "feat: channel metadata collector"`

---

### Task 11: `history` collector — backfill + gap tombstones

**Files:**
- Create: `src/paperboy/collectors/history.py`, `tests/test_collector_history.py`

**Interfaces:**
- Consumes: `CollectContext`, gateway `iter_history/get_messages`, store `upsert_message/upsert_peer/add_range/missing_ids/mark_deleted/add_edge/set_state`.
- Produces: `HistoryCollector` with `async collect(ctx)` (backfill) — pages `iter_history` newest→oldest, `upsert_message` each, records covered `sync_ranges`, upserts `min` peers with `(channel, msg_id)` provenance, adds `forwarded_from` edges from `fwd_from`; after backfill, `missing_ids` over the observed span → `get_messages` → `messageEmpty` ids get `mark_deleted(evidence="empty")`. Returns counts `{messages, revisions, tombstones, edges}`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from tests.fakes import FakeGateway
from paperboy.store.db import Store
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.base import CollectContext
from paperboy.targets import parse_target
from paperboy.config import load_settings

def _m(i): return {"_":"message","id":i,"message":f"m{i}","date":1767322445,"peer_id":{"channel_id":5}}

@pytest.mark.asyncio
async def test_backfill_detects_gap(tmp_path):
    # ids 5,4,2,1 present; id 3 is a hole -> get_messages returns messageEmpty for 3
    gw = FakeGateway({
      "history": [_m(5), _m(4), _m(2), _m(1)],
      "get_messages": {3: {"_":"messageEmpty","id":3}},
    })
    with Store.open(tmp_path/"p.sqlite") as st:
        ctx = CollectContext(gw, st, load_settings("default", {}), parse_target("@x"),
                             {"channel_id":5,"access_hash":9}, 5, "stranger", __import__("logging").getLogger("t"))
        res = await HistoryCollector().collect(ctx)
        assert res.counts["messages"] == 4
        tomb = st.conn.execute("select message_uri, evidence from message_tombstones").fetchall()
        assert tomb and tomb[0]["evidence"] == "empty"
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement `history.py`** — loop `iter_history(offset_id=cursor, limit=100)` until empty; raw-log each message, `upsert_message`; track min/max id; after the sweep `add_range(min,max)` for contiguous runs (record each page's id set, coalesce); `missing = missing_ids(channel, min, max)`; chunk `get_messages` (≤200); for each returned `messageEmpty`, `mark_deleted(evidence="empty")`. Commit cursor to `sync_state('history', str(channel), {"offset_id": min_seen})` each page.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: history backfill with gap-based deletion tombstones"`

---

### Task 12: `history` catch-up via getChannelDifference

**Files:**
- Modify: `src/paperboy/collectors/history.py` (add `catch_up`)
- Create: `tests/test_history_catchup.py`

**Interfaces:**
- Produces: `HistoryCollector.catch_up(ctx)` reading stored `pts`, calling `get_channel_difference`, applying `new_messages` (upsert), `other_updates` (`updateEditChannelMessage`→upsert new revision; `updateDeleteChannelMessages`→`mark_deleted(evidence="update")`), advancing and persisting `pts`; handles `channelDifferenceTooLong` by re-seeding pts from the payload and returning a flag `resynced=True`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from tests.fakes import FakeGateway
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.base import CollectContext
from paperboy.store.sync import set_state
from paperboy.targets import parse_target
from paperboy.config import load_settings

@pytest.mark.asyncio
async def test_catchup_applies_edit_and_delete(tmp_path):
    diff = {"_":"updates.channelDifference","final":True,"pts":50,
            "new_messages":[{"_":"message","id":20,"message":"new","date":1767322445,"peer_id":{"channel_id":5}}],
            "other_updates":[
              {"_":"updateEditChannelMessage","message":{"_":"message","id":10,"message":"edited","date":1767322445,"edit_date":1767322900,"peer_id":{"channel_id":5}}},
              {"_":"updateDeleteChannelMessages","messages":[11]},
            ],"chats":[],"users":[]}
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path/"p.sqlite") as st:
        # seed msg 10 and 11 and pts
        for mid in (10,11):
            r = st.add_raw("message", {"_":"message","id":mid,"message":"orig","date":1767322445,"peer_id":{"channel_id":5}}, "stranger", None)
            upsert_message(st, 5, {"_":"message","id":mid,"message":"orig","date":1767322445,"peer_id":{"channel_id":5}}, r, "2026-01-01T00:00:00+00:00", "stranger")
        set_state(st, "channel", "5", {"pts": 40})
        ctx = CollectContext(gw, st, load_settings("default", {}), parse_target("@x"), {"channel_id":5,"access_hash":9}, 5, "stranger", __import__("logging").getLogger("t"))
        await HistoryCollector().catch_up(ctx)
        assert st.conn.execute("select deleted_at from messages where uri='tg:msg:5/11'").fetchone()["deleted_at"] is not None
        assert st.conn.execute("select text from messages where uri='tg:msg:5/10'").fetchone()["text"] == "edited"
        assert st.conn.execute("select value_json from sync_state where scope='channel' and key='5'").fetchone()["value_json"].find("50") >= 0
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement `catch_up`** per interface; raw-log the difference; branch on `_`.

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: pts catch-up applying edits and deletions"`

---

### Task 13: Recipe orchestrator

**Files:**
- Create: `src/paperboy/recipes.py`, `tests/test_recipe.py`

**Interfaces:**
- Consumes: collectors, `CollectContext`, `Budget`.
- Produces: `async collect_channel(gateway, store, settings, target, phases:list[str]|None, log)->list[CollectResult]` — runs `channel` first (populates ctx), then `history` (+`catch_up`), honoring `phases` filter; catches `PhaseStop` (records, continues) and `HardStop` (records, aborts remaining); returns results. Writes a `run_events` row per phase.

- [ ] **Step 1: Write the failing test** — with `FakeGateway` fixtures for resolve+full+history, assert `collect_channel(..., phases=["channel","history"])` returns two results and populates `channels` + `messages`. Assert a `HardStop` from a stubbed collector aborts later phases (inject a fake collector list).

- [ ] **Step 2–4:** implement, run.

- [ ] **Step 5: Commit** — `git commit -m "feat: collect-channel recipe orchestrator"`

---

### Task 14: JSONL export

**Files:**
- Create: `src/paperboy/export/__init__.py`, `src/paperboy/export/jsonl.py`, `tests/test_export_jsonl.py`

**Interfaces:**
- Produces: `export_jsonl(store, channel_uri:str, out_dir:Path)->dict[str,int]` writing `channel.jsonl`, `messages.jsonl` (current state, with a `revisions` array and `deleted_at`), `edges.jsonl`; scrubs the collecting account's own peer row (drop rows where `tier=='self'` / `out` flag). Returns counts.

- [ ] **Step 1: Write the failing test** — seed a store via prior helpers, export, assert `messages.jsonl` line count and that an edited message carries a `revisions` list of length 2.

- [ ] **Step 2–4:** implement (stream rows, `json.dumps` per line), run.

- [ ] **Step 5: Commit** — `git commit -m "feat: jsonl export view"`

---

### Task 15: Doctor preflight + opsec doc

**Files:**
- Create: `src/paperboy/doctor.py`, `tests/test_doctor.py`
- Modify: `docs/opsec.md` (fill from spec §3)

**Interfaces:**
- Produces: dataclass `Check(name, ok:bool, detail:str, severity:'fail'|'warn')`; `async run_doctor(gateway, settings)->list[Check]` checking: proxy configured when `require_proxy`; session age ≥ `min_session_age_days` (via `gateway.get_authorizations` — add to Protocol + fakes); 2FA present (`gateway.get_password_state`); privacy keys restrictive (`gateway.get_privacy` for phone/lastseen/photo); minimal profile (no username/photo/bio on self). `doctor_blocks(checks)->bool` True if any `fail` and not overridden.

- [ ] **Step 1: Write the failing test** — with a `FakeGateway` returning a young session and no proxy, assert `run_doctor` yields a `fail` for proxy and session age and `doctor_blocks` is True; with a compliant fake, all pass.

- [ ] **Step 2–4:** implement; extend Protocol + `FakeGateway` + `TelethonGateway` with the read-only account methods. Fill `docs/opsec.md`.

- [ ] **Step 5: Commit** — `git commit -m "feat: opsec doctor preflight and runbook"`

---

### Task 16: CLI wiring

**Files:**
- Create: `src/paperboy/cli.py`, `src/paperboy/app.py` (composition root building gateway/store/budget), `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: Typer `app` with `auth`, `doctor`, `collect`, `status`, `export` commands per spec §9 (`watch`/`lookup` are Phase 2 stubs raising a clear "not in core" message). `collect` refuses to run if `doctor_blocks` unless `--unsafe`; wires overrides→`load_settings`; opens store at `profile_dir/paperboy.sqlite`; registers session/api_hash as secrets for redaction; runs `collect_channel`; prints a rich summary.

- [ ] **Step 1: Write the failing test** — `typer.testing.CliRunner`: `paperboy --help` exits 0 and lists commands; `paperboy collect @x` with a monkeypatched `build_gateway` returning a `FakeGateway` and a temp profile writes a sqlite file and exits 0; `paperboy doctor` on a non-compliant fake exits non-zero.

- [ ] **Step 2–4:** implement `app.py` (`build_gateway(settings, secrets)`, `build_store(...)`) and `cli.py` (thin Typer wrappers, `asyncio.run`), run.

- [ ] **Step 5: Commit** — `git commit -m "feat: CLI commands wired to the collect-channel recipe"`

---

### Task 17: Definition-of-Done smoke against a live channel

**Files:**
- Modify: none (produces a transcript for the DoD report)
- Create: `docs/features/collect-channel.md`

**Interfaces:** none.

- [ ] **Step 1:** Operator provides `api_id`/`api_hash` + an aged research session and a public channel. Run `uv run paperboy auth --profile smoke`, then `uv run paperboy doctor --profile smoke`.
- [ ] **Step 2:** `uv run paperboy collect @<public_channel> --profile smoke --phases channel,history`; capture stdout, exit code, and `paperboy status`.
- [ ] **Step 3:** Cover the §13 spike items reachable here: `getHistory` un-joined succeeds; interrupt with Ctrl-C mid-backfill and re-run to confirm resume; force a `CHAT_ADMIN_REQUIRED` skip (request an admin-only method against the channel) and confirm the phase continues; confirm a `FLOOD_WAIT` sleep is logged under load.
- [ ] **Step 4:** `uv run paperboy export @<channel> --format jsonl --out /tmp/out`; confirm row counts match `status`.
- [ ] **Step 5:** Write `docs/features/collect-channel.md` (purpose, inputs/outputs, edge cases, operational notes) and paste the transcript into the DoD report. Commit `docs: collect-channel feature doc + DoD transcript`.
