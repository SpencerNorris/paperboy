"""`TelethonGateway`'s CHANNEL_INVALID scoping (errors.py review finding):
`classify` has no per-method scope, so the profiles-only skip must be caught
locally by the two gateway methods that need it (`get_users`/`get_full_user`)
rather than globally in `classify` — verified end to end through a real
`Budget` with a fake Telethon client, never touching the network."""

from __future__ import annotations

from typing import Any, cast

import pytest
from telethon.errors import ChannelInvalidError

from paperboy.budget import Budget, SkipAndRecord
from paperboy.config import load_settings
from paperboy.gateway import TelethonGateway
from paperboy.store.db import Store


class _RaisingClient:
    """A fake Telethon `TelegramClient`: `__call__` raises the given
    exception instead of making a request — no network involved."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, _request: object) -> None:
        raise self._exc


def _gateway(tmp_path, exc: Exception) -> tuple[TelethonGateway, Store]:
    store = Store.open(tmp_path / "p.sqlite")
    settings = load_settings("default", {})
    budget = Budget(settings, store)
    # `TelethonGateway` only ever calls `self.client(request)` — it needs no
    # other `TelegramClient` surface, so the fake is cast rather than made to
    # subclass the real (heavyweight, network-capable) client.
    return TelethonGateway(cast(Any, _RaisingClient(exc)), budget), store


@pytest.mark.asyncio
async def test_get_users_turns_channel_invalid_into_skip_and_record(tmp_path):
    gw, store = _gateway(tmp_path, ChannelInvalidError(None))
    with store, pytest.raises(SkipAndRecord):
        await gw.get_users([{"user_id": 5, "access_hash": 99}])


@pytest.mark.asyncio
async def test_get_full_user_turns_channel_invalid_into_skip_and_record(tmp_path):
    gw, store = _gateway(tmp_path, ChannelInvalidError(None))
    with store, pytest.raises(SkipAndRecord):
        await gw.get_full_user({"user_id": 5, "access_hash": 99})


@pytest.mark.asyncio
async def test_get_full_channel_turns_channel_invalid_into_skip_and_record(tmp_path):
    # #84: a stale saved key / from-message reference answers CHANNEL_INVALID on
    # getFullChannel. That is an access refusal the channel collector falls back
    # from, so it is a SkipAndRecord (scoped here, not in `classify`); the
    # original error stays on `__cause__` so the receipt can name it.
    gw, store = _gateway(tmp_path, ChannelInvalidError(None))
    with store, pytest.raises(SkipAndRecord) as exc:
        await gw.get_full_channel({"channel_id": 7, "access_hash": 4242})
    assert isinstance(exc.value.__cause__, ChannelInvalidError)
