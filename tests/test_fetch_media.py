"""`fetch_media` driver: ordered cross-channel fetch, resume, stops, report (#68).

Synthetic channels/messages only.
"""

import csv
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperboy.budget import HardStop, PhaseStop, SkipAndRecord
from paperboy.config import load_settings
from paperboy.fetch_media import fetch_media
from paperboy.media_list import classify_rows, parse_media_list
from paperboy.media_store import LocalMediaStore
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
        "resolve": AssertionError("no handle lookup: list rows are fetched by id"),
        "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
        "media": media,
    }
    fx.update(extra)
    return FakeGateway(fx)


def _settings(tmp_path):
    return load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})


def _seed_store(st):
    seed_channel(st, 10, "chan_a", access_hash=100)
    seed_channel(st, 20, "chan_b", access_hash=200)
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
    return classify_rows(
        st, parse_media_list(path), media_store=LocalMediaStore(tmp_path / "p")
    )


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
        # By id: no handle lookup at all, and the key is fetched once per channel.
        assert "resolve" not in gw.calls
        assert [i["channel_id"] for i in gw.full_channel_inputs] == [10, 20]
        access = [
            json.loads(r["payload_json"]) for r in st.conn.execute(
                "SELECT payload_json FROM raw_records WHERE kind='ChannelAccess' ORDER BY id"
            )
        ]
        assert [(a["channel_id"], a["via"], a["granted"]) for a in access] == [
            (10, "saved_key", True), (20, "saved_key", True),
        ]
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
    assert gw.calls.count("get_full_channel") == 1  # never went on to the next channel


@pytest.mark.asyncio
async def test_channel_phase_skip_continues_with_other_channels(tmp_path):
    # The saved key is rejected, so Step A falls through to the stored handle
    # (route 3), which resolves to 10 again and is rejected too.
    gw = _gateway(BYTES, resolve=_resolved(10, "chan_a"), full_channel_by_id={
        10: SkipAndRecord("channel is private"), 20: _full(20, "chan_b"),
    })
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    rows = _report(report)
    assert [r["outcome"] for r in rows] == [
        "no_access", "downloaded", "no_access", "downloaded", "no_access",
    ]
    assert "cannot get access to channel 10" in rows[0]["reason"] and rows[1]["reason"] == ""
    assert summary.complete  # every row has a final outcome
    # Channel A's routes were tried once; its later segment was not retried.
    assert [i["channel_id"] for i in gw.full_channel_inputs] == [10, 10, 20]
    assert gw.download_media_calls == [11, 12]


@pytest.mark.asyncio
async def test_stored_handle_now_elsewhere_still_fetches_by_id(tmp_path):
    """The stored handle of channel 10 now belongs to channel 6, but the run
    holds a saved key for 10: it goes by id and never looks the handle up."""
    gw = _gateway(BYTES, resolve=_resolved(6, "chan_a"))
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=tmp_path / "r.csv",
        )
    assert summary.complete and summary.counts["downloaded"] == 5
    assert "resolve" not in gw.calls
    assert gw.download_media_calls == [1, 11, 2, 3, 12]


@pytest.mark.asyncio
async def test_segment_without_any_route_is_no_access_and_the_run_continues(
    tmp_path, caplog
):
    """Channel 10 has no saved key, no from-message key and no stored handle:
    route 4. Its rows are `no_access`; channel 20 still downloads."""
    gw = _gateway(BYTES)
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        seed_channel(st, 10, None)
        seed_channel(st, 20, "chan_b", access_hash=200)
        for ch, mid in [(10, 1), (10, 2), (10, 3), (20, 11), (20, 12)]:
            seed_msg(st, ch, mid, photo_id=ch * 100 + mid)
        with caplog.at_level(logging.WARNING):
            summary = await fetch_media(
                gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
                profile="p", report_path=report,
            )
    assert summary.complete and summary.stop_reason is None
    rows = _report(report)
    assert [r["outcome"] for r in rows] == [
        "no_access", "downloaded", "no_access", "downloaded", "no_access",
    ]
    assert gw.download_media_calls == [11, 12]
    assert "resolve" not in gw.calls
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "no_access" in r.getMessage()
    ]
    assert len(warnings) == 1 and "channel=10" in warnings[0].getMessage()
    assert rows[0]["reason"] and rows[0]["reason"] == rows[2]["reason"]


