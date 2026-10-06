"""The `MediaStore` contract, run against the local store and an in-memory GCS fake (#63)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from google.api_core.exceptions import PreconditionFailed, ServiceUnavailable

from paperboy.config import load_settings
from paperboy.media_keys import media_key
from paperboy.media_store import (
    GcsMediaStore,
    LocalMediaStore,
    MediaStoreError,
    MediaStoreIntegrityError,
    build_media_store,
    stored_in,
)
from tests.fake_gcs import FakeGcsClient, crc32c_b64

DATA = b"hello media"
SHA = "ab" + "0" * 62
KEY = media_key(SHA, ".pdf")
CRC = crc32c_b64(DATA)


@pytest.fixture(params=["local", "gcs"])
def env(request, tmp_path):
    client = FakeGcsClient()
    if request.param == "local":
        store = LocalMediaStore(tmp_path / "profile")
    else:
        store = GcsMediaStore("b", "p/x", client_factory=lambda: client)
    return request.param, store, client, tmp_path


def _temp(tmp_path: Path, data: bytes = DATA) -> Path:
    p = tmp_path / "t.part"
    p.write_bytes(data)
    return p


def test_store_id(env):
    kind, store, _, _ = env
    assert store.store_id == ("local" if kind == "local" else "gs://b/p/x")


def test_exists_false_then_true_after_commit(env):
    _, store, _, tmp_path = env
    assert store.exists(KEY) is False
    assert store.commit(_temp(tmp_path), KEY, CRC) is True
    assert store.exists(KEY) is True


def test_second_commit_is_a_duplicate_not_an_overwrite(env):
    kind, store, client, tmp_path = env
    assert store.commit(_temp(tmp_path), KEY, CRC) is True
    assert store.commit(_temp(tmp_path, b"different"), KEY, crc32c_b64(b"different")) is False
    with store.open_read(KEY) as fh:
        assert fh.read() == DATA  # the first bytes survive
    if kind == "gcs":
        assert len(client.bucket("b").objects) == 1


def test_open_read_streams_back(env):
    _, store, _, tmp_path = env
    store.commit(_temp(tmp_path), KEY, CRC)
    with store.open_read(KEY) as fh:
        assert fh.read(5) == DATA[:5]
        assert fh.read() == DATA[5:]


def test_open_read_missing_is_file_not_found(env):
    _, store, _, _ = env
    with pytest.raises(FileNotFoundError):
        store.open_read(KEY)


def test_find_key_finds_a_legacy_suffixed_object(env):
    kind, store, client, tmp_path = env
    assert store.find_key(SHA) is None
    legacy = media_key(SHA, ".docx")
    store.commit(_temp(tmp_path), legacy, CRC)
    assert store.find_key(SHA) == legacy


def test_keys_are_validated(env):
    _, store, _, _ = env
    with pytest.raises(ValueError):
        store.exists("../../etc/passwd")


def test_gcs_commit_is_create_only_and_never_deletes(tmp_path):
    client = FakeGcsClient()
    store = GcsMediaStore("b", "p/x", client_factory=lambda: client)
    store.commit(_temp(tmp_path), KEY, CRC)
    bucket = client.bucket("b")
    assert list(bucket.objects) == [f"p/x/{KEY}"]  # object name = prefix + key
    assert bucket.calls["delete"] == 0 and bucket.calls["upload"] == 1  # fake asserts gen==0


def test_gcs_crc_mismatch_raises_with_both_digests_and_leaves_the_object(tmp_path):
    client = FakeGcsClient()
    client.bucket("b").corrupt_crc = "AAAAAA=="
    store = GcsMediaStore("b", "p/x", client_factory=lambda: client)
    with pytest.raises(MediaStoreIntegrityError) as err:
        store.commit(_temp(tmp_path), KEY, CRC)
    assert err.value.local_crc32c == CRC and err.value.remote_crc32c == "AAAAAA=="
    assert CRC in str(err.value) and "AAAAAA==" in str(err.value)
    assert f"p/x/{KEY}" in client.bucket("b").objects  # no delete path


def test_gcs_precondition_failed_is_a_lost_race_not_an_error(tmp_path):
    client = FakeGcsClient()
    client.bucket("b").upload_error = PreconditionFailed("exists")
    store = GcsMediaStore("b", "p/x", client_factory=lambda: client)
    assert store.commit(_temp(tmp_path), KEY, CRC) is False


def test_gcs_transport_failure_is_a_store_error(tmp_path):
    client = FakeGcsClient()
    client.bucket("b").upload_error = ServiceUnavailable("down")
    store = GcsMediaStore("b", "p/x", client_factory=lambda: client)
    with pytest.raises(MediaStoreError):
        store.commit(_temp(tmp_path), KEY, CRC)


def test_gcs_client_is_built_lazily(tmp_path):
    def boom():
        raise AssertionError("client must not be built before first use")

    GcsMediaStore("b", "p/x", client_factory=boom)  # constructing is free


def test_build_media_store_selects_by_setting(tmp_path, monkeypatch):
    local = build_media_store(load_settings("default", {"data_dir": tmp_path}), "default")
    assert local.store_id == "local"

    def boom():
        raise AssertionError("the default factory must not run for a fake")

    monkeypatch.setattr("paperboy.media_store.default_client_factory", boom)
    s = load_settings(
        "default",
        {"data_dir": tmp_path, "media_store": "gs://bkt/pre/fix", "media_store_buckets": "bkt"},
    )
    gcs = build_media_store(s, "default")
    assert gcs.store_id == "gs://bkt/pre/fix"  # building is lazy: boom is not called


def test_default_factory_is_used_when_none_given(tmp_path, monkeypatch):
    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    s = load_settings(
        "default",
        {"data_dir": tmp_path, "media_store": "gs://bkt/p", "media_store_buckets": "bkt"},
    )
    store = build_media_store(s, "default")
    assert store.exists(KEY) is False
    assert client.bucket("bkt").calls["exists"] == 1


def test_stored_in_reads_custody_store():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE custody_log (id INTEGER PRIMARY KEY, path TEXT, sha256 TEXT, "
        "recorded_at TEXT, source_message_uri TEXT, store TEXT NOT NULL DEFAULT 'local')"
    )
    conn.execute("INSERT INTO custody_log (path, sha256, recorded_at) VALUES ('k', 's', 'n')")
    conn.execute(
        "INSERT INTO custody_log (path, sha256, recorded_at, store) VALUES ('k', 't', 'n', 'gs://b/p')"
    )
    assert stored_in(conn, "s", "local") and not stored_in(conn, "s", "gs://b/p")
    assert stored_in(conn, "t", "gs://b/p") and not stored_in(conn, "t", "local")
