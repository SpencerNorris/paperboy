"""`reproject` replays `fetch-media` segment runs (#68): first segments (channel +
media) and later, media-only segments that reuse a resolved channel via the
`ChannelContextReused` marker. Synthetic channels/messages only."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.config import load_settings
from paperboy.fetch_media import fetch_media
from paperboy.media_list import classify_rows, parse_media_list
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


def _collect_fixtures(cid: int, username: str, photos: dict[int, int]) -> dict:
    return {
        "self": {"_": "user", "id": 1, "self": True},
        "resolve": _resolved(cid, username),
        "full_channel": _full(cid, username),
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


async def build_source(tmp_path: Path) -> Path:
    """Runs 1-2 collect the channels; runs 3-6 are the four fetch-media segments."""
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        for cid, username, photos in [(10, "chan_a", PHOTOS_A), (20, "chan_b", PHOTOS_B)]:
            await collect_channel(
                FakeGateway(_collect_fixtures(cid, username, photos)), store, settings,
                parse_target(f"@{username}"), ["channel", "history"], LOG,
            )
        gw = FakeGateway({
            "self": {"_": "user", "id": 1, "self": True},
            "resolve_by_target": {
                "chan_a": _resolved(10, "chan_a"), "chan_b": _resolved(20, "chan_b"),
            },
            "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
            "media": {1: b"a1", 2: b"a2", 3: b"a3", 11: b"b11", 12: b"b12"},
        })
        listing = tmp_path / "list.csv"
        listing.write_text(LIST, encoding="utf-8")
        summary = await fetch_media(
            gw, store, settings, classify_rows(store, parse_media_list(listing)), LOG,
            profile="default", report_path=tmp_path / "report.csv",
        )
        assert summary.complete
    return db


def _reproject(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    return runner.invoke(app, ["reproject", "--profile", "default"])


def test_detect_phases_media_from_selection_marker_without_download_raw(tmp_path):
    """A segment whose files were all dedup'd leaves custody rows but no
    MediaDownload raw; the MediaSelection marker still implies `media`."""
    db = asyncio.run(build_source(tmp_path))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM raw_records WHERE lower(kind) = 'mediadownload'")
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    assert phases[2] == ["channel", "media"] and phases[4] == ["media"]


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
