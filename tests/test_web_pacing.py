"""The web collector's inter-request pause scales with `pacing_factor` (#69)."""

import logging
from pathlib import Path

import pytest

from paperboy.collectors.base import CollectContext
from paperboy.collectors.web import WebCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_collector_web import _handler_factory, _mock_client

FX = Path("tests/fixtures/web")


async def _slept(tmp_path, overrides, **collector_kwargs):
    slept: list[float] = []
    client = _mock_client(_handler_factory((FX / "tme_durov_page1.html").read_text(), "[]", []))
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            FakeGateway({}), st, load_settings("default", overrides), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        collector = WebCollector(client=client, sleep=slept.append, **collector_kwargs)
        await collector.collect(ctx)
    return slept


@pytest.mark.asyncio
async def test_web_collector_interval_scales_with_pacing_factor(tmp_path):
    assert set(await _slept(tmp_path, {})) == {2.0}
    (tmp_path / "b").mkdir()
    assert set(await _slept(tmp_path / "b", {"pacing_factor": 1.0})) == {1.0}
    (tmp_path / "c").mkdir()
    assert set(await _slept(tmp_path / "c", {}, min_interval=0.0)) == {0.0}
