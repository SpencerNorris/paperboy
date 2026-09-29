"""`TelethonGateway.download_media` streams into a `MediaSink` through a real
`Budget` with a scripted fake Telethon client (#64): retries start from a
fresh sink, size overruns are never retried, no network involved."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telethon.errors import FileReferenceExpiredError

from paperboy.budget import Budget, SkipAndRecord
from paperboy.config import load_settings
from paperboy.gateway import TelethonGateway
from paperboy.media_sink import MediaSink, MediaSizeExceeded
from paperboy.store.db import Store

IC = {"channel_id": 5, "access_hash": 1}
FULL = b"0123456789abcdef"


class _Step:
    """One scripted `download_media` attempt: write `chunks`, then raise
    `exc` (if given), else return `result`."""

    def __init__(self, chunks: list[bytes], exc: Exception | None = None, result: Any = "ok"):
        self.chunks, self.exc, self.result = chunks, exc, result


class _FakeClient:
    def __init__(self, steps: list[_Step], *, messages: list[object] | None = None) -> None:
        self.steps = steps
        self.messages = [object()] if messages is None else messages
        self.get_messages_calls = 0
        self.download_calls = 0

    async def __call__(self, _request: object) -> SimpleNamespace:
        self.get_messages_calls += 1
        return SimpleNamespace(messages=self.messages)

    async def download_media(self, _msg: object, file: Any = None) -> Any:
        step = self.steps[self.download_calls]
        self.download_calls += 1
        for chunk in step.chunks:
            file.write(chunk)
        if step.exc is not None:
            raise step.exc
        return step.result


def _gateway(tmp_path: Path, client: _FakeClient) -> TelethonGateway:
    store = Store.open(tmp_path / "p.sqlite")
    settings = load_settings("default", {"flood_sleep_threshold": 3600})
    budget = Budget(settings, store, sleeper=lambda s: None, min_interval=0)
    return TelethonGateway(cast(Any, client), budget)


@pytest.mark.asyncio
async def test_transient_error_retries_with_a_fresh_sink(tmp_path: Path) -> None:
    client = _FakeClient(
        [_Step([FULL[:8]], ConnectionError("reset")), _Step([FULL[:5], FULL[5:]])]
    )
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part") as sink:
        assert await gw.download_media(IC, {"id": 7}, sink) is True
    assert client.download_calls == 2
    assert sink.sha256 == hashlib.sha256(FULL).hexdigest()
    assert sink.size == len(FULL)
    assert (tmp_path / "t.part").read_bytes() == FULL


@pytest.mark.asyncio
async def test_file_reference_expiry_refetches_and_resets(tmp_path: Path) -> None:
    client = _FakeClient(
        [_Step([b"abc"], FileReferenceExpiredError(None)), _Step([FULL])]
    )
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part") as sink:
        assert await gw.download_media(IC, {"id": 7}, sink) is True
    assert client.get_messages_calls == 2
    assert sink.sha256 == hashlib.sha256(FULL).hexdigest()


@pytest.mark.asyncio
async def test_file_reference_expiry_twice_is_skip_and_record(tmp_path: Path) -> None:
    client = _FakeClient(
        [
            _Step([b"abc"], FileReferenceExpiredError(None)),
            _Step([b"abc"], FileReferenceExpiredError(None)),
        ]
    )
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part") as sink:
        with pytest.raises(SkipAndRecord, match="7"):
            await gw.download_media(IC, {"id": 7}, sink)
        assert sink.size <= 3


@pytest.mark.asyncio
async def test_size_exceeded_is_not_retried(tmp_path: Path) -> None:
    client = _FakeClient([_Step([b"x" * 6, b"y" * 5]), _Step([b"x"])])
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part", limit=10) as sink, pytest.raises(MediaSizeExceeded):
        await gw.download_media(IC, {"id": 7}, sink)
    assert client.download_calls == 1


@pytest.mark.asyncio
async def test_message_gone_returns_false(tmp_path: Path) -> None:
    client = _FakeClient([], messages=[])
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part") as sink:
        assert await gw.download_media(IC, {"id": 7}, sink) is False
    assert client.download_calls == 0


@pytest.mark.asyncio
async def test_non_downloadable_media_returns_false(tmp_path: Path) -> None:
    client = _FakeClient([_Step([], result=None)])
    gw = _gateway(tmp_path, client)
    with MediaSink(tmp_path / "t.part") as sink:
        assert await gw.download_media(IC, {"id": 7}, sink) is False
    assert sink.size == 0
