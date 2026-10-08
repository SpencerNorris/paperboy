"""`fetch_from_list` driver: ordered cross-channel fetch of posts then media, resume,
stops, report (#68, #91).

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
from paperboy.fetch_from_list import fetch_from_list
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


def _photo_msg(mid, photo_id):
    return {
        "_": "message", "id": mid, "message": "", "date": 1767322445,
        "media": {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": photo_id}},
    }


# What `channels.getMessages` answers for the seeded messages (ids are distinct
# across the two channels, and the fake ignores the channel).
POSTS = {mid: _photo_msg(mid, ch * 100 + mid) for ch, mid in
         [(10, 1), (10, 2), (10, 3), (20, 11), (20, 12)]}


def _gateway(media, **extra):
    fx = {
        "get_messages": dict(POSTS),
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
        summary = await fetch_from_list(
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
        # A channel is established ALONE once (2 runs); every segment's posts run
        # (4) and media run (4) then reuse that context.
        assert len(markers) == 8
        assert len({m["run_id"] for m in markers}) == 8
        rows = _report(report)
        assert [r["line_no"] for r in rows] == ["2", "3", "4", "5", "6"]
        assert [r["outcome"] for r in rows] == ["downloaded"] * 5
        assert [r["uri"] for r in rows][0] == "tg:msg:10/1"
        for r in rows:
            assert len(r["sha256"]) == 64 and r["key"].startswith("media/")
            assert (tmp_path / "p" / r["key"]).is_file()


@pytest.mark.asyncio
async def test_already_stored_row_is_refetched_but_not_redownloaded(tmp_path):
    report1, report2 = tmp_path / "r1.csv", tmp_path / "r2.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report1,
        )
        custody = st.conn.execute("select count(*) from custody_log").fetchone()[0]
        gw = _gateway({})
        second_rows = _classified(st, tmp_path)
        assert all(c.media_held for c in second_rows)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), second_rows, LOG,
            profile="p", report_path=report2,
        )
        assert summary.complete
        assert summary.counts["already_stored"] == 5
        assert gw.calls.count("get_messages") == 4  # one batch per segment: posts re-fetched
        assert gw.download_media_calls == []
        assert st.conn.execute("select count(*) from custody_log").fetchone()[0] == custody
        first, second = _report(report1), _report(report2)
        assert [(r["outcome"], r["post"]) for r in second] == [("already_stored", "fetched")] * 5
        assert [(r["sha256"], r["key"]) for r in second] == [
            (r["sha256"], r["key"]) for r in first
        ]


@pytest.mark.asyncio
async def test_hard_stop_marks_rest_not_attempted(tmp_path):
    gw = _gateway({**BYTES, 11: HardStop("boom")})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
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
            summary = await fetch_from_list(
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
    """No fetch-from-list-only collector: the live run uses `channel`, `posts`, `media`."""
    from paperboy import fetch_from_list as fm
    from paperboy.collectors.channel import ChannelCollector
    from paperboy.collectors.media import MediaCollector
    from paperboy.collectors.posts import PostsCollector

    seen = []
    real = fm.collect_channel_with_context

    async def spy(*args, **kwargs):
        seen.append(kwargs["collectors"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(fm, "collect_channel_with_context", spy)
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=tmp_path / "r.csv",
        )
    # A channel is established alone, first; per segment the posts, then (once
    # the posts are stored and the files this run's store lacks are known) the
    # media phase alone.
    assert seen and all(
        [type(c) for c in cs] in ([ChannelCollector], [PostsCollector], [MediaCollector])
        for cs in seen
    )
    assert {type(c) for cs in seen for c in cs} == {
        ChannelCollector, PostsCollector, MediaCollector,
    }


@pytest.mark.asyncio
async def test_report_written_on_unexpected_error(tmp_path):
    report = tmp_path / "r.csv"

    class Boom(Exception):
        pass

    gw = _gateway({**BYTES, 11: Boom("bug")})
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        with pytest.raises(Boom):
            await fetch_from_list(
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
            await fetch_from_list(
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
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
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
        summary = await fetch_from_list(
            _gateway(BYTES), st, settings, first, LOG, profile="p", report_path=tmp_path / "r1.csv"
        )
        assert summary.counts["downloaded"] == 5 and len(client.bucket("bkt").objects) == 5
        assert summary.bytes_downloaded == sum(len(b) for b in BYTES.values())

        heads = client.bucket("bkt").calls["exists"]
        second = classify_rows(st, parse_media_list(path), media_store=media_store)
        assert {c.media_held for c in second} == {True}
        assert client.bucket("bkt").calls["exists"] == heads  # custody rows answer offline
        gw = _gateway({})
        summary = await fetch_from_list(
            gw, st, settings, second, LOG, profile="p", report_path=tmp_path / "r2.csv"
        )
        assert gw.download_media_calls == [] and summary.counts["already_stored"] == 5
        assert gw.calls.count("get_messages") == 4


# ── #91: posts before media ───────────────────────────────────────────────


def _store_without_posts(st):
    """Keys for both channels, but NO message rows: every listed post is new."""
    seed_channel(st, 10, "chan_a", access_hash=100)
    seed_channel(st, 20, "chan_b", access_hash=200)


@pytest.mark.asyncio
async def test_post_absent_from_store_is_fetched_projected_and_downloaded(tmp_path):
    msg = {**_photo_msg(1, 101), "from_id": {"_": "PeerUser", "user_id": 42}}
    gw = _gateway({1: b"a1"}, get_messages={1: msg})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        classified = _classified(st, tmp_path, "tg:msg:10/1\n")
        assert [c.in_store for c in classified] == [False]
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
        assert summary.complete
        assert st.conn.execute("select count(*) from messages").fetchone()[0] == 1
        assert st.conn.execute("select count(*) from peers where id=42").fetchone()[0] == 1
    [row] = _report(report)
    assert (row["outcome"], row["post"]) == ("downloaded", "fetched")
    assert gw.download_media_calls == [1]


@pytest.mark.asyncio
async def test_deleted_upstream_has_no_media_attempt(tmp_path):
    gw = _gateway({}, get_messages={})  # every id answers MessageEmpty
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/5\n"), LOG,
            profile="p", report_path=report,
        )
        assert summary.complete
        tomb = st.conn.execute("select evidence from message_tombstones").fetchall()
        assert [t["evidence"] for t in tomb] == ["empty"]
    [row] = _report(report)
    assert (row["outcome"], row["post"]) == ("deleted_upstream", "deleted_upstream")
    assert gw.download_media_calls == []


@pytest.mark.asyncio
async def test_no_media_flag_gives_post_only(tmp_path):
    gw = _gateway(BYTES)
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report, with_media=False,
        )
        assert summary.complete
        assert st.conn.execute(
            "select count(*) from raw_records where kind='MediaSelection'"
        ).fetchone()[0] == 0
        assert st.conn.execute(
            "select count(*) from run_events where phase='media'"
        ).fetchone()[0] == 0
    assert {(r["outcome"], r["post"]) for r in _report(report)} == {("post_only", "fetched")}
    assert gw.download_media_calls == []


@pytest.mark.asyncio
async def test_no_media_outcome_for_text_post(tmp_path):
    text_post = {"_": "message", "id": 4, "message": "hi", "date": 1767322445}
    gw = _gateway({}, get_messages={4: text_post})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/4\n"), LOG,
            profile="p", report_path=report,
        )
    [row] = _report(report)
    assert (row["outcome"], row["post"]) == ("no_media", "fetched")


@pytest.mark.asyncio
async def test_posts_phase_stop_ends_the_command(tmp_path):
    gw = _gateway(BYTES, get_messages_errors=[PhaseStop("flood wait 3600s")])
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    assert not summary.complete
    assert "posts phase_stop" in (summary.stop_reason or "")
    assert gw.calls.count("get_messages") == 1  # channel B never attempted
    assert {r["outcome"] for r in _report(report)} == {"not_attempted"}


@pytest.mark.asyncio
async def test_posts_skip_marks_channel_no_access(tmp_path):
    gw = _gateway(BYTES, get_messages_errors=[SkipAndRecord("channel is private")])
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    rows = _report(report)
    assert [r["outcome"] for r in rows] == [
        "no_access", "downloaded", "no_access", "downloaded", "no_access",
    ]
    assert rows[0]["reason"] == "channel is private"
    assert summary.complete


@pytest.mark.asyncio
async def test_report_has_post_column(tmp_path):
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path), LOG,
            profile="p", report_path=report,
        )
    assert report.read_text(encoding="utf-8").splitlines()[0] == (
        "line_no,uri,outcome,post,sha256,key,reason"
    )


@pytest.mark.asyncio
async def test_handle_row_for_unseen_channel_is_resolved_live_by_handle(tmp_path):
    """Orchestrator decision (#91): an unseen handle is looked up through the #84
    handle route (a `ChannelAccess` receipt says so), not a dead end."""
    gw = _gateway(
        {1: b"n1"},
        resolve=_resolved(30, "chan_new"),
        full_channel_by_id={30: _full(30, "chan_new")},
        get_messages={1: _photo_msg(1, 301)},
    )
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        classified = _classified(st, tmp_path, "https://t.me/Chan_New/1\n")
        assert [c.needs_resolve for c in classified] == [True]
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
        assert summary.complete
        access = [
            json.loads(r["payload_json"]) for r in st.conn.execute(
                "SELECT payload_json FROM raw_records WHERE kind='ChannelAccess'"
            )
        ]
        assert [(a["channel_id"], a["via"]) for a in access] == [(30, "handle")]
        assert st.conn.execute(
            "select count(*) from messages where uri='tg:msg:30/1'"
        ).fetchone()[0] == 1
    [row] = _report(report)
    assert (row["uri"], row["outcome"], row["post"]) == ("tg:msg:30/1", "downloaded", "fetched")


# ── review fixes (#91): live-but-tombstoned, alias handles, exclusion ─────


@pytest.mark.asyncio
async def test_live_answer_for_a_tombstoned_row_is_a_live_post(tmp_path):
    """Spec 2.3: Telegram's answer decides. A row an earlier run tombstoned but
    Telegram now answers live is fetched, un-tombstoned and downloaded; the
    tombstone history stays."""
    from paperboy.store.messages import mark_deleted

    gw = _gateway({1: b"a1"})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        mark_deleted(st, 10, 1, "update", "2026-01-01T00:00:00+00:00")
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/1\n"), LOG,
            profile="p", report_path=report,
        )
        assert summary.complete
        row = st.conn.execute(
            "select deleted_at from messages where uri='tg:msg:10/1'"
        ).fetchone()
        assert row["deleted_at"] is None
        assert st.conn.execute(
            "select count(*) from message_tombstones where message_uri='tg:msg:10/1'"
        ).fetchone()[0] == 1  # history stays
    [r] = _report(report)
    assert (r["outcome"], r["post"], r["reason"]) == ("downloaded", "fetched", "")
    assert gw.download_media_calls == [1]


def _alias_gateway(media, **extra):
    """`resolve` answers channel 30 whose PRIMARY username is `chan_primary`."""
    fx = {
        "resolve": _resolved(30, "chan_primary"),
        "full_channel_by_id": {30: _full(30, "chan_primary"), 10: _full(10, "chan_a")},
        "get_messages": {1: _photo_msg(1, 301)},
    }
    fx.update(extra)
    return _gateway(media, **fx)


@pytest.mark.asyncio
async def test_handle_row_with_a_non_primary_handle_is_settled(tmp_path):
    """The channel id comes from the resolved context, not a username lookup: a
    list handle that is not the stored primary username still settles its row."""
    gw = _alias_gateway({1: b"n1"})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path),
            _classified(st, tmp_path, "https://t.me/chan_alias/1\n"), LOG,
            profile="p", report_path=report,
        )
        assert summary.complete
    [r] = _report(report)
    assert (r["uri"], r["outcome"], r["post"]) == ("tg:msg:30/1", "downloaded", "fetched")


@pytest.mark.asyncio
async def test_excluded_channel_reached_by_an_unknown_handle_is_not_fetched(tmp_path):
    """`--exclude-target` is re-checked against the id a handle resolves to: a
    renamed or alias handle must not let an excluded channel through."""
    gw = _gateway(
        {}, resolve=_resolved(10, "chan_renamed"), get_messages={1: _photo_msg(1, 101)},
        full_channel_by_id={10: _full(10, "chan_renamed")},
    )
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        path = tmp_path / "list.csv"
        path.write_text("https://t.me/chan_renamed/1\ntg:msg:10/2\n", encoding="utf-8")
        excluded = frozenset({10})
        classified = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path / "p"),
            excluded_ids=excluded,
        )
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
            excluded_ids=excluded,
        )
        assert summary.complete
        assert st.conn.execute("select count(*) from messages").fetchone()[0] == 0
    assert [r["outcome"] for r in _report(report)] == ["excluded", "excluded"]
    assert gw.download_media_calls == [] and "get_messages" not in gw.calls


@pytest.mark.asyncio
async def test_handle_and_id_rows_for_one_message_are_one_fetch(tmp_path):
    gw = _alias_gateway({1: b"n1"})
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path),
            _classified(st, tmp_path, "https://t.me/chan_alias/1\ntg:msg:30/1\n"), LOG,
            profile="p", report_path=report,
        )
        assert summary.complete
    assert [r["outcome"] for r in _report(report)] == ["downloaded", "duplicate_row"]
    assert gw.download_media_calls == [1]


@pytest.mark.asyncio
async def test_held_file_but_deleted_upstream_reports_deleted_upstream(tmp_path):
    gw = _gateway({}, get_messages={})  # MessageEmpty for the id
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        classified = _classified(st, tmp_path, "tg:msg:10/1\n")
        classified[0].media_held = True
        await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
    [r] = _report(report)
    assert (r["outcome"], r["post"]) == ("deleted_upstream", "deleted_upstream")


# ── an edited post: the NEW media is decided after the posts phase (#91 B1) ──


def _edited_scenario_gateway(media):
    """Channel 10 / message 1 now carries a different photo than the stored one."""
    return _gateway(media, get_messages={1: _photo_msg(1, 999_001)})


@pytest.mark.asyncio
async def test_edited_post_new_media_is_downloaded_not_reported_as_the_old_file(tmp_path):
    """A post held as file A and then edited to photo B: the re-fetched post's NEW
    media is downloaded (eligibility is decided after the posts phase), the report
    shows B, A's custody row is untouched, and a re-run says `already_stored` with B."""
    one = "tg:msg:10/1\n"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(  # file A is stored for 10/1
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path, one), LOG,
            profile="p", report_path=tmp_path / "r0.csv",
        )
        (sha_a,) = {r[0] for r in st.conn.execute("select sha256 from custody_log")}
        custody_a = st.conn.execute(
            "select id, path, sha256, recorded_at, source_message_uri from custody_log"
        ).fetchall()

        # The channel edits the post to photo B. Classified BEFORE the fetch, the
        # row still looks held: the decision must be remade after the posts phase.
        stale = _classified(st, tmp_path, one)
        assert stale[0].media_held
        gw = _edited_scenario_gateway({1: b"new-photo"})
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), stale, LOG,
            profile="p", report_path=tmp_path / "r1.csv",
        )
        assert summary.complete and summary.counts["downloaded"] == 1
        assert gw.download_media_calls == [1]
        (row,) = _report(tmp_path / "r1.csv")
        assert row["outcome"] == "downloaded" and row["sha256"] != sha_a
        assert (tmp_path / "p" / row["key"]).read_bytes() == b"new-photo"
        # A's custody row is untouched; B added its own.
        after = st.conn.execute(
            "select id, path, sha256, recorded_at, source_message_uri from custody_log "
            "where sha256 = ?", (sha_a,)
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in custody_a]

        # Re-run: held now, and the report names B (not the older A).
        again = _classified(st, tmp_path, one)
        assert again[0].media_held
        gw2 = _edited_scenario_gateway({})
        summary = await fetch_from_list(
            gw2, st, _settings(tmp_path), again, LOG,
            profile="p", report_path=tmp_path / "r2.csv",
        )
        assert summary.counts["already_stored"] == 1 and gw2.download_media_calls == []
        (row2,) = _report(tmp_path / "r2.csv")
        assert (row2["outcome"], row2["sha256"], row2["key"]) == (
            "already_stored", row["sha256"], row["key"],
        )


