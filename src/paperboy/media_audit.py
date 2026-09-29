"""List the media files a store no longer references (#70).

After a profile split the old profile's `media/` still holds the files that
now belong only to the split-out profile. This module finds them so the
operator can decide what to delete; it never deletes, and it opens the store
strictly read-only (never `Store.open`, which would migrate it).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from paperboy.media_keys import MEDIA_PREFIX, key_sha256

_INCOMING = ".incoming"


@dataclass(frozen=True)
class UnreferencedFile:
    key: str  # profile-relative, POSIX-style: `media/<xx>/<sha><ext>`
    size: int


def _referenced_shas(conn: sqlite3.Connection) -> set[str]:
    """Every sha256 a row of the store names. Matching is by sha, never by the
    stored `path`: pre-0006 rows hold absolute or cwd-relative locations
    (ADR-0007), and a sha names the same bytes under any extension.
    `user_photos` reference `media(sha256)`, so they are covered by `media`.
    A table the store predates (a pre-custody archive) contributes nothing."""
    shas: set[str] = set()
    for table in ("media", "custody_log"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            shas.update(r[0] for r in conn.execute(f"SELECT sha256 FROM {table}"))
    return shas


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` without writing anything next to it. A cleanly closed WAL
    store has no `-wal` file, so `immutable=1` is safe and avoids the `-shm`/
    `-wal` sidecars a plain read-only open would leave behind; a store with
    pending WAL frames is opened plain so those commits are seen."""
    wal = db_path.with_name(db_path.name + "-wal")
    pending = wal.exists() and wal.stat().st_size > 0
    flags = "mode=ro" if pending else "mode=ro&immutable=1"
    return sqlite3.connect(f"file:{db_path}?{flags}", uri=True)


def find_unreferenced(profile: Path) -> list[UnreferencedFile]:
    """Files under `<profile>/media/` (excluding `.incoming/`) whose sha256 no
    `media` or `custody_log` row references, sorted by key. A file whose name
    is not a well-formed key names no sha and is listed too. Raises
    `FileNotFoundError` when `<profile>/paperboy.sqlite` does not exist."""
    db_path = profile / "paperboy.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(f"no store at {db_path}")
    conn = _open_readonly(db_path)
    try:
        referenced = _referenced_shas(conn)
    finally:
        conn.close()

    media_root = profile / MEDIA_PREFIX
    found: list[UnreferencedFile] = []
    for dirpath, dirnames, filenames in os.walk(media_root):
        if Path(dirpath) == media_root:
            dirnames[:] = [d for d in dirnames if d != _INCOMING]
        for name in filenames:
            path = Path(dirpath) / name
            key = path.relative_to(profile).as_posix()
            if key_sha256(key) not in referenced:
                found.append(UnreferencedFile(key, path.stat().st_size))
    return sorted(found, key=lambda f: f.key)
