"""`MediaCollector(outcomes=...)`: a per-message outcome for every row it considers (#68)."""

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.collectors.media import DiskFloorStop, MediaCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from tests.fakes import FakeGateway
from tests.test_collector_media import _ctx, _doc_msg, _seed, _settings, _sized_doc


@pytest.mark.asyncio
async def test_outcomes_dict_records_every_row(tmp_path):
    settings = load_settings("default", {"data_dir": tmp_path, "media_max_mb": 1})
    gw = FakeGateway({
        "media": {
            1: b"one", 3: SkipAndRecord("file reference expired"), 5: None,
        }
    })
    outcomes: dict[str, str] = {}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=1))               # downloaded
        _seed(st, _doc_msg(2, doc_id=1))               # same document id: duplicate
        _seed(st, _doc_msg(3, doc_id=3))               # per-file skip
        _seed(st, _sized_doc(4, 1_000_001))            # too_large
        _seed(st, _doc_msg(5, doc_id=5))               # unavailable
        _seed(st, {
            "_": "message", "id": 6, "message": "", "date": 1767322445,
            "media": {"_": "MessageMediaGeo", "geo": {}},
        })                                             # nothing to download
        res = await MediaCollector(outcomes=outcomes).collect(_ctx(st, gw, settings))
    assert outcomes == {
        "tg:msg:5/1": "downloaded",
        "tg:msg:5/2": "duplicate",
        "tg:msg:5/3": "skipped",
        "tg:msg:5/4": "too_large",
        "tg:msg:5/5": "unavailable",
        "tg:msg:5/6": "skipped",
    }
    assert res.counts["downloaded"] == 1


@pytest.mark.asyncio
async def test_outcomes_survive_disk_floor_stop(tmp_path, monkeypatch):
    import paperboy.collectors.media as media_mod

    calls = {"n": 0}

    def free(_root):
        # Plenty of space for the first file, then the disk "fills".
        calls["n"] += 1
        return 10**12 if calls["n"] == 1 else 1

    monkeypatch.setattr(media_mod, "_free_bytes", free)
    outcomes: dict[str, str] = {}
    gw = FakeGateway({"media": {1: b"first", 2: b"second"}})
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _doc_msg(1, doc_id=1))
        _seed(st, _doc_msg(2, doc_id=2))
        with pytest.raises(DiskFloorStop):
            await MediaCollector(outcomes=outcomes).collect(_ctx(st, gw, _settings(tmp_path)))
    assert outcomes == {"tg:msg:5/1": "downloaded"}
    assert gw.download_media_calls == [1]
