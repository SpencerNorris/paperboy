import json
import logging
from pathlib import Path

import pytest

from paperboy.budget import HardStop, PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.config import load_settings
from paperboy.recipes import _default_collectors, collect_channel
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/tl")


def _fixtures():
    return {
        "resolve": json.loads((FX / "resolve_durov.json").read_text()),
        "full_channel": json.loads((FX / "full_channel.json").read_text()),
        "self": {"_": "user", "id": 1, "self": True},
        "history": [
            {
                "_": "message", "id": 2, "message": "", "date": 1767322445,
                "media": {
                    "_": "MessageMediaDocument",
                    "document": {
                        "_": "Document", "id": 1, "access_hash": 1, "mime_type": "text/plain",
                        "attributes": [{"_": "DocumentAttributeFilename", "file_name": "a.txt"}],
                    },
                },
            },
            {"_": "message", "id": 1, "message": "m1", "date": 1767322445},
        ],
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 42},
        "media": {2: b"file contents"},
    }


@pytest.mark.asyncio
async def test_collect_channel_runs_channel_then_history(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel", "history"], log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == ["channel", "history"]
        assert st.conn.execute("select count(*) as n from channels").fetchone()["n"] == 1
        assert st.conn.execute("select count(*) as n from messages").fetchone()["n"] == 2


@pytest.mark.asyncio
async def test_collect_channel_media_is_opt_out_by_default(tmp_path):
    # `media` is opt-in (spec §6): a plain run with no `media=True` and no
    # `--phases media` must never touch the network for downloads, even
    # though `history` populated a message with downloadable media above.
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path}), parse_target("@durov"),
            phases=["channel", "history"], log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == ["channel", "history"]
        assert gw.download_media_calls == []
        assert st.conn.execute("select count(*) as n from media").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_collect_channel_media_flag_opts_in(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path}), parse_target("@durov"),
            phases=None, log=logging.getLogger("t"), media=True, profile="mediarecipe",
        )
        # graph is in the default set now (opt-in media appends after it)
        assert [r.name for r in results] == ["channel", "history", "discussion", "graph", "media"]
        media_result = next(r for r in results if r.name == "media")
        assert media_result.counts["downloaded"] == 1
        assert st.conn.execute("select count(*) as n from media").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_collect_channel_phases_media_opts_in_without_flag(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path}), parse_target("@durov"),
            phases=["channel", "history", "media"], log=logging.getLogger("t"),
            profile="mediarecipe2",
        )
        assert [r.name for r in results] == ["channel", "history", "media"]
        assert st.conn.execute("select count(*) as n from media").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_collect_channel_honors_phases_filter(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel"], log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == ["channel"]
        assert st.conn.execute("select count(*) as n from messages").fetchone()["n"] == 0


def test_default_collectors_web_is_opt_in():
    # `web` is pure HTTP to an external host (t.me/archive.org) — a different
    # trust boundary than the MTProto session — so like `media` it is OPT-IN,
    # not in the default set. Running it end to end needs a mocked WebClient
    # (see tests/test_collector_web.py), not a real network call.
    assert [c.name for c in _default_collectors(include_media=False, include_web=False)] == [
        "channel", "history", "discussion", "graph",
    ]
    assert [c.name for c in _default_collectors(include_media=False, include_web=True)] == [
        "channel", "history", "discussion", "graph", "web",
    ]


def test_discussion_runs_by_default_immediately_after_history():
    """Design spec §10: `channel -> history -> discussion -> participants ->
    profiles -> media -> graph -> web`. Nothing previously pinned
    `discussion`'s position in `_default_collectors` — a registration
    anywhere else in the list (e.g. after `graph`, or before `history`)
    would still pass every other test, including
    `tests/test_integration_discussion.py`, which bypasses
    `_default_collectors` entirely via an explicit `collectors=` override.
    Expected to fail with `ValueError` (`"discussion"` not yet in the list)
    until plan Task 4 registers `DiscussionCollector`."""
    names = [c.name for c in _default_collectors(include_media=False, include_web=False)]
    assert names.index("discussion") == names.index("history") + 1


class _StubCollector:
    def __init__(self, name, exc=None):
        self.name = name
        self._exc = exc

    def applies_to(self, target):
        return True

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if self._exc:
            raise self._exc
        return CollectResult(name=self.name, counts={"ok": 1})


@pytest.mark.asyncio
async def test_hard_stop_aborts_remaining_phases(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        collectors = [
            _StubCollector("a"),
            _StubCollector("b", exc=HardStop("boom")),
            _StubCollector("c"),
        ]
        results = await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["a", "b", "c"], log=logging.getLogger("t"), collectors=collectors,
        )
        assert [r.name for r in results] == ["a", "b"]
        assert results[-1].stopped == "hard_stop"
        events = st.conn.execute("select kind from run_events order by id").fetchall()
        assert [e["kind"] for e in events] == ["complete", "hard_stop"]


def _diff_page(pts, *, final, mid):
    return {
        "_": "updates.channelDifference", "final": final, "pts": pts,
        "new_messages": [
            {"_": "message", "id": mid, "message": f"c{mid}", "date": 1767322445,
             "peer_id": {"channel_id": 5}}
        ],
        "other_updates": [], "chats": [], "users": [],
    }


@pytest.mark.asyncio
async def test_history_phase_stop_reports_backfill_plus_catchup_counts(tmp_path):
    # A catch-up budget stop must not erase the backfill that already completed:
    # _run_one folds the backfill counts into catch_up's PhaseStop, so the
    # history phase reports every message it stored (2a40754), not just the
    # catch-up's partial page.
    fx = _fixtures()
    fx["channel_difference"] = [
        _diff_page(50, final=False, mid=10),
        _diff_page(60, final=True, mid=11),
    ]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"catchup_page_budget": 1}),
            parse_target("@durov"), phases=["channel", "history"],
            log=logging.getLogger("t"),
        )
        history = next(r for r in results if r.name == "history")
        assert history.stopped == "phase_stop"
        # 2 backfilled (fixture history) + 1 catch-up page applied before the stop
        assert history.counts["messages"] == 3
        assert st.conn.execute("select count(*) as n from messages").fetchone()["n"] == 3
        event = st.conn.execute(
            "select detail_json from run_events where phase='history' and kind='phase_stop'"
        ).fetchone()
        assert event is not None and '"messages": 3' in event["detail_json"]


