# `reproject` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `paperboy reproject` command that rebuilds every normalized projection from `raw_records` into a fresh SQLite DB by replaying the raw log through the real collectors — offline, zero network, zero credentials, original timestamps preserved.

**Architecture:** A `RawReplayGateway` (plus `RawReplayWebClient`) implements the existing `Gateway` seam backed by a source DB's `raw_records`, so `recipes.collect_channel` runs *unchanged* against it into a fresh target DB. A new `Clock` seam threads each raw record's original `observed_at` into every projection site, making the projection a pure function of the raw log (the §7 round-trip identity test proves it).

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, Typer, pytest + pytest-asyncio, ruff + pyright. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-25-reproject-design.md`

## Global Constraints

- **Zero network:** `reproject` constructs only the replay pair — never `TelethonGateway`, a real `WebClient`, a Telethon session, or `Budget` (spec §2). Asserted by test.
- **Zero credentials:** no keychain access at all on the reproject path (spec §8). Asserted by test (keyring monkeypatched to raise).
- **Source is read-only:** the source DB is opened `mode=ro`; only `raw_records` is consulted for *rebuilding*. (The CLI's diff summary counts source projection rows — a read-only report, spec §6 mandates it.)
- **Timestamp fidelity:** projections in the target DB carry the original `observed_at` from the raw log, not reproject-time (spec §5).
- **Media:** no file is re-downloaded or re-written; bytes are read back from the source profile's content-addressed store (spec §4).
- The one write path (`join_channel`) is replay-simulated (synthetic success dict, zero side effects) — a reproject never joins anything.
- `uv run pytest -q`, `uv run ruff check`, `uv run pyright` must all pass after every task.

## Locked design decisions (read before implementing anything)

These resolve everything spec §5/§3 leaves open. Do not re-derive them.

**D1 — one timestamp per raw record, computed by the collector, passed into `add_raw`.**
Today `Store.add_raw` stamps its own `utc_now_iso()` while collectors stamp a
*separate* `utc_now_iso()` for projections — two different strings microseconds
apart. `add_raw` gains an `observed_at: str | None = None` parameter (None →
`utc_now_iso()`, so existing callers/tests are unaffected); every collector
call site passes the timestamp it will also use for the projections of that
record. Post-refactor invariant: **a raw record and every projection row
derived from it carry the same `observed_at`** — which is what makes replay
able to reproduce projection timestamps from raw alone.

**D2 — the `Clock` seam is payload-keyed, not call-order-keyed.**
`CollectContext` gains `clock: Clock` (default `LiveClock`). Collectors call
`ctx.clock.for_payload(payload)` **immediately after the gateway call that
produced `payload`** and reuse the captured variable for `add_raw` + every
projection from it. `LiveClock.for_payload` ignores the payload and returns
`utc_now_iso()` (live behavior unchanged). `ReplayClock` is a registry fed by
the replay gateway as it serves records: it maps `dumps(payload)` → the source
record's `observed_at`. Payload-keyed lookup (not a mutable "current" value) is
what survives `history`'s pattern of consuming a whole page into a list before
projecting each message, and `catch_up`'s nested messages inside a
`ChannelDifference` envelope.

**D3 — derived rows (no gateway payload) take their timestamp from the row they derive from, in live mode too.**
Three producers stamp `utc_now_iso()` while deriving from *stored* rows, which
makes the projection impure (unreproducible from raw):
- `graph._write_mention_edges` / `discussion._write_thread_edges` → use the
  source message's `messages.first_seen` (stable across re-runs; equals the
  message's raw `observed_at` under D1).
- `store.repliers.backfill_recent_repliers` → use each raw row's own
  `observed_at` (it already reads `raw_records` directly).
- `media` duplicate-custody rows (dedup hit, no download) → the message row's
  `first_seen`. Fresh downloads use the timestamp captured for the
  `MediaDownload` raw record (D1/D2).
This is a deliberate, documented live-behavior change: the evidence timestamp
(when the underlying observation was captured) replaces the derivation
timestamp (when a later phase happened to re-scan it). `run_events` and
`schema_migrations` stay wall-clock — they are operational logs, not
projections, and are excluded from round-trip comparison.

**D4 — spec §3 deviations, forced by the spec's own §7 round-trip contract**
(surface these in the PR body; they are refinements, not direction changes):
1. `get_messages` for an id with **no raw record** returns
   `{"_": "ReplayUnknownMessage", "id": i}` — *not* the spec table's synthetic
   `messageEmpty`. A synthetic empty would fabricate deletion evidence
   (`mark_deleted(evidence='empty')`) for ids the original run never observed
   as deleted — e.g. gap ids the original probe found *alive* (which
   `_probe_gaps` deliberately does not record), or range ids only reachable
   because replayed backfill also serves catch-up-delivered messages. The
   collector skips any non-`messageEmpty` shape, so the placeholder projects
   nothing — which is exactly DB1's state.
2. `get_sponsored_messages` — the original collector never stores the
   envelope, only each `SponsoredMessage` individually. Replay *reconstructs*
   `{"_": "SponsoredMessages", "messages": [...]}` from those records, and
   serves a synthetic `{"_": "sponsoredMessagesEmpty"}` when none exist
   (empty-and-skipped originals are indistinguishable; both project nothing).
3. `join_channel` returns a synthetic `{"_": "Updates", "updates": []}` and
   reproject runs with `allow_join=True` — otherwise a source whose original
   run used `--join` would skip its discussion sweep and lose projections. No
   network is involved; nothing is joined.
4. `get_channel_difference` past the last stored record serves a synthetic
   final `{"_": "updates.channelDifferenceEmpty", "final": True, "pts": pts}`
   rather than `SkipAndRecord` — a mid-`catch_up` `SkipAndRecord` would mark
   the whole folded history phase skipped and discard backfill counts.
5. Phase-set reproduction ("a run that never did graph reprojects without
   graph") is by **raw-kind detection** (see Task 6), not per-method
   `SkipAndRecord` alone — the graph phase's mention scan is RPC-free and
   would otherwise project edges a graph-less DB1 never had.

**D5 — round-trip equality contract** (what the §7 test asserts):
all of `channels`, `channel_snapshots`, `peers`, `messages`,
`message_revisions`, `message_metrics`, `message_tombstones`, `edges`,
`media`, `custody_log`, `web_snapshots`, plus `raw_records` itself — compared
as **sets of rows after dropping autoincrement pk columns and
`source_raw_id`** ("modulo primary keys / autoincrement ids"; `source_raw_id`
is such an id transitively — the referenced record's *content* round-trips via
the `raw_records` comparison). Set (distinct-row) comparison, not multiset: a
message delivered by both `getHistory` and `getChannelDifference` is
legitimately served twice on replay, producing byte-identical duplicate
raw/metrics rows. `run_events`, `sync_state`, `sync_ranges`, `flood_log`,
`schema_migrations`, `messages_fts` are excluded (operational/bookkeeping).

**File structure** (new files):

| File | Responsibility |
|---|---|
| `src/paperboy/clock.py` | `Clock` Protocol, `LiveClock`, `ReplayClock` |
| `src/paperboy/replay.py` | `ReplaySource` (read-only raw access + media root), `RawReplayGateway`, `RawReplayWebClient` |
| `src/paperboy/reproject.py` | target enumeration, phase detection, orchestration, summary |
| `tests/test_reproject_parity.py` | frozen-clock golden parity test (seam safety net) |
| `tests/test_clock.py`, `tests/test_replay_gateway.py`, `tests/test_replay_web.py`, `tests/test_reproject.py` | unit + round-trip/guardrail suites |

Modified: `store/db.py` (`add_raw` param), `collectors/base.py` (clock field),
`recipes.py` (clock passthrough), all six collectors + `store/repliers.py`
(timestamp sites), `web/client.py`/`collectors/web.py` (a `WebGetter`
Protocol so the replay client type-checks), `app.py` (`build_reproject`),
`cli.py` (`reproject` command).

---

### Task 1: Frozen-clock parity golden test

The seam refactor (Tasks 2–3) touches every projection site. This test pins
the *entire* DB a full collect produces — under a frozen clock — as a
committed golden fixture **before** the refactor, so the refactor provably
changes nothing about live collection.

**Files:**
- Create: `tests/test_reproject_parity.py`
- Create: `tests/fixtures/reproject/parity_golden.json` (generated in step 3)

**Interfaces:**
- Produces: `full_collect_fixtures() -> dict` (FakeGateway fixtures covering
  every phase), `run_full_collect(tmp_path, monkeypatch) -> Store`
  (frozen-clock collect of channel,history,discussion,graph,web,media),
  `dump_db(conn, data_dir: Path) -> dict[str, list]` (canonical, path- and
  pk-normalized dump). Task 7 reuses `dump_db`'s exclusion sets via import.

- [ ] **Step 1: Write the test module**

```python
"""Live-collect parity: a full frozen-clock collect must produce byte-identical
projections before and after the observed-at seam (spec §5). Regenerate the
golden with: UPDATE_GOLDEN=1 uv run pytest tests/test_reproject_parity.py -q
"""

import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.collectors.web import WebCollector
from paperboy.store.db import Store
from paperboy.targets import parse_target
from paperboy.web.client import WebClient
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/tl")
GOLDEN = Path("tests/fixtures/reproject/parity_golden.json")
FROZEN_NOW = "2026-01-01T00:00:00+00:00"

# Tables whose content the parity golden pins. Excludes operational /
# bookkeeping tables (schema_migrations, flood_log) and the FTS shadow tables.
PARITY_TABLES = (
    "raw_records", "channels", "channel_snapshots", "peers", "messages",
    "message_revisions", "message_metrics", "message_tombstones", "edges",
    "media", "custody_log", "web_snapshots", "sync_state", "sync_ranges",
    "run_events",
)


def freeze_clock(monkeypatch) -> None:
    """Pin utc_now_iso in EVERY imported paperboy module. Modules import it
    by name (`from paperboy.ids import utc_now_iso`), so patching the
    definition alone would miss every importer's local reference."""
    for name, mod in list(sys.modules.items()):
        if name.startswith(("paperboy", "tests")) and hasattr(mod, "utc_now_iso"):
            monkeypatch.setattr(mod, "utc_now_iso", lambda: FROZEN_NOW)


def full_collect_fixtures() -> dict:
    resolve = json.loads((FX / "resolve_durov.json").read_text())
    full_channel = json.loads((FX / "full_channel.json").read_text())
    return {
        "resolve": resolve,
        "full_channel": full_channel,
        "self": {"_": "user", "id": 1, "self": True, "phone": "+15551234567"},
        "history": [
            {
                "_": "message", "id": 3, "message": "see https://t.me/other_channel",
                "date": 1767322500, "views": 10,
                "entities": [{"_": "MessageEntityUrl", "offset": 4, "length": 24}],
            },
            {
                "_": "message", "id": 2, "message": "", "date": 1767322445,
                "media": {
                    "_": "MessageMediaDocument",
                    "document": {
                        "_": "Document", "id": 9, "access_hash": 1,
                        "mime_type": "text/plain",
                        "attributes": [
                            {"_": "DocumentAttributeFilename", "file_name": "a.txt"}
                        ],
                    },
                },
            },
            {"_": "message", "id": 1, "message": "m1", "date": 1767322400},
        ],
        # One catch-up page carrying an edit of msg 1 — exercises the
        # revisions path and (later) replay's nested-payload clock.
        "channel_difference": {
            "_": "updates.channelDifference", "final": True, "pts": 43,
            "new_messages": [
                {"_": "message", "id": 1, "message": "m1 edited",
                 "date": 1767322400, "edit_date": 1767322600},
            ],
            "other_updates": [],
        },
        "get_messages": {},
        "media": {2: b"file contents"},
        "channel_recommendations": json.loads((FX / "recommendations.json").read_text()),
        "sponsored_messages": json.loads((FX / "sponsored_messages.json").read_text()),
        "chat_invite": {},
    }


_TME_HTML = """<html><body>
<div class="tgme_widget_message" data-post="durov/1">
  <div class="tgme_widget_message_text">m1</div>
  <time datetime="2026-01-01T00:00:00+00:00"></time>
</div>
</body></html>"""


