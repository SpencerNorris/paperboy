"""Streaming media downloads (#64) and the free-disk floor (#53)."""

import hashlib
import logging
import os
import shutil
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperboy.budget import PhaseStop
from paperboy.collectors.media import DiskFloorStop, MediaCollector
from paperboy.config import load_settings
from paperboy.media_sink import MediaSink, MediaSinkWriteError
from paperboy.store.db import Store
from tests.fakes import FakeGateway
from tests.test_collector_media import (
    _ctx,
    _doc_msg,
    _photo_msg,
    _seed,
    _settings,
    _sized_doc,
)

MB = 1 << 20


def _stream(total: int, chunk: int = MB, fill: bytes = b"\x07"):
    """A lazy chunk factory: `total` bytes in `chunk`-sized pieces, never all in memory."""

    def factory():
        left = total
        while left > 0:
            n = min(chunk, left)
            yield fill * n
            left -= n

    return factory


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _incoming(tmp_path: Path) -> Path:
    return tmp_path / "p" / "media" / ".incoming"


def _count(st: Store, table: str) -> int:
    return st.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def _fake_free(monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=10**12, used=10**12 - free_bytes, free=free_bytes),
    )


@pytest.mark.asyncio
async def test_bounded_memory_for_a_large_stream(tmp_path):
    # ~1-2 s: 200 MB streamed through the collector in 1 MB chunks.
    total = 200 * MB
    gw = FakeGateway({"media": {1: _stream(total)}})
    expect = hashlib.sha256()
    for chunk in _stream(total)():
        expect.update(chunk)

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        tracemalloc.start()
        try:
            res = await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert res.counts["downloaded"] == 1
        assert peak < 20 * MB
        row = st.conn.execute("SELECT sha256, size, path FROM media").fetchone()
    assert row["sha256"] == expect.hexdigest()
    assert row["size"] == total
    assert (tmp_path / "p" / row["path"]).stat().st_size == total


