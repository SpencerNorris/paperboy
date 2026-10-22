"""`GcsMediaStore.commit` driven through the REAL `google.cloud.storage.Blob`.

`tests/fake_gcs.py` replaces `upload_from_filename` wholesale, so it cannot see
what the library itself does. In particular, `checksum="crc32c"` on a resumable
upload (> 8 MiB) makes the library call `blob.delete()` on a mismatch - a DELETE
against the write-once evidence bucket. Here only the HTTP transport is stubbed,
and every request is recorded so the test can assert no DELETE is ever sent and
that our CRC32C reaches the server in the object metadata.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import google_crc32c
import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from paperboy import media_store
from paperboy.media_store import GcsMediaStore, MediaStoreError, MediaStoreIntegrityError

OVER_MULTIPART = 8 * 1024 * 1024 + 1  # the library's resumable-upload threshold + 1
KEY = "media/ab/" + "ab" * 32 + ".bin"


def _crc(data: bytes) -> str:
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


def _response(status: int, body: dict | None = None, headers: dict | None = None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body or {}).encode()
    resp.headers.update({"content-type": "application/json", **(headers or {})})
    resp.request = requests.Request("PUT", "https://upload.example/session").prepare()
    return resp


class _Transport:
    """A `requests`-session stand-in: a scripted server that records requests."""

    def __init__(self, *, reject_crc: bool = False, bad_object_crc: bool = False) -> None:
        self.reject_crc = reject_crc
        self.bad_object_crc = bad_object_crc  # create the object with a different crc32c
        self.requests: list[tuple[str, str]] = []
        self.declared_crc: str | None = None
        self.is_mtls = False  # read by the client's base-url selection

    def request(self, method, url, data=None, headers=None, **_):
        self.requests.append((method, url))
        if method == "POST" and "uploadType=resumable" in url:
            body = data if isinstance(data, bytes) else (data.read() if data else b"")
            self.declared_crc = json.loads(body or b"{}").get("crc32c")
            return _response(200, headers={"location": "https://upload.example/session"})
        if method == "PUT":
            if self.reject_crc:
                return _response(
                    400, {"error": {"code": 400, "message": "Provided CRC32C does not match"}}
                )
            if self.bad_object_crc:
                # What a corrupting server would answer; with the library's own
                # `checksum="crc32c"` this makes it call `blob.delete()`.
                return _response(
                    200,
                    {"name": "p/" + KEY, "crc32c": "AAAAAA=="},
                    headers={"x-goog-hash": "crc32c=AAAAAA=="},
                )
            return _response(200, {"name": "p/" + KEY, "crc32c": self.declared_crc})
        if method == "DELETE":
            return _response(204)
        raise AssertionError(f"unexpected request {method} {url}")

    def deletes(self) -> list[str]:
        return [url for method, url in self.requests if method == "DELETE"]


def _store(transport: _Transport) -> GcsMediaStore:
    def factory():
        client = storage.Client(project="p", credentials=AnonymousCredentials())
        client._http_internal = transport
        return client

    return GcsMediaStore("bkt", "p", factory)


def _big_file(tmp_path: Path) -> tuple[Path, str]:
    data = b"\x07" * OVER_MULTIPART
    path = tmp_path / "big.part"
    path.write_bytes(data)
    return path, _crc(data)


def test_resumable_upload_sends_our_crc_and_no_delete(tmp_path):
    transport = _Transport(reject_crc=False)
    path, crc = _big_file(tmp_path)
    assert _store(transport).commit(path, KEY, crc) is True
    assert transport.declared_crc == crc  # the server validates against OUR crc
    assert transport.deletes() == []


def test_server_side_crc_rejection_is_integrity_error_and_never_a_delete(tmp_path):
    transport = _Transport(reject_crc=True)
    path, crc = _big_file(tmp_path)
    with pytest.raises(MediaStoreIntegrityError):
        _store(transport).commit(path, KEY, crc)
    assert transport.deletes() == []


def test_object_created_with_a_wrong_crc_is_integrity_error_and_never_a_delete(tmp_path):
    transport = _Transport(bad_object_crc=True)
    path, crc = _big_file(tmp_path)
    with pytest.raises(MediaStoreIntegrityError):
        _store(transport).commit(path, KEY, crc)
    assert transport.deletes() == []


class _ResettingTransport(_Transport):
    """Every upload request dies with a reset connection, like a VPN reconnect."""

    def request(self, method, url, data=None, headers=None, **kw):
        if method in ("POST", "PUT"):
            self.requests.append((method, url))
            raise requests.exceptions.ConnectionError("Connection reset by peer")
        return super().request(method, url, data=data, headers=headers, **kw)


def test_upload_that_never_gets_through_is_a_store_error_not_a_raw_retry_error(
    tmp_path, monkeypatch
):
    # The real library gives up with `RetryError` (not a GoogleAPICallError). It
    # must surface as MediaStoreError so fetch-from-list ends with a reason.
    # raising=False: on code without the constant this still reaches the upload and
    # fails for the real reason (a raw RetryError), not on the missing attribute.
    monkeypatch.setattr(media_store, "UPLOAD_RETRY_TIMEOUT_S", 1.0, raising=False)
    transport = _ResettingTransport()
    path, crc = _big_file(tmp_path)
    with pytest.raises(MediaStoreError):
        _store(transport).commit(path, KEY, crc)
    assert len(transport.requests) >= 2  # it did retry before giving up
    assert transport.deletes() == []


class _ChunkedTransport(_Transport):
    """A resumable-upload server that answers each non-final chunk with 308.

    Records every upload PUT as (start, end, payload bytes, timeout). `stall_put`
    (1-based) makes that PUT die once the way a stalled VPN socket does.
    """

    def __init__(self, *, stall_put: int | None = None, always_stall: bool = False) -> None:
        super().__init__()
        self.puts: list[tuple[int, int, int, object]] = []
        self.timeouts: list[object] = []  # of every upload POST/PUT
        self.stall_put = stall_put
        self.always_stall = always_stall

    def request(self, method, url, data=None, headers=None, timeout=None, **kw):
        if "upload" in url:  # not the library's own bucket-metadata GET
            self.timeouts.append(timeout)
        if method != "PUT":
            return super().request(method, url, data=data, headers=headers, **kw)
        self.requests.append((method, url))
        assert headers is not None and isinstance(data, bytes)  # a chunk PUT always has both
        span, _, total = headers["content-range"].removeprefix("bytes ").partition("/")
        start, _, end = span.partition("-")
        self.puts.append((int(start), int(end), len(data), timeout))
        if self.always_stall or len(self.puts) == self.stall_put:
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool: Max retries exceeded with url: "
                "/upload/storage/v1/b/bkt/o?uploadType=resumable&upload_id=SESSION-SECRET"
            ) from TimeoutError("The write operation timed out")
        if int(end) + 1 < int(total):
            return _response(308, headers={"range": f"bytes=0-{end}"})
        return _response(200, {"name": "p/" + KEY, "crc32c": self.declared_crc})


def test_big_upload_goes_in_bounded_chunks_with_the_long_request_timeout(tmp_path, monkeypatch):
    # #107: one request per file (the library's 100 MiB default chunk) with its
    # 60 s request timeout never finished over a slow, stalling uplink.
    monkeypatch.setattr(media_store, "UPLOAD_CHUNK_BYTES", 4 * 1024 * 1024)
    transport = _ChunkedTransport()
    path, crc = _big_file(tmp_path)
    assert _store(transport).commit(path, KEY, crc) is True
    assert len(transport.puts) == 3  # 4 MiB + 4 MiB + 1 byte
    assert all(size <= media_store.UPLOAD_CHUNK_BYTES for _, _, size, _ in transport.puts)
    assert set(transport.timeouts) == {media_store.UPLOAD_REQUEST_TIMEOUT_S}
    assert transport.deletes() == []


def test_a_stalled_chunk_is_resent_from_its_own_offset_not_from_byte_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(media_store, "UPLOAD_CHUNK_BYTES", 4 * 1024 * 1024)
    transport = _ChunkedTransport(stall_put=2)
    path, crc = _big_file(tmp_path)
    assert _store(transport).commit(path, KEY, crc) is True
    starts = [start for start, _, _, _ in transport.puts]
    assert starts == [0, 4 * 1024 * 1024, 4 * 1024 * 1024, 8 * 1024 * 1024]
    assert transport.deletes() == []


def test_upload_failure_names_the_root_cause_but_not_the_session_url(tmp_path, monkeypatch):
    # #107: the error used to say only "RetryError", hiding the write timeout.
    # The resumable session URL is a write capability, so it must not be logged.
    monkeypatch.setattr(media_store, "UPLOAD_RETRY_TIMEOUT_S", 1.0)
    transport = _ChunkedTransport(always_stall=True)
    path, crc = _big_file(tmp_path)
    with pytest.raises(MediaStoreError) as caught:
        _store(transport).commit(path, KEY, crc)
    message = str(caught.value)
    assert "RetryError" in message
    assert "TimeoutError" in message
    assert "SESSION-SECRET" not in message
    assert transport.deletes() == []
