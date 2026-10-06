"""Where a run's media bytes live: the profile folder or a GCS bucket (#63, ADR-0008).

`MediaStore` is the only seam the collectors, replay and `fetch-media` use to
write or read media bytes. A run has exactly one store, never both. A file's
full location is *store root + key* (`media_keys`, ADR-0007); the key never
changes, only the root does.

The bucket is write-once by design: it carries a retention policy, so a deleted
or overwritten object stays billable and recoverable for months. Therefore this
module has **no delete path at all** and `commit` is create-only
(`if_generation_match=0`). A lost create race is reported as `False`, never as
an overwrite.

`google.cloud.storage` costs ~2.7 s to import, so it is imported only inside
`default_client_factory` (and the error-path helpers). A local-only run, and a
reproject of a local-only source, never import it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, Self

from paperboy.config import Settings, parse_media_store_url, profile_dir
from paperboy.media_keys import (
    MEDIA_PREFIX,
    find_existing_key,
    is_media_key,
    resolve_key_under,
)

log = logging.getLogger(__name__)

LOCAL_STORE_ID = "local"
_READ_CHUNK = 1 << 20


class MediaStoreError(Exception):
    """The store could not be reached or written (after the library's own
    retries). Not per-file: the media phase stops rather than re-downloading."""


class MediaStoreIntegrityError(MediaStoreError):
    """The object was created but its server-side CRC32C differs from the one
    computed while streaming. The object is left in place (there is no delete
    path); ADR-0008 documents the manual recovery."""

    def __init__(self, key: str, local_crc32c: str, remote_crc32c: str) -> None:
        super().__init__(
            f"crc32c mismatch for {key}: local {local_crc32c}, remote {remote_crc32c}"
        )
        self.key = key
        self.local_crc32c = local_crc32c
        self.remote_crc32c = remote_crc32c


class ByteStream(Protocol):
    """What `open_read` returns: a closable, context-managed binary reader."""

    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any, /) -> object: ...


class MediaStore(Protocol):
    """Byte storage for media, addressed by profile-relative keys."""

    store_id: str

    def exists(self, key: str) -> bool:
        """True if an object/file lives under exactly `key`."""
        ...

    def find_key(self, sha256: str) -> str | None:
        """The key of an object already stored for `sha256` under any extension
        (a pre-#62 legacy suffix), else `None`."""
        ...

    def commit(self, temp: Path, key: str, crc32c_b64: str) -> bool:
        """Create `key` from the finished temp file. Create-only: `False` means
        the key already existed (a lost race) and nothing was overwritten."""
        ...

    def open_read(self, key: str) -> ByteStream:
        """A binary stream of the stored bytes; `FileNotFoundError` if absent."""
        ...


class LocalMediaStore:
    """The profile folder: today's behaviour behind the `MediaStore` seam."""

    store_id = LOCAL_STORE_ID

    def __init__(self, profile_root: Path) -> None:
        self._root = profile_root

    def exists(self, key: str) -> bool:
        return resolve_key_under(self._root, key).exists()

    def find_key(self, sha256: str) -> str | None:
        return find_existing_key(self._root, sha256)

    def commit(self, temp: Path, key: str, crc32c_b64: str) -> bool:
        del crc32c_b64  # the local volume has no server-side checksum to compare
        dest = resolve_key_under(self._root, key)
        if dest.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, dest)  # atomic: same filesystem as `.incoming/`
        return True

    def open_read(self, key: str) -> ByteStream:
        return open(resolve_key_under(self._root, key), "rb")  # noqa: SIM115 - caller closes


def _transport_errors() -> tuple[type[BaseException], ...]:
    """Errors that mean "the bucket is unreachable/unusable", not "this file"."""
    import requests
    from google.api_core.exceptions import GoogleAPICallError
    from google.auth.exceptions import GoogleAuthError

    return (GoogleAPICallError, GoogleAuthError, requests.exceptions.RequestException)


