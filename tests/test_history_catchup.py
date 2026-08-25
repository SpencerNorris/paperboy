import logging

import pytest

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import HistoryCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.sync import get_state, set_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway


def _ctx(st, gw, channel_id=5, overrides=None):
    return CollectContext(
        gw, st, load_settings("default", overrides or {}), parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, "stranger",
        logging.getLogger("t"),
    )


def _msg(mid, text):
    return {
        "_": "message", "id": mid, "message": text, "date": 1767322445,
        "peer_id": {"channel_id": 5},
    }


def _page(pts, *, final, messages=()):
    return {
        "_": "updates.channelDifference", "final": final, "pts": pts,
        "new_messages": list(messages), "other_updates": [], "chats": [], "users": [],
    }


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
        "_": "ChannelDifferenceTooLong",
        "dialog": {"_": "Dialog", "pts": 999},
        "messages": [], "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 1})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert get_state(st, "channel", "5") == {"pts": 999}
        assert res.stopped == "resynced"


@pytest.mark.asyncio
async def test_catchup_too_long_with_unset_dialog_pts_keeps_the_stored_cursor(tmp_path):
    # `Dialog.pts` is flags.0?int, and Telethon emits it present-with-None when
    # the flag is unset — a shape the hand-written {"pts": 999} fixture above
    # can never produce (issue #22). The corrupted-cursor failure only shows at
    # the gateway boundary, so the assertion is on what the gateway received.
    diff = {
        "_": "ChannelDifferenceTooLong",
        "dialog": {"_": "Dialog", "pts": None},
        "messages": [], "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 40})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert res.stopped == "resynced"
        assert get_state(st, "channel", "5") == {"pts": 40}
        await HistoryCollector().catch_up(_ctx(st, gw))
        assert None not in gw.channel_difference_pts


@pytest.mark.asyncio
async def test_catchup_too_long_projects_its_recovery_messages(tmp_path):
    # channelDifferenceTooLong carries the newest messages as a recovery
    # payload; discarding them strands data that already reached raw_records.
    diff = {
        "_": "ChannelDifferenceTooLong",
        "dialog": {"_": "Dialog", "pts": 999},
        "messages": [
            {
                "_": "message", "id": 30, "message": "carried by the resync",
                "date": 1767322445, "peer_id": {"channel_id": 5},
            }
        ],
        "chats": [], "users": [],
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 1})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        row = st.conn.execute(
            "select text from messages where uri='tg:msg:5/30'"
        ).fetchone()
        assert row is not None and row["text"] == "carried by the resync"
        assert res.counts["messages"] == 1
        assert res.stopped == "resynced"


@pytest.mark.asyncio
async def test_catchup_treats_a_corrupted_none_cursor_as_unseeded(tmp_path):
    # A pre-fix run could have persisted {"pts": None}; reading it back must
    # not forward None to the gateway (real Telethon dies on it with a
    # struct.error that bypasses the disposition system).
    diff = {
        "_": "updates.channelDifferenceEmpty", "final": True, "pts": 60, "timeout": 30,
    }
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": None})
        await HistoryCollector().catch_up(_ctx(st, gw))
        assert gw.channel_difference_pts == [0]
        assert get_state(st, "channel", "5") == {"pts": 60}


@pytest.mark.asyncio
async def test_catchup_loops_until_final_and_applies_every_page(tmp_path):
    # getChannelDifference must be called until the server sets `final`. A
    # backlog spanning two pages (first final=False) must apply BOTH and
    # persist the last page's pts — the single-call version truncated it and
    # reported success (issue #25).
    pages = [
        _page(50, final=False, messages=[_msg(10, "page one")]),
        _page(60, final=True, messages=[_msg(11, "page two")]),
    ]
    gw = FakeGateway({"channel_difference": pages})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 40})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert gw.calls.count("get_channel_difference") == 2
        assert gw.channel_difference_pts == [40, 50]  # second call resumes from page-one pts
        for mid, text in ((10, "page one"), (11, "page two")):
            row = st.conn.execute(
                "select text from messages where uri=?", (f"tg:msg:5/{mid}",)
            ).fetchone()
            assert row is not None and row["text"] == text
        assert res.counts["messages"] == 2
        assert get_state(st, "channel", "5") == {"pts": 60}
        assert res.stopped is None


@pytest.mark.asyncio
async def test_catchup_empty_difference_ends_immediately(tmp_path):
    # channelDifferenceEmpty (real capture: {'final': True, 'pts': …, 'timeout': …})
    # is a clean caught-up signal — one call, no spin.
    diff = {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 60, "timeout": 30}
    gw = FakeGateway({"channel_difference": diff})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 55})
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert gw.calls.count("get_channel_difference") == 1
        assert get_state(st, "channel", "5") == {"pts": 60}
        assert res.stopped is None


@pytest.mark.asyncio
async def test_catchup_stops_if_pts_does_not_advance(tmp_path):
    # A server that returns final=False but never advances pts must not spin
    # forever — stop with a warning instead.
    stuck = _page(50, final=False, messages=[_msg(10, "stuck")])
    gw = FakeGateway({"channel_difference": stuck})  # bare dict → same page every call
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 50})  # equal to the page's pts → no progress
        res = await HistoryCollector().catch_up(_ctx(st, gw))
        assert gw.calls.count("get_channel_difference") == 1
        assert res.stopped is None


@pytest.mark.asyncio
async def test_catchup_page_budget_stops_and_reports_partial_counts(tmp_path):
    # A backlog larger than the budget stops politely, carrying the counts it
    # accumulated (per 2a40754) and distinguishable from a clean finish.
    pages = [
        _page(50, final=False, messages=[_msg(10, "a")]),
        _page(60, final=False, messages=[_msg(11, "b")]),
        _page(70, final=True, messages=[_msg(12, "c")]),
    ]
    gw = FakeGateway({"channel_difference": pages})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "channel", "5", {"pts": 40})
        with pytest.raises(PhaseStop) as ei:
            await HistoryCollector().catch_up(_ctx(st, gw, overrides={"catchup_page_budget": 2}))
        assert ei.value.counts["messages"] == 2  # two pages applied before the stop
        assert gw.calls.count("get_channel_difference") == 2
        # progress was persisted, so a re-run resumes rather than restarts
        assert get_state(st, "channel", "5") == {"pts": 60}


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
