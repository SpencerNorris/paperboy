"""Conservative pacing and patient flood handling (ADR-0003 amendment, #69).

All timing is on a fake clock with a recording sleeper: nothing here blocks or
touches the network.
"""

import pytest

from paperboy.budget import Budget
from paperboy.config import load_settings
from paperboy.store.db import Store


class _Clock:
    """Fake clock: only advances when a test moves it."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


async def _two_calls_slept(tmp_path, method, overrides=None, **budget_kwargs):
    """Call `method` twice back-to-back on a frozen clock; return what was slept."""
    slept: list[float] = []
    s = load_settings("default", overrides or {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, clock=_Clock(), sleeper=lambda x: slept.append(x), **budget_kwargs)

        async def ok():
            return 1

        await b.call(method, ok)
        await b.call(method, ok)
    return slept


@pytest.mark.asyncio
async def test_default_factor_doubles_the_default_interval(tmp_path):
    assert await _two_calls_slept(tmp_path, "messages.getHistory") == [2.0]


@pytest.mark.asyncio
async def test_resolve_username_base_is_five_seconds_scaled(tmp_path):
    assert await _two_calls_slept(tmp_path, "contacts.resolveUsername") == [10.0]


@pytest.mark.asyncio
async def test_factor_one_reproduces_previous_intervals(tmp_path):
    one = {"pacing_factor": 1.0}
    assert await _two_calls_slept(tmp_path, "messages.getHistory", one) == [1.0]
    (tmp_path / "b").mkdir()
    assert await _two_calls_slept(tmp_path / "b", "contacts.resolveUsername", one) == [5.0]


@pytest.mark.asyncio
async def test_method_intervals_are_scaled_too(tmp_path):
    slept = await _two_calls_slept(
        tmp_path, "users.getFullUser", method_intervals={"users.getFullUser": 2.5}
    )
    assert slept == [5.0]


def test_describe_pacing_lists_factor_and_non_default_methods(tmp_path):
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, method_intervals={"users.getFullUser": 2.5})
        line = b.describe_pacing()
        assert "factor=2.0" in line
        assert "default=2.0s" in line
        assert "contacts.resolveUsername=10.0s" in line
        assert "users.getFullUser=5.0s" in line
        assert b.effective_interval("anything.else") == 2.0
