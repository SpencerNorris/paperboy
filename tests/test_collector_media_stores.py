"""The media collector against a bucket store vs the local store (#63, ADR-0008)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from google.api_core.exceptions import ServiceUnavailable

from paperboy.budget import PhaseStop
from paperboy.collectors.base import ChannelContext
from paperboy.collectors.media import MediaCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel_with_context
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fake_gcs import FakeGcsClient, crc32c_b64
from tests.fakes import FakeGateway
from tests.test_collector_media import CHANNEL_ID, _ctx, _doc_msg, _seed

URL = "gs://bkt/p/x"


def _bucket_settings(tmp_path: Path, monkeypatch, client: FakeGcsClient):
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    return load_settings(
        "default",
        {"data_dir": tmp_path, "media_store": URL, "media_store_buckets": "bkt"},
    )


def _local_settings(tmp_path: Path):
    return load_settings("default", {"data_dir": tmp_path})


def _key(data: bytes, ext: str = ".pdf") -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    return sha, f"media/{sha[:2]}/{sha}{ext}"


def _payload(st: Store) -> list[dict]:
    rows = st.conn.execute(
        "SELECT payload_json FROM raw_records WHERE kind='MediaDownload'"
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _local_files(tmp_path: Path) -> list[Path]:
    media = tmp_path / "p" / "media"
    return sorted(p for p in media.rglob("*") if p.is_file()) if media.exists() else []


@pytest.mark.asyncio
async def test_bucket_run_uploads_and_keeps_no_local_copy(tmp_path, monkeypatch):
    data = b"%PDF bucket document"
    sha, key = _key(data)
    client = FakeGcsClient()
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    gw = FakeGateway({"media": {1: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))
        assert res.counts["downloaded"] == 1
        assert client.bucket("bkt").objects == {f"p/x/{key}": data}
        row = st.conn.execute("SELECT * FROM media WHERE sha256=?", (sha,)).fetchone()
        assert row["path"] == key  # the store-neutral key, never a URL or path
        custody = st.conn.execute("SELECT * FROM custody_log").fetchall()
        assert [(c["path"], c["store"]) for c in custody] == [(key, URL)]
        assert _payload(st)[0]["store"] == URL
    assert _local_files(tmp_path) == []  # no local copy, .incoming is empty
    assert list((tmp_path / "p" / "media" / ".incoming").iterdir()) == []


@pytest.mark.asyncio
async def test_local_run_receipt_has_no_store_key_and_custody_says_local(tmp_path):
    data = b"local doc"
    gw = FakeGateway({"media": {1: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        await MediaCollector().collect(_ctx(st, gw, _local_settings(tmp_path)))
        assert "store" not in _payload(st)[0]  # absence means local (ADR-0008)
        assert st.conn.execute("SELECT store FROM custody_log").fetchone()["store"] == "local"


@pytest.mark.asyncio
async def test_file_from_local_run_is_downloaded_again_in_bucket_run(tmp_path, monkeypatch):
    data = b"first stored locally"
    sha, key = _key(data)
    client = FakeGcsClient()
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        await MediaCollector().collect(
            _ctx(st, FakeGateway({"media": {1: data}}), _local_settings(tmp_path))
        )
        gw = FakeGateway({"media": {1: data}})
        res = await MediaCollector().collect(
            _ctx(st, gw, _bucket_settings(tmp_path, monkeypatch, client))
        )
        assert gw.download_media_calls == [1]  # the DB has it, the bucket does not
        assert res.counts["downloaded"] == 1 and res.counts["duplicates"] == 0
        assert client.bucket("bkt").calls["upload"] == 1
        assert st.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1  # no 2nd row
        stores = [r["store"] for r in st.conn.execute("SELECT store FROM custody_log ORDER BY id")]
        assert stores == ["local", URL]
        assert [p.get("store") for p in _payload(st)] == [None, URL]
        assert client.bucket("bkt").objects == {f"p/x/{key}": data}
        assert sha in key


@pytest.mark.asyncio
async def test_file_already_in_bucket_is_duplicate_without_upload(tmp_path, monkeypatch):
    data = b"already in the bucket"
    sha, key = _key(data)
    client = FakeGcsClient()
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    with Store.open(tmp_path / "p.sqlite") as st:
        # A local-run row for message 1 exists; the bucket holds the object by hand.
        _seed(st, _doc_msg(1))
        await MediaCollector().collect(
            _ctx(st, FakeGateway({"media": {1: data}}), _local_settings(tmp_path))
        )
        client.bucket("bkt").objects[f"p/x/{key}"] = data
        gw = FakeGateway({"media": {1: data}})
        res = await MediaCollector().collect(_ctx(st, gw, settings))
        assert res.counts["duplicates"] == 1 and res.counts["downloaded"] == 0
        assert gw.download_media_calls == []
        assert client.bucket("bkt").calls["exists"] >= 1
        assert client.bucket("bkt").calls["upload"] == 0
        last = st.conn.execute("SELECT * FROM custody_log ORDER BY id DESC").fetchone()
        assert (last["store"], last["sha256"]) == (URL, sha)
        assert len(_payload(st)) == 1  # custody only: no new receipt


@pytest.mark.asyncio
async def test_custody_fast_path_skips_the_head(tmp_path, monkeypatch):
    data = b"bucket run twice"
    client = FakeGcsClient()
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        await MediaCollector().collect(_ctx(st, FakeGateway({"media": {1: data}}), settings))
        before = dict(client.bucket("bkt").calls)
        gw = FakeGateway({"media": {1: data}})
        res = await MediaCollector().collect(_ctx(st, gw, settings))
        assert res.counts["duplicates"] == 1
        assert client.bucket("bkt").calls == before  # a custody row names the bucket: no GET
        assert gw.download_media_calls == []


@pytest.mark.asyncio
async def test_crc_mismatch_skips_file_writes_no_rows_logs_both_digests(
    tmp_path, monkeypatch, caplog
):
    data = b"bad crc"
    _, key = _key(data)
    client = FakeGcsClient()
    client.bucket("bkt").corrupt_crc = "AAAAAA=="
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    gw = FakeGateway({"media": {1: data}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        with caplog.at_level(logging.ERROR):
            res = await MediaCollector().collect(_ctx(st, gw, settings))
        assert res.counts["skipped"] == 1 and res.counts["downloaded"] == 0
        for table in ("media", "custody_log"):
            assert st.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert _payload(st) == []
    errors = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert crc32c_b64(data) in errors and "AAAAAA==" in errors
    assert f"p/x/{key}" in client.bucket("bkt").objects  # left in place: no delete path
    assert list((tmp_path / "p" / "media" / ".incoming").iterdir()) == []


@pytest.mark.asyncio
async def test_store_transport_error_is_a_phase_stop_not_a_retry(tmp_path, monkeypatch):
    client = FakeGcsClient()
    client.bucket("bkt").upload_error = ServiceUnavailable("down")
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    gw = FakeGateway({"media": {1: b"one", 2: b"two"}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=1))
        _seed(st, _doc_msg(2, doc_id=2))
        with pytest.raises(PhaseStop, match="media store"):
            await MediaCollector().collect(_ctx(st, gw, settings))
        assert gw.download_media_calls == [1]  # the phase stopped; nothing was retried
        assert st.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert list((tmp_path / "p" / "media" / ".incoming").iterdir()) == []


@pytest.mark.asyncio
async def test_lost_create_race_continues_as_downloaded(tmp_path, monkeypatch):
    data = b"raced"
    _, key = _key(data)
    client = FakeGcsClient()
    client.bucket("bkt").race_once = True
    settings = _bucket_settings(tmp_path, monkeypatch, client)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(
            _ctx(st, FakeGateway({"media": {1: data}}), settings)
        )
        assert res.counts["downloaded"] == 1
        assert st.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
        assert _payload(st)[0]["store"] == URL
    assert client.bucket("bkt").objects == {f"p/x/{key}": data}


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_run", [True, False])
async def test_run_marker_written_only_for_bucket_runs(tmp_path, monkeypatch, bucket_run):
    client = FakeGcsClient()
    settings = (
        _bucket_settings(tmp_path, monkeypatch, client)
        if bucket_run
        else _local_settings(tmp_path)
    )
    gw = FakeGateway({"media": {1: b"x"}})
    ctx_in = ChannelContext(
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, "stranger", "r0"
    )
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        await collect_channel_with_context(
            gw, st, settings, parse_target("@x"), ["media"], logging.getLogger("t"),
            collectors=[MediaCollector()], profile="p", channel_context=ctx_in,
        )
        markers = [
            json.loads(r["payload_json"])
            for r in st.conn.execute("SELECT payload_json FROM raw_records WHERE kind='MediaStore'")
        ]
        assert markers == ([{"store": URL}] if bucket_run else [])
