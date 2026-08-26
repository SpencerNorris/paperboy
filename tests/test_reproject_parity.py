"""Live-collect parity: a full frozen-clock collect must produce byte-identical
projections before and after the observed-at seam (spec §5). Regenerate the
golden with: UPDATE_GOLDEN=1 uv run pytest tests/test_reproject_parity.py -q
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from paperboy.collectors.web import WebCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
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


async def run_full_collect(
    data_dir: Path,
    mutate_fixtures: Callable[[dict], dict] | None = None,
) -> Path:
    """Collect every phase into <data_dir>/default/paperboy.sqlite; returns the DB path.

    `mutate_fixtures` (ADR-0005 / #33) lets a caller run a SECOND collect pass
    against the same DB with varied FakeGateway fixtures — e.g. a later run
    observing a new message — to exercise multi-run replay. The web transport
    is unchanged; only the FakeGateway fixtures dict is mutated.
    """
    settings = load_settings("default", {"data_dir": data_dir})
    db = data_dir / "default" / "paperboy.sqlite"
    web = WebCollector(
        client=WebClient(transport=_web_transport()),
        min_interval=0.0, sleep=lambda s: None,
    )
    from paperboy.collectors.media import MediaCollector
    from paperboy.recipes import _default_collectors
    collectors = [
        c for c in _default_collectors(include_media=False, include_web=False)
    ] + [web, MediaCollector()]
    fixtures = full_collect_fixtures()
    if mutate_fixtures is not None:
        fixtures = mutate_fixtures(fixtures)
    with Store.open(db) as store:
        await collect_channel(
            FakeGateway(fixtures), store, settings,
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
                for c, v in zip(cols, row, strict=True)
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
        # One-shot test-fixture regeneration, not runtime code — a
        # trio.Path/anyio.Path dependency for this would buy nothing.
        GOLDEN.write_text(  # noqa: ASYNC240
            json.dumps(dumped, indent=1, sort_keys=True, default=str)
        )
        pytest.skip("golden regenerated")
    golden = json.loads(GOLDEN.read_text())  # noqa: ASYNC240 - see above
    assert dumped == golden
