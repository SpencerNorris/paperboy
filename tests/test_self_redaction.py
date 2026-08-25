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
async def test_account_state_still_records_self(tmp_path):
    """Excluding self from the dataset must not break the operational cursor:
    `sync_state('account','self')` still records the id/uri (issue #12)."""
    st = await _run(tmp_path)
    try:
        assert get_state(st, "account", "self") == {
            "uri": "tg:user:8846802359",
            "id": 8846802359,
        }
    finally:
        st.close()


def _fixtures_with_self_in_vector() -> dict:
    """self also appears in getFullChannel's `users` vector — the incidental
    path issue #12 names (self rides along in the users/chats of responses),
    distinct from the explicit get_me() upsert."""
    fx = _fixtures()
    full = json.loads((FX / "full_channel.json").read_text())
    full["users"] = [
        {"_": "user", "id": 8846802359, "first_name": "Mark", "access_hash": 1},
        {"_": "user", "id": 555, "first_name": "Someone Else"},
    ]
    fx["full_channel"] = full
    return fx


@pytest.mark.asyncio
async def test_self_is_excluded_from_peers_even_via_a_response_vector(tmp_path):
    """The collecting account must not land in `peers` — neither from the
    explicit get_me() nor from riding along in a response's users vector
    (issue #12). A genuine other peer in the same vector is still stored."""
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            FakeGateway(_fixtures_with_self_in_vector()), st,
            load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        self_row = st.conn.execute(
            "SELECT uri FROM peers WHERE uri='tg:user:8846802359'"
        ).fetchone()
        assert self_row is None, "self must never be a peer row"
        other = st.conn.execute("SELECT uri FROM peers WHERE uri='tg:user:555'").fetchone()
        assert other is not None, "a genuine other peer is still stored"
        # the operational cursor is intact
        state = get_state(st, "account", "self")
        assert state is not None and state["id"] == 8846802359
