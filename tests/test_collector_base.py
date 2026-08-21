import logging

from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.config import load_settings
from paperboy.targets import parse_target
from tests.fakes import FakeGateway


class DummyCollector:
    name = "dummy"

    def applies_to(self, target):
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        ctx.channel_id = 7
        return CollectResult(name=self.name, counts={"things": 1})


async def test_dummy_collector_runs_against_a_context(tmp_path):
    from paperboy.store.db import Store

    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gateway=FakeGateway({}),
            store=st,
            settings=load_settings("default", {}),
            target=parse_target("@durov"),
            input_channel=None,
            channel_id=None,
            tier="stranger",
            log=logging.getLogger("t"),
        )
        collector = DummyCollector()
        assert collector.applies_to(ctx.target)
        result = await collector.collect(ctx)
        assert result.name == "dummy"
        assert result.counts == {"things": 1}
        assert result.stopped is None
        assert ctx.channel_id == 7


def test_collect_result_defaults():
    r = CollectResult(name="x")
    assert r.counts == {}
    assert r.stopped is None
