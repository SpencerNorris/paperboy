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


def _fixtures():
    return {
        "resolve": json.loads((FX / "resolve_durov.json").read_text()),
        "full_channel": json.loads((FX / "full_channel.json").read_text()),
        "self": {"_": "user", "id": 1, "self": True},
    }


@pytest.mark.asyncio
async def test_channel_collector(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        res = await ChannelCollector().collect(ctx)
        assert ctx.channel_id is not None
        row = st.conn.execute("select title, participants_count from channels").fetchone()
        assert row["participants_count"] >= 0
        assert res.counts["channels"] == 1


@pytest.mark.asyncio
async def test_channel_collector_sets_context_for_history(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert ctx.channel_id == 5
        assert ctx.input_channel == {"channel_id": 5, "access_hash": 99}


@pytest.mark.asyncio
async def test_channel_collector_seeds_pts(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert get_state(st, "channel", "5") == {"pts": 42}


@pytest.mark.asyncio
async def test_channel_collector_records_raw_and_peers(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        raw_kinds = {r["kind"] for r in st.conn.execute("select kind from raw_records")}
        assert "channelFull" in raw_kinds or "messages.chatFull" in raw_kinds
        peer_row = st.conn.execute("select uri from peers where uri='tg:channel:5'").fetchone()
        assert peer_row is not None


def test_applies_to_channel_like_targets():
    assert ChannelCollector().applies_to(parse_target("@durov"))
    assert not ChannelCollector().applies_to(parse_target("#osint"))
