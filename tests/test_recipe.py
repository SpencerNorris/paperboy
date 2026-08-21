import json
import logging
from pathlib import Path

import pytest

from paperboy.budget import HardStop, PhaseStop
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
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
            {"_": "message", "id": i, "message": f"m{i}", "date": 1767322445} for i in (2, 1)
        ],
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 42},
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
async def test_collect_channel_honors_phases_filter(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel"], log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == ["channel"]
        assert st.conn.execute("select count(*) as n from messages").fetchone()["n"] == 0


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