def _web_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://t.me/s/durov?before="):
            return httpx.Response(200, text="<html><body></body></html>")
        if url.startswith("https://t.me/s/durov"):
            return httpx.Response(200, text=_TME_HTML)
        if url.startswith("https://web.archive.org/cdx/"):
            return httpx.Response(200, text=json.dumps([
                ["urlkey", "timestamp", "original", "mimetype", "statuscode",
                 "digest", "length"],
                ["me,t)/s/durov", "20260101000000", "https://t.me/s/durov",
                 "text/html", "200", "DIGEST1", "1234"],
            ]))
        raise AssertionError(f"unexpected URL fetched: {url}")

    return httpx.MockTransport(handler)


async def run_full_collect(data_dir: Path) -> Path:
    """Collect every phase into <data_dir>/default/paperboy.sqlite; returns the DB path."""
    settings = load_settings("default", {"data_dir": data_dir})
    db = data_dir / "default" / "paperboy.sqlite"
    web = WebCollector(
        client=WebClient(transport=_web_transport()),
        min_interval=0.0, sleep=lambda s: None,
    )
    from paperboy.recipes import _default_collectors
    from paperboy.collectors.media import MediaCollector
    collectors = [
        c for c in _default_collectors(include_media=False, include_web=False)
    ] + [web, MediaCollector()]
    with Store.open(db) as store:
        await collect_channel(
            FakeGateway(full_collect_fixtures()), store, settings,
            parse_target("@durov"),
            phases=["channel", "history", "discussion", "graph", "web", "media"],
            log=logging.getLogger("parity"), collectors=collectors,
        )
    return db


def dump_db(conn, data_dir: Path, tables=PARITY_TABLES) -> dict[str, list]:
    """Canonical dump: per table, sorted rows as column->value dicts, with the
    machine-specific data_dir prefix normalized out of path-bearing values."""
    prefix = str(data_dir)
    out: dict[str, list] = {}
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        rows = []
        for row in conn.execute(f"SELECT * FROM {table}"):
            d = {
                c: (v.replace(prefix, "<DATA_DIR>") if isinstance(v, str) else v)
                for c, v in zip(cols, row)
            }
            rows.append(d)
        out[table] = sorted(rows, key=lambda d: json.dumps(d, sort_keys=True, default=str))
    return out


@pytest.mark.asyncio
async def test_full_collect_matches_golden(tmp_path, monkeypatch):
    freeze_clock(monkeypatch)
    db = await run_full_collect(tmp_path)
    with Store.open(db) as store:
        dumped = dump_db(store.conn, tmp_path)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(dumped, indent=1, sort_keys=True, default=str))
        pytest.skip("golden regenerated")
    golden = json.loads(GOLDEN.read_text())
    assert dumped == golden
```

Note: `run_full_collect` opens the store with `Store.open` (not read-only) —
this is the *live* collect being pinned. If `parse_tme_page` needs different
HTML attributes than `_TME_HTML` sketches, read
`src/paperboy/web/tme_parser.py` and adjust the fixture HTML until
`tme_posts == 1`; the golden pins whatever the parser actually produces.

- [ ] **Step 2: Verify the test fails for the right reason (no golden yet)**

Run: `uv run pytest tests/test_reproject_parity.py -q`
Expected: FAIL with `FileNotFoundError` on `parity_golden.json` (or the
explicit golden-missing branch).

- [ ] **Step 3: Generate the golden from CURRENT (pre-seam) code**

Run: `UPDATE_GOLDEN=1 uv run pytest tests/test_reproject_parity.py -q` then
`uv run pytest tests/test_reproject_parity.py -q`
Expected: skip, then PASS. Manually inspect the fixture: every `observed_at`/
`first_seen`/`fetched_at` must equal `2026-01-01T00:00:00+00:00`; if any
differs, `freeze_clock` missed a module — fix before committing (a leaked
real timestamp makes the golden machine-specific).

- [ ] **Step 4: Lint/type, commit**

```bash
uv run ruff check && uv run pyright
git add tests/test_reproject_parity.py tests/fixtures/reproject/parity_golden.json
git commit -m "test(reproject): frozen-clock parity golden for the observed-at seam"
```

---

### Task 2: The `Clock` seam and `add_raw(observed_at=...)`

**Files:**
- Create: `src/paperboy/clock.py`
- Modify: `src/paperboy/store/db.py:86-100` (`add_raw`)
- Test: `tests/test_clock.py`

**Interfaces:**
- Produces: `Clock` (Protocol: `for_payload(payload: dict) -> str`),
  `LiveClock`, `ReplayClock` (`serve(observed_at: str, *payloads: dict)`,
  `serve_json(observed_at: str, payload_json: str)`, `begin_batch()`,
  `for_payload(payload)`), `ReplayClockError`.
  `Store.add_raw(kind, payload, tier, context, observed_at: str | None = None)`.
- Consumed by: Task 3 (collectors), Task 4/5 (replay pair feeds `ReplayClock`).

- [ ] **Step 1: Write failing tests**

```python
"""tests/test_clock.py"""
import pytest

from paperboy.clock import LiveClock, ReplayClock, ReplayClockError
from paperboy.store.db import Store, dumps


def test_live_clock_returns_fresh_iso_utc():
    t = LiveClock().for_payload({"_": "Message", "id": 1})
    assert t.endswith("+00:00")


def test_replay_clock_returns_served_payloads_stamp():
    clock = ReplayClock()
    m1, m2 = {"_": "Message", "id": 1}, {"_": "Message", "id": 2}
    clock.serve("2026-01-01T00:00:01+00:00", m1)
    clock.serve("2026-01-01T00:00:02+00:00", m2)
    # Payload-keyed: order of lookup does not matter (history consumes a
    # whole page before projecting each message).
    assert clock.for_payload(m2) == "2026-01-01T00:00:02+00:00"
    assert clock.for_payload(m1) == "2026-01-01T00:00:01+00:00"
    # Lookup is by value, not object identity — a re-parsed equal dict hits.
    assert clock.for_payload({"_": "Message", "id": 1}) == "2026-01-01T00:00:01+00:00"


def test_replay_clock_serve_json_matches_dict_lookup():
    clock = ReplayClock()
    payload = {"sha256": "ab", "kind": "photo"}
    clock.serve_json("2026-01-01T00:00:03+00:00", dumps(payload))
    assert clock.for_payload(payload) == "2026-01-01T00:00:03+00:00"


def test_replay_clock_unknown_payload_falls_back_to_last_served():
    clock = ReplayClock()
    clock.serve("2026-01-01T00:00:04+00:00", {"_": "ChannelDifference"})
    # A nested dict with no individually-stored record inherits its
    # envelope's stamp.
    assert clock.for_payload({"_": "novel"}) == "2026-01-01T00:00:04+00:00"


def test_replay_clock_raises_before_anything_served():
    with pytest.raises(ReplayClockError):
        ReplayClock().for_payload({"_": "x"})


def test_begin_batch_clears_registry_but_keeps_current():
    clock = ReplayClock()
    clock.serve("2026-01-01T00:00:05+00:00", {"_": "a"})
    clock.begin_batch()
    assert clock.for_payload({"_": "a"}) == "2026-01-01T00:00:05+00:00"  # fallback


def test_add_raw_accepts_explicit_observed_at(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger",
                      {"channel_id": 5}, observed_at="2026-01-01T00:00:06+00:00")
        row = store.conn.execute("SELECT observed_at FROM raw_records").fetchone()
        assert row["observed_at"] == "2026-01-01T00:00:06+00:00"


