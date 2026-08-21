import logging

import pytest

from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import HistoryCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.sync import get_state, set_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway


def _ctx(st, gw, channel_id=5):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, "stranger",
        logging.getLogger("t"),
    )


@pytest.mark.asyncio
async def test_catchup_applies_edit_and_delete(tmp_path):
    diff = {
        "_": "updates.channelDifference", "final": True, "pts": 50,
        "new_messages": [
            {
                "_": "message", "id": 20, "message": "new", "date": 1767322445,
                "peer_id": {"channel_id": 5},
            }
        ],
        "other_updates": [
            {
                "_": "updateEditChannelMessage",
                "message": {
                    "_": "message", "id": 10, "message": "edited", "date": 1767322445,
                    "edit_date": 1767322900, "peer_id": {"channel_id": 5},
                },
            },
            {"_": "updateDeleteChannelMessages", "messages": [11]},
        ],
        "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        for mid in (10, 11):
            m = {
                "_": "message", "id": mid, "message": "orig", "date": 1767322445,
                "peer_id": {"channel_id": 5},
            }
            r = st.add_raw("message", m, "stranger", None)
            upsert_message(st, 5, m, r, "2026-01-01T00:00:00+00:00", "stranger")
        set_state(st, "channel", "5", {"pts": 40})
        ctx = _ctx(st, gw)
        res = await HistoryCollector().catch_up(ctx)
        assert st.conn.execute(
            "select deleted_at from messages where uri='tg:msg:5/11'"
        ).fetchone()["deleted_at"] is not None
        assert st.conn.execute(
            "select text from messages where uri='tg:msg:5/10'"
        ).fetchone()["text"] == "edited"
        assert get_state(st, "channel", "5") == {"pts": 50}
        assert res.counts["messages"] == 2  # the new message + the edit-applied one
        assert res.counts["tombstones"] == 1


@pytest.mark.asyncio
async def test_catchup_new_message_is_inserted(tmp_path):
    diff = {
        "_": "updates.channelDifference", "final": True, "pts": 5,
        "new_messages": [
            {
                "_": "message", "id": 1, "message": "hi", "date": 1767322445,
                "peer_id": {"channel_id": 5},
            }
        ],
        "other_updates": [], "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 1})
        await HistoryCollector().catch_up(_ctx(st, gw))
        row = st.conn.execute("select text from messages where uri='tg:msg:5/1'").fetchone()
        assert row["text"] == "hi"


@pytest.mark.asyncio
async def test_catchup_too_long_resyncs_pts_and_flags_resynced(tmp_path):
    diff = {
        "_": "updates.channelDifferenceTooLong",
        "dialog": {"_": "dialog", "pts": 999},
        "messages": [], "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 1})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert get_state(st, "channel", "5") == {"pts": 999}
        assert res.stopped == "resynced"


@pytest.mark.asyncio
async def test_catchup_defaults_pts_to_zero_when_unseeded(tmp_path):
    diff = {
        "_": "updates.channelDifference", "final": True, "pts": 5,
        "new_messages": [], "other_updates": [], "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().catch_up(_ctx(st, gw))
        assert get_state(st, "channel", "5") == {"pts": 5}
