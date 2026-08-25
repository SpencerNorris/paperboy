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


def _linked_group_first_fixtures():
    """Both chats vectors list the linked megagroup BEFORE the target.

    Telegram does not promise a vector ordering, and a linked discussion
    megagroup serialises as `Channel` too — the live ChatFull capture already
    carries both (issue #23). Identity must come from `peer` / `full_chat.id`,
    never from position.
    """
    group = {
        "_": "channel", "id": 777, "access_hash": 11,
        "title": "linked group", "megagroup": True,
    }
    target = {
        "_": "channel", "id": 5, "access_hash": 99,
        "title": "Durov", "username": "durov", "broadcast": True,
    }
    return {
        "resolve": {
            "_": "contacts.resolvedPeer",
            "peer": {"_": "PeerChannel", "channel_id": 5},
            "chats": [group, target], "users": [],
        },
        "full_channel": {
            "_": "messages.chatFull",
            "full_chat": {
                "_": "channelFull", "id": 5, "participants_count": 100,
                "about": "x", "pts": 42, "linked_chat_id": 777,
            },
            "chats": [group, target], "users": [],
        },
        "self": {"_": "user", "id": 1, "self": True},
    }


@pytest.mark.asyncio
async def test_channel_collector_picks_the_target_not_the_first_channel(tmp_path):
    gw = FakeGateway(_linked_group_first_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert ctx.channel_id == 5
        assert ctx.input_channel == {"channel_id": 5, "access_hash": 99}
        rows = [r["id"] for r in st.conn.execute("select id from channels")]
        assert rows == [5]  # the target's row — never the group's
        assert get_state(st, "channel", "5") == {"pts": 42}


@pytest.mark.asyncio
async def test_channel_collector_trusts_peer_over_the_chats_vector(tmp_path):
    # A resolution whose `peer` is not a channel must fail loudly even when a
    # channel happens to sit in `chats` — position is never identity.
    fx = _fixtures()
    fx["resolve"] = {
        "_": "contacts.resolvedPeer",
        "peer": {"_": "PeerUser", "user_id": 42},
        "chats": fx["resolve"]["chats"], "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(ValueError):
            await ChannelCollector().collect(ctx)


@pytest.mark.asyncio
async def test_channel_collector_rejects_resolve_full_identity_mismatch(tmp_path):
    # ctx.input_channel (access_hash) comes from the resolve-side pick;
    # ctx.channel_id / pts come from full_chat.id. If the two ever disagreed,
    # history would key state to one channel while addressing another — fail
    # loudly rather than split identity across the run.
    fx = _fixtures()
    fx["full_channel"] = {
        "_": "messages.chatFull",
        "full_chat": {
            "_": "channelFull", "id": 6, "participants_count": 1, "about": "x",
            "pts": 1, "linked_chat_id": 0,
        },
        "chats": [{"_": "channel", "id": 6, "access_hash": 77, "title": "other"}],
        "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(ValueError):
            await ChannelCollector().collect(ctx)


@pytest.mark.asyncio
async def test_channel_collector_rejects_a_resolution_without_peer(tmp_path):
    # `contacts.ResolvedPeer` always carries `peer` in the wild; a response
    # without it gives us no authoritative identity, so guessing is refused.
    fx = _fixtures()
    del fx["resolve"]["peer"]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(ValueError):
            await ChannelCollector().collect(ctx)