def test_add_raw_defaults_to_now(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        row = store.conn.execute("SELECT observed_at FROM raw_records").fetchone()
        assert row["observed_at"].endswith("+00:00")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_clock.py -q` — Expected: FAIL,
`ModuleNotFoundError: paperboy.clock`.

- [ ] **Step 3: Implement `src/paperboy/clock.py`**

```python
"""The observation clock (spec §5): where a projection's `observed_at` comes from.

Projections must be a pure function of the raw log, so their timestamps must
come from the observation being projected, not from the wall clock at
projection time. `LiveClock` IS the wall clock (a live collect observes now);
`ReplayClock` returns the ORIGINAL `observed_at` of the raw record being
replayed, fed by `RawReplayGateway`/`RawReplayWebClient` as they serve records.

Lookup is payload-keyed (canonical JSON of the served dict), because
collectors do not project in serve order: `history` consumes a whole
`iter_history` page into a list first, and `catch_up` projects messages
nested inside a `ChannelDifference` envelope.
"""

from __future__ import annotations

from typing import Protocol

from paperboy.ids import utc_now_iso
from paperboy.store.db import dumps


class ReplayClockError(Exception):
    """A replay projection asked for a timestamp before anything was served —
    a replay-wiring bug, never a data condition; fail loudly."""


class Clock(Protocol):
    def for_payload(self, payload: dict) -> str:
        """The `observed_at` for a projection derived from `payload`."""
        ...


class LiveClock:
    """Live collection: every observation happens now."""

    def for_payload(self, payload: dict) -> str:
        del payload
        return utc_now_iso()


class ReplayClock:
    """Replay: observations happened when the raw log says they did.

    `serve`/`serve_json` are called by the replay gateway per record served;
    `begin_batch` bounds the registry to one gateway response (the collector
    always projects a response fully before making the next call).
    A payload with no registered stamp (e.g. a dict nested in an envelope
    whose members were never individually recorded) inherits the most
    recently served record's stamp.
    """

    def __init__(self) -> None:
        self._current: str | None = None
        self._by_payload: dict[str, str] = {}

    def begin_batch(self) -> None:
        self._by_payload.clear()

    def serve(self, observed_at: str, *payloads: dict) -> None:
        self._current = observed_at
        for payload in payloads:
            self._by_payload[dumps(payload)] = observed_at

    def serve_json(self, observed_at: str, payload_json: str) -> None:
        """Register a record by its stored canonical JSON without re-parsing."""
        self._current = observed_at
        self._by_payload[payload_json] = observed_at

    def for_payload(self, payload: dict) -> str:
        stamp = self._by_payload.get(dumps(payload), self._current)
        if stamp is None:
            raise ReplayClockError(
                "ReplayClock.for_payload before any record was served"
            )
        return stamp
```

- [ ] **Step 4: Amend `Store.add_raw`** (`src/paperboy/store/db.py`)

```python
    def add_raw(
        self,
        kind: str,
        payload: dict,
        tier: str,
        context: dict | None,
        observed_at: str | None = None,
    ) -> int:
        """Append one TL object (as `to_dict()`) to the raw log; returns its rowid.

        `observed_at` is the caller's per-record observation stamp — the same
        value the caller passes to every projection of this record, so raw and
        projection agree (spec §5, the reproject clock seam). `None` (legacy
        callers, tests) stamps now.
        """
        cur = self.conn.execute(
            "INSERT INTO raw_records(kind, observed_at, tier, context_json, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                kind,
                observed_at if observed_at is not None else utc_now_iso(),
                tier,
                dumps(context) if context is not None else None,
                dumps(payload),
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid
```

- [ ] **Step 5: Run tests, full suite, lint/type**

Run: `uv run pytest tests/test_clock.py -q && uv run pytest -q && uv run ruff check && uv run pyright`
Expected: all PASS (nothing consumes the clock yet; parity golden untouched).

- [ ] **Step 6: Commit**

```bash
git add src/paperboy/clock.py src/paperboy/store/db.py tests/test_clock.py
git commit -m "feat(clock): observation-clock seam + explicit add_raw observed_at (spec §5)"
```

---

### Task 3: Thread the clock through every projection site

The "mechanical but broad" refactor (spec §5). The parity golden from Task 1
is the gate: after every edit below, a frozen-clock live collect must still
produce the identical DB.

**Files:**
- Modify: `src/paperboy/collectors/base.py` (`CollectContext`),
  `src/paperboy/recipes.py` (`collect_channel` clock passthrough),
  `src/paperboy/collectors/channel.py`, `history.py`, `graph.py`,
  `discussion.py`, `media.py`, `web.py`, `src/paperboy/store/repliers.py`
- Test: existing suite + `tests/test_reproject_parity.py` (unchanged, must stay green)

**Interfaces:**
- Consumes: `Clock`/`LiveClock` from Task 2.
- Produces: `CollectContext.clock: Clock`;
  `collect_channel(..., clock: Clock | None = None)`;
  `MediaCollector._record_custody(ctx, path, sha, message_uri, recorded_at)`;
  `_scan_message_entities` tuples gain a trailing `observed_at: str` element.

- [ ] **Step 1: `CollectContext` gains the clock** (`collectors/base.py`)

After the `profile: str = "default"` field add:

```python
    # Where a projection's `observed_at` comes from (spec §5 / clock.py):
    # the wall clock on a live collect, the raw log's original stamps on a
    # reproject replay. Appended with a default for the same reason `profile`
    # is — every existing positional construction stays valid.
    clock: Clock = field(default_factory=LiveClock)
```

with `from paperboy.clock import Clock, LiveClock` imported at module top
(real import — `default_factory` needs it at runtime, so not TYPE_CHECKING).

- [ ] **Step 2: `collect_channel` passthrough** (`recipes.py`)

Add keyword param `clock: Clock | None = None` (import `Clock`, `LiveClock`
from `paperboy.clock`; `Clock` under TYPE_CHECKING, `LiveClock` real) and build:

```python
    ctx = CollectContext(
        gateway, store, settings, target, None, None, "stranger", log, profile,
        clock or LiveClock(),
    )
```

- [ ] **Step 3: `channel.py` — per-record stamps**

Replace the single phase-start `observed_at = utc_now_iso()` (line 77) with
per-record captures taken immediately after each gateway call, and pass each
into its `add_raw` and projections. Full replacement of `collect`'s body
mechanics (drop the `utc_now_iso` import, import nothing new — the clock
rides on `ctx`):

```python
    async def collect(self, ctx: CollectContext) -> CollectResult:
        peer_uris: set[str] = set()

        # (existing comment about self-first, #12, retained)
        self_user = _redact_self(await ctx.gateway.get_self())
        t_self = ctx.clock.for_payload(self_user)
        ctx.store.add_raw(
            self_user.get("_", "User"), self_user, "self", None, observed_at=t_self
        )
        self_uri = user_uri(self_user["id"])
        set_state(ctx.store, "account", "self", {"uri": self_uri, "id": self_user.get("id")})

        resolved = await ctx.gateway.resolve(ctx.target.value)
        t_resolved = ctx.clock.for_payload(resolved)
        resolve_raw_id = ctx.store.add_raw(
            resolved.get("_", "ResolvedPeer"), resolved, ctx.tier,
            {"target": ctx.target.raw}, observed_at=t_resolved,
        )
        chan = _pick_channel(resolved.get("chats", []), _resolved_channel_id(resolved))
        input_channel = {"channel_id": chan["id"], "access_hash": chan["access_hash"]}

        full = await ctx.gateway.get_full_channel(input_channel)
        t_full = ctx.clock.for_payload(full)
        full_raw_id = ctx.store.add_raw(
            full.get("_", "ChatFull"), full, ctx.tier, {"channel_id": chan["id"]},
            observed_at=t_full,
        )
        ...  # identity check + chan_for_channel selection unchanged

        channel_uri_ = upsert_channel(
            ctx.store, full_chat, chan_for_channel, full_raw_id, t_full
        )
        set_state(ctx.store, "channel", str(channel_id), {"pts": full_chat["pts"]})

        if linked_chat_id:
            add_edge(..., t_full, ctx.tier, full_raw_id, {"field": "linked_chat_id"})

        for source_raw_id, payload, t in (
            (resolve_raw_id, resolved, t_resolved), (full_raw_id, full, t_full),
        ):
            for obj in (*payload.get("chats", []), *payload.get("users", [])):
                uri = upsert_peer(
                    ctx.store, obj, source_raw_id, t,
                    seen_in_chat=None, seen_in_msg=None,
                )
                ...
```

(Every `...` is existing code retained verbatim; only the timestamp plumbing
changes. The self-user timestamp `t_self` exists for the raw record even
though no projection uses it.)

- [ ] **Step 4: `history.py`**

- `_observe_message`, both branches: replace `observed_at = utc_now_iso()`
  with `observed_at = ctx.clock.for_payload(m)`, and pass
  `observed_at=observed_at` into both `add_raw` calls.
- `_probe_gaps`: `observed_at = ctx.clock.for_payload(r)`; pass into
  `add_raw(..., observed_at=observed_at)`.
- `catch_up`: after the `get_channel_difference` call add
  `t_diff = ctx.clock.for_payload(diff)`; pass `observed_at=t_diff` to the
  diff `add_raw`; replace the `updateDeleteChannelMessages` loop's
  `utc_now_iso()` with `t_diff` (the deletion was observed when the
  difference was). Nested `_observe_message` calls need no change — they
  stamp per-payload themselves.
- Drop `utc_now_iso` from the imports.

- [ ] **Step 5: `graph.py`**

- `_collect_recommendations`: `observed_at = ctx.clock.for_payload(result)`
  (replacing `utc_now_iso()`); pass into `add_raw`.
- `_collect_invite_previews`: `observed_at = ctx.clock.for_payload(preview)`;
  pass into `add_raw`.
- `_collect_sponsored`: move the timestamp inside the per-message loop:
  `observed_at = ctx.clock.for_payload(sponsored)`; pass into that message's
  `add_raw` and its edge.
- Mention edges (D3): extend the `collect()` messages query with
  `first_seen`:

```python
        rows = ctx.store.conn.execute(
            "SELECT channel_id, msg_id, text, entities_json, source_raw_id, first_seen "
            "FROM messages WHERE channel_id=? AND entities_json IS NOT NULL",
            (ctx.channel_id,),
        ).fetchall()
```

  In `_scan_message_entities`, capture `observed_at = row["first_seen"]` and
  append it to every emitted tuple (type becomes
  `tuple[str, str, dict, int | None, str]`; `_record_link` gains an
  `observed_at: str` parameter it forwards). `_write_mention_edges` drops its
  own `utc_now_iso()` and unpacks
  `for subject, object_, evidence, source_raw_id, observed_at in mention_edges:`.
  Update both functions' docstrings: the edge's `observed_at` is the message
  observation the mention was found in — the scan itself adds no new
  observation.
- Drop `utc_now_iso` from the imports.

- [ ] **Step 6: `discussion.py`**

`_write_thread_edges`: add `first_seen` to the SELECT column list and replace
the per-row `observed_at = utc_now_iso()` with
`observed_at = row["first_seen"]` (same D3 rationale, note it in the
docstring). Drop `utc_now_iso` import.

- [ ] **Step 7: `store/repliers.py`**

Add `observed_at` to the raw SELECT
(`"SELECT id, observed_at, payload_json FROM raw_records ..."`) and replace
the per-row `observed_at = utc_now_iso()` with
`observed_at = row["observed_at"]`. Docstring note: the replier sample is
projected at the stamp of the message observation that carried it. Drop the
import.

- [ ] **Step 8: `media.py`**

- Build the `MediaDownload` payload dict *before* stamping, capture
  `downloaded_at = ctx.clock.for_payload(raw_payload)`, and use it for the
  `media` INSERT, the custody row, and `add_raw(..., observed_at=downloaded_at)`.
  Concretely, replace lines 179–199 with:

```python
            raw_payload = {
                "sha256": sha, "kind": kind, "size": len(data), "mime_type": mime_type,
                "file_name": file_name, "path": path_str, "message_uri": row["uri"],
            }
            downloaded_at = ctx.clock.for_payload(raw_payload)
            ctx.store.conn.execute(
                "INSERT INTO media (sha256, message_uri, kind, mime_type, size, file_name, "
                "attributes_json, path, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sha, row["uri"], kind, mime_type, len(data), file_name,
                    dumps(attributes) if attributes is not None else None,
                    path_str, downloaded_at,
                ),
            )
            self._record_custody(ctx, path_str, sha, row["uri"], downloaded_at)
            ctx.store.add_raw(
                "MediaDownload", raw_payload, ctx.tier,
                {"channel_id": channel_id, "msg_id": row["msg_id"]},
                observed_at=downloaded_at,
            )
```

- `_record_custody` gains a `recorded_at: str` parameter (replacing its
  internal `utc_now_iso()`); the two dedup call sites pass the message row's
  `first_seen` (add `first_seen` to the messages SELECT at line 121).
- Guard the file write for replay idempotency (spec §4 — content-addressed,
  so an existing path is already the right bytes; also spares live re-runs):

```python
            path = media_root / sha[:2] / f"{sha}{ext}"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
```

- Drop `utc_now_iso` from the imports.

- [ ] **Step 9: `web.py`**

In `_collect_tme` and `_collect_wayback`, build the raw payload dict first,
stamp from it, and pass the stamp into `add_raw`:

```python
            response = self._paced_get(client, url)
            raw_payload = {
                "url": url, "status_code": response.status_code, "text": response.text,
            }
            fetched_at = ctx.clock.for_payload(raw_payload)
            ctx.store.add_raw(
                "tme_page", raw_payload, ctx.tier,
                {"channel_username": username}, observed_at=fetched_at,
            )
```

(and identically for `wayback_cdx`). Drop `utc_now_iso` from the imports.

- [ ] **Step 10: The gate — parity + full suite + lint/type**

Run: `uv run pytest -q && uv run ruff check && uv run pyright`
Expected: ALL PASS, **including `tests/test_reproject_parity.py` against the
Task-1 golden with zero fixture changes**. If parity fails, the refactor
changed live behavior — fix the refactor, never regenerate the golden. (The
D3 sites are safe under a frozen clock: every candidate value is the same
frozen instant.)

- [ ] **Step 11: Commit**

```bash
git add -A src/paperboy tests
git commit -m "refactor(collectors): thread the observation clock through every projection site (spec §5)"
```

---

### Task 4: `ReplaySource` + `RawReplayGateway`

**Files:**
- Create: `src/paperboy/replay.py`
- Test: `tests/test_replay_gateway.py`

**Interfaces:**
- Consumes: `ReplayClock` (Task 2), `SkipAndRecord` from `paperboy.budget`,
  `parse_target` from `paperboy.targets`.
- Produces:

```python
class ReplaySource:
    conn: sqlite3.Connection          # opened file:...?mode=ro
    media_root: Path
    @classmethod
    def open(cls, db_path: Path, media_root: Path) -> ReplaySource: ...
    def close(self) -> None: ...      # + __enter__/__exit__
    def resolve_targets(self) -> list[str]         # distinct context.target, capture order
    def linked_group_ids(self) -> set[int]         # from ChatFull payloads
    def has_kind(self, *kinds: str) -> bool        # lower(kind) IN kinds
    def has_context_channel(self, channel_ids: set[int]) -> bool

class RawReplayGateway:                # implements the Gateway Protocol
    def __init__(self, source: ReplaySource, clock: ReplayClock) -> None: ...
```

- [ ] **Step 1: Write failing tests** (`tests/test_replay_gateway.py`)

Build a source DB by hand with `Store.open` + `add_raw(observed_at=...)` —
no collector involved, so each method's serving contract is tested in
isolation:

```python
import pytest

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayGateway, ReplaySource
from paperboy.store.db import Store

CID = 100
IC = {"channel_id": CID, "access_hash": 7}


def _seed(tmp_path):
    """A minimal raw log: self, resolve, full, three messages (one edited),
    a probe MessageEmpty, one diff, one recommendation set, one MediaDownload."""
    db = tmp_path / "src.sqlite"
    media_root = tmp_path / "media"
    with Store.open(db) as st:
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None,
                   observed_at="2026-01-01T00:00:00+00:00")
        st.add_raw("ResolvedPeer",
                   {"_": "contacts.ResolvedPeer",
                    "peer": {"_": "PeerChannel", "channel_id": CID},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"target": "@durov"},
                   observed_at="2026-01-01T00:00:01+00:00")
        st.add_raw("ChatFull",
                   {"_": "messages.ChatFull",
                    "full_chat": {"_": "ChannelFull", "id": CID, "pts": 40,
                                  "linked_chat_id": 555},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:00:02+00:00")
        for i, (mid, text, t) in enumerate([
            (3, "m3", "2026-01-01T00:01:03+00:00"),
            (2, "m2", "2026-01-01T00:01:02+00:00"),
            (1, "m1", "2026-01-01T00:01:01+00:00"),
            (1, "m1 edited", "2026-01-01T00:02:00+00:00"),  # later revision
        ]):
            st.add_raw("Message", {"_": "message", "id": mid, "message": text},
                       "stranger", {"channel_id": CID}, observed_at=t)
        st.add_raw("MessageEmpty", {"_": "MessageEmpty", "id": 4}, "stranger",
                   {"channel_id": CID}, observed_at="2026-01-01T00:03:00+00:00")
        st.add_raw("ChannelDifference",
                   {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:04:00+00:00")
        st.add_raw("Chats", {"_": "messages.chats",
                             "chats": [{"_": "Channel", "id": 200, "access_hash": 9}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:05:00+00:00")
        sha = "ab" + "0" * 62
        path = media_root / sha[:2] / f"{sha}.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"file contents")
        st.add_raw("MediaDownload",
                   {"sha256": sha, "kind": "document", "size": 13,
                    "mime_type": "text/plain", "file_name": "a.txt",
                    "path": str(path), "message_uri": f"tg:msg:{CID}/2"},
                   "stranger", {"channel_id": CID, "msg_id": 2},
                   observed_at="2026-01-01T00:06:00+00:00")
    return db, media_root


def _gateway(tmp_path):
    db, media_root = _seed(tmp_path)
    clock = ReplayClock()
    return RawReplayGateway(ReplaySource.open(db, media_root), clock), clock


@pytest.mark.asyncio
async def test_resolve_matches_target_and_stamps_clock(tmp_path):
    gw, clock = _gateway(tmp_path)
    resolved = await gw.resolve("durov")
    assert resolved["peer"]["channel_id"] == CID
    assert clock.for_payload(resolved) == "2026-01-01T00:00:01+00:00"


@pytest.mark.asyncio
async def test_resolve_unknown_target_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    with pytest.raises(SkipAndRecord):
        await gw.resolve("someone_else")


@pytest.mark.asyncio
async def test_get_self_serves_self_tier_record(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_self())["id"] == 1


@pytest.mark.asyncio
async def test_iter_history_pages_newest_first_excluding_empties(tmp_path):
    gw, clock = _gateway(tmp_path)
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=100)]
    # id DESC; both revisions of msg 1 in capture order; MessageEmpty excluded.
    assert [(m["id"], m["message"]) for m in page] == [
        (3, "m3"), (2, "m2"), (1, "m1"), (1, "m1 edited"),
    ]
    assert clock.for_payload(page[3]) == "2026-01-01T00:02:00+00:00"
    assert clock.for_payload(page[1]) == "2026-01-01T00:01:02+00:00"


@pytest.mark.asyncio
async def test_iter_history_never_splits_an_id_group_across_pages(tmp_path):
    gw, _ = _gateway(tmp_path)
    # limit=3 would cut between msg 1's two revisions; the page extends.
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=3)]
    assert [m["id"] for m in page] == [3, 2, 1, 1]
    next_page = [m async for m in gw.iter_history(IC, offset_id=1, limit=3)]
    assert next_page == []


@pytest.mark.asyncio
async def test_get_messages_serves_stored_and_placeholder(tmp_path):
    gw, _ = _gateway(tmp_path)
    out = await gw.get_messages(IC, [4, 99])
    assert out[0]["_"] == "MessageEmpty"          # stored probe result
    assert out[1] == {"_": "ReplayUnknownMessage", "id": 99}  # D4.1: no fabricated evidence


@pytest.mark.asyncio
async def test_channel_difference_serves_stored_then_synthetic_final(tmp_path):
    gw, _ = _gateway(tmp_path)
    first = await gw.get_channel_difference(IC, 40, 100)
    assert first["pts"] == 41 and first["final"]
    again = await gw.get_channel_difference(IC, 41, 100)
    assert again == {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41}


@pytest.mark.asyncio
async def test_recommendations_served_and_missing_raw_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    recs = await gw.get_channel_recommendations(IC)
    assert recs["chats"][0]["id"] == 200
    with pytest.raises(SkipAndRecord):
        await gw.get_channel_recommendations({"channel_id": 999, "access_hash": 0})


@pytest.mark.asyncio
async def test_sponsored_reconstructs_envelope_or_empty(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_sponsored_messages(IC))["_"] == "sponsoredMessagesEmpty"


@pytest.mark.asyncio
async def test_download_media_reads_content_addressed_file(tmp_path):
    gw, clock = _gateway(tmp_path)
    data = await gw.download_media(IC, {"id": 2})
    assert data == b"file contents"
    assert await gw.download_media(IC, {"id": 3}) is None  # no record -> unavailable


@pytest.mark.asyncio
async def test_join_channel_is_synthetic_and_offline(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.join_channel(IC))["_"] == "Updates"


@pytest.mark.asyncio
async def test_doctor_methods_are_not_replayable(tmp_path):
    gw, _ = _gateway(tmp_path)
    for coro in (gw.get_authorizations(), gw.get_password_state(), gw.get_privacy("phone")):
        with pytest.raises(SkipAndRecord):
            await coro


def test_source_helpers(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    assert src.resolve_targets() == ["@durov"]
    assert src.linked_group_ids() == {555}
    assert src.has_kind("mediadownload") and not src.has_kind("tme_page")


def test_source_is_read_only(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        src.conn.execute("DELETE FROM raw_records")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_replay_gateway.py -q` — Expected: FAIL,
`ModuleNotFoundError: paperboy.replay`.

- [ ] **Step 3: Implement `src/paperboy/replay.py`**

Core mechanics (implement fully; every method mirrors this pattern):

```python
"""Replay `Gateway`/web-client pair (spec §2–§4): serve `raw_records` back to
the real collectors, so a reproject is the same code path as a live collect.

`ReplaySource` is strictly read-only (`mode=ro` URI); every serve registers
the record's original `observed_at` on the shared `ReplayClock` so
projections carry capture-time stamps (spec §5). A method with no matching
raw raises `SkipAndRecord`, reproducing the phase set the original run
executed (spec §3) — with the documented deviations D4.1–D4.4 in
docs/superpowers/plans/2026-08-26-reproject.md.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.store.db import dumps
from paperboy.targets import parse_target

_MESSAGE_KINDS = ("message", "messageservice")


class ReplaySource:
    """Read-only access to a source DB's raw log + its content-addressed media."""

    def __init__(self, conn: sqlite3.Connection, media_root: Path) -> None:
        self.conn = conn
        self.media_root = media_root

    @classmethod
    def open(cls, db_path: Path, media_root: Path) -> "ReplaySource":
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return cls(conn, media_root)

    def close(self) -> None: ...
    # __enter__/__exit__ like Store

    def resolve_targets(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT json_extract(context_json, '$.target') AS target FROM raw_records "
            "WHERE lower(kind) = 'resolvedpeer' AND target IS NOT NULL ORDER BY id"
        ).fetchall()
        seen: dict[str, None] = {}
        for r in rows:
            seen.setdefault(r["target"])
        return list(seen)

    def linked_group_ids(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT json_extract(payload_json, '$.full_chat.linked_chat_id') AS g "
            "FROM raw_records WHERE lower(kind) = 'chatfull'"
        ).fetchall()
        return {r["g"] for r in rows if r["g"]}

    def has_kind(self, *kinds: str) -> bool:
        qmarks = ",".join("?" * len(kinds))
        return self.conn.execute(
            f"SELECT 1 FROM raw_records WHERE lower(kind) IN ({qmarks}) LIMIT 1",
            kinds,
        ).fetchone() is not None

    def has_context_channel(self, channel_ids: set[int]) -> bool:
        return any(
            self.conn.execute(
                "SELECT 1 FROM raw_records "
                "WHERE json_extract(context_json, '$.channel_id') = ? LIMIT 1",
                (cid,),
            ).fetchone() is not None
            for cid in channel_ids
        )


class RawReplayGateway:
    """`Gateway` served from a raw log. Never touches the network — there is
    no client, no session, no Budget anywhere in this class."""

    def __init__(self, source: ReplaySource, clock: ReplayClock) -> None:
        self._src = source
        self._clock = clock
        # get_channel_difference is inherently sequential (a pts catch-up
        # loop); a per-channel cursor over the stored pages models that.
        self._diff_cursor: dict[int, int] = {}

    def _latest(self, kinds: tuple[str, ...], where: str, params: tuple) -> sqlite3.Row | None:
        qmarks = ",".join("?" * len(kinds))
        return self._src.conn.execute(
            f"SELECT observed_at, payload_json FROM raw_records "
            f"WHERE lower(kind) IN ({qmarks}) AND {where} ORDER BY id DESC LIMIT 1",
            (*kinds, *params),
        ).fetchone()

    def _serve(self, row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return payload

    async def resolve(self, target_value: str) -> dict:
        rows = self._src.conn.execute(
            "SELECT observed_at, payload_json, "
            "json_extract(context_json, '$.target') AS target "
            "FROM raw_records WHERE lower(kind) = 'resolvedpeer' ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            raw_target = row["target"]
            if raw_target and parse_target(raw_target).value == target_value:
                return self._serve(row)
        raise SkipAndRecord(f"replay: no ResolvedPeer recorded for {target_value!r}")

    async def get_full_channel(self, input_channel: dict) -> dict:
        row = self._latest(
            ("chatfull",),
            "json_extract(context_json, '$.channel_id') = ?",
            (input_channel["channel_id"],),
        )
        if row is None:
            raise SkipAndRecord(
                f"replay: no ChatFull recorded for channel {input_channel['channel_id']}"
            )
        return self._serve(row)

    async def get_self(self) -> dict:
        row = self._latest(("user",), "tier = 'self'", ())
        if row is None:
            raise SkipAndRecord("replay: no self User recorded")
        return self._serve(row)

    async def iter_history(
        self, input_channel: dict, *, offset_id: int, limit: int
    ) -> AsyncIterator[dict]:
        # Reconstructs the original paging (spec §3): id DESC below the
        # cursor. Secondary order id ASC (capture order) so an edited
        # message's revisions replay oldest-first. MessageEmpty is excluded —
        # getHistory never yielded one; they came from the probe.
        rows = self._src.conn.execute(
            "SELECT id, observed_at, payload_json, "
            "CAST(json_extract(payload_json, '$.id') AS INTEGER) AS msg_id "
            "FROM raw_records "
            "WHERE lower(kind) IN ('message', 'messageservice') "
            "AND json_extract(context_json, '$.channel_id') = ? "
            "AND (? = 0 OR CAST(json_extract(payload_json, '$.id') AS INTEGER) < ?) "
            "ORDER BY msg_id DESC, id ASC",
            (input_channel["channel_id"], offset_id, offset_id),
        ).fetchall()
        # Never split one msg_id's records across pages: the collector's next
        # cursor is `min(page ids)` and the next page takes strictly-below,
        # so a split id's tail records would be unreachable forever.
        page = list(rows[:limit])
        while len(rows) > len(page) and rows[len(page)]["msg_id"] == page[-1]["msg_id"]:
            page.append(rows[len(page)])
        self._clock.begin_batch()
        for row in page:
            self._clock.serve_json(row["observed_at"], row["payload_json"])
            yield json.loads(row["payload_json"])
```

`get_messages(ic, ids)`: per id, `_latest(("message","messageservice","messageempty"), "context channel AND json_extract(payload_json,'$.id')=?", ...)`;
serve found rows (register each on the clock, one shared `begin_batch` at
the top, `serve_json` per row); `{"_": "ReplayUnknownMessage", "id": i}` for
misses (D4.1 — with the comment explaining why not `messageEmpty`).

`get_channel_difference(ic, pts, limit)`: fetch all
`lower(kind) LIKE 'channeldifference%'` rows for the channel `ORDER BY id
ASC` once, cursor per channel; serve next; when serving, also register every
nested message so `_observe_message` gets per-message stamps:

```python
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        nested = [
            *payload.get("new_messages", []),
            *payload.get("messages", []),
            *(u["message"] for u in payload.get("other_updates", [])
              if isinstance(u.get("message"), dict)),
        ]
        for m in nested:
            m_row = self._latest(
                ("message", "messageservice", "messageempty"),
                "json_extract(context_json, '$.channel_id') = ? "
                "AND payload_json = ?",
                (input_channel["channel_id"], dumps(m)),
            )
            if m_row is not None:
                self._clock.serve_json(m_row["observed_at"], m_row["payload_json"])
        return payload
```

Exhausted cursor → `{"_": "updates.channelDifferenceEmpty", "final": True,
"pts": pts}` served with the channel's latest raw `observed_at` (query
`MAX(observed_at)` for the channel's records) — D4.4.

`check_chat_invite(hash_)`: kinds `('chatinvite','chatinvitealready','chatinvitepeek')`,
`json_extract(context_json,'$.hash') = ?`; miss → `SkipAndRecord`.

`get_channel_recommendations(ic)`: kinds `('chats','chatsslice')` by context
channel; miss → `SkipAndRecord`.

`get_sponsored_messages(ic)`: all `lower(kind)='sponsoredmessage'` rows for
the channel `ORDER BY id ASC`; none → `{"_": "sponsoredMessagesEmpty"}`;
else register each row on the clock and return
`{"_": "SponsoredMessages", "messages": [payloads]}` (D4.2).

`download_media(ic, message)`: latest `mediadownload` row matching
`context.channel_id` and `context.msg_id = message["id"]`; none → `None`.
Else resolve the file: `Path(payload["path"])` if it exists, otherwise
`self._src.media_root / sha[:2] / (sha + Path(payload["path"]).suffix)`;
still missing → `SkipAndRecord(f"replay: media file missing for sha {sha}")`.
Read bytes, `begin_batch()` + `serve_json(row["observed_at"], row["payload_json"])`,
return bytes.

`join_channel(ic)`: `return {"_": "Updates", "updates": []}` (D4.3, with the
zero-network comment).

`get_authorizations` / `get_password_state` / `get_privacy`: raise
`SkipAndRecord("replay: doctor state is not recorded; reproject never runs doctor")`.

- [ ] **Step 4: Run tests to green, then full suite + lint/type**

Run: `uv run pytest tests/test_replay_gateway.py -q && uv run pytest -q && uv run ruff check && uv run pyright`

- [ ] **Step 5: Commit**

```bash
git add src/paperboy/replay.py tests/test_replay_gateway.py
git commit -m "feat(replay): RawReplayGateway + ReplaySource — serve raw_records through the Gateway seam"
```

---

### Task 5: `RawReplayWebClient` + the `WebGetter` seam

**Files:**
- Modify: `src/paperboy/web/client.py` (add `WebGetter` Protocol),
  `src/paperboy/collectors/web.py` (annotations only),
  `src/paperboy/replay.py` (append `RawReplayWebClient`)
- Test: `tests/test_replay_web.py`

**Interfaces:**
- Produces: `WebGetter` Protocol (`def get(self, url: str) -> httpx.Response`);
  `RawReplayWebClient(source: ReplaySource, clock: ReplayClock)` satisfying it.
- `WebCollector.__init__`'s `client` param + `_client` attr + `_get_client`
  return type become `WebGetter | None` / `WebGetter`.

- [ ] **Step 1: Write failing tests**

```python
"""tests/test_replay_web.py"""
from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayWebClient, ReplaySource
from paperboy.store.db import Store


def _seed(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        for i, t in enumerate(["2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00"]):
            st.add_raw("tme_page",
                       {"url": "https://t.me/s/durov", "status_code": 200,
                        "text": f"<html>page capture {i}</html>"},
                       "stranger", {"channel_username": "durov"}, observed_at=t)
        st.add_raw("wayback_cdx",
                   {"url": "https://web.archive.org/cdx/search/cdx?url=t.me/s/durov*"
                           "&output=json&filter=statuscode:200&collapse=digest&limit=10000",
                    "status_code": 200, "text": "[]"},
                   "stranger", {"channel_username": "durov"},
                   observed_at="2026-01-01T00:00:03+00:00")
    return db


def _client(tmp_path):
    clock = ReplayClock()
    return RawReplayWebClient(ReplaySource.open(_seed(tmp_path), tmp_path / "media"), clock), clock


def test_serves_stored_response_and_stamps_clock(tmp_path):
    client, clock = _client(tmp_path)
    resp = client.get("https://t.me/s/durov")
    assert resp.status_code == 200
    assert resp.text == "<html>page capture 0</html>"
    assert clock.for_payload(
        {"url": "https://t.me/s/durov", "status_code": 200, "text": resp.text}
    ) == "2026-01-01T00:00:01+00:00"


def test_same_url_serves_captures_in_order(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("https://t.me/s/durov").text == "<html>page capture 0</html>"
    assert client.get("https://t.me/s/durov").text == "<html>page capture 1</html>"


def test_unrecorded_url_is_a_definitive_404(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("https://t.me/s/durov?before=5")
    # 404, not 5xx: an unambiguous "nothing there", so the collector's page
    # loop stops cleanly instead of reporting a failure (web.py's
    # _is_ambiguous_failure treats 404 as an answer).
    assert resp.status_code == 404 and resp.text == ""


def test_web_client_satisfies_web_getter():
    from paperboy.web.client import WebClient, WebGetter
    client: WebGetter = WebClient()   # structural check exercised by pyright too
    client.close()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_replay_web.py -q`
Expected: FAIL, `ImportError: RawReplayWebClient`.

- [ ] **Step 3: Implement**

In `web/client.py` (below the imports):

```python
class WebGetter(Protocol):
    """The shape `WebCollector` needs from an HTTP client — satisfied by the
    real `WebClient` and by `replay.RawReplayWebClient` (spec §4)."""

    def get(self, url: str) -> httpx.Response: ...
```

(`from typing import Protocol`; `WebClient.get`'s extra keyword-only
`max_redirects=...` default keeps it call-compatible.) In `collectors/web.py`
change the three annotations to `WebGetter`.

Append to `replay.py`:

```python
class RawReplayWebClient:
    """Serve stored `tme_page`/`wayback_cdx` captures as `httpx.Response`s.

    Keyed by exact URL — the web collector re-derives the same URL sequence
    from the same parsed posts, so replay requests exactly the recorded set.
    Repeat captures of one URL (multi-run sources) serve in capture order.
    An unrecorded URL is a definitive empty 404: the page loop must stop
    cleanly there, exactly where the original run stopped.
    """

    def __init__(self, source: ReplaySource, clock: ReplayClock) -> None:
        self._src = source
        self._clock = clock
        self._served: dict[str, int] = {}   # url -> raw ids already served

    def get(self, url: str) -> httpx.Response:
        row = self._src.conn.execute(
            "SELECT id, observed_at, payload_json FROM raw_records "
            "WHERE lower(kind) IN ('tme_page', 'wayback_cdx') "
            "AND json_extract(payload_json, '$.url') = ? AND id > ? "
            "ORDER BY id ASC LIMIT 1",
            (url, self._served.get(url, 0)),
        ).fetchone()
        if row is None:
            return httpx.Response(404, text="")
        self._served[url] = row["id"]
        payload = json.loads(row["payload_json"])
        self._clock.begin_batch()
        self._clock.serve_json(row["observed_at"], row["payload_json"])
        return httpx.Response(payload["status_code"], text=payload["text"])
```

- [ ] **Step 4: Green + full suite + lint/type; commit**

```bash
uv run pytest tests/test_replay_web.py -q && uv run pytest -q && uv run ruff check && uv run pyright
git add src/paperboy/replay.py src/paperboy/web/client.py src/paperboy/collectors/web.py tests/test_replay_web.py
git commit -m "feat(replay): RawReplayWebClient + WebGetter protocol seam"
```

---

### Task 6: `reproject` recipe, composition, and CLI

**Files:**
- Create: `src/paperboy/reproject.py`
- Modify: `src/paperboy/app.py` (add `build_reproject`),
  `src/paperboy/cli.py` (add the `reproject` command)
- Test: `tests/test_reproject.py` (CLI/orchestration cases; the round-trip
  battery lands in Task 7 in the same file)

**Interfaces:**
- Produces:

```python
# reproject.py
class ReprojectError(Exception): ...   # operator-facing (empty source, etc.)

REPROJECT_TABLES: tuple[str, ...]  # the diff-summary table list (raw + projections)

def detect_phases(source: ReplaySource) -> list[str]: ...

async def reproject(
    source: ReplaySource, out_store: Store, settings: Settings,
    profile: str, phases: list[str] | None, log: logging.Logger,
) -> ReprojectSummary: ...

@dataclass
class ReprojectSummary:
    phases: list[str]
    results: dict[str, list[CollectResult]]        # target raw string -> phase results
    table_counts: dict[str, tuple[int, int]]       # table -> (source_rows, target_rows)

# app.py
def build_reproject(settings: Settings, profile: str, out_path: Path) -> tuple[ReplaySource, Store]
```

- [ ] **Step 1: Write failing orchestration tests** (append to a new `tests/test_reproject.py`)

```python
import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store

from tests.test_reproject_parity import run_full_collect

runner = CliRunner()


@pytest.mark.asyncio
async def test_detect_phases_reflects_recorded_raw_kinds(tmp_path):
    db = await run_full_collect(tmp_path)
    src = ReplaySource.open(db, tmp_path / "default" / "media")
    phases = detect_phases(src)
    assert phases[:2] == ["channel", "history"]
    assert "graph" in phases and "web" in phases and "media" in phases


@pytest.mark.asyncio
async def test_detect_phases_minimal_source(tmp_path):
    # channel+history-only raw log -> no graph/web/media/discussion phases.
    ...  # build with collect_channel(phases=["channel", "history"]) via the
         # parity fixtures, then assert detect_phases == ["channel", "history"]


@pytest.mark.asyncio
async def test_cli_reproject_writes_fresh_db_and_prints_diff(tmp_path, monkeypatch):
    await run_full_collect(tmp_path)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    out_db = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert out_db.exists()
    assert "channels" in result.output and "messages" in result.output


@pytest.mark.asyncio
async def test_cli_reproject_refuses_existing_out(tmp_path, monkeypatch):
    await run_full_collect(tmp_path)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    out.write_bytes(b"")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 1
    assert "refusing" in result.output.lower()


def test_cli_reproject_empty_source_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    (tmp_path / "default").mkdir(parents=True)
    with Store.open(tmp_path / "default" / "paperboy.sqlite"):
        pass  # schema only, no raws
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 1
    assert "no resolve records" in result.output
```

(Fill the `...` in `test_detect_phases_minimal_source` with a real
channel+history-only collect using `full_collect_fixtures()` — no
placeholder survives into the actual test file.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_reproject.py -q`

- [ ] **Step 3: Implement `src/paperboy/reproject.py`**

```python
"""The reproject recipe (spec §6): enumerate targets and phases from the raw
log, then run the NORMAL collectors against the replay pair into a fresh
store. Everything collector-shaped is reused; this module only wires."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from paperboy.clock import ReplayClock
from paperboy.collectors.base import CollectResult
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.collectors.web import WebCollector
from paperboy.config import Settings
from paperboy.recipes import collect_channel
from paperboy.replay import RawReplayGateway, RawReplayWebClient, ReplaySource
from paperboy.store.db import Store
from paperboy.targets import parse_target


class ReprojectError(Exception):
    """Operator-facing reproject failure (empty source, bad --out)."""


REPROJECT_TABLES = (
    "raw_records", "channels", "channel_snapshots", "peers", "messages",
    "message_revisions", "message_metrics", "message_tombstones", "edges",
    "media", "custody_log", "web_snapshots",
)


@dataclass
class ReprojectSummary:
    phases: list[str]
    results: dict[str, list[CollectResult]]
    table_counts: dict[str, tuple[int, int]]


def detect_phases(source: ReplaySource) -> list[str]:
    """The phase set the original run(s) executed, inferred from raw kinds
    (spec §3: a source that never ran graph reprojects without graph). The
    inference is necessarily raw-only (spec §8) and conservative: a phase
    whose every RPC was skipped leaves no raw and is treated as never-run;
    --phases overrides.
    """
    phases = ["channel", "history"]
    linked = source.linked_group_ids()
    if linked and source.has_context_channel(linked):
        phases.append("discussion")
    if source.has_kind("chats", "chatsslice", "chatinvite", "chatinvitealready",
                       "chatinvitepeek", "sponsoredmessage"):
        phases.append("graph")
    if source.has_kind("tme_page", "wayback_cdx"):
        phases.append("web")
    if source.has_kind("mediadownload"):
        phases.append("media")
    return phases


async def reproject(
    source: ReplaySource,
    out_store: Store,
    settings: Settings,
    profile: str,
    phases: list[str] | None,
    log: logging.Logger,
) -> ReprojectSummary:
    targets = source.resolve_targets()
    if not targets:
        raise ReprojectError(
            "source has no resolve records in raw_records — nothing to reproject"
        )
    active_phases = phases if phases is not None else detect_phases(source)
    # allow_join=True so a source whose original run used --join replays its
    # discussion sweep; RawReplayGateway.join_channel is a synthetic no-op
    # (plan D4.3) — nothing is joined, nothing leaves this machine.
    replay_settings = settings.model_copy(update={"allow_join": True})

    results: dict[str, list[CollectResult]] = {}
    for raw_target in targets:
        clock = ReplayClock()
        gateway = RawReplayGateway(source, clock)
        web_client = RawReplayWebClient(source, clock)
        collectors = [
            ChannelCollector(), HistoryCollector(), DiscussionCollector(),
            GraphCollector(),
            WebCollector(client=web_client, min_interval=0.0, sleep=lambda s: None),
            MediaCollector(),
        ]
        results[raw_target] = await collect_channel(
            gateway, out_store, replay_settings, parse_target(raw_target),
            list(active_phases), log,
            collectors=collectors, profile=profile, clock=clock,
        )

    counts = {
        t: (
            source.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
            out_store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0],
        )
        for t in REPROJECT_TABLES
    }
    return ReprojectSummary(list(active_phases), results, counts)
```

- [ ] **Step 4: `app.py` — `build_reproject`**

```python
def build_reproject(
    settings: Settings, profile: str, out_path: Path
) -> tuple[ReplaySource, Store]:
    """Wire the replay pair's source + a fresh target Store. Deliberately the
    ONLY composition path for `reproject`: no client, no gateway, no Budget,
    no secrets — a reproject is incapable of touching Telegram, the web, or
    the keychain (spec §2, §8).
    """
    source_db = profile_dir(settings, profile) / "paperboy.sqlite"
    if not source_db.exists():
        raise ConfigError(f"no source DB for profile {profile!r} at {source_db}")
    if out_path.exists():
        raise ConfigError(
            f"refusing to overwrite existing {out_path} — move it aside or pass a fresh --out"
        )
    media_root = profile_dir(settings, profile) / "media"
    return ReplaySource.open(source_db, media_root), Store.open(out_path)
```

(imports: `from pathlib import Path`, `from paperboy.replay import ReplaySource`.)

- [ ] **Step 5: `cli.py` — the command**

```python
@app.command()
def reproject(
    profile: str = typer.Option("default", "--profile"),
    out: str = typer.Option(
        None, "--out",
        help="Target DB path (default <data_dir>/<profile>/paperboy.reprojected.sqlite). "
             "The source DB is never touched.",
    ),
    phases: str = typer.Option(
        None, "--phases",
        help="Comma-separated phase subset; default: auto-detected from the raw log.",
    ),
) -> None:
    """Rebuild all projections from raw_records into a fresh DB — offline,
    no network, no credentials. See the reproject design doc."""
    settings = _settings_with_overrides(profile)
    configure_logging(profile_dir(settings, profile) / "paperboy.log", console=True)
    log = logging.getLogger("paperboy.cli")
    out_path = Path(out) if out else profile_dir(settings, profile) / "paperboy.reprojected.sqlite"
    phase_list = phases.split(",") if phases else None

    try:
        source, out_store = composition.build_reproject(settings, profile, out_path)
    except composition.ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from None
    try:
        with source, out_store:
            summary = asyncio.run(
                reproject_run(source, out_store, settings, profile, phase_list, log)
            )
    except ReprojectError as exc:
        console.print(f"[red]{exc}[/]")
        out_path.unlink(missing_ok=True)  # don't leave a half-made target behind
        raise typer.Exit(code=1) from None

    for raw_target, results in summary.results.items():
        table = Table(title=f"reproject {raw_target} -> {out_path}")
        table.add_column("phase"); table.add_column("counts"); table.add_column("stopped")
        for r in results:
            table.add_row(r.name, str(r.counts), r.stopped or "-")
        console.print(table)

    diff = Table(title="row counts — source vs reprojected")
    diff.add_column("table"); diff.add_column("source"); diff.add_column("reprojected")
    for name, (src_n, out_n) in summary.table_counts.items():
        diff.add_row(name, str(src_n), str(out_n))
    console.print(diff)
```

with `from paperboy.reproject import ReprojectError, reproject as reproject_run`
at the top. Note the command name `reproject` vs the imported function —
alias the import as shown to avoid the collision.

- [ ] **Step 6: Green + full suite + lint/type; commit**

```bash
uv run pytest tests/test_reproject.py -q && uv run pytest -q && uv run ruff check && uv run pyright
git add src/paperboy/reproject.py src/paperboy/app.py src/paperboy/cli.py tests/test_reproject.py
git commit -m "feat(reproject): recipe, composition root, and CLI command (spec §6)"
```

---### Task 7: The correctness battery — round-trip identity + guardrails

All in `tests/test_reproject.py`. These are spec §7 verbatim; the round-trip
is the feature's correctness contract.

**Files:**
- Modify: `tests/test_reproject.py` (append), reusing
  `run_full_collect`/`full_collect_fixtures`/`dump_db` from
  `tests/test_reproject_parity.py`.

**Interfaces:**
- Consumes: everything shipped in Tasks 1–6. No new production code except
  fixes this battery forces.

- [ ] **Step 1: The round-trip comparison helper + identity test**

```python
# D5 (plan): equality modulo autoincrement pks and source_raw_id, compared as
# DISTINCT row sets — replay legitimately serves one observation through two
# paths (getHistory + getChannelDifference), duplicating byte-identical rows.
ROUND_TRIP_EXCLUDE = {
    "raw_records": {"id"},
    "channels": {"source_raw_id"},
    "channel_snapshots": {"id", "source_raw_id"},
    "peers": {"source_raw_id"},
    "messages": {"source_raw_id"},
    "message_revisions": {"id", "source_raw_id"},
    "message_metrics": {"id"},
    "message_tombstones": {"id"},
    "edges": {"id", "source_raw_id"},
    "media": set(),
    "custody_log": {"id"},
    "web_snapshots": {"id"},
}


def _table_set(conn, table: str, exclude: set[str]) -> set[tuple]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in exclude]
    sql = f"SELECT {', '.join(cols)} FROM {table}"
    return {tuple(row) for row in conn.execute(sql)}


def assert_round_trip(db1: Path, db2: Path) -> None:
    import sqlite3
    c1, c2 = sqlite3.connect(db1), sqlite3.connect(db2)
    try:
        for table, exclude in ROUND_TRIP_EXCLUDE.items():
            s1, s2 = _table_set(c1, table, exclude), _table_set(c2, table, exclude)
            assert s1 == s2, (
                f"{table} diverged:\n only in source: {sorted(s1 - s2)[:5]}\n"
                f" only in reprojected: {sorted(s2 - s1)[:5]}"
            )
    finally:
        c1.close(); c2.close()


@pytest.mark.asyncio
async def test_round_trip_identity(tmp_path, monkeypatch):
    """Spec §7: collect -> reproject -> projections identical, timestamps
    included. Runs with the REAL clock (no freezing) — timestamp equality is
    exactly what the observed-at seam exists to guarantee."""
    db1 = await run_full_collect(tmp_path)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db1, tmp_path / "default" / "paperboy.reprojected.sqlite")
```

- [ ] **Step 2: Zero network / zero credentials**

```python
@pytest.mark.asyncio
async def test_reproject_is_incapable_of_network_or_keychain(tmp_path, monkeypatch):
    db1 = await run_full_collect(tmp_path)
    del db1

    def _forbidden(*a, **k):
        raise AssertionError("reproject touched a forbidden constructor")

    import keyring
    import paperboy.gateway as gw
    import paperboy.web.client as wc
    monkeypatch.setattr(gw.TelethonGateway, "__init__", _forbidden)
    monkeypatch.setattr(wc.WebClient, "__init__", _forbidden)
    monkeypatch.setattr(keyring, "get_password", _forbidden)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
```

- [ ] **Step 3: Corrected-projection test** (the feature's raison d'être)

```python
@pytest.mark.asyncio
async def test_reproject_corrects_old_code_projections(tmp_path, monkeypatch):
    """A DB whose projections were written by OLD code (10-flag channels, a
    stored self peer, a lost edge) reprojects to the current correct shape —
    raw is the system of record."""
    import sqlite3
    db1 = await run_full_collect(tmp_path)
    conn = sqlite3.connect(db1)
    good_flags = conn.execute("SELECT flags_json FROM channels").fetchone()[0]
    assert len(json.loads(good_flags)) > 3
    conn.execute("UPDATE channels SET flags_json = ?",
                 (json.dumps({"broadcast": True}),))           # old 1-flag shape
    conn.execute(
        "INSERT INTO peers (uri, kind, id, is_min, first_seen, last_seen) "
        "VALUES ('tg:user:1', 'user', 1, 0, 'x', 'x')")        # self row (pre-#12)
    conn.execute("DELETE FROM edges")                          # lost projections
    conn.commit(); conn.close()

    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    assert runner.invoke(app, ["reproject", "--profile", "default"]).exit_code == 0
    out = sqlite3.connect(tmp_path / "default" / "paperboy.reprojected.sqlite")
    assert json.loads(out.execute("SELECT flags_json FROM channels").fetchone()[0]) \
        == json.loads(good_flags)
    assert out.execute("SELECT count(*) FROM peers WHERE uri='tg:user:1'").fetchone()[0] == 0
    assert out.execute("SELECT count(*) FROM edges").fetchone()[0] > 0
    out.close()
```

- [ ] **Step 4: Missing-raw (phase-set reproduction), partial source, media file-read**

```python
@pytest.mark.asyncio
async def test_source_without_graph_reprojects_without_graph(tmp_path, monkeypatch):
    # channel+history-only DB1 (no graph/web/media raws) -> DB2 must not grow
    # mention edges the original never projected (plan D4.5).
    ...  # collect with phases=["channel","history"], reproject, then
         # assert_round_trip(db1, db2) — the round-trip helper IS the assertion.


@pytest.mark.asyncio
async def test_partial_interrupted_source_reprojects_to_same_partial_state(tmp_path, monkeypatch):
    # DB1 whose catch_up PhaseStopped mid-backlog: channel_difference fixture
    # is [non_final_page(pts 41), PhaseStop("flood")]. Reproject must land on
    # the same partial projections (raw_records excluded here: replay closes
    # the log with one synthetic final empty diff, plan D4.4).
    ...  # build via collect_channel with the exception-fixture FakeGateway,
         # reproject via CLI, compare all ROUND_TRIP_EXCLUDE tables except
         # raw_records.


@pytest.mark.asyncio
async def test_reproject_never_rewrites_media_files(tmp_path, monkeypatch):
    await run_full_collect(tmp_path)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    from pathlib import Path as _P
    def _no_write(self, data):
        raise AssertionError(f"reproject wrote a media file: {self}")
    monkeypatch.setattr(_P, "write_bytes", _no_write)
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
```

Fill both `...` bodies fully (no placeholders in the committed file): the
first mirrors `test_round_trip_identity` with a reduced phase list; the
second builds DB1 inline with
`fx["channel_difference"] = [<non-final page dict>, PhaseStop("flood")]`
exactly as `tests/test_recipe.py::_diff_page` does.

- [ ] **Step 5: Run the battery, then everything**

Run: `uv run pytest tests/test_reproject.py -q && uv run pytest -q && uv run ruff check && uv run pyright`
Expected: PASS. Any round-trip divergence is a real seam/replay bug — debug
with `superpowers:systematic-debugging`, never by widening the exclusions
(the exclusion sets in D5 are the contract; changing them requires updating
the plan/PR rationale, not making a red test green).

- [ ] **Step 6: Commit**

```bash
git add tests/test_reproject.py
git commit -m "test(reproject): round-trip identity + zero-network/credential + corrected-projection battery (spec §7)"
```

---

### Task 8: Documentation + real-archive smoke (DoD input)

**Files:**
- Create: `docs/features/reproject.md`
- Modify: `CLAUDE.md` (Status + Commands), `README.md` (command list, if it enumerates commands)
- Modify: `docs/superpowers/specs/2026-08-25-reproject-design.md` (append an
  "Implementation notes" section recording deviations D4.1–D4.5 and the D5
  equality contract, each with one-line rationale)

- [ ] **Step 1: Write `docs/features/reproject.md`**

Cover: what it does, the replay architecture (one paragraph + the clock
seam), CLI usage with the default `--out`, phase auto-detection and its
conservative-inference caveat, the D4 deviations, and a **Smoke transcript**
section to be filled in step 2.

- [ ] **Step 2: Real-archive smoke (safe: offline, source untouched)**

```bash
uv run paperboy reproject --profile default
sqlite3 data/default/paperboy.reprojected.sqlite \
  "SELECT json_array_length(json_extract(flags_json,'$')) IS NOT NULL, length(flags_json) FROM channels;
   SELECT count(*) FROM peers WHERE uri IN (SELECT json_extract(value_json,'$.uri') FROM sync_state WHERE scope='account');
   SELECT count(*) FROM messages; SELECT count(*) FROM edges;"
```

Verify against spec §9's payoff: the reprojected `channels.flags_json` has
the full flag set (~48, not 10), `peers` contains no self row, message/edge
counts are ≥ the source's live-code counts. Paste the actual command output
into the feature doc's smoke section and into the DoD report. If anything
errors on the real archive (older raw shapes), that is a bug to fix in
`replay.py` (no-shed), then re-run.

- [ ] **Step 3: Update CLAUDE.md status line + commands list; spec appendix; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add docs CLAUDE.md README.md
git commit -m "docs(reproject): feature doc with real-archive smoke, spec implementation notes"
```

---

## Self-Review (performed while writing)

- **Spec coverage:** §2 replay pair → Tasks 4–5; §3 method table → Task 4
  (with D4 deviations recorded and tested); §4 media/web → Tasks 4–5 + media
  write-guard in Task 3; §5 clock seam + parity test → Tasks 1–3; §6 CLI +
  composition + diff summary → Task 6; §7 full test battery → Task 7 (+ Task
  4 unit tests); §8 guardrails → Task 6 composition + Task 7 step 2; §9
  payoff → Task 8 smoke. Deferred items (§9: `--force`, `--verify`,
  incremental) deliberately absent.
- **Type consistency:** `Clock.for_payload(dict) -> str` is the single
  collector-facing clock method everywhere; `ReplayClock.serve/serve_json/
  begin_batch` are gateway-facing only. `add_raw`'s new param is
  keyword-with-default everywhere it's passed. `WebGetter` is the only
  annotation change the web collector needs. `collect_channel(..., clock=)`
  matches Task 6's call.
- **Known judgment calls** (flag in the PR body): D3's live-behavior change
  for derived-edge timestamps; D4.1–D4.5; D5's `source_raw_id` exclusion and
  distinct-row comparison. Each is argued inline where implemented.

---

# Revision R (2026-08-26): run-structure redesign (ADR-0005)

> Authorized after the first review loop exhausted (#33, escalation comment).
> Base: `40088db` on `feat/reproject` — the green single-run implementation of
> Tasks 1–8 **without** the rejected `_backfill_older_*` shadow path (that
> path exists only on parked branch `worktree-wf_d33e1b80-e14-7`; do not merge
> it — R5 re-derives multi-run support from the run-id design instead).
> Read `docs/adr/0005-run-structure.md` and spec §11 before starting.
> The D1–D5 design decisions above still stand; R-tasks build on them.

**Goal of the revision:** faithful replay of multi-run archives by recording
run structure (`raw_records.run_id`) and replaying once per historical run —
deleting the need for any projection logic outside the collectors.

## Task R1: The two-run round-trip gate (red first, committed as strict xfail)

The missing convergence signal from #33. Written and confirmed failing
BEFORE any implementation, committed with `@pytest.mark.xfail(strict=True)`
so every intermediate commit stays green while the gate stays real — R5
removes the marker and the test must then pass (strict xfail turns an
unexpected pass into a failure, so the marker cannot silently linger).

**Files:**
- Modify: `tests/test_reproject_parity.py` (a fixtures hook on
  `run_full_collect`), `tests/test_reproject.py` (the gate test)

**Interfaces:**
- `run_full_collect(data_dir: Path, mutate_fixtures: Callable[[dict], dict] | None = None) -> Path`
  — the hook applies to the FakeGateway fixtures dict only; web transport
  unchanged. Existing callers unaffected (default None).

- [ ] **Step 1: Add the hook** — in `run_full_collect`, replace
  `FakeGateway(full_collect_fixtures())` with:

```python
    fixtures = full_collect_fixtures()
    if mutate_fixtures is not None:
        fixtures = mutate_fixtures(fixtures)
    ...
        await collect_channel(FakeGateway(fixtures), store, settings, ...)
```

- [ ] **Step 2: Write the gate test** (append to `tests/test_reproject.py`)

```python
def _second_run_fixtures(fx: dict) -> dict:
    """Run 2 observes one NEW message carrying NEW media, on top of
    everything run 1 saw — so run 2 exercises incremental backfill, repeat
    snapshots/metrics/web captures, AND a fresh MediaDownload (which is what
    makes run 2's media phase raw-detectable, spec D4.5/ADR-0005 residual)."""
    fx = {**fx, "history": [
        {
            "_": "message", "id": 4, "message": "new in run 2",
            "date": 1769322400, "views": 3,
            "media": {
                "_": "MessageMediaDocument",
                "document": {
                    "_": "Document", "id": 77, "access_hash": 1,
                    "mime_type": "text/plain",
                    "attributes": [
                        {"_": "DocumentAttributeFilename", "file_name": "b.txt"}
                    ],
                },
            },
        },
        *fx["history"],
    ]}
    fx["media"] = {**fx["media"], 4: b"second run bytes"}
    return fx


@pytest.mark.xfail(
    reason="#33 / ADR-0005: multi-run replay lands in tasks R2-R5",
    strict=True,
)
def test_two_run_round_trip_identity(tmp_path, monkeypatch):
    """ADR-0005 gate: a source built from TWO collect passes — the ordinary
    real-archive shape — must round-trip identically, time series included."""
    asyncio.run(run_full_collect(tmp_path))
    db1 = asyncio.run(run_full_collect(tmp_path, mutate_fixtures=_second_run_fixtures))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db1, tmp_path / "default" / "paperboy.reprojected.sqlite")
```

- [ ] **Step 3: Verify it fails for the RIGHT reason** — run once with the
  xfail marker commented out:
  `uv run pytest tests/test_reproject.py::test_two_run_round_trip_identity -q`
  Expected: FAIL with table divergence (at minimum `channel_snapshots`,
  `web_snapshots`, `raw_records` counts collapsed to one run's worth —
  the #33 reproduction). Restore the marker; the run must then report
  xfailed, and the FULL suite stays green.

- [ ] **Step 4: Commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add tests/test_reproject_parity.py tests/test_reproject.py
git commit -m "test(reproject): two-run round-trip gate, strict-xfail until ADR-0005 lands (#33)"
```

## Task R2: `run_id` in the raw log

**Files:**
- Create: `src/paperboy/store/migrations/0003_run_id.sql`
- Modify: `src/paperboy/store/db.py` (`begin_run`, `add_raw`),
  `src/paperboy/recipes.py` (`collect_channel(run_id=...)`),
  `tests/test_reproject_parity.py` (`dump_db` run_id normalization),
  `tests/fixtures/reproject/parity_golden.json` (regenerated, see step 5)
- Test: `tests/test_clock.py` or `tests/test_store_migrations.py` additions

**Interfaces:**
- `Store.begin_run(run_id: str | None = None) -> str` — sets the id stamped
  on every subsequent `add_raw`; generates `uuid4().hex` when None.
- `collect_channel(..., run_id: str | None = None)` — calls
  `store.begin_run(run_id)` before building the context. Live callers pass
  nothing (fresh id); replay passes the source run's id.

- [ ] **Step 1: Failing tests**

```python
def test_add_raw_stamps_the_begun_run(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        rid = store.begin_run()
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        row = store.conn.execute("SELECT run_id FROM raw_records").fetchone()
        assert row["run_id"] == rid and len(rid) == 32


def test_add_raw_without_begin_run_leaves_null(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        assert store.conn.execute(
            "SELECT run_id FROM raw_records"
        ).fetchone()["run_id"] is None


def test_begin_run_accepts_injected_id(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        assert store.begin_run("legacy-0001") == "legacy-0001"
```

- [ ] **Step 2: Migration** — `0003_run_id.sql`:

```sql
-- 0003_run_id: which collect pass produced each raw record (ADR-0005).
-- Nullable: legacy rows predate the column and are segmented at replay
-- time by the tier='self' marker rule — never rewritten here.
ALTER TABLE raw_records ADD COLUMN run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_raw_records_run ON raw_records(run_id);
```

  NOTE: `executescript` re-runs the whole file if interrupted, and
  `ALTER TABLE ADD COLUMN` is not idempotent — but `schema_migrations`
  gates by stem exactly as for 0001/0002, and the index line is. Match the
  existing migration style; no special handling needed.

- [ ] **Step 3: `Store.begin_run` + `add_raw` stamping**

```python
    def begin_run(self, run_id: str | None = None) -> str:
        """Mark the start of one collect pass (ADR-0005): every subsequent
        `add_raw` carries this id, so replay can reconstruct pass boundaries.
        Replay injects the SOURCE run's id, making a reprojected DB itself
        re-reprojectable; live runs take a fresh opaque id."""
        self._run_id = run_id if run_id is not None else uuid4().hex
        return self._run_id
```

  with `self._run_id: str | None = None` initialised in `__init__`, and the
  INSERT extended with `run_id` = `self._run_id`.

- [ ] **Step 4: `collect_channel` begins the run** — add keyword
  `run_id: str | None = None`; first statement of the function body:
  `store.begin_run(run_id)`.

- [ ] **Step 5: Parity golden — normalize and regenerate ONCE, reviewed**

  `run_id` values are random per process, so `dump_db` must normalize them:
  map each distinct non-NULL `run_id` to `run-0001`, `run-0002`, … in order
  of first appearance (by raw rowid). Then regenerate the golden
  (`UPDATE_GOLDEN=1 …`) and **diff-review it**: the ONLY change must be the
  new `"run_id": "run-0001"` field on `raw_records` rows. Any other diff
  means live behavior changed — stop and fix instead of committing. This is
  the one sanctioned regeneration (schema addition), per the golden's
  protocol.

- [ ] **Step 6: Full suite + lint/type; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add -A src/paperboy tests
git commit -m "feat(store): run_id on raw_records + begin_run seam (ADR-0005)"
```

## Task R3: Order-independent `upsert_peer` / `upsert_channel`

A live-collect correctness fix in its own right (two observations applied
out of order must not invert `first_seen`/`last_seen` or let stale data
clobber newer state), and defense-in-depth for replay.

**Files:**
- Modify: `src/paperboy/store/peers.py`, `src/paperboy/store/channels.py`
- Test: `tests/test_store_peers.py`, `tests/test_store_channels.py`

- [ ] **Step 1: Failing tests** (one shown per module; mirror for channels)

```python
def test_out_of_order_observation_keeps_seen_window_and_newest_state(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        raw_new = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        raw_old = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        upsert_peer(store, {"_": "user", "id": 9, "username": "new_name"},
                    raw_new, "2026-02-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        # The OLDER observation arrives second (out of order):
        upsert_peer(store, {"_": "user", "id": 9, "username": "old_name"},
                    raw_old, "2026-01-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        row = store.conn.execute(
            "SELECT username, first_seen, last_seen FROM peers"
        ).fetchone()
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen"] == "2026-02-01T00:00:00+00:00"
        assert row["username"] == "new_name"   # stale data must not clobber
```

- [ ] **Step 2: Implement** — in `upsert_peer`'s main upsert, guard every
  current-state column and widen the seen window (ISO-8601 UTC strings
  compare lexicographically — the repo's canonical timestamp form):

```sql
ON CONFLICT(uri) DO UPDATE SET
    kind        = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.kind        ELSE peers.kind        END,
    id          = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.id          ELSE peers.id          END,
    access_hash = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.access_hash ELSE peers.access_hash END,
    is_min      = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.is_min      ELSE peers.is_min      END,
    seen_in_chat= CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.seen_in_chat ELSE peers.seen_in_chat END,
    seen_in_msg = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.seen_in_msg ELSE peers.seen_in_msg END,
    username    = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.username    ELSE peers.username    END,
    first_name  = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.first_name  ELSE peers.first_name  END,
    last_name   = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.last_name   ELSE peers.last_name   END,
    title       = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.title       ELSE peers.title       END,
    flags_json  = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.flags_json  ELSE peers.flags_json  END,
    source_raw_id = CASE WHEN excluded.last_seen >= peers.last_seen THEN excluded.source_raw_id ELSE peers.source_raw_id END,
    first_seen  = MIN(peers.first_seen, excluded.first_seen),
    last_seen   = MAX(peers.last_seen,  excluded.last_seen)
```

  The min-observation-on-richer-row branch (`UPDATE peers SET seen_in_chat=…
  WHERE uri=?`) gets the same treatment: apply its field updates only when
  `? >= last_seen` (bind `observed_at`), and always
  `last_seen = MAX(last_seen, ?)`. `upsert_channel`: same CASE pattern for
  its update set; `channel_snapshots` append stays unconditional (it is the
  time series). Update both docstrings: last-write-wins becomes
  newest-observation-wins, and say why (ADR-0005 §6).

- [ ] **Step 3: Full suite + parity golden still green** (frozen clock ⇒
  all comparisons `>=` true ⇒ behavior identical) **; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add src/paperboy/store tests
git commit -m "fix(store): order-independent peer/channel upserts — newest observation wins (ADR-0005)"
```

## Task R4: #34 — non-channel resolution is a skip, not a crash

**Files:**
- Modify: `src/paperboy/collectors/channel.py` (`_resolved_channel_id`)
- Test: `tests/test_collector_channel.py`

- [ ] **Step 1: Failing test** — a `resolve` fixture whose `peer` is
  `{"_": "PeerUser", "user_id": 7}` run through `collect_channel` must yield
  the channel phase `stopped="skip"` (and the run continue), not a raised
  `ValueError`. Check the existing non-channel test asserting `ValueError`
  and repoint it at `SkipAndRecord`.

- [ ] **Step 2: Implement** — in `_resolved_channel_id`, replace
  `raise ValueError(...)` with `raise SkipAndRecord(...)` (import from
  `paperboy.budget`), keeping the message; update its docstring ("refuse" →
  "skip cleanly: a username can legitimately be a user or basic group —
  issue #34"). `_pick_channel`'s `ValueError` stays: that one is an internal
  invariant breach (resolve *said* channel but chats disagree), not a
  data condition.

- [ ] **Step 3: Full suite; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add src/paperboy/collectors/channel.py tests/test_collector_channel.py
git commit -m "fix(channel): non-channel resolution skips cleanly instead of crashing (#34)"
```

## Task R5: Per-run replay (the core)

**Files:**
- Modify: `src/paperboy/replay.py`, `src/paperboy/reproject.py`,
  `tests/test_replay_gateway.py`, `tests/test_replay_web.py`,
  `tests/test_reproject.py` (remove the R1 xfail marker)

**Interfaces:**

```python
@dataclass(frozen=True)
class ReplayRun:
    run_id: str      # real id, or "legacy-000N" for an inferred segment
    lo: int          # first raw rowid of the pass (inclusive)
    hi: int          # last raw rowid of the pass (inclusive)

class ReplaySource:
    def runs(self) -> list[ReplayRun]: ...                      # capture order
    def resolve_targets(self, run: ReplayRun) -> list[str]: ... # now run-scoped
    def linked_group_ids(self, run: ReplayRun) -> set[int]: ...
    def has_kind(self, run: ReplayRun, *kinds: str) -> bool: ...
    def has_context_channel(self, run: ReplayRun, channel_ids: set[int]) -> bool: ...

class RawReplayGateway:
    def __init__(self, source: ReplaySource, clock: ReplayClock, run: ReplayRun) -> None: ...

class RawReplayWebClient:
    def __init__(self, source: ReplaySource, clock: ReplayClock, run: ReplayRun) -> None: ...

def detect_phases(source: ReplaySource, run: ReplayRun) -> list[str]: ...
```

- [ ] **Step 1: Failing tests for `runs()`** (in `test_replay_gateway.py`)

```python
def test_runs_groups_by_run_id_in_capture_order(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.begin_run("aaa")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        st.begin_run("bbb")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
    src = ReplaySource.open(db, tmp_path / "media")
    runs = src.runs()
    assert [(r.run_id, r.lo, r.hi) for r in runs] == [("aaa", 1, 2), ("bbb", 3, 3)]


def test_runs_segments_legacy_rows_at_self_markers(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:  # no begin_run: run_id stays NULL (legacy)
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 2), ("legacy-0002", 3, 4),
    ]


def test_runs_mixed_legacy_then_stamped(tmp_path):
    # Legacy segment(s) precede stamped runs — the migration boundary shape.
    ...  # two legacy rows (one self marker), then begin_run("ccc") + rows;
         # expect [("legacy-0001", 1, 2), ("ccc", 3, ...)]
```

  (Write the `...` body out fully in the actual test file.)

- [ ] **Step 2: Implement `ReplaySource.runs()`**

```python
    def runs(self) -> list[ReplayRun]:
        """Capture-ordered collect passes (ADR-0005). Stamped rows group by
        run_id; legacy NULL rows are segmented at each tier='self' User
        record — every collect pass's first raw write (channel.py writes
        self before anything, and the CLI refuses dependent phases without
        `channel`) — rows before the first marker join the first segment.
        Runs must be contiguous rowid ranges (one sequential process per
        pass); interleaving means a corrupt source and fails loudly."""
        rows = self.conn.execute(
            "SELECT id, run_id, tier, lower(kind) AS k FROM raw_records ORDER BY id"
        ).fetchall()
        runs: list[ReplayRun] = []
        current_id: str | None = None
        legacy_n = 0
        lo = hi = None
        seen_run_ids: set[str] = set()

        def _flush() -> None:
            nonlocal lo, hi
            if lo is not None:
                assert current_id is not None
                runs.append(ReplayRun(current_id, lo, hi))
                lo = hi = None

        for row in rows:
            if row["run_id"] is not None:
                boundary = row["run_id"] != current_id
                next_id = row["run_id"]
            else:
                is_marker = row["tier"] == "self" and (
                    row["k"] == "user" or row["k"].endswith(".user")
                )
                boundary = is_marker or current_id is None
                next_id = f"legacy-{legacy_n + 1:04d}" if boundary else current_id
            if boundary:
                _flush()
                if next_id in seen_run_ids:
                    raise ReprojectSourceError(
                        f"raw log run {next_id!r} is not contiguous — refusing to replay"
                    )
                seen_run_ids.add(next_id)
                if row["run_id"] is None:
                    legacy_n += 1
                current_id = next_id
                lo = row["id"]
            hi = row["id"]
        _flush()
        return runs
```

  with `class ReprojectSourceError(Exception)` in `replay.py` (re-exported or
  caught by `reproject.py` as operator-facing). NOTE the subtlety the first
  legacy segment needs: `boundary` is also true for the very first NULL row
  even when it is not a marker — rows before the first marker form
  `legacy-0001`, and a later marker then STARTS `legacy-0002`. Hand-check
  against `test_runs_segments_legacy_rows_at_self_markers` (marker-first
  source: the marker itself opens segment 1 — the `current_id is None`
  clause must not double-count it; get this right against the tests, they
  are the contract).

- [ ] **Step 3: Scope every replay query to the run** — `RawReplayGateway`
  and `RawReplayWebClient` take `run: ReplayRun`; every SQL in both classes
  gains `AND id BETWEEN ? AND ?` (bind `run.lo`, `run.hi`) — including
  `_latest`, `resolve`, `iter_history`, `get_messages`, the diff query and
  its synthetic-stamp `MAX(observed_at)` query, `download_media`,
  recommendations/invites/sponsored, and the web client's URL lookup (whose
  `id > ?` cursor then starts at `run.lo - 1`). `ReplaySource`'s helper
  queries (`resolve_targets`, `linked_group_ids`, `has_kind`,
  `has_context_channel`) same. Within one run each call site now has at
  most one record, which is what makes `_latest`'s "a live RPC has one
  now" comment true again — update the comments accordingly. Update every
  existing test in `test_replay_gateway.py`/`test_replay_web.py` to build
  the gateway with `src.runs()[0]` (the seeded fixtures are single-run, so
  behavior is unchanged — no assertion should need to move; if one does,
  that is a real regression to investigate, not adjust).

- [ ] **Step 4: Per-run orchestration in `reproject()`**

  Replace the per-target loop with runs-outer / targets-inner; per-run
  detection; thread the run id into the target store:

```python
    runs = source.runs()
    if not runs:
        raise ReprojectError("source raw log is empty — nothing to reproject")
    replayed_any = False
    for run in runs:
        run_phases = phases if phases is not None else detect_phases(source, run)
        for raw_target in source.resolve_targets(run):
            replayed_any = True
            clock = ReplayClock()
            gateway = RawReplayGateway(source, clock, run)
            web_client = RawReplayWebClient(source, clock, run)
            collectors = [...]  # unchanged list
            try:
                run_results = await collect_channel(
                    gateway, out_store, replay_settings, parse_target(raw_target),
                    list(run_phases), log,
                    collectors=collectors, profile=profile, clock=clock,
                    run_id=run.run_id,
                )
            except Exception as exc:
                ...  # existing per-target isolation (D4.7), keyed per run
            results.setdefault(raw_target, []).extend(run_results)
    if not replayed_any:
        raise ReprojectError(
            "source has no resolve records in raw_records — nothing to reproject"
        )
```

  `ReprojectSummary.phases` becomes the union in first-seen order (or a
  per-run mapping — pick one, render it sensibly in the CLI tables; the CLI
  currently prints one table per target, which still works with
  results-extended-per-run). `detect_phases(source, run)` passes `run`
  through to the source helpers; docstring keeps the D4.5 conservatism note
  and adds the per-run scope (ADR-0005). Catch `ReprojectSourceError` in
  the CLI alongside `ReprojectError`.

- [ ] **Step 5: Remove the R1 xfail marker.** Run the gate:
  `uv run pytest tests/test_reproject.py::test_two_run_round_trip_identity -q`
  Expected: PASS — the redesign's definition of done. Then the whole
  battery: every single-run test (round-trip, corrected-projection,
  zero-network, partial-source, media-no-rewrite) must still pass — a
  single-run source is now just the one-run case of the same model.

- [ ] **Step 6: Full suite + lint/type; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add -A src/paperboy tests
git commit -m "feat(reproject): replay once per historical run — run-scoped gateway, per-run phases (ADR-0005, #33)"
```

## Task R6: Docs + real-archive smoke (multi-run this time)

**Files:**
- Modify: `docs/features/reproject.md` (run-structure section + NEW smoke
  transcript), `CLAUDE.md` (status), ADR-0005 (status stays accepted; add a
  "verified" note with the smoke date)

- [ ] **Step 1: Real-archive smoke.** Delete any stale
  `data/default/paperboy.reprojected.sqlite` from the previous round first
  (it is a generated artifact of this feature's own earlier smoke — confirm
  the filename matches before deleting; touch nothing else under `data/`).

```bash
uv run paperboy reproject --profile default
```

  Verify, and paste actual output into the feature doc:
  - segmentation: number of replayed runs ≈ `run_events`' distinct run count
    (cross-check only — `SELECT count(*) FROM run_events WHERE kind='complete'
    AND phase='channel'` approximates pass count);
  - time series now survive: `web_snapshots`, `channel_snapshots`,
    `message_metrics` reprojected counts match the source's (they collapsed
    to one run's worth in round 1 — the #33 evidence table);
  - the round-1 payoffs still hold: 48-flag `flags_json`, zero self peers;
  - `@atom8388` (the non-channel target) now shows as a clean per-run skip
    in the output, not an error row.

- [ ] **Step 2: Update docs; full suite; commit**

```bash
uv run pytest -q && uv run ruff check && uv run pyright
git add docs CLAUDE.md
git commit -m "docs(reproject): ADR-0005 run-structure revision — multi-run real-archive smoke"
```

## Revision self-review

- R1 is the gate #33 said was missing, strict-xfail keeps commits green
  while it is red, and its fixture varies run 2 (new message + new media) so
  incremental backfill and per-run media detection are both exercised.
- R2/R3/R4 are independent, individually-tested, and each is also a
  live-collect improvement; R5 consumes R2's ids and R1's gate; R6 proves
  the ADR's claims against the archive that broke round 1.
- Deliberately NOT done: merging `worktree-wf_d33e1b80-e14-7` (the shadow
  path), sticky media detection (would fabricate custody observations for
  runs that never ran media — #36 stays open, narrowed), and any mutation of
  the source DB for legacy segmentation (replay-time inference only).