@pytest.mark.asyncio
async def test_live_collector_list_is_the_standard_one(tmp_path, monkeypatch):
    """No fetch-media-only collector: the live run uses `channel` + `media`."""
    from paperboy import fetch_media as fm
    from paperboy.collectors.channel import ChannelCollector
    from paperboy.collectors.media import MediaCollector

    seen = []
    real = fm.collect_channel_with_context

    async def spy(*args, **kwargs):
        seen.append(kwargs["collectors"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(fm, "collect_channel_with_context", spy)
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_media(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=tmp_path / "r.csv",
        )
    assert seen and all(
        [type(c) for c in cs] == [ChannelCollector, MediaCollector] for cs in seen
    )


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


@pytest.mark.asyncio
async def test_unexpected_error_mid_segment_keeps_earlier_rows_downloaded(tmp_path):
    """The crash hits the second row of the P2/chan_a segment (msgs 2, 3): row 2
    was already downloaded and recorded, so the report must say so."""

    class Boom(Exception):
        pass

    gw = _gateway({**BYTES, 3: Boom("bug")})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        with pytest.raises(Boom):
            await fetch_media(
                gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
                profile="p", report_path=report,
            )
        stored = st.conn.execute(
            "SELECT sha256, path FROM media WHERE message_uri = 'tg:msg:10/2'"
        ).fetchone()
    assert stored is not None
    by_uri = {r["uri"]: r for r in _report(report)}
    assert by_uri["tg:msg:10/2"]["outcome"] == "downloaded"
    assert by_uri["tg:msg:10/2"]["sha256"] == stored["sha256"]
    assert by_uri["tg:msg:10/2"]["key"] == stored["path"]
    assert by_uri["tg:msg:10/3"]["outcome"] == "not_attempted"


@pytest.mark.asyncio
async def test_channel_phase_stop_ends_the_command(tmp_path):
    """A channel-phase PhaseStop (e.g. resolve FLOOD_WAIT over the ceiling) must
    not go on to channel B: its RPC would sleep the persisted cooldown."""
    gw = _gateway(BYTES, full_channel_by_id={
        10: PhaseStop("flood wait 3600s"), 20: _full(20, "chan_b"),
    })
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    assert not summary.complete
    assert "channel phase_stop" in (summary.stop_reason or "")
    assert gw.calls.count("get_full_channel") == 1  # channel B never attempted
    assert gw.download_media_calls == []
    assert {r["outcome"] for r in _report(report)} == {"not_attempted"}


@pytest.mark.asyncio
async def test_inherited_media_since_does_not_filter_list_rows(tmp_path):
    """The list is the selection: a collect-era `media_since` window must not
    leave list rows not_attempted."""
    settings = load_settings("default", {
        "data_dir": tmp_path, "media_min_free_gb": 0,
        "media_since": "2027-01-01T00:00:00+00:00",  # after every seeded message
    })
    report = tmp_path / "r.csv"
    gw = _gateway(BYTES)
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_media(
            gw, st, settings, _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    assert summary.complete
    assert "not_attempted" not in {r["outcome"] for r in _report(report)}


@pytest.mark.asyncio
async def test_excluded_rows_are_never_fetched_and_say_why(tmp_path):
    gw = _gateway(BYTES)
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        path = tmp_path / "list.csv"
        path.write_text(LIST, encoding="utf-8")
        classified = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path / "p"),
            excluded_ids=frozenset({10}),
        )
        summary = await fetch_media(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
    assert summary.complete
    rows = _report(report)
    assert [r["outcome"] for r in rows] == [
        "excluded", "downloaded", "excluded", "downloaded", "excluded",
    ]
    assert rows[0]["reason"] == "channel excluded by --exclude-target"
    assert [i["channel_id"] for i in gw.full_channel_inputs] == [20]
    assert gw.download_media_calls == [11, 12]


@pytest.mark.asyncio
async def test_second_run_against_bucket_is_already_stored(tmp_path, monkeypatch):
    from paperboy.media_store import build_media_store
    from tests.fake_gcs import FakeGcsClient

    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    settings = load_settings(
        "default",
        {
            "data_dir": tmp_path, "media_min_free_gb": 0,
            "media_store": "gs://bkt/p/x", "media_store_buckets": "bkt",
        },
    )
    media_store = build_media_store(settings, "p")
    path = tmp_path / "list.csv"
    path.write_text(LIST, encoding="utf-8")
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        first = classify_rows(st, parse_media_list(path), media_store=media_store)
        summary = await fetch_media(
            _gateway(BYTES), st, settings, first, LOG, profile="p", report_path=tmp_path / "r1.csv"
        )
        assert summary.counts["downloaded"] == 5 and len(client.bucket("bkt").objects) == 5
        assert summary.bytes_downloaded == sum(len(b) for b in BYTES.values())

        heads = client.bucket("bkt").calls["exists"]
        second = classify_rows(st, parse_media_list(path), media_store=media_store)
        assert {c.outcome for c in second} == {"already_stored"}
        assert client.bucket("bkt").calls["exists"] == heads  # custody rows answer offline
        gw = _gateway({})
        summary = await fetch_media(
            gw, st, settings, second, LOG, profile="p", report_path=tmp_path / "r2.csv"
        )
        assert gw.download_media_calls == [] and summary.counts["already_stored"] == 5