@pytest.mark.asyncio
async def test_phase_stop_continues_to_next_phase(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        collectors = [
            _StubCollector("a", exc=PhaseStop("flood")),
            _StubCollector("b"),
        ]
        results = await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["a", "b"], log=logging.getLogger("t"), collectors=collectors,
        )
        assert [r.name for r in results] == ["a", "b"]
        assert results[0].stopped == "phase_stop"
        assert results[1].stopped is None


@pytest.mark.asyncio
async def test_skip_and_record_continues_to_next_phase(tmp_path):
    # SkipAndRecord (Budget's Disposition.SKIP: e.g. ChannelPrivateError,
    # ChatAdminRequiredError) means "skip this RPC/collector, the run
    # continues" — it must never abort the whole run (spec §8).
    with Store.open(tmp_path / "p.sqlite") as st:
        collectors = [
            _StubCollector("a", exc=SkipAndRecord("Channel is private")),
            _StubCollector("b"),
        ]
        results = await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["a", "b"], log=logging.getLogger("t"), collectors=collectors,
        )
        assert [r.name for r in results] == ["a", "b"]
        assert results[0].stopped == "skip"
        assert results[1].stopped is None
        events = st.conn.execute("select kind from run_events order by id").fetchall()
        assert [e["kind"] for e in events] == ["skip", "complete"]