# ── an unknown handle under two priorities resolves once (#91 M1) ────────────


@pytest.mark.asyncio
async def test_same_unknown_handle_under_two_priorities_is_resolved_once(tmp_path):
    """`contacts.resolveUsername` is flood-limited and a channel is established
    once per command: the second segment of the handle reuses the first's id."""
    gw = _gateway(
        {5: b"n5", 6: b"n6"},
        resolve=_resolved(30, "chan_new"),
        full_channel_by_id={30: _full(30, "chan_new")},
        get_messages={5: _photo_msg(5, 305), 6: _photo_msg(6, 306)},
    )
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        classified = _classified(
            st, tmp_path,
            "uri,priority\nhttps://t.me/Chan_New/5,P1\nhttps://t.me/Chan_New/6,P2\n",
        )
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
    assert summary.complete and summary.counts["downloaded"] == 2
    assert gw.calls.count("resolve") == 1
    assert [(r["uri"], r["outcome"]) for r in _report(report)] == [
        ("tg:msg:30/5", "downloaded"), ("tg:msg:30/6", "downloaded"),
    ]


# ── linked-group exclusion works in both edge directions (#91 M3) ────────────


@pytest.mark.parametrize("subject,obj", [(10, 77), (77, 10)])
def test_excludes_a_group_linked_to_an_excluded_parent_in_either_edge_direction(
    tmp_path, subject, obj
):
    from paperboy.fetch_from_list import _excludes
    from paperboy.ids import utc_now_iso
    from paperboy.store.edges import add_edge

    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 77, None, megagroup=True)
        seed_channel(st, 20, "chan_b")
        add_edge(st, f"tg:channel:{subject}", "linked_group", f"tg:channel:{obj}",
                 utc_now_iso(), "stranger", None, None)
        excluded = {10}
        assert _excludes(st, excluded, 77)  # the group follows its excluded parent
        assert excluded == {10, 77}
        assert not _excludes(st, excluded, 20)  # an unrelated channel is untouched


