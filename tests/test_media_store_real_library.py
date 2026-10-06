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

from paperboy.media_store import GcsMediaStore, MediaStoreIntegrityError

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
