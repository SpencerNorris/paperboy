"""The collecting account's own credentials must never be persisted.

`channel` calls `users.getFullUser`-equivalent `get_me()` to learn which
account is doing the collecting. Telegram returns the *full* `User` object for
self, which — uniquely among all peers — carries `phone`. CLAUDE.md is
explicit: "Credentials (phone, `api_hash`, session, login codes) never in logs
or the repo" and "Exports scrub the collecting account".

`raw_records` is the system of record and is written verbatim before any
projection, so it is exactly where an unredacted self-object does the most
damage: it survives into Datasette, screenshots and `export` output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from paperboy.collectors.base import CollectContext
from paperboy.collectors.channel import ChannelCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.store.sync import get_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/tl")

PHONE = "12039783618"


def _fixtures() -> dict:
    return {
        "resolve": json.loads((FX / "resolve_durov.json").read_text()),
        "full_channel": json.loads((FX / "full_channel.json").read_text()),
        # The shape Telegram actually returns for self: `phone` present.
        "self": {
            "_": "user",
            "id": 8846802359,
            "is_self": True,
            "first_name": "Mark",
            "phone": PHONE,
            "access_hash": -887699644970735024,
        },
    }


async def _run(tmp_path: Path) -> Store:
    st = Store.open(tmp_path / "p.sqlite").__enter__()
    ctx = CollectContext(
        FakeGateway(_fixtures()), st, load_settings("default", {}), parse_target("@durov"),
        None, None, "stranger", logging.getLogger("t"),
    )
    await ChannelCollector().collect(ctx)
    return st


def _all_text_in_db(st: Store) -> str:
    """Every value in every user table, flattened — the blunt instrument.

    A redaction that only covers the column someone remembered is not a
    redaction, so this asserts against the whole database rather than
    against `raw_records.payload_json` specifically.
    """
    tables = [
        r["name"]
        for r in st.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    chunks: list[str] = []
    for table in tables:
        for row in st.conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 - names from schema
            chunks.extend("" if v is None else str(v) for v in tuple(row))
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_self_phone_is_not_written_to_raw_records(tmp_path):
    st = await _run(tmp_path)
    try:
        payloads = "\n".join(
            r["payload_json"] for r in st.conn.execute("SELECT payload_json FROM raw_records")
        )
        assert PHONE not in payloads
    finally:
        st.close()


@pytest.mark.asyncio
async def test_self_phone_is_not_written_anywhere_in_the_database(tmp_path):
    st = await _run(tmp_path)
    try:
        assert PHONE not in _all_text_in_db(st)
    finally:
        st.close()


@pytest.mark.asyncio
async def test_redacted_self_record_is_still_recognisable_as_the_self_user(tmp_path):
    """Redaction removes the credential, not the record.

    The self raw record still has to identify *which* account collected, or
    provenance breaks — so id and the `self` marker must survive.
    """
    st = await _run(tmp_path)
    try:
        row = st.conn.execute(
            "SELECT payload_json FROM raw_records WHERE tier='self'"
        ).fetchone()
        assert row is not None, "the self observation must still be recorded"
        payload = json.loads(row["payload_json"])
        assert payload["id"] == 8846802359
        assert payload["is_self"] is True
        assert "phone" not in payload
    finally:
        st.close()


@pytest.mark.asyncio
async def test_self_peer_projection_and_account_state_are_unaffected(tmp_path):
    """Regression guard: scrubbing must not break what depends on self."""
    st = await _run(tmp_path)
    try:
        assert get_state(st, "account", "self") == {
            "uri": "tg:user:8846802359",
            "id": 8846802359,
        }
        peer = st.conn.execute(
            "SELECT first_name FROM peers WHERE uri='tg:user:8846802359'"
        ).fetchone()
        assert peer is not None
        assert peer["first_name"] == "Mark"
    finally:
        st.close()
