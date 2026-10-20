"""`PostsCollector` (#91): fetch listed message ids by `channels.getMessages`
and project them through the same projection `history` uses."""

import json
import logging

import pytest

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import observe_message
from paperboy.collectors.posts import PostsCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
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


def _ctx(st, gw, channel_id=5, tier="stranger", **settings):
    return CollectContext(
        gw, st, load_settings("default", settings), parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, tier, logging.getLogger("t"),
    )


def test_observe_message_context_overrides_channel_only(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _ctx(st, FakeGateway({}))
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}
        observe_message(
            ctx, 5, _m(1), counts,
            context={"channel_id": 5, "method": "channels.getMessages"},
        )
        row = st.conn.execute("select context_json from raw_records").fetchone()
        assert json.loads(row["context_json"]) == {
            "channel_id": 5, "method": "channels.getMessages",
        }
        assert st.conn.execute("select count(*) from messages").fetchone()[0] == 1
        assert counts["messages"] == 1


def _posts_ctx(st, gw, ids, **kw):
    return _ctx(st, gw, post_msgs=list(ids), **kw)


@pytest.mark.asyncio
async def test_batches_of_100(tmp_path):
    gw = FakeGateway({"get_messages": {i: _m(i) for i in range(1, 251)}})
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await PostsCollector().collect(_posts_ctx(st, gw, range(1, 251)))
    assert gw.calls.count("get_messages") == 3
    assert res.counts["messages"] == 250


@pytest.mark.asyncio
async def test_new_post_projected_with_peer_and_edge(tmp_path):
    msg = _m(
        7, from_id={"_": "PeerUser", "user_id": 42},
        fwd_from={"_": "MessageFwdHeader", "from_id": {"_": "PeerChannel", "channel_id": 99}},
    )
    gw = FakeGateway({"get_messages": {7: msg}})
    outcomes: dict[str, str] = {}
    with Store.open(tmp_path / "p.sqlite") as st:
        await PostsCollector(outcomes=outcomes).collect(_posts_ctx(st, gw, [7]))
        assert st.conn.execute("select count(*) from messages").fetchone()[0] == 1
        assert st.conn.execute("select count(*) from peers").fetchone()[0] >= 1
        edge = st.conn.execute("select predicate from edges").fetchone()
        assert edge["predicate"] == "forwarded_from"
        ctx_json = st.conn.execute("select context_json from raw_records").fetchone()[0]
        assert json.loads(ctx_json)["method"] == "channels.getMessages"
    assert outcomes == {"tg:msg:5/7": "fetched"}


@pytest.mark.asyncio
async def test_refetch_edit_adds_revision_and_metric(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        first = FakeGateway({"get_messages": {3: _m(3)}})
        await PostsCollector().collect(_posts_ctx(st, first, [3]))
        edited = FakeGateway({"get_messages": {3: _m(3, message="edited", views=10)}})
        res = await PostsCollector().collect(_posts_ctx(st, edited, [3]))
        assert res.counts["revisions"] == 1
        assert st.conn.execute("select count(*) from message_revisions").fetchone()[0] == 2
        assert st.conn.execute("select count(*) from message_metrics").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_refetch_unchanged_photo_post_records_no_revision(tmp_path):
    """#96: Telegram re-issues `file_reference` on every fetch; not an edit."""
    def photo(ref):
        return {"_": "messageMediaPhoto",
                "photo": {"_": "Photo", "id": 9, "file_reference": ref}}

    with Store.open(tmp_path / "p.sqlite") as st:
        first = FakeGateway({"get_messages": {3: _m(3, media=photo("aa"))}})
        await PostsCollector().collect(_posts_ctx(st, first, [3]))
        again = FakeGateway({"get_messages": {3: _m(3, media=photo("bb"))}})
        res = await PostsCollector().collect(_posts_ctx(st, again, [3]))
        assert res.counts["revisions"] == 0
        assert st.conn.execute("select count(*) from message_revisions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_message_empty_is_tombstone_deleted_upstream(tmp_path):
    gw = FakeGateway({})  # an id missing from the table answers MessageEmpty
    outcomes: dict[str, str] = {}
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await PostsCollector(outcomes=outcomes).collect(_posts_ctx(st, gw, [9]))
        tomb = st.conn.execute("select message_uri, evidence from message_tombstones").fetchall()
        assert [(t["message_uri"], t["evidence"]) for t in tomb] == [("tg:msg:5/9", "empty")]
        assert st.conn.execute("select count(*) from messages").fetchone()[0] == 0
    assert res.counts["tombstones"] == 1
    assert outcomes == {"tg:msg:5/9": "deleted_upstream"}


@pytest.mark.asyncio
async def test_replay_unknown_placeholder_projects_nothing(tmp_path):
    gw = FakeGateway({"get_messages": {4: {"_": "ReplayUnknownMessage", "id": 4}}})
    outcomes: dict[str, str] = {}
    with Store.open(tmp_path / "p.sqlite") as st:
        await PostsCollector(outcomes=outcomes).collect(_posts_ctx(st, gw, [4]))
        assert st.conn.execute("select count(*) from raw_records").fetchone()[0] == 0
        assert st.conn.execute("select count(*) from message_tombstones").fetchone()[0] == 0
    assert outcomes == {}


@pytest.mark.asyncio
async def test_phase_stop_without_context(tmp_path):
    gw = FakeGateway({})
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _posts_ctx(st, gw, [1])
        ctx.input_channel = None
        with pytest.raises(PhaseStop, match="channel context not established"):
            await PostsCollector().collect(ctx)


@pytest.mark.asyncio
async def test_phase_stop_mid_batch_carries_counts(tmp_path):
    gw = FakeGateway({
        "get_messages": {i: _m(i) for i in range(1, 201)},
        "get_messages_errors": [None, PhaseStop("flood")],
    })
    outcomes: dict[str, str] = {}
    with Store.open(tmp_path / "p.sqlite") as st, pytest.raises(PhaseStop) as info:
        await PostsCollector(outcomes=outcomes).collect(_posts_ctx(st, gw, range(1, 201)))
    assert info.value.counts["messages"] == 100
    assert len(outcomes) == 100


@pytest.mark.asyncio
async def test_failed_posts_phase_withdraws_the_channel_context(tmp_path):
    """So the `media` phase after it stops at its own guard instead of acting."""
    from paperboy.budget import SkipAndRecord

    gw = FakeGateway({"get_messages_errors": [SkipAndRecord("channel is private")]})
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _posts_ctx(st, gw, [1])
        with pytest.raises(SkipAndRecord):
            await PostsCollector().collect(ctx)
        assert ctx.channel_id is None and ctx.input_channel is None
