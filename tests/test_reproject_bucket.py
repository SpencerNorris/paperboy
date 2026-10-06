"""Reproject of a bucket run reads media back from the bucket, read-only (#63)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paperboy.cli import app
from tests.fake_gcs import FakeGcsClient
from tests.test_reproject import assert_round_trip
from tests.test_reproject_parity import run_full_collect
from tests.test_reproject_people import run_people_collect

runner = CliRunner()
URL = "gs://bkt/p/x"
BUCKET_OVER = {"media_store": URL, "media_store_buckets": "bkt"}


def _rows(db: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        return [tuple(r) for r in conn.execute(sql)]


@pytest.fixture
def bucket_source(tmp_path, monkeypatch):
    """A full collect whose media went to the fake bucket (no local media files)."""
    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    db = asyncio.run(run_full_collect(tmp_path, settings_over=BUCKET_OVER))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE_BUCKETS", "bkt")
    return db, client


def _local_media(tmp_path: Path) -> list[Path]:
    media = tmp_path / "default" / "media"
    return [p for p in media.rglob("*") if p.is_file()] if media.exists() else []


def test_reproject_of_bucket_run_rebuilds_store_rows(tmp_path, monkeypatch, bucket_source):
    db, client = bucket_source
    bucket = client.bucket("bkt")
    assert bucket.objects and _local_media(tmp_path) == []  # the fixture really used the bucket
    uploads = bucket.calls["upload"]
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"

    result = runner.invoke(app, ["reproject", "--profile", "default"])

    assert result.exit_code == 0, result.output
    assert_round_trip(db, out)  # raw_records (incl. the MediaStore marker), media, custody_log
    assert _rows(out, "SELECT DISTINCT store FROM custody_log") == [(URL,)]
    assert _rows(out, "SELECT count(*) FROM raw_records WHERE kind='MediaStore'") == [(1,)]
    receipts = _rows(out, "SELECT payload_json FROM raw_records WHERE kind='MediaDownload'")
    assert receipts and all(json.loads(r[0])["store"] == URL for r in receipts)
    # Read-only against the bucket: reads happened, nothing was written or deleted.
    assert bucket.calls["open"] > 0
    assert bucket.calls["upload"] == uploads and bucket.calls["delete"] == 0
    assert _local_media(tmp_path) == []  # a plain reproject writes no bytes anywhere


def test_reproject_of_bucket_run_outside_the_allow_list_skips_files(
    tmp_path, monkeypatch, bucket_source
):
    _, client = bucket_source
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE_BUCKETS", "someone-else")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert client.bucket("bkt").calls["open"] == 0  # never fetched
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert _rows(out, "SELECT count(*) FROM media") == [(0,)]


def test_reproject_with_a_local_source_never_builds_a_gcs_client(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))

    def _forbidden():
        raise AssertionError("reproject of a local source touched the GCS client")

    monkeypatch.setattr("paperboy.media_store.default_client_factory", _forbidden)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE", URL)  # even a configured store is not used
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE_BUCKETS", "bkt")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output


def test_out_profile_copies_bucket_media_into_local_profile(tmp_path, monkeypatch, bucket_source):
    _, client = bucket_source
    uploads = client.bucket("bkt").calls["upload"]
    result = runner.invoke(
        app, ["reproject", "--profile", "default", "--out-profile", "copy"]
    )
    assert result.exit_code == 0, result.output
    copied = [
        p for p in (tmp_path / "copy" / "media").rglob("*")
        if p.is_file() and ".incoming" not in p.parts
    ]
    assert copied  # the bucket bytes landed in the local output profile
    db = tmp_path / "copy" / "paperboy.sqlite"
    assert _rows(db, "SELECT DISTINCT store FROM custody_log") == [("local",)]
    assert _rows(db, "SELECT count(*) FROM raw_records WHERE kind='MediaStore'") == [(0,)]
    assert client.bucket("bkt").calls["upload"] == uploads  # copying never writes to a bucket


def _reproject_default(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE_BUCKETS", "bkt")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    return tmp_path / "default" / "paperboy.reprojected.sqlite"


def test_bucket_run_with_avatars_and_media_phase_reprojects_faithfully(tmp_path, monkeypatch):
    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    db = asyncio.run(run_people_collect(tmp_path, settings_over=BUCKET_OVER, with_media=True))
    markers = _rows(db, "SELECT payload_json FROM raw_records WHERE kind='MediaStore'")
    assert [json.loads(m[0]) for m in markers] == [{"store": URL}]
    out = _reproject_default(tmp_path, monkeypatch)
    assert_round_trip(db, out)
    assert _rows(out, "SELECT DISTINCT store FROM custody_log") == [(URL,)]
    assert client.bucket("bkt").calls["delete"] == 0


def test_avatar_refetched_into_a_bucket_is_reproduced_on_replay(tmp_path, monkeypatch):
    """Run 1 stores the avatar locally; run 2 (bucket) fetches the same avatar again,
    leaving a custody row and receipt that replay must reproduce, not skip as known."""
    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    asyncio.run(run_people_collect(tmp_path))
    db = asyncio.run(run_people_collect(tmp_path, settings_over=BUCKET_OVER))
    assert {r[0] for r in _rows(db, "SELECT store FROM custody_log")} == {"local", URL}
    out = _reproject_default(tmp_path, monkeypatch)
    assert_round_trip(db, out)
    assert {r[0] for r in _rows(out, "SELECT store FROM custody_log")} == {"local", URL}


def test_avatar_only_bucket_run_reprojects_with_bucket_custody(tmp_path, monkeypatch):
    """A bucket run with `profiles` but no `media` phase still records which store
    its avatars went to, so a reproject (and a reproject of that) keeps the
    bucket as custody and the receipt's `store` (review of #63)."""

    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    db = asyncio.run(run_people_collect(tmp_path, settings_over=BUCKET_OVER))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAPERBOY_MEDIA_STORE_BUCKETS", "bkt")
    assert client.bucket("bkt").objects  # the avatar really went to the bucket
    assert _rows(db, "SELECT DISTINCT store FROM custody_log") == [(URL,)]
    markers = _rows(db, "SELECT payload_json FROM raw_records WHERE kind='MediaStore'")
    assert [json.loads(m[0]) for m in markers] == [{"store": URL, "media": False}]

    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db, out)
    assert _rows(out, "SELECT DISTINCT store FROM custody_log") == [(URL,)]
    receipts = _rows(out, "SELECT payload_json FROM raw_records WHERE kind='AvatarDownload'")
    assert receipts and all(json.loads(r[0])["store"] == URL for r in receipts)
    # The marker must not make replay invent a media phase.
    assert _rows(out, "SELECT count(*) FROM raw_records WHERE kind='MediaDownload'") == [(0,)]
