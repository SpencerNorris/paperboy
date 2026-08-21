import logging

import pytest

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import HistoryCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.store.sync import get_state, set_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway


def _m(i, **extra):
    m = {
        "_": "message",
        "id": i,
        "message": f"m{i}",
        "date": 1767322445,
        "peer_id": {"channel_id": 5},
    }
    m.update(extra)
    return m


def _ctx(st, gw, channel_id=5, tier="stranger"):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, tier, logging.getLogger("t"),
    )


@pytest.mark.asyncio
async def test_backfill_detects_gap(tmp_path):
    # ids 5,4,2,1 present; id 3 is a hole -> get_messages returns messageEmpty for 3
    gw = FakeGateway({
        "history": [_m(5), _m(4), _m(2), _m(1)],
        "get_messages": {3: {"_": "messageEmpty", "id": 3}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        assert res.counts["messages"] == 4
        tomb = st.conn.execute("select message_uri, evidence from message_tombstones").fetchall()
        assert tomb and tomb[0]["evidence"] == "empty"
        assert tomb[0]["message_uri"] == "tg:msg:5/3"


@pytest.mark.asyncio
async def test_backfill_records_verified_range(tmp_path):
    gw = FakeGateway({
        "history": [_m(5), _m(4), _m(2), _m(1)],
        "get_messages": {3: {"_": "messageEmpty", "id": 3}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().collect(_ctx(st, gw))
        rows = st.conn.execute("select lo, hi from sync_ranges where channel_id=5").fetchall()
        assert [(r["lo"], r["hi"]) for r in rows] == [(1, 5)]


@pytest.mark.asyncio
async def test_backfill_persists_resume_cursor(tmp_path):
    gw = FakeGateway({"history": [_m(5), _m(4), _m(3)]})
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().collect(_ctx(st, gw))
        assert get_state(st, "history", "5") == {"offset_id": 3}


@pytest.mark.asyncio
async def test_backfill_resumes_from_stored_cursor(tmp_path):
    from paperboy.store.sync import set_state

    gw = FakeGateway({"history": [_m(5), _m(4), _m(3), _m(2), _m(1)]})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "history", "5", {"offset_id": 3})
        res = await HistoryCollector().collect(_ctx(st, gw))
        # Resumes at offset_id=3 -> only ids strictly below 3 are fetched.
        assert res.counts["messages"] == 2
        stored = {r["msg_id"] for r in st.conn.execute("select msg_id from messages")}
        assert stored == {1, 2}


@pytest.mark.asyncio
async def test_no_gap_means_no_tombstones(tmp_path):
    gw = FakeGateway({"history": [_m(3), _m(2), _m(1)]})
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        assert res.counts["messages"] == 3
        assert res.counts["tombstones"] == 0
        assert st.conn.execute("select count(*) as n from message_tombstones").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_forwarded_from_edge_recorded(tmp_path):
    fwd_from = {"_": "messageFwdHeader", "from_id": {"_": "peerChannel", "channel_id": 999}}
    msg = _m(1, fwd_from=fwd_from)
    gw = FakeGateway({"history": [msg]})
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        assert res.counts["edges"] == 1
        row = st.conn.execute(
            "select subject_uri, predicate, object_uri from edges where predicate='forwarded_from'"
        ).fetchone()
        assert row["subject_uri"] == "tg:msg:5/1"
        assert row["object_uri"] == "tg:channel:999"


@pytest.mark.asyncio
async def test_min_peer_recorded_with_message_provenance(tmp_path):
    msg = _m(1, from_id={"_": "peerUser", "user_id": 42})
    gw = FakeGateway({"history": [msg]})
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().collect(_ctx(st, gw))
        row = st.conn.execute(
            "select is_min, seen_in_chat, seen_in_msg from peers where uri='tg:user:42'"
        ).fetchone()
        assert row is not None
        assert row["is_min"] == 1
        assert row["seen_in_chat"] == 5
        assert row["seen_in_msg"] == 1


def test_applies_to_channel_like_targets():
    assert HistoryCollector().applies_to(parse_target("@durov"))
    assert not HistoryCollector().applies_to(parse_target("#osint"))


def _unset_ctx(st, gw):
    # Mirrors what `collect_channel` builds before `channel` has run, and
    # what it's left with if `channel` raised `PhaseStop` before setting
    # `input_channel`/`channel_id` (e.g. a FLOOD_WAIT during resolve()).
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        None, None, "stranger", logging.getLogger("t"),
    )


@pytest.mark.asyncio
async def test_collect_raises_phase_stop_when_channel_context_unset(tmp_path):
    gw = FakeGateway({"history": [_m(1)]})
    with Store.open(tmp_path / "p.sqlite") as st, pytest.raises(PhaseStop):
        await HistoryCollector().collect(_unset_ctx(st, gw))


@pytest.mark.asyncio
async def test_catch_up_raises_phase_stop_when_channel_context_unset(tmp_path):
    gw = FakeGateway({
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
    })
    with Store.open(tmp_path / "p.sqlite") as st, pytest.raises(PhaseStop):
        await HistoryCollector().catch_up(_unset_ctx(st, gw))


# --- generalization: explicit target + probe_gaps switch -------------------
#
# `discussion` reuses this same page loop against a linked group instead of
# duplicating cursor/resumability/FLOOD_WAIT handling (plan Task 1). These
# reuse this file's own `_m`/`_ctx` helpers rather than a `_msg` of their
# own — `_m` already parameterizes the message id and accepts overrides via
# `**extra`, and the channel the loop writes rows under is controlled
# entirely by `collect()`'s `channel_id` kwarg (or `ctx.channel_id` by
# default), never by anything inside the message payload itself, so no
# helper change is needed to target a different channel.


@pytest.mark.asyncio
async def test_collect_defaults_to_the_context_channel(tmp_path):
    """No kwargs => today's behaviour, bit for bit."""
    gw = FakeGateway({"history": [_m(2), _m(1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        rows = st.conn.execute("select channel_id from messages").fetchall()
        assert {r["channel_id"] for r in rows} == {5}
        assert res.counts["messages"] == 2


@pytest.mark.asyncio
async def test_collect_targets_an_explicit_channel(tmp_path):
    gw = FakeGateway({"history": [_m(2), _m(1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().collect(
            _ctx(st, gw), channel_id=77, input_channel={"channel_id": 77, "access_hash": 3},
        )
        rows = st.conn.execute("select channel_id from messages").fetchall()
        assert {r["channel_id"] for r in rows} == {77}


@pytest.mark.asyncio
async def test_probe_gaps_false_writes_no_tombstones(tmp_path):
    # ids 1 and 3 present, 2 missing: probing ON would tombstone 2.
    gw = FakeGateway({
        "history": [_m(3), _m(1)],
        "get_messages": {2: {"_": "MessageEmpty", "id": 2}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw), probe_gaps=False)
        assert res.counts["tombstones"] == 0
        assert st.conn.execute("select count(*) c from message_tombstones").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_probe_gaps_true_still_tombstones(tmp_path):
    gw = FakeGateway({
        "history": [_m(3), _m(1)],
        "get_messages": {2: {"_": "MessageEmpty", "id": 2}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        assert res.counts["tombstones"] == 1


@pytest.mark.asyncio
async def test_explicit_target_resumes_on_its_own_cursor(tmp_path):
    gw = FakeGateway({"history": [_m(2), _m(1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "history", "5", {"offset_id": 999})
        await HistoryCollector().collect(
            _ctx(st, gw), channel_id=77, input_channel={"channel_id": 77, "access_hash": 3},
        )
        assert get_state(st, "history", "5") == {"offset_id": 999}
        assert get_state(st, "history", "77") == {"offset_id": 1}