class _GcsReader:
    """Wraps a blob reader so transport failures surface as `MediaStoreError`
    and a missing object as `FileNotFoundError`, whenever they happen."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def read(self, size: int = -1, /) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return self._raw.read(size)
        except NotFound as exc:
            raise FileNotFoundError("media object not found") from exc
        except _transport_errors() as exc:
            raise MediaStoreError(f"read failed: {type(exc).__name__}") from exc

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any, /) -> None:
        self.close()


class GcsMediaStore:
    """A `gs://<bucket>/<prefix>` store: object name is `<prefix>/<key>`.

    Create-only, no delete. Credentials are Application Default Credentials,
    resolved by the client factory on first use; nothing is logged about them.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str,
        client_factory: Callable[[], Any],
    ) -> None:
        self._bucket_name = bucket
        self._prefix = prefix
        self._client_factory = client_factory
        self._client: Any = None
        self.store_id = f"gs://{bucket}/{prefix}"

    def _bucket(self) -> Any:
        if self._client is None:
            try:
                self._client = self._client_factory()
            except _transport_errors() as exc:
                raise MediaStoreError(
                    f"cannot create the storage client: {type(exc).__name__}"
                ) from exc
        return self._client.bucket(self._bucket_name)

    def _name(self, key: str) -> str:
        if not is_media_key(key):
            raise ValueError("not a media key")
        return f"{self._prefix}/{key}"

    def exists(self, key: str) -> bool:
        name = self._name(key)
        try:
            found = bool(self._bucket().blob(name).exists())
        except _transport_errors() as exc:
            raise MediaStoreError(f"exists check failed: {type(exc).__name__}") from exc
        log.debug("media store: exists(%s) -> %s", key, found)
        return found

    def find_key(self, sha256: str) -> str | None:
        shard_prefix = f"{self._prefix}/{MEDIA_PREFIX}/{sha256[:2]}/{sha256}"
        try:
            names = sorted(b.name for b in self._bucket().list_blobs(prefix=shard_prefix))
        except _transport_errors() as exc:
            raise MediaStoreError(f"list failed: {type(exc).__name__}") from exc
        for name in names:
            key = name[len(self._prefix) + 1 :]
            if is_media_key(key):
                return key
        return None

    def commit(self, temp: Path, key: str, crc32c_b64: str) -> bool:
        from google.api_core.exceptions import PreconditionFailed
        from google.cloud.storage.exceptions import DataCorruption

        blob = self._bucket().blob(self._name(key))
        try:
            # if_generation_match=0: create only if no live object has this name.
            # It also makes the library's resumable upload retry-safe.
            blob.upload_from_filename(str(temp), if_generation_match=0, checksum="crc32c")
        except PreconditionFailed:
            log.info("media store: %s already exists (lost create race); not overwritten", key)
            return False
        except DataCorruption as exc:
            log.error("media store: server reported corruption uploading %s", key)
            raise MediaStoreIntegrityError(
                key, crc32c_b64, "reported by the client library"
            ) from exc
        except _transport_errors() as exc:
            raise MediaStoreError(f"upload failed: {type(exc).__name__}") from exc
        remote = blob.crc32c
        if remote != crc32c_b64:
            raise MediaStoreIntegrityError(key, crc32c_b64, str(remote))
        log.info("media store: created %s", key)
        return True

    def open_read(self, key: str) -> ByteStream:
        blob = self._bucket().blob(self._name(key))
        try:
            if not blob.exists():
                raise FileNotFoundError("media object not found")
            raw = blob.open("rb", chunk_size=_READ_CHUNK)
        except _transport_errors() as exc:
            raise MediaStoreError(f"open failed: {type(exc).__name__}") from exc
        return _GcsReader(raw)


def default_client_factory() -> Any:
    """The one place `google.cloud.storage` is imported; ADC only."""
    from google.cloud import storage

    return storage.Client()


def build_media_store(
    settings: Settings,
    profile: str,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> MediaStore:
    """The store this run writes to: GCS when `settings.media_store` is set,
    else the local profile folder. The only constructor callers use."""
    if settings.media_store is None:
        return LocalMediaStore(profile_dir(settings, profile))
    bucket, prefix = parse_media_store_url(settings.media_store)
    return GcsMediaStore(bucket, prefix, client_factory or default_client_factory)


def stored_in(conn: sqlite3.Connection, sha256: str, store_id: str) -> bool:
    """True if a `custody_log` row already places `sha256` in `store_id`.

    Offline answer to "does this store have it?" - lets a re-run skip the
    metadata GET for a file this database already put in the bucket.
    """
    row = conn.execute(
        "SELECT 1 FROM custody_log WHERE sha256 = ? AND store = ? LIMIT 1", (sha256, store_id)
    ).fetchone()
    return row is not None
