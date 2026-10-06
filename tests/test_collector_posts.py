"""`PostsCollector` (#91): fetch listed message ids by `channels.getMessages`
and project them through the same projection `history` uses."""

import json
import logging

from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import observe_message
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