@pytest.mark.parametrize("subject,obj", [(10, 77), (77, 10)])
def test_excluding_only_a_group_does_not_exclude_its_parent_channel(tmp_path, subject, obj):
    """Exclusion is one-way: a group follows its parent, never the reverse - in
    either stored edge direction, and for both the fetch-time and the offline
    (`--exclude-target`) check."""
    from paperboy.fetch_from_list import _excludes
    from paperboy.ids import utc_now_iso
    from paperboy.media_list import excluded_channel_ids
    from paperboy.store.edges import add_edge

    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 77, "chan_group", megagroup=True)
        add_edge(st, f"tg:channel:{subject}", "linked_group", f"tg:channel:{obj}",
                 utc_now_iso(), "stranger", None, None)
        excluded = {77}
        assert not _excludes(st, excluded, 10)
        assert excluded == {77}
        assert excluded_channel_ids(st, ["@chan_group"]) == {77}
        assert excluded_channel_ids(st, ["@chan_a"]) == {10, 77}


# ── a same-channel repost keeps its own custody row; only the download is skipped ──


@pytest.mark.asyncio
async def test_repost_in_a_later_segment_writes_its_own_custody_row(tmp_path):
    """Posts 10/1 (P1) and 10/2 (P2) carry the SAME photo. The file is downloaded
    once, but every sighting - where and when the file appeared - keeps a custody
    row naming the content, exactly as `collect`'s media phase writes one."""
    gw = _gateway(
        {1: b"same", 2: b"same"},
        get_messages={1: _photo_msg(1, 555), 2: _photo_msg(2, 555)},
    )
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _store_without_posts(st)
        classified = _classified(st, tmp_path, "uri,priority\ntg:msg:10/1,P1\ntg:msg:10/2,P2\n")
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG, profile="p", report_path=report,
        )
        assert summary.complete and gw.download_media_calls == [1]  # one download
        assert [(r["uri"], r["outcome"]) for r in _report(report)] == [
            ("tg:msg:10/1", "downloaded"), ("tg:msg:10/2", "duplicate"),
        ]
        custody = st.conn.execute(
            "select source_message_uri, content_key from custody_log order by id"
        ).fetchall()
        assert [(r[0], r[1]) for r in custody] == [
            ("tg:msg:10/1", "photo:555"), ("tg:msg:10/2", "photo:555"),
        ]
        # A re-run adds nothing: both sightings are already recorded.
        again = _classified(st, tmp_path, "tg:msg:10/1\ntg:msg:10/2\n")
        gw2 = _gateway({}, get_messages={1: _photo_msg(1, 555), 2: _photo_msg(2, 555)})
        await fetch_from_list(
            gw2, st, _settings(tmp_path), again, LOG, profile="p",
            report_path=tmp_path / "r2.csv",
        )
        assert gw2.download_media_calls == []
        assert st.conn.execute("select count(*) from custody_log").fetchone()[0] == 2


