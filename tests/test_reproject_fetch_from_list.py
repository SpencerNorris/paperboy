"""`reproject` replays `fetch-media` segment runs (#68): first segments (channel +
media) and later, media-only segments that reuse a resolved channel via the
`ChannelContextReused` marker. Synthetic channels/messages only."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from paperboy.fetch_media import fetch_media
from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.config import load_settings
from paperboy.media_list import classify_rows, parse_media_list
from paperboy.media_store import LocalMediaStore
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_fetch_media import _full, _resolved
from tests.test_reproject import assert_round_trip

runner = CliRunner()
LOG = logging.getLogger("t")

# Message id -> photo id. Messages 1, 3 and 4 are the same photo (a repost),
# and message 4 is NOT on the list: a replay that walked every stored media
# message would give it a dedup custody row the live run never wrote.
PHOTOS_A = {1: 101, 2: 102, 3: 101, 4: 101}
PHOTOS_B = {11: 211, 12: 212}


def _history(photos: dict[int, int]) -> list[dict]:
    return [
        {
            "_": "message", "id": mid, "message": "", "date": 1767322445 + mid,
            "media": {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": pid}},
        }
        for mid, pid in sorted(photos.items(), reverse=True)
    ]


def _min_chats(payload: dict) -> dict:
    """The same response with every chat marked `min`: such a peer is stored but
    gives no usable key (Step A routes 1 and 2 are unavailable for it)."""
    return payload | {"chats": [dict(c, min=True) for c in payload["chats"]]}


def _collect_fixtures(
    cid: int, username: str, photos: dict[int, int], *, keyless: bool = False
) -> dict:
    wrap = _min_chats if keyless else (lambda p: p)
    return {
        "self": {"_": "user", "id": 1, "self": True},
        "resolve": wrap(_resolved(cid, username)),
        "full_channel": wrap(_full(cid, username)),
        "history": _history(photos),
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
    }


LIST = (
    "uri,priority\n"
    "tg:msg:10/1,P1\n"
    "tg:msg:20/11,P1\n"
    "tg:msg:10/2,P2\n"
    "tg:msg:10/3,P2\n"
    "tg:msg:20/12,P2\n"
)


# The extended source adds channel 30, known only through `min` chats (no usable
# key) whose stored handle now resolves to ANOTHER channel (6): route 3's
# verification refuses it, so its segment is `no_access` and its run holds a
# `granted: false` receipt and no `ChatFull`. P3 also repeats the photo of 10/1
# in message 10/4: its segment's only row is a dedup (custody row, no download).
PHOTOS_C = {21: 301}
EXTENDED_LIST = LIST + "tg:msg:10/4,P3\ntg:msg:30/21,P3\n"


async def build_source(tmp_path: Path, *, extended: bool = False) -> Path:
    """Runs 1-2 (extended: 1-3) collect the channels; the rest are the fetch-media
    segments (four, or six extended)."""
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        channels = [(10, "chan_a", PHOTOS_A), (20, "chan_b", PHOTOS_B)]
        if extended:
            channels.append((30, "chan_c", PHOTOS_C))
        for cid, username, photos in channels:
            await collect_channel(
                FakeGateway(_collect_fixtures(cid, username, photos, keyless=cid == 30)),
                store, settings, parse_target(f"@{username}"), ["channel", "history"], LOG,
            )
        gw = FakeGateway({
            "self": {"_": "user", "id": 1, "self": True},
            # Only the keyless channel's stored handle is ever looked up.
            "resolve": (
                _resolved(6, "chan_c") if extended
                else AssertionError("fetch-media addresses channels by id")
            ),
            "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
            "media": {1: b"a1", 2: b"a2", 3: b"a3", 11: b"b11", 12: b"b12", 21: b"c21"},
        })
        listing = tmp_path / "list.csv"
        listing.write_text(EXTENDED_LIST if extended else LIST, encoding="utf-8")
        summary = await fetch_media(
            gw, store, settings, classify_rows(
                store, parse_media_list(listing),
                media_store=LocalMediaStore(tmp_path / "default"),
            ), LOG,
            profile="default", report_path=tmp_path / "report.csv",
        )
        assert summary.complete
        if extended:
            assert summary.counts["no_access"] == 1
            assert sum(summary.counts[k] for k in ("downloaded", "duplicate")) == 6
    return db


def _reproject(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    return runner.invoke(app, ["reproject", "--profile", "default"])


def test_detect_phases_media_requires_established_channel(tmp_path):
    """Replay what ran, never what was intended (#68 spec 9.3). A granted segment
    keeps `media` without its MediaDownload raws (the selection plus the
    established channel are evidence); a refused one, whose run has no `ChatFull`,
    never does; a selection naming a channel the run did not establish does not
    either."""
    db = asyncio.run(build_source(tmp_path, extended=True))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM raw_records WHERE lower(kind) = 'mediadownload'")
    src = ReplaySource.open(db, tmp_path / "default")
    runs = src.runs()
    phases = [detect_phases(src, run) for run in runs]
    # 3 collect runs, then P1(10) P1(20) P2(10)/marker P2(20)/marker P3(10)/marker P3(30).
    assert phases[3] == ["channel", "media"] and phases[4] == ["channel", "media"]
    assert phases[5] == ["media"] and phases[7] == ["media"]
    assert phases[8] == ["channel"], "refused channel: no media phase to replay"
    # A selection naming another channel than the run established: no media.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE raw_records SET payload_json = json_set(payload_json, '$.channel_id', 999) "
            "WHERE kind = 'MediaSelection' AND run_id = ?", (runs[3].run_id,),
        )
    src = ReplaySource.open(db, tmp_path / "default")
    assert detect_phases(src, src.runs()[3]) == ["channel"]


def test_detect_phases_marker_run_is_media_only(tmp_path):
    db = asyncio.run(build_source(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    # Two collect runs, two first segments (channel + media, no history: none
    # ran), two marker (media-only) segments.
    assert phases[:2] == [["channel", "history"]] * 2
    assert phases[2] == ["channel", "media"] and phases[3] == phases[2]
    assert phases[4] == ["media"] and phases[5] == ["media"]


def test_reproject_replays_marker_runs_to_identical_media_and_custody(
    tmp_path, monkeypatch
):
    db = asyncio.run(build_source(tmp_path))
    src = sqlite3.connect(db)
    # The repost got its custody row from the P2 segment only (msg 3), and
    # message 4 - never selected - has none.
    custody = {
        r[0] for r in src.execute("SELECT source_message_uri FROM custody_log")
    }
    assert "tg:msg:10/3" in custody and "tg:msg:10/4" not in custody
    src.close()

    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert_round_trip(db, out)

    conn = sqlite3.connect(out)
    try:
        markers = conn.execute(
            "SELECT run_id FROM raw_records WHERE kind='ChannelContextReused'"
        ).fetchall()
        assert len(markers) == 2
        assert conn.execute(
            "SELECT count(*) FROM raw_records WHERE kind='MediaSelection'"
        ).fetchone()[0] == 4
    finally:
        conn.close()


def test_channel_only_run_round_trips_without_a_history_replay(tmp_path, monkeypatch):
    # A `--phases channel` run has no history evidence; replaying `history`
    # anyway appended a synthetic getChannelDifference raw the source never had.
    async def collect_channel_only() -> Path:
        settings = load_settings("default", {"data_dir": tmp_path})
        db = tmp_path / "default" / "paperboy.sqlite"
        with Store.open(db) as store:
            await collect_channel(
                FakeGateway(_collect_fixtures(10, "chan_a", PHOTOS_A)), store, settings,
                parse_target("@chan_a"), ["channel"], LOG,
            )
        return db

    db = asyncio.run(collect_channel_only())
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert_round_trip(db, tmp_path / "default" / "paperboy.reprojected.sqlite")


def test_exclude_target_drops_that_channels_marker_runs_too(tmp_path, monkeypatch):
    # #70's split must not leak an excluded channel through its media-only
    # segments, which carry no resolve record of their own.
    asyncio.run(build_source(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(
        app, ["reproject", "--profile", "default", "--exclude-target", "chan_a"]
    )
    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(tmp_path / "default" / "paperboy.reprojected.sqlite")
    try:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE channel_id = 10"
        ).fetchone()[0] == 0
        markers = conn.execute(
            "SELECT payload_json FROM raw_records WHERE kind='ChannelContextReused'"
        ).fetchall()
        assert len(markers) == 1 and '"channel_id": 20' in markers[0][0]
        assert conn.execute(
            "SELECT count(*) FROM custody_log WHERE source_message_uri LIKE 'tg:msg:10/%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_marker_with_unknown_source_run_is_a_source_error(tmp_path, monkeypatch):
    db = asyncio.run(build_source(tmp_path))
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE raw_records SET payload_json = json_set(payload_json, '$.source_run_id', 'gone') "
        "WHERE kind='ChannelContextReused'"
    )
    conn.commit()
    conn.close()
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code != 0
    assert "gone" in result.output


def _custody_and_media(db: Path):
    conn = sqlite3.connect(db)
    try:
        custody = conn.execute(
            "SELECT source_message_uri, sha256, path FROM custody_log ORDER BY 1, 2, 3"
        ).fetchall()
        media = conn.execute(
            "SELECT message_uri, sha256, path FROM media ORDER BY 1"
        ).fetchall()
        return custody, media
    finally:
        conn.close()


def test_reproject_matches_live_for_normal_refused_and_zero_download_segments(
    tmp_path, monkeypatch
):
    db = asyncio.run(build_source(tmp_path, extended=True))
    custody, media = _custody_and_media(db)
    # Zero-download segment: 10/4 dedup'd to 10/1's file (custody, no MediaDownload).
    assert any(uri == "tg:msg:10/4" for uri, _, _ in custody)
    # Refused segment: nothing for channel 30 anywhere.
    assert not any(uri.startswith("tg:msg:30/") for uri, _, _ in custody)
    assert not any(uri.startswith("tg:msg:30/") for uri, _, _ in media)

    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert_round_trip(db, out)
    assert _custody_and_media(out) == (custody, media)

    conn = sqlite3.connect(out)
    try:
        receipts = [
            json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM raw_records WHERE kind='ChannelAccess'"
            )
        ]
        refused = [a for a in receipts if a["channel_id"] == 30 and a["via"] == "handle"]
        assert refused and not refused[-1]["granted"]  # the fetch-media attempt
    finally:
        conn.close()


def test_marker_replay_reads_the_hash_from_chatfull_not_a_resolve(tmp_path):
    """No fetch-media run of the extended source holds a `ResolvedPeer` for its
    channel except the refused one's (a different channel); the marker segments
    still replay because their context is rebuilt from the source run's `ChatFull`."""
    db = asyncio.run(build_source(tmp_path, extended=True))
    src = ReplaySource.open(db, tmp_path / "default")
    marker_runs = [r for r in src.runs() if src.context_markers(r)]
    assert len(marker_runs) == 3
    for run in src.runs()[3:8]:
        assert not src.has_kind(run, "resolvedpeer")
    marker = src.context_markers(marker_runs[0])[0]
    assert src.resolved_access_hash(
        marker.payload["source_run_id"], marker.payload["channel_id"]
    ) == marker.payload["channel_id"] * 10


def test_legacy_msg_ids_only_selection_replays_unchanged(tmp_path, monkeypatch):
    db = asyncio.run(build_source(tmp_path))
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE raw_records SET payload_json = json_remove(payload_json, '$.channel_id') "
        "WHERE kind='MediaSelection'"
    )
    conn.commit()
    conn.close()
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    # The replayed selection is minted in the current shape (it names the
    # channel), so raw_records legitimately differs from the legacy source in
    # that one payload; every projection, media and custody must still match.
    assert_round_trip(db, out, skip_tables=frozenset({"raw_records"}))


def test_no_fetch_media_only_collector_in_replay(tmp_path, monkeypatch):
    """Live and replay run the SAME collectors for a segment: only the standard
    classes from `paperboy.collectors`, among them the two the live driver uses."""
    from paperboy import reproject as rp
    from paperboy.collectors.channel import ChannelCollector
    from paperboy.collectors.media import MediaCollector

    asyncio.run(build_source(tmp_path))
    seen: list[list[type]] = []
    real = rp.collect_channel

    async def spy(*args, **kwargs):
        seen.append([type(c) for c in kwargs["collectors"]])
        return await real(*args, **kwargs)

    monkeypatch.setattr(rp, "collect_channel", spy)
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert seen
    for collectors in seen:
        assert {ChannelCollector, MediaCollector} <= set(collectors)
        assert all(c.__module__.startswith("paperboy.collectors.") for c in collectors)
