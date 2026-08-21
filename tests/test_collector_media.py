import hashlib
import logging

import pytest

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.collectors.media import MediaCollector
from paperboy.config import load_settings
from paperboy.ids import utc_now_iso
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5


def _doc_msg(msg_id, *, doc_id=111, mime="application/pdf", file_name="report.pdf", **extra):
    m = {
        "_": "message",
        "id": msg_id,
        "message": "",
        "date": 1767322445,
        "media": {
            "_": "MessageMediaDocument",
            "document": {
                "_": "Document",
                "id": doc_id,
                "access_hash": 222,
                "mime_type": mime,
                "attributes": [{"_": "DocumentAttributeFilename", "file_name": file_name}],
            },
        },
    }
    m.update(extra)
    return m


def _photo_msg(msg_id, *, photo_id=333):
    return {
        "_": "message",
        "id": msg_id,
        "message": "",
        "date": 1767322445,
        "media": {
            "_": "MessageMediaPhoto",
            "photo": {"_": "Photo", "id": photo_id, "access_hash": 444},
        },
    }


def _text_msg(msg_id):
    return {"_": "message", "id": msg_id, "message": f"m{msg_id}", "date": 1767322445}


def _seed(store, msg, channel_id=CHANNEL_ID, tier="stranger"):
    raw_id = store.add_raw(msg.get("_", "Message"), msg, tier, {"channel_id": channel_id})
    return upsert_message(store, channel_id, msg, raw_id, utc_now_iso(), tier)


def _settings(tmp_path):
    return load_settings("default", {"data_dir": tmp_path})


def _ctx(st, gw, settings, channel_id=CHANNEL_ID, tier="stranger", profile="p"):
    return CollectContext(
        gw, st, settings, parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, tier,
        logging.getLogger("t"), profile,
    )


@pytest.mark.asyncio
async def test_document_downloads_hashes_and_records_media_and_custody(tmp_path):
    data = b"%PDF-1.4 fake document bytes"
    sha = hashlib.sha256(data).hexdigest()
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {1: data}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 1
        assert gw.download_media_calls == [1]

        expected_path = tmp_path / "p" / "media" / sha[:2] / f"{sha}.pdf"
        assert expected_path.exists()
        assert expected_path.read_bytes() == data

        row = st.conn.execute("SELECT * FROM media WHERE sha256=?", (sha,)).fetchone()
        assert row is not None
        assert row["message_uri"] == "tg:msg:5/1"
        assert row["kind"] == "document"
        assert row["mime_type"] == "application/pdf"
        assert row["file_name"] == "report.pdf"
        assert row["size"] == len(data)
        assert row["path"] == str(expected_path)
        assert row["downloaded_at"] is not None
        assert row["exif_json"] is None

        custody = st.conn.execute(
            "SELECT * FROM custody_log WHERE sha256=?", (sha,)
        ).fetchall()
        assert len(custody) == 1
        assert custody[0]["source_message_uri"] == "tg:msg:5/1"
        assert custody[0]["path"] == str(expected_path)

        raw_kinds = [
            r["kind"] for r in st.conn.execute("SELECT kind FROM raw_records").fetchall()
        ]
        assert "MediaDownload" in raw_kinds


@pytest.mark.asyncio
async def test_photo_recorded_as_kind_photo_with_jpg_extension(tmp_path):
    data = b"\xff\xd8\xff fake jpeg bytes"
    sha = hashlib.sha256(data).hexdigest()
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {1: data}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _photo_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 1
        row = st.conn.execute("SELECT * FROM media WHERE sha256=?", (sha,)).fetchone()
        assert row["kind"] == "photo"
        assert row["mime_type"] == "image/jpeg"
        assert row["path"].endswith(".jpg")