# ── a cross-channel repost is reported as stored, never walked (ADR-0009, #95) ──


def _doc_msg(mid, doc_id, size=2_000_000):
    return {"_": "message", "id": mid, "message": "", "date": 1767322445,
            "media": {"_": "MessageMediaDocument", "document": {
                "_": "Document", "id": doc_id, "access_hash": 1, "size": size,
                "mime_type": "video/mp4", "attributes": []}}}


@pytest.mark.asyncio
async def test_cross_channel_repost_is_already_stored_without_download_or_custody(tmp_path):
    """10/1 holds photo 1001. 20/11 carries the same photo: nothing is downloaded,
    no custody row is written for it, the report names the holding file, and a
    re-run changes nothing."""
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway({1: b"same-bytes"}, get_messages={1: _photo_msg(1, 1001)}),
            st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/1\n"), LOG,
            profile="p", report_path=tmp_path / "r0.csv",
        )
        (held,) = _report(tmp_path / "r0.csv")
        assert held["outcome"] == "downloaded"
        for n in (1, 2):  # the run, then its re-run
            gw = _gateway({11: b"same-bytes"}, get_messages={11: _photo_msg(11, 1001)})
            await fetch_from_list(
                gw, st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:20/11\n"), LOG,
                profile="p", report_path=tmp_path / f"r{n}.csv",
            )
            (row,) = _report(tmp_path / f"r{n}.csv")
            assert gw.download_media_calls == []
            assert (row["outcome"], row["sha256"], row["key"]) == (
                "already_stored", held["sha256"], held["key"],
            )
            assert st.conn.execute(
                "select count(*) from custody_log where source_message_uri = 'tg:msg:20/11'"
            ).fetchone()[0] == 0
        assert st.conn.execute("select count(*) from custody_log").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cross_channel_repost_is_never_too_large(tmp_path):
    """The cross-channel decision is made before any size cap: with
    `--media-max-mb 1` the repost of a 2 MB file is `already_stored`, not
    `too_large`."""
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway({1: b"v" * 2_000_000}, get_messages={1: _doc_msg(1, 5005)}),
            st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/1\n"), LOG,
            profile="p", report_path=tmp_path / "r0.csv",
        )
        capped = _settings(tmp_path).model_copy(update={"media_max_mb": 1})
        gw = _gateway({11: b"v" * 2_000_000}, get_messages={11: _doc_msg(11, 5005)})
        await fetch_from_list(
            gw, st, capped, _classified(st, tmp_path, "tg:msg:20/11\n"), LOG,
            profile="p", report_path=tmp_path / "r1.csv",
        )
        (row,) = _report(tmp_path / "r1.csv")
        assert row["outcome"] == "already_stored" and gw.download_media_calls == []


