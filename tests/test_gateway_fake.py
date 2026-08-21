import pytest

from paperboy.budget import SkipAndRecord
from tests.fakes import FakeGateway


@pytest.mark.asyncio
async def test_fake_history_pages():
    fx = {
        "history": [
            {"_": "message", "id": i, "message": f"m{i}", "date": 1767322445} for i in (3, 2, 1)
        ]
    }
    gw = FakeGateway(fx)
    ids = [m["id"] async for m in gw.iter_history({"channel_id": 7}, offset_id=0, limit=100)]
    assert ids == [3, 2, 1]


@pytest.mark.asyncio
async def test_fake_history_resumes_from_offset_id():
    fx = {"history": [{"_": "message", "id": i, "date": 1767322445} for i in (5, 4, 3, 2, 1)]}
    gw = FakeGateway(fx)
    ids = [m["id"] async for m in gw.iter_history({"channel_id": 7}, offset_id=3, limit=100)]
    assert ids == [2, 1]


@pytest.mark.asyncio
async def test_fake_history_respects_limit():
    fx = {"history": [{"_": "message", "id": i, "date": 1767322445} for i in (5, 4, 3, 2, 1)]}
    gw = FakeGateway(fx)
    ids = [m["id"] async for m in gw.iter_history({"channel_id": 7}, offset_id=0, limit=2)]
    assert ids == [5, 4]


@pytest.mark.asyncio
async def test_fake_get_messages_fills_unknown_ids_with_message_empty():
    gw = FakeGateway({"get_messages": {3: {"_": "MessageEmpty", "id": 3}}})
    result = await gw.get_messages({"channel_id": 7}, [3, 4])
    assert result[0] == {"_": "MessageEmpty", "id": 3}
    assert result[1] == {"_": "MessageEmpty", "id": 4}


@pytest.mark.asyncio
async def test_fake_resolve_and_full_channel_and_self():
    fx = {
        "resolve": {"chats": [{"_": "channel", "id": 5}], "users": []},
        "full_channel": {"full_chat": {"_": "channelFull", "id": 5}},
        "self": {"_": "user", "id": 1, "self": True},
    }
    gw = FakeGateway(fx)
    assert await gw.resolve("durov") == fx["resolve"]
    assert await gw.get_full_channel({"channel_id": 5}) == fx["full_channel"]
    assert await gw.get_self() == fx["self"]


@pytest.mark.asyncio
async def test_fake_channel_difference():
    diff = {"_": "updates.channelDifference", "pts": 50}
    gw = FakeGateway({"channel_difference": diff})
    assert await gw.get_channel_difference({"channel_id": 5}, pts=40, limit=100) == diff


@pytest.mark.asyncio
async def test_fake_download_media_returns_fixture_bytes_by_msg_id():
    gw = FakeGateway({"media": {7: b"file bytes"}})
    assert await gw.download_media({"channel_id": 5}, {"id": 7}) == b"file bytes"
    assert await gw.download_media({"channel_id": 5}, {"id": 8}) is None
    assert gw.download_media_calls == [7, 8]

async def test_fake_channel_recommendations():
    fx = {"channel_recommendations": {"_": "messages.ChatsSlice", "count": 5, "chats": []}}
    gw = FakeGateway(fx)
    assert await gw.get_channel_recommendations({"channel_id": 5}) == fx["channel_recommendations"]


@pytest.mark.asyncio
async def test_fake_channel_recommendations_raises_configured_skip():
    gw = FakeGateway({"channel_recommendations": SkipAndRecord("CHAT_NOT_MODIFIED")})
    with pytest.raises(SkipAndRecord):
        await gw.get_channel_recommendations({"channel_id": 5})


@pytest.mark.asyncio
async def test_fake_check_chat_invite_keyed_by_hash():
    preview = {"_": "ChatInvite", "title": "X", "participants_count": 3}
    gw = FakeGateway({"chat_invite": {"abc123": preview}})
    assert await gw.check_chat_invite("abc123") == preview


@pytest.mark.asyncio
async def test_fake_sponsored_messages():
    fx = {"sponsored_messages": {"_": "messages.SponsoredMessagesEmpty"}}
    gw = FakeGateway(fx)
    assert await gw.get_sponsored_messages({"channel_id": 5}) == fx["sponsored_messages"]


@pytest.mark.asyncio
async def test_calls_log_records_every_protocol_method():
    """Every zero-RPC / read-only-RPC assertion elsewhere in the suite
    (`test_the_backfill_issues_no_gateway_calls`,
    `test_discussion_run_issues_only_read_rpcs`) depends on `FakeGateway.calls`
    actually recording what happened. If some method's `self.calls.append(...)`
    is missing, those assertions pass vacuously — 'no RPCs happened' with no
    mechanism capable of detecting one. This calls every `Gateway` Protocol
    method once and pins that each one left its name in `gw.calls`, so a
    missing instrumentation line fails loudly here instead of silently
    weakening every test that trusts the log elsewhere."""
    fx = {
        "resolve": {"chats": [], "users": []},
        "full_channel": {"full_chat": {"_": "channelFull", "id": 5}, "chats": [], "users": []},
        "self": {"_": "user", "id": 1, "self": True},
        "history": [{"_": "message", "id": 1, "date": 1767322445}],
        "get_messages": {},
        "channel_difference": {"_": "updates.channelDifferenceEmpty"},
        "authorizations": {"authorizations": []},
        "password_state": {"has_password": False},
        "privacy": {"phone": {"rules": []}},
        "media": {},
        "channel_recommendations": {"_": "messages.chats", "chats": []},
        "chat_invite": {},
        "sponsored_messages": {"_": "messages.sponsoredMessagesEmpty"},
    }
    gw = FakeGateway(fx)
    ic = {"channel_id": 5, "access_hash": 1}

    await gw.resolve("x")
    await gw.get_full_channel(ic)
    await gw.get_self()
    async for _ in gw.iter_history(ic, offset_id=0, limit=10):
        pass
    await gw.get_messages(ic, [1])
    await gw.get_channel_difference(ic, pts=1, limit=10)
    await gw.get_authorizations()
    await gw.get_password_state()
    await gw.get_privacy("phone")
    await gw.download_media(ic, {"id": 1})
    await gw.get_channel_recommendations(ic)
    await gw.check_chat_invite("abc")
    await gw.get_sponsored_messages(ic)

    expected = {
        "resolve", "get_full_channel", "get_self", "iter_history", "get_messages",
        "get_channel_difference", "get_authorizations", "get_password_state",
        "get_privacy", "download_media", "get_channel_recommendations",
        "check_chat_invite", "get_sponsored_messages",
    }
    assert expected <= set(gw.calls)
    assert len(gw.calls) == len(expected)  # one call each above, no duplicates
