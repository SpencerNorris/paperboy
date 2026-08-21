import pytest

from paperboy.budget import Budget, HardStop, PhaseStop, SkipAndRecord
from paperboy.config import load_settings
from paperboy.errors import Disposition, FakeFlood, FakePeerFlood, classify
from paperboy.store.db import Store


def test_flood_short_is_retryable():
    assert classify(FakeFlood(3)) == Disposition.RETRY


def test_flood_long_is_phase_stop():
    assert classify(FakeFlood(120), threshold=60) == Disposition.PHASE_STOP


def test_peer_flood_is_hard_stop():
    assert classify(FakePeerFlood()) == Disposition.HARD_STOP


def test_unrecognized_exception_is_reraised():
    class Weird(Exception):
        pass

    with pytest.raises(Weird):
        classify(Weird("boom"))


@pytest.mark.asyncio
async def test_rpc_cap(tmp_path):
    s = load_settings("default", {"max_rpc_per_run": 2})
    with Store.open(tmp_path / "p.sqlite") as st:
        slept = []
        b = Budget(s, st, sleeper=lambda x: slept.append(x))

        async def ok():
            return 1

        await b.call("m", ok)
        await b.call("m", ok)
        with pytest.raises(HardStop):
            await b.call("m", ok)


@pytest.mark.asyncio
async def test_short_flood_wait_sleeps_and_retries(tmp_path):
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        slept = []
        b = Budget(s, st, sleeper=lambda x: slept.append(x))

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeFlood(3)
            return "ok"

        result = await b.call("messages.getHistory", flaky)
        assert result == "ok"
        assert slept == [3]
        row = st.conn.execute("select method, seconds from flood_log").fetchone()
        assert row["method"] == "messages.getHistory"
        assert row["seconds"] == 3


@pytest.mark.asyncio
async def test_long_flood_wait_raises_phase_stop_and_persists_cooldown(tmp_path):
    s = load_settings("default", {"flood_sleep_threshold": 10})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, sleeper=lambda x: None)

        async def boom():
            raise FakeFlood(999)

        with pytest.raises(PhaseStop):
            await b.call("messages.getHistory", boom)
        row = st.conn.execute("select seconds from flood_log").fetchone()
        assert row["seconds"] == 999


@pytest.mark.asyncio
async def test_admin_required_is_skip_and_record(tmp_path):
    from telethon.errors import ChatAdminRequiredError

    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, sleeper=lambda x: None)

        async def boom():
            raise ChatAdminRequiredError(None)

        with pytest.raises(SkipAndRecord):
            await b.call("channels.getParticipants", boom)


@pytest.mark.asyncio
async def test_min_interval_paces_repeat_calls_to_same_method(tmp_path):
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        slept = []
        clock_time = [1000.0]

        class FakeClock:
            def time(self):
                return clock_time[0]

        b = Budget(s, st, clock=FakeClock(), sleeper=lambda x: slept.append(x))

        async def ok():
            return 1

        await b.call("m", ok)
        # No time has passed -> the second call to the same method must wait
        # out the per-method minimum interval before proceeding.
        await b.call("m", ok)
        assert slept and slept[0] > 0