# ── exclusion is decided on the channel as Telegram reports it (fail-closed) ──


@pytest.mark.asyncio
async def test_group_of_an_excluded_parent_without_a_stored_edge_is_not_fetched(tmp_path):
    """P (10) is excluded. The store has a saved key for its discussion group G
    (77) but no `linked_group` edge yet; only G's `ChatFull` says
    `linked_chat_id = 10`. G's channel is established ALONE first, the edge is
    recorded, and only then is exclusion decided: no `getMessages`, no download."""
    from paperboy.media_list import excluded_channel_ids

    g_chat = {"_": "channel", "id": 77, "access_hash": 770, "title": "G", "megagroup": True}
    g_full = {
        "_": "messages.chatFull",
        "full_chat": {"_": "channelFull", "id": 77, "participants_count": 1, "pts": 1,
                      "linked_chat_id": 10},
        "chats": [g_chat], "users": [],
    }
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        seed_channel(st, 10, "chan_a", access_hash=100)
        seed_channel(st, 77, None, access_hash=770, megagroup=True)
        excluded = excluded_channel_ids(st, ["@chan_a"])
        assert excluded == {10}  # no edge yet: the group is not known to follow
        gw = _gateway(
            {5: b"g5"}, get_messages={5: _photo_msg(5, 7705)},
            full_channel_by_id={77: g_full, 10: _full(10, "chan_a")},
        )
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path, "tg:msg:77/5\n"), LOG,
            profile="p", report_path=report, excluded_ids=excluded,
        )
    assert summary.complete
    assert "get_messages" not in gw.calls and gw.download_media_calls == []
    (row,) = _report(report)
    assert (row["outcome"], row["post"]) == ("excluded", "skipped")


