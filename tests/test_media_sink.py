"""MediaSink: incremental sha256 into a temp file with a size limit (#64)."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from paperboy.media_sink import (
    MediaSink,
    MediaSinkWriteError,
    MediaSizeExceeded,
    stream_file_into,
)


def test_write_appends_hashes_and_counts(tmp_path: Path) -> None:
    p = tmp_path / "x.part"
    with MediaSink(p) as sink:
        for chunk in (b"abc", b"defg", b"h"):
            assert sink.write(chunk) == len(chunk)
    assert sink.size == 8
    assert sink.sha256 == hashlib.sha256(b"abcdefgh").hexdigest()
    assert p.read_bytes() == b"abcdefgh"


def test_reset_truncates_and_restarts_hash(tmp_path: Path) -> None:
    p = tmp_path / "x.part"
    with MediaSink(p) as sink:
        sink.write(b"garbage")
        sink.reset()
        sink.write(b"good")
    assert sink.size == 4
    assert sink.sha256 == hashlib.sha256(b"good").hexdigest()
    assert p.read_bytes() == b"good"


def test_limit_raises_before_exceeding(tmp_path: Path) -> None:
    p = tmp_path / "x.part"
    with MediaSink(p, limit=10) as sink:
        sink.write(b"x" * 10)
        with pytest.raises(MediaSizeExceeded) as ei:
            sink.write(b"y")
    assert ei.value.declared == 10
    assert ei.value.received == 11
    assert p.stat().st_size == 10


def test_no_limit_when_none(tmp_path: Path) -> None:
    with MediaSink(tmp_path / "x.part", limit=None) as sink:
        sink.write(b"z" * (1 << 20))
    assert sink.size == 1 << 20


def test_write_oserror_is_wrapped(tmp_path: Path) -> None:
    sink = MediaSink(tmp_path / "x.part")

    class _Broken:
        def write(self, _d: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

        def close(self) -> None:
            pass

    real = sink._fh
    assert real is not None
    sink._fh = _Broken()  # type: ignore[assignment]
    with pytest.raises(MediaSinkWriteError) as ei:
        sink.write(b"abc")
    assert not isinstance(ei.value, OSError)
    assert "ENOSPC" in str(ei.value)
    assert str(tmp_path) not in str(ei.value)
    real.close()


def test_context_manager_closes_file(tmp_path: Path) -> None:
    with MediaSink(tmp_path / "x.part") as sink:
        sink.write(b"abc")
        assert not sink.closed
    assert sink.closed
    assert sink.size == 3
    assert sink.sha256 == hashlib.sha256(b"abc").hexdigest()


def test_stream_file_into_copies_in_chunks(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"0123456789")

    class Recording(MediaSink):
        writes = 0

        def write(self, chunk: bytes) -> int:
            type(self).writes += 1
            return super().write(chunk)

    with Recording(tmp_path / "x.part") as sink:
        stream_file_into(src, sink, chunk_size=4)
    assert Recording.writes == 3
    assert sink.sha256 == hashlib.sha256(b"0123456789").hexdigest()


def test_close_fsyncs_before_closing(tmp_path, monkeypatch):
    import os

    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    with MediaSink(tmp_path / "f.part") as sink:
        sink.write(b"abc")
    assert len(synced) == 1


def test_failed_close_does_not_mask_the_original_exception(tmp_path, monkeypatch):
    import os

    def _enospc(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", _enospc)
    with pytest.raises(MediaSizeExceeded), MediaSink(tmp_path / "f.part", limit=2) as sink:
        sink.write(b"abc")


def test_failed_close_without_prior_error_still_raises(tmp_path, monkeypatch):
    import os

    def _enospc(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", _enospc)
    with pytest.raises(MediaSinkWriteError), MediaSink(tmp_path / "f.part") as sink:
        sink.write(b"abc")


def test_hash_only_mode_writes_nothing_but_hashes_counts_and_limits(tmp_path):
    with MediaSink(None, limit=5) as sink:
        sink.write(b"abc")
        sink.reset()
        sink.write(b"hello")
        assert sink.size == 5
        assert sink.sha256 == hashlib.sha256(b"hello").hexdigest()
        with pytest.raises(MediaSizeExceeded):
            sink.write(b"x")
    assert sink.closed
    assert list(tmp_path.iterdir()) == []


def test_open_failure_becomes_media_sink_write_error(tmp_path):
    with pytest.raises(MediaSinkWriteError):
        MediaSink(tmp_path / "missing-dir" / "x.part")


def test_close_releases_the_descriptor_when_fsync_fails(tmp_path, monkeypatch):
    sink = MediaSink(tmp_path / "x.part")
    sink.write(b"abc")

    def _boom(fd):
        raise OSError(errno.EIO, "boom")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(MediaSinkWriteError):
        sink.close()
    assert sink.closed


def test_sink_exposes_crc32c_base64(tmp_path: Path) -> None:
    import base64

    import google_crc32c

    data = b"abcdefgh"
    with MediaSink(tmp_path / "x.part") as sink:
        sink.write(b"abc")
        sink.write(b"defgh")
        expected = base64.b64encode(google_crc32c.Checksum(data).digest()).decode()
        assert sink.crc32c == expected
        sink.reset()
        assert sink.crc32c == base64.b64encode(google_crc32c.Checksum(b"").digest()).decode()


def test_crc32c_uses_the_c_implementation() -> None:
    import google_crc32c

    assert google_crc32c.implementation == "c"
