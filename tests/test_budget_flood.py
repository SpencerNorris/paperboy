"""FLOOD_WAIT margin, patience and retries in `Budget.call` (ADR-0003 amendment, #69)."""

import logging

import pytest

from paperboy.budget import Budget, HardStop, PhaseStop, SkipAndRecord, flood_applied_seconds
from paperboy.config import load_settings
from paperboy.errors import FakeFlood
from paperboy.store.db import Store


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


def _budget(tmp_path, overrides=None, clock=None):
    """(Budget, Store, slept) with a recording sleeper; caller closes the store."""
    slept: list[float] = []
    st = Store.open(tmp_path / "p.sqlite")
    b = Budget(
        load_settings("default", overrides or {}),
        st,
        clock=clock or _Clock(),
        sleeper=lambda x: slept.append(x),
    )
    return b, st, slept


class _flaky:  # noqa: N801 - reads as a factory function at call sites
    """A factory that raises each of `errors` in turn, then returns `result`."""

    def __init__(self, *errors, result="ok"):
        self.errors = errors
        self.result = result
        self.n = 0

    async def __call__(self):
        i = self.n
        self.n += 1
        if i < len(self.errors):
            raise self.errors[i]
        return self.result


@pytest.mark.parametrize(
    ("seconds", "applied"), [(20, 27), (3, 9), (0, 5), (1800, 1985), (7200, 7925)]
)
def test_flood_applied_seconds_is_ceil_ten_percent_plus_five(seconds, applied):
    assert flood_applied_seconds(seconds) == applied


@pytest.mark.asyncio
async def test_flood_wait_20_sleeps_27_and_records_both(tmp_path):
    clock = _Clock()
    b, st, slept = _budget(tmp_path, clock=clock)
    with st:
        assert await b.call("m", _flaky(FakeFlood(20))) == "ok"
        assert slept == [27]
        row = st.conn.execute("select seconds, applied_seconds, until from flood_log").fetchone()
        assert (row["seconds"], row["applied_seconds"]) == (20, 27)
        assert row["until"].startswith("1970-01-01T00:17:07")  # 1000 + 27 s


@pytest.mark.asyncio
async def test_long_flood_under_ceiling_is_slept_through_with_heartbeat(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="paperboy.budget")
    b, st, slept = _budget(tmp_path)
    with st:
        assert await b.call("channels.getMessages", _flaky(FakeFlood(1800))) == "ok"
        assert sum(slept) == 1985
        assert max(slept) <= 60
        beats = [r for r in caplog.records if r.levelno == logging.INFO and "flood" in r.message]
        assert len(beats) >= 30
        assert "channels.getMessages" in beats[0].message
        assert "remaining" in beats[0].message and "total" in beats[0].message
        row = st.conn.execute("select seconds, applied_seconds from flood_log").fetchone()
        assert (row["seconds"], row["applied_seconds"]) == (1800, 1985)


@pytest.mark.asyncio
async def test_short_flood_has_no_heartbeat(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="paperboy.budget")
    b, st, slept = _budget(tmp_path)
    with st:
        await b.call("m", _flaky(FakeFlood(3)))
        assert slept == [9]
        assert not [r for r in caplog.records if r.levelno == logging.INFO]


@pytest.mark.asyncio
async def test_exact_multiple_of_sixty_emits_one_heartbeat(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="paperboy.budget")
    # applied = ceil(100*1.1)+5 = 115; use seconds so applied == 120: 105 -> 116+... compute
    seconds = next(s for s in range(200) if flood_applied_seconds(s) == 120)
    b, st, slept = _budget(tmp_path)
    with st:
        await b.call("m", _flaky(FakeFlood(seconds)))
        assert slept == [60, 60]
        assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1


@pytest.mark.asyncio
async def test_flood_over_ceiling_is_phase_stop_with_applied_cooldown(tmp_path):
    b, st, slept = _budget(tmp_path)
    with st:
        with pytest.raises(PhaseStop):
            await b.call("m", _flaky(FakeFlood(7200)))
        assert slept == []
        row = st.conn.execute("select seconds, applied_seconds, until from flood_log").fetchone()
        assert (row["seconds"], row["applied_seconds"]) == (7200, 7925)
        assert row["until"].startswith("1970-01-01T02:28:45")  # 1000 + 7925 s