# ── --no-media describes the post as stored now ──


@pytest.mark.asyncio
async def test_no_media_edited_post_is_post_only_without_the_old_file(tmp_path):
    """A post held as file A and edited to photo B: under `--no-media` nothing is
    downloaded, and the row is `post_only` with no sha - not `already_stored`
    with A."""
    one = "tg:msg:10/1\n"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
        await fetch_from_list(
            _gateway(BYTES), st, _settings(tmp_path), _classified(st, tmp_path, one), LOG,
            profile="p", report_path=tmp_path / "r0.csv",
        )
        gw = _edited_scenario_gateway({})
        await fetch_from_list(
            gw, st, _settings(tmp_path), _classified(st, tmp_path, one), LOG,
            profile="p", report_path=tmp_path / "r1.csv", with_media=False,
        )
        (row,) = _report(tmp_path / "r1.csv")
        assert (row["outcome"], row["post"], row["sha256"], row["key"]) == (
            "post_only", "fetched", "", "",
        )
        assert gw.download_media_calls == []


@pytest.mark.asyncio
async def test_posts_stop_after_partial_batches_still_reports_the_fetched_rows(tmp_path):
    """101 listed ids = two `getMessages` batches; the second hits a FLOOD_WAIT over
    the ceiling. The 100 rows of the first batch were stored, and their report
    describes the stored posts (edited to new photos: `post_only`, no sha) rather
    than the offline classification; the 101st stays `not_attempted`."""
    from paperboy.budget import PhaseStop

    ids = range(1, 102)
    gw = _gateway(
        {}, get_messages={i: _photo_msg(i, 90_000 + i) for i in ids},
        get_messages_errors=[None, PhaseStop("flood wait 3600s")],
    )
    report = tmp_path / "r.csv"
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        seed_channel(st, 10, "chan_a", access_hash=100)
        for i in ids:
            seed_msg(st, 10, i, photo_id=7)  # every post is photo 7 ...
        await fetch_from_list(  # ... whose file a first run stores: all rows look held
            _gateway({1: b"seven"}, get_messages={1: _photo_msg(1, 7)}), st,
            _settings(tmp_path), _classified(st, tmp_path, "tg:msg:10/1\n"), LOG,
            profile="p", report_path=tmp_path / "r0.csv",
        )
        classified = _classified(st, tmp_path, "".join(f"tg:msg:10/{i}\n" for i in ids))
        summary = await fetch_from_list(
            gw, st, _settings(tmp_path), classified, LOG,
            profile="p", report_path=report, with_media=False,
        )
    assert not summary.complete and "posts phase_stop" in (summary.stop_reason or "")
    rows = _report(report)
    assert [r["outcome"] for r in rows[:100]] == ["post_only"] * 100
    assert {r["sha256"] for r in rows[:100]} == {""}
    assert rows[100]["outcome"] == "not_attempted"