@pytest.mark.asyncio
async def test_interrupted_stream_leaves_nothing(tmp_path):
    def boom():
        yield b"partial"
        raise RuntimeError("boom")

    gw = FakeGateway({"media": {1: boom}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        with pytest.raises(RuntimeError, match="boom"):
            await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert _count(st, "media") == 0
        assert _count(st, "custody_log") == 0
        assert st.conn.execute(
            "SELECT count(*) AS n FROM raw_records WHERE kind='MediaDownload'"
        ).fetchone()["n"] == 0
    assert _files_under(tmp_path / "p" / "media") == []


@pytest.mark.asyncio
async def test_oversized_stream_is_size_mismatch(tmp_path, caplog):
    gw = FakeGateway({"media": {1: _stream(11_000_000)}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _sized_doc(1, 10_000_000))
        with caplog.at_level(logging.WARNING):
            res = await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["size_mismatch"] == 1
        assert res.counts["downloaded"] == 0
        assert _count(st, "media") == 0
        assert _count(st, "custody_log") == 0
    assert _files_under(tmp_path / "p" / "media") == []
    assert "10000000" in caplog.text
    assert "declared" in caplog.text


@pytest.mark.asyncio
async def test_short_document_is_size_mismatch(tmp_path, caplog):
    gw = FakeGateway({"media": {1: b"x" * 60}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _sized_doc(1, 100))
        with caplog.at_level(logging.WARNING):
            res = await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["size_mismatch"] == 1
        assert res.counts["downloaded"] == 0
        assert _count(st, "media") == 0
    assert _files_under(tmp_path / "p" / "media") == []
    assert "100" in caplog.text
    assert "60" in caplog.text


@pytest.mark.asyncio
async def test_photo_size_is_only_an_upper_bound(tmp_path):
    photo = _photo_msg(1)
    photo["media"]["photo"]["sizes"] = [{"_": "PhotoSize", "type": "m", "size": 50}]
    photo["media"]["photo"]["video_sizes"] = [{"_": "VideoSize", "type": "v", "size": 500}]
    gw = FakeGateway({"media": {1: b"j" * 400}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, photo)
        res = await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["downloaded"] == 1
        assert res.counts["size_mismatch"] == 0
        assert st.conn.execute("SELECT size FROM media").fetchone()["size"] == 400


@pytest.mark.asyncio
async def test_media_max_mb_counts_video_sizes(tmp_path):
    settings = load_settings("default", {"data_dir": tmp_path, "media_max_mb": 1})
    photo = _photo_msg(1)
    photo["media"]["photo"]["sizes"] = [{"_": "PhotoSize", "type": "m", "size": 30_000}]
    photo["media"]["photo"]["video_sizes"] = [{"_": "VideoSize", "type": "v", "size": 2_500_000}]
    gw = FakeGateway({"media": {1: b"p"}})
    with Store.open(tmp_path / "db.sqlite") as st:
        _seed(st, photo)
        res = await MediaCollector().collect(_ctx(st, gw, settings))
    assert gw.download_media_calls == []
    assert res.counts["too_large"] == 1


@pytest.mark.asyncio
async def test_disk_floor_stops_before_any_request(tmp_path, monkeypatch, caplog):
    data = b"first file"
    gw1 = FakeGateway({"media": {1: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=111))
        await MediaCollector().collect(_ctx(st, gw1, _settings(tmp_path)))

        # Msg 2 repeats doc 111 (a counted duplicate); msg 3 is new content.
        _seed(st, _doc_msg(2, doc_id=111))
        _seed(st, _doc_msg(3, doc_id=333))
        _fake_free(monkeypatch, 2 * 10**9)
        gw2 = FakeGateway({"media": {3: b"never fetched"}})
        with pytest.raises(DiskFloorStop) as ei:
            await MediaCollector().collect(_ctx(st, gw2, _settings(tmp_path)))
    assert isinstance(ei.value, PhaseStop)
    assert gw2.download_media_calls == []
    assert ei.value.counts["duplicates"] == 2
    assert "2.00 GB" in str(ei.value)
    assert "--media-min-free-gb" in str(ei.value)


@pytest.mark.asyncio
async def test_disk_floor_zero_disables_the_check(tmp_path, monkeypatch):
    _fake_free(monkeypatch, 1)
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    gw = FakeGateway({"media": {1: b"x"}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))
    assert res.counts["downloaded"] == 1


@pytest.mark.asyncio
async def test_disk_floor_subtracts_declared_size(tmp_path, monkeypatch):
    _fake_free(monkeypatch, 5_500_000_000)
    gw = FakeGateway({"media": {1: b"never fetched"}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _sized_doc(1, 1_000_000_000))  # 5.5 GB - 1 GB < 5 GB floor
        with pytest.raises(DiskFloorStop):
            await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
    assert gw.download_media_calls == []

    gw2 = FakeGateway({"media": {2: b"x" * 100}})
    with Store.open(tmp_path / "p2.sqlite") as st:
        _seed(st, _sized_doc(2, 100))  # 5.5 GB - 100 B >= 5 GB floor
        res = await MediaCollector().collect(_ctx(st, gw2, _settings(tmp_path), profile="q"))
    assert res.counts["downloaded"] == 1


@pytest.mark.asyncio
async def test_dedup_by_sha_after_streaming_keeps_one_file(tmp_path):
    data = b"identical bytes despite different document ids"
    gw = FakeGateway({"media": {1: data, 2: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=111, file_name="a.bin"))
        _seed(st, _doc_msg(2, doc_id=222, file_name="b.bin"))
        await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert _count(st, "custody_log") == 2
    stored = [p for p in _files_under(tmp_path / "p" / "media") if ".incoming" not in p.parts]
    assert len(stored) == 1
    assert _files_under(_incoming(tmp_path)) == []


@pytest.mark.asyncio
async def test_existing_file_without_rows_is_reused_not_rewritten(tmp_path):
    # Replay/legacy idempotency: bytes already on disk, no `media` row yet.
    data = b"already on disk"
    sha = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "p" / "media" / sha[:2] / f"{sha}.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    inode = dest.stat().st_ino
    gw = FakeGateway({"media": {1: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["downloaded"] == 1
        assert _count(st, "media") == 1
    assert dest.stat().st_ino == inode  # never replaced by the temp file
    assert _files_under(_incoming(tmp_path)) == []


@pytest.mark.asyncio
async def test_stale_incoming_part_is_swept_fresh_is_kept(tmp_path, caplog):
    incoming = _incoming(tmp_path)
    incoming.mkdir(parents=True)
    old, new = incoming / "old.part", incoming / "new.part"
    old.write_bytes(b"12345")
    new.write_bytes(b"1")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    gw = FakeGateway({})
    with Store.open(tmp_path / "p.sqlite") as st, caplog.at_level(logging.INFO):
        await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
    assert not old.exists()
    assert new.exists()
    assert "swept 1 stale part file(s), 5 bytes" in caplog.text


@pytest.mark.asyncio
async def test_sink_write_error_is_phase_stop(tmp_path, monkeypatch):
    def broken(self, chunk):
        raise MediaSinkWriteError("ENOSPC: No space left on device")

    monkeypatch.setattr(MediaSink, "write", broken)
    gw = FakeGateway({"media": {1: b"abc"}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        with pytest.raises(PhaseStop) as ei:
            await MediaCollector().collect(_ctx(st, gw, _settings(tmp_path)))
    assert not isinstance(ei.value, DiskFloorStop)
    assert "ENOSPC" in str(ei.value)
    assert _files_under(_incoming(tmp_path)) == []