@pytest.mark.asyncio
async def test_message_with_no_media_is_skipped(tmp_path):
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _text_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 0
        assert gw.download_media_calls == []
        assert st.conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_non_downloadable_media_kind_is_skipped(tmp_path):
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {}})
    webpage_msg = {
        "_": "message", "id": 1, "message": "link", "date": 1767322445,
        "media": {"_": "MessageMediaWebPage", "webpage": {"_": "WebPage", "id": 9}},
    }

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, webpage_msg)
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 0
        assert res.counts["skipped_kind"] == 1
        assert gw.download_media_calls == []


@pytest.mark.asyncio
async def test_duplicate_content_id_not_redownloaded(tmp_path):
    # Two different messages, same underlying Telegram document id (a
    # repost/forward of the identical file) — the second should be
    # recognized as a duplicate *before* any network call, purely from the
    # document id already embedded in its stored `media_json`.
    data = b"shared file bytes"
    sha = hashlib.sha256(data).hexdigest()
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {1: data}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=999, file_name="a.bin"))
        _seed(st, _doc_msg(2, doc_id=999, file_name="a.bin"))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 1
        assert res.counts["duplicates"] == 1
        assert gw.download_media_calls == [1]  # msg 2 never reached the gateway

        assert st.conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 1
        custody = st.conn.execute(
            "SELECT source_message_uri FROM custody_log WHERE sha256=? ORDER BY id", (sha,)
        ).fetchall()
        assert [c["source_message_uri"] for c in custody] == ["tg:msg:5/1", "tg:msg:5/2"]


@pytest.mark.asyncio
async def test_duplicate_sha_across_distinct_content_ids_is_safety_netted(tmp_path):
    # Two messages with *different* document ids whose bytes happen to hash
    # identically — the content-id fast path misses this, but the
    # post-download sha256 check still catches it and dedupes.
    data = b"identical bytes despite different document ids"
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {1: data, 2: data}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=111, file_name="a.bin"))
        _seed(st, _doc_msg(2, doc_id=222, file_name="b.bin"))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 1
        assert res.counts["duplicates"] == 1
        assert gw.download_media_calls == [1, 2]  # both were fetched...
        n_media = st.conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"]
        assert n_media == 1  # ...but deduped by sha256 afterward


@pytest.mark.asyncio
async def test_unavailable_media_is_counted_not_errored(tmp_path):
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {}})  # download_media returns None for msg 1

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1))
        res = await MediaCollector().collect(_ctx(st, gw, settings))

        assert res.counts["downloaded"] == 0
        assert res.counts["unavailable"] == 1
        assert st.conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_cross_run_dedup_uses_persisted_media_table(tmp_path):
    # A second, separate `collect()` call (simulating a fresh CLI run) must
    # not re-download a file already recorded in `media` for a *different*
    # message with the same document id.
    data = b"already downloaded last run"
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {1: data}})

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=555, file_name="a.bin"))
        await MediaCollector().collect(_ctx(st, gw, settings))

        _seed(st, _doc_msg(2, doc_id=555, file_name="a.bin"))
        gw2 = FakeGateway({"media": {2: data}})
        res2 = await MediaCollector().collect(_ctx(st, gw2, settings))

        assert res2.counts["downloaded"] == 0
        # Both msg 1 (an idempotent re-check of its own already-downloaded
        # file) and msg 2 (the new repost sharing its document id) resolve
        # via the persisted content-index, never touching the gateway.
        assert res2.counts["duplicates"] == 2
        assert gw2.download_media_calls == []
        assert st.conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 1


def test_applies_to_channel_like_targets():
    assert MediaCollector().applies_to(parse_target("@durov"))
    assert not MediaCollector().applies_to(parse_target("#osint"))


@pytest.mark.asyncio
async def test_collect_raises_phase_stop_when_channel_context_unset(tmp_path):
    settings = _settings(tmp_path)
    gw = FakeGateway({"media": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, settings, parse_target("@x"), None, None, "stranger",
            logging.getLogger("t"), "p",
        )
        with pytest.raises(PhaseStop):
            await MediaCollector().collect(ctx)
