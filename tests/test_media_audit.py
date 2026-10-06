"""`media_audit` / `scripts/unreferenced_media.py` (#70): list, never delete,
the media files a store no longer references."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from paperboy.media_audit import UnreferencedFile, find_unreferenced
from paperboy.media_keys import media_key
from paperboy.store.db import Store

ROOT = Path(__file__).resolve().parent.parent


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _put(profile: Path, text: str, ext: str = ".txt") -> tuple[str, str]:
    """Write a content-addressed file; return `(sha, key)`."""
    sha = _sha(text)
    key = media_key(sha, ext)
    path = profile / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return sha, key


def _media_row(store: Store, sha: str, key: str, uri: str = "tg:msg:1/1") -> None:
    store.conn.execute(
        "INSERT INTO media (sha256, message_uri, kind, size, path, downloaded_at) "
        "VALUES (?, ?, 'document', 1, ?, '2026-01-01T00:00:00+00:00')",
        (sha, uri, key),
    )


def _custody_row(store: Store, sha: str, key: str) -> None:
    store.conn.execute(
        "INSERT INTO custody_log (path, sha256, recorded_at, source_message_uri) "
        "VALUES (?, ?, '2026-01-01T00:00:00+00:00', 'tg:msg:1/1')",
        (key, sha),
    )


def _profile(tmp_path: Path) -> tuple[Path, Store]:
    profile = tmp_path / "p"
    store = Store.open(profile / "paperboy.sqlite")
    store.conn.execute("PRAGMA foreign_keys=OFF")  # rows here need no message/media parents
    return profile, store


def test_referenced_files_are_not_listed_unreferenced_ones_are(tmp_path):
    profile, store = _profile(tmp_path)
    sha_m, key_m = _put(profile, "media only")
    sha_c, key_c = _put(profile, "custody only")
    sha_b, key_b = _put(profile, "both")
    _, key_x = _put(profile, "orphan, 14 b")
    _media_row(store, sha_m, key_m)
    _custody_row(store, sha_c, key_c)
    _media_row(store, sha_b, key_b, "tg:msg:1/2")
    _custody_row(store, sha_b, key_b)
    store.conn.commit()
    store.close()

    assert find_unreferenced(profile) == [UnreferencedFile(key_x, len("orphan, 14 b"))]


def test_incoming_parts_are_ignored(tmp_path):
    profile, store = _profile(tmp_path)
    store.close()
    incoming = profile / "media" / ".incoming"
    incoming.mkdir(parents=True)
    (incoming / "abc.part").write_bytes(b"half a file")
    assert find_unreferenced(profile) == []


def test_a_legacy_form_path_row_still_counts_as_referenced(tmp_path):
    """Pre-0006 rows hold absolute or cwd-relative `path` values: matching is by
    sha, never by the stored location string."""
    profile, store = _profile(tmp_path)
    sha, key = _put(profile, "legacy")
    _media_row(store, sha, f"/old/machine/data/default/{key}")
    store.conn.commit()
    store.close()
    assert find_unreferenced(profile) == []


def test_a_file_whose_name_is_not_a_sha_is_listed(tmp_path):
    profile, store = _profile(tmp_path)
    store.close()
    stray = profile / "media" / "ab" / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    assert find_unreferenced(profile) == [UnreferencedFile("media/ab/notes.txt", 1)]


def test_no_media_dir_means_nothing_to_list(tmp_path):
    profile, store = _profile(tmp_path)
    store.close()
    assert find_unreferenced(profile) == []


def test_missing_store_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_unreferenced(tmp_path / "nope")


def _digest(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in [root, *root.rglob("*")]
    }


def test_audit_writes_nothing_anywhere(tmp_path):
    profile, store = _profile(tmp_path)
    _put(profile, "orphan")
    store.conn.commit()
    store.close()
    for sidecar in profile.glob("paperboy.sqlite-*"):
        sidecar.unlink()
    before = _digest(profile)
    assert len(find_unreferenced(profile)) == 1
    assert _digest(profile) == before  # no -wal/-shm, no mtime change, nothing removed


def test_script_prints_sorted_key_size_lines_and_a_total_and_deletes_nothing(tmp_path):
    profile, store = _profile(tmp_path)
    _, key_a = _put(profile, "aaa")
    _, key_b = _put(profile, "bbbbb")
    store.conn.commit()
    store.close()
    env = {**os.environ, "PAPERBOY_DATA_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "unreferenced_media.py"), "--profile", "p"],
        capture_output=True, text=True, env=env, cwd=ROOT, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[:-1] == sorted([f"{key_a}  3", f"{key_b}  5"])
    assert lines[-1].startswith("total: 2 file(s), 8 bytes")
    assert (profile / key_a).exists() and (profile / key_b).exists()
