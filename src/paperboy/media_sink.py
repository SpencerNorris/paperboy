"""A file-like sink that Telethon streams a download into (#64).

Telethon's `download_media(file=...)` accepts any object with `write(chunk)`
(it calls `write` per chunk, `flush` if present, and never closes a caller's
object). `MediaSink` writes each chunk to a temp file while updating a
`hashlib.sha256` incrementally, so a download of any size needs only one
chunk of memory and is hashed without a second read.

Every download attempt must begin with `reset()`: `Budget.call` re-invokes
its factory after a RETRY-class error, and a retried download must never
append to a partial file or hash garbage.

`tell()` is deliberately not implemented: Telethon only calls it when a
`progress_callback` is passed, and we never pass one.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
from pathlib import Path
from types import TracebackType
from typing import IO

log = logging.getLogger(__name__)


class MediaSizeExceeded(Exception):
    """The stream delivered more bytes than the declared size.

    Deliberately not an `OSError`: `errors.classify` would treat that as a
    transient RETRY and re-download the whole file.
    """

    def __init__(self, declared: int, received: int) -> None:
        super().__init__(f"received {received} bytes, more than the declared {declared}")
        self.declared = declared
        self.received = received


class MediaSinkWriteError(Exception):
    """A local file operation on the temp file failed (disk full, EIO, ...).

    Not an `OSError` for the same reason as `MediaSizeExceeded`: a full disk
    is not transient, so it must not trigger three full re-downloads. The
    message carries the errno name and strerror, never the path.
    """


def _describe(exc: OSError) -> str:
    name = errno.errorcode.get(exc.errno or 0, "OSError")
    return f"{name}: {exc.strerror or exc}"


class MediaSink:
    """Write chunks to `path`, hashing and counting as they arrive.

    `path=None` is hash-and-count-only mode: nothing is written anywhere, but
    the hash, the byte count and the `limit` still apply. Reproject replay uses
    it so it never writes into the (possibly read-only) source profile.
    """

    def __init__(self, path: Path | None, *, limit: int | None = None) -> None:
        self._path = path
        self._limit = limit
        self._fh: IO[bytes] | None = None
        if path is not None:
            try:
                self._fh = open(path, "wb")  # noqa: SIM115 - closed by close()/__exit__
            except OSError as exc:
                raise MediaSinkWriteError(_describe(exc)) from exc
        self._hasher = hashlib.sha256()
        self._size = 0

    def write(self, chunk: bytes) -> int:
        received = self._size + len(chunk)
        if self._limit is not None and received > self._limit:
            raise MediaSizeExceeded(self._limit, received)
        try:
            if self._fh is not None:
                self._fh.write(chunk)
        except OSError as exc:
            raise MediaSinkWriteError(_describe(exc)) from exc
        self._hasher.update(chunk)
        self._size = received
        return len(chunk)

    def reset(self) -> None:
        """Truncate the temp file and restart the hash: a fresh attempt."""
        try:
            if self._fh is not None:
                self._fh.seek(0)
                self._fh.truncate()
        except OSError as exc:
            raise MediaSinkWriteError(_describe(exc)) from exc
        self._hasher = hashlib.sha256()
        self._size = 0

    def flush(self) -> None:
        try:
            if self._fh is not None:
                self._fh.flush()
        except OSError as exc:
            raise MediaSinkWriteError(_describe(exc)) from exc

    def close(self) -> None:
        """Flush, fsync and close, so a later rename never exposes bytes that
        an OS crash could still lose."""
        fh = self._fh
        if fh is None or fh.closed:
            return
        error: OSError | None = None
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except OSError as exc:
            error = exc
        finally:
            # Always release the descriptor, even when flush/fsync failed, so
            # repeated disk errors cannot leak fds.
            try:
                fh.close()
            except OSError as exc:
                error = error or exc
        if error is not None:
            raise MediaSinkWriteError(_describe(error)) from error

    @property
    def closed(self) -> bool:
        return self._fh is None or self._fh.closed

    @property
    def size(self) -> int:
        return self._size

    @property
    def sha256(self) -> str:
        """Hex digest of everything written since the last reset."""
        return self._hasher.hexdigest()

    def __enter__(self) -> MediaSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
            return
        # Already unwinding: a failed close must not replace the original
        # exception (e.g. a per-file size mismatch becoming a phase stop).
        try:
            self.close()
        except MediaSinkWriteError as close_exc:
            log.warning("media sink close failed while handling %s: %s",
                        exc_type.__name__, close_exc)


def stream_file_into(path: Path, sink: MediaSink, *, chunk_size: int = 1 << 20) -> None:
    """Copy the file at `path` into `sink` in `chunk_size` pieces."""
    with open(path, "rb") as fh:
        shutil.copyfileobj(fh, sink, chunk_size)