@pytest.mark.asyncio
async def test_ceiling_is_compared_against_applied_not_raw(tmp_path):
    b, st, slept = _budget(tmp_path, {"flood_sleep_threshold": 60})
    with st, pytest.raises(PhaseStop):
        await b.call("m", _flaky(FakeFlood(55)))  # applied 66 > 60
    (tmp_path / "b").mkdir()
    b, st, slept = _budget(tmp_path / "b", {"flood_sleep_threshold": 60})
    with st:
        assert await b.call("m", _flaky(FakeFlood(50))) == "ok"  # applied 60 <= 60
        assert sum(slept) == 60


@pytest.mark.asyncio
async def test_ceiling_zero_stops_every_flood_and_persists_cooldown(tmp_path):
    b, st, _ = _budget(tmp_path, {"flood_sleep_threshold": 0})
    with st:
        with pytest.raises(PhaseStop):
            await b.call("m", _flaky(FakeFlood(0)))
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_three_consecutive_floods_then_success(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="paperboy.budget")
    b, st, _ = _budget(tmp_path)
    factory = _flaky(FakeFlood(1), FakeFlood(2), FakeFlood(3))
    with st:
        assert await b.call("m", factory) == "ok"
        assert factory.n == 4
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 3
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    for i, msg in enumerate(warnings, start=1):
        assert f"attempt {i}" in msg


@pytest.mark.asyncio
async def test_four_consecutive_floods_is_phase_stop(tmp_path):
    b, st, _ = _budget(tmp_path)
    with st:
        with pytest.raises(PhaseStop):
            await b.call("m", _flaky(*[FakeFlood(1)] * 4))
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(("factor", "expected"), [(2.0, [10, 20, 40]), (1.0, [5, 10, 20])])
async def test_transient_errors_back_off_5_10_20_times_factor(tmp_path, factor, expected):
    b, st, slept = _budget(tmp_path, {"pacing_factor": factor})
    with st:
        assert await b.call("m", _flaky(*[ConnectionError("x")] * 3)) == "ok"
        assert slept == expected
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_fourth_transient_error_is_phase_stop(tmp_path):
    b, st, _ = _budget(tmp_path)
    err = ConnectionError("reset")
    with st:
        with pytest.raises(PhaseStop) as info:
            await b.call("m", _flaky(err, err, err, err))
        assert info.value.__cause__ is err


@pytest.mark.asyncio
async def test_mixed_flood_and_transient_counters_are_independent(tmp_path):
    b, st, _ = _budget(tmp_path)
    factory = _flaky(FakeFlood(1), ConnectionError("x"), FakeFlood(1))
    with st:
        assert await b.call("m", factory) == "ok"


@pytest.mark.asyncio
async def test_retry_reinvokes_the_factory_fresh_each_attempt(tmp_path):
    b, st, _ = _budget(tmp_path)
    made: list[int] = []

    def factory():
        made.append(len(made))

        async def attempt():
            if len(made) < 3:
                raise FakeFlood(1)
            return "ok"

        return attempt()

    with st:
        assert await b.call("m", factory) == "ok"
    assert made == [0, 1, 2]


@pytest.mark.asyncio
async def test_every_attempt_counts_toward_max_rpc(tmp_path):
    b, st, _ = _budget(tmp_path, {"max_rpc_per_run": 3})
    with st, pytest.raises(HardStop):
        await b.call("m", _flaky(*[FakeFlood(1)] * 4))


@pytest.mark.asyncio
async def test_hard_stop_and_skip_are_not_retried(tmp_path):
    from telethon.errors import ChatAdminRequiredError

    from paperboy.errors import FakePeerFlood

    b, st, _ = _budget(tmp_path)
    with st:
        f1 = _flaky(FakePeerFlood())
        with pytest.raises(HardStop):
            await b.call("m", f1)
        assert f1.n == 1
        f2 = _flaky(ChatAdminRequiredError(None))
        with pytest.raises(SkipAndRecord):
            await b.call("m2", f2)
        assert f2.n == 1
        # ... and on attempt >= 2 they propagate immediately too.
        f3 = _flaky(FakeFlood(1), FakePeerFlood())
        with pytest.raises(HardStop):
            await b.call("m3", f3)
        assert f3.n == 2


@pytest.mark.asyncio
async def test_pre_69_null_applied_row_does_not_break_cooldown(tmp_path):
    b, st, slept = _budget(tmp_path)
    with st:
        st.conn.execute(
            "insert into flood_log(method, until, seconds, recorded_at) "
            "values ('m', '1970-01-01T00:17:30+00:00', 30, '1970-01-01T00:16:40+00:00')"
        )
        assert await b.call("m", _flaky()) == "ok"
        assert slept == [50]  # clock 1000 s = 00:16:40; cooldown ends 00:17:30
