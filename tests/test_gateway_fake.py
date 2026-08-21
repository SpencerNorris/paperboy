import pytest

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
