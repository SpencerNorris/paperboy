"""`fetch_media` driver: ordered cross-channel fetch, resume, stops, report (#68).

Synthetic channels/messages only.
"""

import csv
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperboy.budget import HardStop, SkipAndRecord
from paperboy.config import load_settings
from paperboy.fetch_media import fetch_media
from paperboy.media_list import classify_rows, parse_media_list
from paperboy.store.db import Store
from tests.fakes import FakeGateway
from tests.test_media_list import seed_channel, seed_msg

LOG = logging.getLogger("t")


def _resolved(cid, username):
    chan = {
        "_": "channel", "id": cid, "access_hash": cid * 10, "title": "T",
        "username": username, "broadcast": True,
    }
    return {
        "_": "contacts.resolvedPeer",
        "peer": {"_": "PeerChannel", "channel_id": cid}, "chats": [chan], "users": [],
    }


def _full(cid, username):
    return {
        "_": "messages.chatFull",
        "full_chat": {"_": "channelFull", "id": cid, "participants_count": 1, "pts": 1,
                      "linked_chat_id": 0},
        "chats": _resolved(cid, username)["chats"], "users": [],
    }


def _gateway(media, **extra):
    fx = {
        "self": {"_": "user", "id": 1, "self": True},
        "resolve_by_target": {
            "chan_a": _resolved(10, "chan_a"), "chan_b": _resolved(20, "chan_b"),
        },
        "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
        "media": media,
    }
    fx.update(extra)
    return FakeGateway(fx)


def _settings(tmp_path):
    return load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})


def _seed_store(st):
    seed_channel(st, 10, "chan_a")
    seed_channel(st, 20, "chan_b")
    for ch, mid in [(10, 1), (10, 2), (10, 3), (20, 11), (20, 12)]:
        seed_msg(st, ch, mid, photo_id=ch * 100 + mid)


LIST = (
    "uri,priority\n"
    "tg:msg:10/1,P1\n"
    "tg:msg:20/11,P1\n"
    "tg:msg:10/2,P2\n"
    "tg:msg:20/12,P2\n"
    "tg:msg:10/3,P2\n"
)
BYTES = {1: b"a1", 2: b"a2", 3: b"a3", 11: b"b11", 12: b"b12"}


def _classified(st, tmp_path, text=LIST):
    path = tmp_path / "list.csv"
    path.write_text(text, encoding="utf-8")
    return classify_rows(st, parse_media_list(path))


def _report(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.mark.asyncio
async def test_end_to_end_two_channels_two_tiers(tmp_path):
    gw = _gateway(BYTES)
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
        assert summary.complete and summary.stop_reason is None
        assert summary.counts["downloaded"] == 5
        assert summary.bytes_downloaded == sum(len(b) for b in BYTES.values())
        # Segment order: P1(A), P1(B), P2(A: 2,3), P2(B: 12)
        assert gw.download_media_calls == [1, 11, 2, 3, 12]
        # One resolve per channel per run, not per segment.
        assert gw.calls.count("resolve") == 2
        markers = st.conn.execute(
            "SELECT run_id FROM raw_records WHERE kind='ChannelContextReused'"
        ).fetchall()
        assert len(markers) == 2  # the two P2 segments reused their channel
        assert len({m["run_id"] for m in markers}) == 2
        rows = _report(report)
        assert [r["line_no"] for r in rows] == ["2", "3", "4", "5", "6"]
        assert [r["outcome"] for r in rows] == ["downloaded"] * 5
        assert [r["uri"] for r in rows][0] == "tg:msg:10/1"
        for r in rows:
            assert len(r["sha256"]) == 64 and r["key"].startswith("media/")
            assert (tmp_path / "p" / r["key"]).is_file()


@pytest.mark.asyncio
async def test_second_run_is_already_stored_with_no_gateway(tmp_path):
    report1, report2 = tmp_path / "r1.csv", tmp_path / "r2.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_media(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report1,
        )
        summary = await fetch_media(
            None, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report2,
        )
        assert summary.complete
        assert summary.counts["already_stored"] == 5
        first, second = _report(report1), _report(report2)
        assert [r["outcome"] for r in second] == ["already_stored"] * 5
        assert [(r["sha256"], r["key"]) for r in second] == [
            (r["sha256"], r["key"]) for r in first
        ]


@pytest.mark.asyncio
async def test_hard_stop_marks_rest_not_attempted(tmp_path):
    gw = _gateway({**BYTES, 11: HardStop("boom")})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
        assert not summary.complete and summary.stop_reason == "hard_stop"
        assert [r["outcome"] for r in _report(report)] == [
            "downloaded", "not_attempted", "not_attempted", "not_attempted", "not_attempted",
        ]
        assert gw.download_media_calls == [1, 11]


@pytest.mark.asyncio
async def test_disk_floor_ends_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: SimpleNamespace(total=10**12, used=0, free=1)
    )
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 1})
    gw = _gateway(BYTES)
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, settings, _classified(st, tmp_path), LOG,
            profile="p", report_path=tmp_path / "r.csv",
        )
    assert not summary.complete
    assert "DiskFloorStop" in (summary.stop_reason or "")
    assert gw.download_media_calls == []
    assert gw.calls.count("resolve") == 1  # never went on to the next channel


@pytest.mark.asyncio
async def test_channel_phase_skip_continues_with_other_channels(tmp_path):
    gw = _gateway(BYTES, full_channel_by_id={
        10: SkipAndRecord("channel is private"), 20: _full(20, "chan_b"),
    })
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    assert not summary.complete and summary.stop_reason is None
    assert [r["outcome"] for r in _report(report)] == [
        "not_attempted", "downloaded", "not_attempted", "downloaded", "not_attempted",
    ]
    # Channel A was tried once; its later segment was not re-resolved.
    assert gw.calls.count("resolve") == 2
    assert gw.download_media_calls == [11, 12]


@pytest.mark.asyncio
async def test_handle_resolving_to_another_channel_fetches_nothing(tmp_path):
    gw = _gateway(BYTES, resolve_by_target={
        "chan_a": _resolved(99, "chan_a"), "chan_b": _resolved(20, "chan_b"),
    }, full_channel_by_id={99: _full(99, "chan_a"), 20: _full(20, "chan_b")})
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=tmp_path / "r.csv",
        )
    assert gw.download_media_calls == [11, 12]  # nothing under the wrong channel
    assert summary.counts["not_attempted"] == 3


@pytest.mark.asyncio
async def test_report_written_on_unexpected_error(tmp_path):
    report = tmp_path / "r.csv"

    class Boom(Exception):
        pass

    gw = _gateway({**BYTES, 11: Boom("bug")})
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        with pytest.raises(Boom):
            await fetch_media(
                gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
                profile="p", report_path=report,
            )
    assert [r["outcome"] for r in _report(report)][:2] == ["downloaded", "not_attempted"]
