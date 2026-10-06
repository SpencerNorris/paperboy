"""An in-memory stand-in for the slice of `google.cloud.storage` paperboy uses (#63).

It enforces the write-once contract the real bucket relies on: an upload must
carry `if_generation_match=0` and fails with `PreconditionFailed` if the name
exists, and any delete is an `AssertionError` (also counted in `DELETE_CALLS`,
which a session fixture asserts is zero across the whole suite). It never
touches the network.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterator
from pathlib import Path

import google_crc32c
from google.api_core.exceptions import Forbidden, PreconditionFailed

# Suite-wide: every delete attempt on any fake, asserted == 0 by conftest.
DELETE_CALLS: list[str] = []


def crc32c_b64(data: bytes) -> str:
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.crc32c: str | None = None

    def exists(self) -> bool:
        self._bucket.calls["exists"] += 1
        return self.name in self._bucket.objects

    def upload_from_filename(
        self,
        filename: str,
        if_generation_match: int | None = None,
        checksum: str | None = None,
        **_: object,
    ) -> None:
        bucket = self._bucket
        bucket.calls["upload"] += 1
        assert if_generation_match == 0, "uploads must be create-only (if_generation_match=0)"
        data = Path(filename).read_bytes()
        if bucket.upload_error is not None:
            raise bucket.upload_error
        if bucket.race_once:
            # Someone else created the object between our exists() and upload.
            bucket.race_once = False
            bucket.objects[self.name] = data
        if self.name in bucket.objects:
            raise PreconditionFailed("object exists")
        bucket.objects[self.name] = data
        self.crc32c = bucket.corrupt_crc or crc32c_b64(data)

    def open(self, mode: str = "rb", chunk_size: int | None = None, **_: object) -> io.BytesIO:
        self._bucket.calls["open"] += 1
        assert mode == "rb"
        return io.BytesIO(self._bucket.objects[self.name])

    def delete(self, *_: object, **__: object) -> None:
        DELETE_CALLS.append(self.name)
        self._bucket.calls["delete"] += 1
        raise AssertionError("paperboy must never delete from a bucket")


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, bytes] = {}
        self.calls = {"exists": 0, "upload": 0, "open": 0, "list": 0, "delete": 0}
        self.granted: set[str] = {"storage.objects.create", "storage.objects.get"}
        self.retention_period: int | None = 8_035_200
        self.versioning_enabled: bool | None = True
        self.reload_error: Exception | None = None
        # Fault injection for tests:
        self.corrupt_crc: str | None = None  # server-side crc32c to report after upload
        self.upload_error: Exception | None = None
        self.race_once = False

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str = "", **_: object) -> Iterator[FakeBlob]:
        self.calls["list"] += 1
        return iter([FakeBlob(self, n) for n in sorted(self.objects) if n.startswith(prefix)])

    def test_iam_permissions(self, permissions: list[str]) -> list[str]:
        return [p for p in permissions if p in self.granted]

    def reload(self) -> None:
        if self.reload_error is not None:
            raise self.reload_error

    def delete_blob(self, *_: object, **__: object) -> None:
        DELETE_CALLS.append("delete_blob")
        self.calls["delete"] += 1
        raise AssertionError("paperboy must never delete from a bucket")


class FakeGcsClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


__all__ = ["DELETE_CALLS", "FakeBlob", "FakeBucket", "FakeGcsClient", "Forbidden", "crc32c_b64"]
