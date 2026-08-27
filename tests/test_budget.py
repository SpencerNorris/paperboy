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


def test_expired_invite_hash_classifies_as_skip():
    # A t.me/+<hash> invite link can be dead by preview time (checkChatInvite).
    # It must SKIP that one preview, not abort the whole run.
    from telethon.errors import (
        InviteHashEmptyError,
        InviteHashExpiredError,
        InviteHashInvalidError,
    )

    for exc in (InviteHashExpiredError, InviteHashInvalidError, InviteHashEmptyError):
        assert classify(exc(None)) == Disposition.SKIP


def test_refused_join_classifies_as_skip():
    # A --join can be refused by a real gated group: an approval-gated group
    # returns INVITE_REQUEST_SENT, and a saturated account CHANNELS_TOO_MUCH.
    # These must SKIP that one join (a clean, distinguishable skip), not crash
    # the whole run with a raw Telethon error (issue #20 review).
    from telethon.errors import (
        ChannelsTooMuchError,
        InviteRequestSentError,
        UserBannedInChannelError,
        UserChannelsTooMuchError,
        UsersTooMuchError,
    )

    # Every "this specific group cannot be joined" outcome: request pending,
    # account in too many channels, group at member cap, account banned.
    for exc in (
        InviteRequestSentError, ChannelsTooMuchError, UserChannelsTooMuchError,
        UsersTooMuchError, UserBannedInChannelError,
    ):
        assert classify(exc(None)) == Disposition.SKIP


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
async def test_transient_network_error_retries_without_polluting_flood_log(tmp_path):
    # ConnectionError/TimeoutError/OSError classify as Disposition.RETRY too,
    # but they carry no `.seconds` — `getattr(exc, "seconds", 0)` is 0, so a
    # retry here must not write a spurious flood_log row (until=now,
    # seconds=0); only genuine FloodWait retries belong in flood_log.
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(s, st, sleeper=lambda x: None)

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return "ok"

        result = await b.call("messages.getHistory", flaky)
        assert result == "ok"
        rows = st.conn.execute("select * from flood_log").fetchall()
        assert rows == []


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


def test_user_id_invalid_and_channel_invalid_classify_as_skip():
    # `users.getFullUser` on a stale `inputUserFromMessage` provenance answers
    # USER_ID_INVALID / CHANNEL_INVALID (research Part 2 §1): one user's
    # enrichment is skipped, the sweep continues — never a raw crash.
    from telethon.errors import ChannelInvalidError, UserIdInvalidError

    for exc in (UserIdInvalidError, ChannelInvalidError):
        assert classify(exc(None)) == Disposition.SKIP


@pytest.mark.asyncio
async def test_per_method_interval_paces_only_that_method(tmp_path):
    class Clock:
        t = 1000.0

        def time(self):
            return self.t

    clock = Clock()
    slept: list[float] = []
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        b = Budget(
            s, st, clock=clock, sleeper=lambda x: slept.append(x), min_interval=1.0,
            method_intervals={"users.getFullUser": 2.5},
        )

        async def ok():
            return 1

        await b.call("users.getFullUser", ok)
        clock.t += 0.5
        await b.call("users.getFullUser", ok)  # 0.5s since last -> sleep 2.0 (the METHOD interval)
        await b.call("messages.getHistory", ok)  # first call of that method: no sleep
        clock.t += 0.2
        await b.call("messages.getHistory", ok)  # default 1.0 interval -> sleep 0.8
        assert slept == [2.0, pytest.approx(0.8)]


@pytest.mark.asyncio
async def test_per_method_interval_composes_with_flood_handling(tmp_path):
    # `--profile-interval` never bypasses flood handling: a short FLOOD_WAIT on
    # a paced method is still recorded and slept, then retried once.
    s = load_settings("default", {})
    with Store.open(tmp_path / "p.sqlite") as st:
        slept: list[float] = []
        b = Budget(
            s, st, sleeper=lambda x: slept.append(x),
            method_intervals={"users.getFullUser": 2.0},
        )
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FakeFlood(3)
            return "ok"

        assert await b.call("users.getFullUser", flaky) == "ok"
        assert 3 in slept
        assert st.conn.execute("select count(*) from flood_log").fetchone()[0] == 1
