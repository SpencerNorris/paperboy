"""`reproject` replays `fetch-from-list` segment runs (#68): first segments (channel +
media) and later, media-only segments that reuse a resolved channel via the
`ChannelContextReused` marker. Synthetic channels/messages only."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.config import load_settings
from paperboy.fetch_from_list import fetch_from_list
from paperboy.media_list import classify_rows, parse_media_list
from paperboy.media_store import LocalMediaStore
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_fetch_from_list import _full, _resolved
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


async def build_source(
    tmp_path: Path, *, extended: bool = False, with_media: bool = True,
    post_extras: bool = False,
) -> Path:
    """Runs 1-2 (extended: 1-3) collect the channels; the rest are the fetch-from-list
    segments, each a posts run (`channel` + `posts`, or a marker run reusing the
    channel) followed by a media-only marker run when it has files to download.

    `post_extras` adds, to channel 20's segment, an edited post (message 2 of
    channel 10 gains text and a view counter), a new text post and a post Telegram
    answers `MessageEmpty` for: the replay must reproduce a revision, a metric
    row, a projected new post and a tombstone. `with_media=False` is
    `--no-media`."""
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
        posts = {
            m["id"]: m for photos in (PHOTOS_A, PHOTOS_B, PHOTOS_C) for m in _history(photos)
        }
        if post_extras:
            posts[2] = {**posts[2], "message": "edited", "views": 7}
            posts[13] = {
                "_": "message", "id": 13, "message": "new text post", "date": 1767322500,
                "from_id": {"_": "PeerUser", "user_id": 42},
                "fwd_from": {
                    "_": "MessageFwdHeader", "from_id": {"_": "PeerChannel", "channel_id": 99},
                },
            }
        gw = FakeGateway({
            "self": {"_": "user", "id": 1, "self": True},
            "get_messages": posts,  # id 14 is absent: Telegram answers MessageEmpty
            # Only the keyless channel's stored handle is ever looked up.
            "resolve": (
                _resolved(6, "chan_c") if extended
                else AssertionError("fetch-from-list addresses channels by id")
            ),
            "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
            "media": {1: b"a1", 2: b"a2", 3: b"a3", 11: b"b11", 12: b"b12", 21: b"c21"},
        })
        if post_extras:
            # An earlier fetch (Telegram answered MessageEmpty, so it left a real,
            # raw-backed tombstone on 10/3) precedes the main one, which gets a
            # LIVE answer for it: the fetch clears `deleted_at`, replay must too.
            pre = tmp_path / "pre.csv"
            pre.write_text("tg:msg:10/3\n", encoding="utf-8")
            await fetch_from_list(
                FakeGateway({
                    "self": {"_": "user", "id": 1, "self": True}, "get_messages": {},
                    "full_channel_by_id": {10: _full(10, "chan_a")}, "media": {},
                }),
                store, settings, classify_rows(
                    store, parse_media_list(pre),
                    media_store=LocalMediaStore(tmp_path / "default"),
                ), LOG,
                profile="default", report_path=tmp_path / "pre-report.csv", with_media=False,
            )
            assert store.conn.execute(
                "SELECT deleted_at FROM messages WHERE uri = 'tg:msg:10/3'"
            ).fetchone()[0] is not None
        listing = tmp_path / "list.csv"
        text = EXTENDED_LIST if extended else LIST
        if post_extras:
            text += "tg:msg:20/13,P2\ntg:msg:20/14,P2\n"
        listing.write_text(text, encoding="utf-8")
        summary = await fetch_from_list(
            gw, store, settings, classify_rows(
                store, parse_media_list(listing),
                media_store=LocalMediaStore(tmp_path / "default"),
            ), LOG,
            profile="default", report_path=tmp_path / "report.csv", with_media=with_media,
        )
        assert summary.complete
        if extended:
            assert summary.counts["no_access"] == 1
            # The reposts 10/3 and 10/4 (photo of 10/1) are held by content once
            # 10/1's file is stored earlier in the same command: no download,
            # but each keeps its own custody row (a `duplicate` sighting).
            assert summary.counts["downloaded"] == 4
            assert summary.counts["duplicate"] == 2
    return db


def _reproject(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    return runner.invoke(app, ["reproject", "--profile", "default"])


def test_detect_phases_media_requires_established_channel(tmp_path):
    """Replay what ran, never what was intended (#68 spec 9.3). A granted segment
    keeps `media` without its MediaDownload raws (the selection is the evidence);
    a refused one, whose run has no `ChatFull`, never gets one; and in a
    single-run `channel` + `media` (`collect --media-msgs`), a selection naming a
    channel the run did not establish is not evidence either."""
    db = asyncio.run(build_source(tmp_path, extended=True))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM raw_records WHERE lower(kind) = 'mediadownload'")
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    # 3 collect runs, then each channel established alone (`channel`) and per
    # segment a posts marker run and (if it has files to fetch) a media-only
    # marker run: P1(10) P1(20) P2(10) P2(20); P3(10) holds only a repost of a
    # stored file (its media run records the sighting, no download); P3(30) is
    # refused after its `channel` run.
    assert phases[3] == ["channel"] and phases[4] == ["posts"] and phases[5] == ["media"]
    assert phases[6] == ["channel"] and phases[7] == ["posts"] and phases[8] == ["media"]
    assert phases[9] == ["posts"] and phases[10] == ["media"]
    assert phases[11] == ["posts"] and phases[12] == ["media"]
    assert phases[13] == ["posts"] and phases[14] == ["media"]
    assert phases[15] == ["channel"], "refused channel: no media phase to replay"

    legacy = tmp_path / "legacy"
    settings = load_settings(
        "default", {"data_dir": legacy, "media_min_free_gb": 0, "media_msgs": [1]}
    )
    with Store.open(legacy / "default" / "paperboy.sqlite") as store:
        fx = _collect_fixtures(10, "chan_a", PHOTOS_A) | {"media": {1: b"a1"}}
        asyncio.run(collect_channel(
            FakeGateway(fx), store, settings, parse_target("@chan_a"),
            ["channel", "history", "media"], LOG,
        ))
    ldb = legacy / "default" / "paperboy.sqlite"
    src = ReplaySource.open(ldb, legacy / "default")
    assert detect_phases(src, src.runs()[0]) == ["channel", "history", "media"]
    with sqlite3.connect(ldb) as conn:
        conn.execute("DELETE FROM raw_records WHERE lower(kind) = 'mediadownload'")
    src = ReplaySource.open(ldb, legacy / "default")
    assert detect_phases(src, src.runs()[0]) == ["channel", "history", "media"]
    with sqlite3.connect(ldb) as conn:
        conn.execute(
            "UPDATE raw_records SET payload_json = json_set(payload_json, '$.channel_id', 999) "
            "WHERE kind = 'MediaSelection'"
        )
    src = ReplaySource.open(ldb, legacy / "default")
    assert detect_phases(src, src.runs()[0]) == ["channel", "history"]


def test_detect_phases_marker_run_is_posts_and_media(tmp_path):
    db = asyncio.run(build_source(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    # Two collect runs, then each channel is established alone (`channel`), and
    # every segment is a posts marker run (the getMessages receipts are not
    # history evidence) and a media-only marker run.
    assert phases[:2] == [["channel", "history"]] * 2
    assert phases[2:8] == [["channel"], ["posts"], ["media"]] * 2
    assert phases[8:] == [["posts"], ["media"]] * 2


def test_marker_run_without_posts_evidence_is_media_only(tmp_path):
    """A segment recorded before #91 has a MediaSelection and no getMessages
    receipts: it replays exactly as it did (`media`, and `channel` + `media` for
    a first segment). Here the posts runs lose their receipts and the media runs
    keep their selection."""
    db = asyncio.run(build_source(tmp_path))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "DELETE FROM raw_records WHERE json_extract(context_json, '$.method') = "
            "'channels.getMessages'"
        )
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    assert [phases[i] for i in (4, 7, 9, 11)] == [["media"]] * 4


def test_history_evidence_ignores_getmessages_receipts(tmp_path):
    db = asyncio.run(build_source(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default")
    runs = src.runs()
    assert src.has_history_evidence(runs[0])  # a history run
    # A fetch-from-list segment holds messages, but only as getMessages receipts.
    assert src.has_kind(runs[3], "message") and not src.has_history_evidence(runs[3])
    assert not src.has_history_evidence(runs[4])


def test_fetched_post_ids(tmp_path):
    db = asyncio.run(build_source(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default")
    runs = src.runs()
    assert src.fetched_post_ids(runs[0]) == []  # history's records are not receipts
    # Segments in list order: P1(10) [1], P1(20) [11], P2(10) [2, 3], P2(20) [12];
    # each one's media-only run fetched no posts, nor did a channel-only run.
    assert [src.fetched_post_ids(r) for r in runs[2:]] == [
        [], [1], [], [], [11], [], [2, 3], [], [12], [],
    ]


def test_no_media_run_detects_posts_only(tmp_path):
    db = asyncio.run(build_source(tmp_path, with_media=False))
    src = ReplaySource.open(db, tmp_path / "default")
    phases = [detect_phases(src, run) for run in src.runs()]
    assert phases[2:] == [
        ["channel"], ["posts"], ["channel"], ["posts"], ["posts"], ["posts"],
    ]
    assert not any(src.media_selection(run) for run in src.runs())


def test_reproject_replays_posts_runs_to_identical_messages_revisions_and_tombstones(
    tmp_path, monkeypatch
):
    db = asyncio.run(build_source(tmp_path, post_extras=True))
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT count(*) FROM message_revisions WHERE message_uri = 'tg:msg:10/2'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM message_metrics WHERE message_uri = 'tg:msg:10/2'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT deleted_at FROM messages WHERE uri = 'tg:msg:10/3'"
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT count(*) FROM message_tombstones WHERE message_uri = 'tg:msg:10/3'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE uri = 'tg:msg:20/13'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT message_uri, evidence FROM message_tombstones "
            "WHERE message_uri = 'tg:msg:20/14'"
        ).fetchall() == [("tg:msg:20/14", "empty")]
    finally:
        conn.close()
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert_round_trip(db, tmp_path / "default" / "paperboy.reprojected.sqlite")


def test_reproject_replays_no_media_runs_identically(tmp_path, monkeypatch):
    db = asyncio.run(build_source(tmp_path, with_media=False, post_extras=True))
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert_round_trip(db, tmp_path / "default" / "paperboy.reprojected.sqlite")


def test_reproject_replays_marker_runs_to_identical_media_and_custody(
    tmp_path, monkeypatch
):
    db = asyncio.run(build_source(tmp_path))
    src = sqlite3.connect(db)
    # The repost 10/3 (photo of 10/1) is held by content once 10/1's file is
    # stored, yet keeps its own custody row; message 4 - never listed - has none.
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
        # Every segment's posts run and media run reuse their channel (4 + 4).
        assert len(markers) == 8
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
        # Channel 20: the posts and media runs of its two segments.
        assert len(markers) == 4 and all('"channel_id": 20' in m[0] for m in markers)
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
    # Zero-download segment: 10/4 is held by 10/1's file, so nothing is
    # downloaded, but its media run records the sighting as custody.
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
        assert refused and not refused[-1]["granted"]  # the fetch-from-list attempt
    finally:
        conn.close()


def test_marker_replay_reads_the_hash_from_chatfull_not_a_resolve(tmp_path):
    """No fetch-from-list run of the extended source holds a `ResolvedPeer` for its
    channel except the refused one's (a different channel); the marker segments
    still replay because their context is rebuilt from the source run's `ChatFull`."""
    db = asyncio.run(build_source(tmp_path, extended=True))
    src = ReplaySource.open(db, tmp_path / "default")
    marker_runs = [r for r in src.runs() if src.context_markers(r)]
    assert len(marker_runs) == 10
    for run in src.runs()[3:15]:
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


def test_no_fetch_from_list_only_collector_in_replay(tmp_path, monkeypatch):
    """Live and replay run the SAME collectors for a segment: only the standard
    classes from `paperboy.collectors`, among them the three the live driver uses."""
    from paperboy import reproject as rp
    from paperboy.collectors.channel import ChannelCollector
    from paperboy.collectors.media import MediaCollector
    from paperboy.collectors.posts import PostsCollector

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
        assert {ChannelCollector, PostsCollector, MediaCollector} <= set(collectors)
        assert all(c.__module__.startswith("paperboy.collectors.") for c in collectors)


def test_reproject_reproduces_an_edited_posts_new_download(tmp_path, monkeypatch):
    """Parity for #91 B1: post 10/1 was downloaded (file A), then edited to a new
    photo and fetched again. The live run downloads the new file; replay must
    reproduce the revision, both files and both custody sightings, and a re-run
    afterwards (`already_stored`, no new rows) must replay too."""
    db = asyncio.run(build_source(tmp_path))
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    one = tmp_path / "one.csv"
    one.write_text("tg:msg:10/1\n", encoding="utf-8")

    async def fetch_again(media: dict, report: str):
        gw = FakeGateway({
            "self": {"_": "user", "id": 1, "self": True},
            "get_messages": {1: {
                "_": "message", "id": 1, "message": "", "date": 1767322446,
                "media": {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": 9101}},
            }},
            "full_channel_by_id": {10: _full(10, "chan_a")},
            "media": media,
        })
        with Store.open(db) as store:
            summary = await fetch_from_list(
                gw, store, settings, classify_rows(
                    store, parse_media_list(one),
                    media_store=LocalMediaStore(tmp_path / "default"),
                ), LOG, profile="default", report_path=tmp_path / report,
            )
        return summary, gw

    summary, gw = asyncio.run(fetch_again({1: b"a1-edited"}, "edit.csv"))
    assert summary.counts["downloaded"] == 1 and gw.download_media_calls == [1]
    summary, gw = asyncio.run(fetch_again({}, "again.csv"))
    assert summary.counts["already_stored"] == 1 and gw.download_media_calls == []

    src = sqlite3.connect(db)
    try:
        sightings = src.execute(
            "SELECT content_key FROM custody_log WHERE source_message_uri = 'tg:msg:10/1' "
            "ORDER BY id"
        ).fetchall()
        assert [r[0] for r in sightings] == ["photo:101", "photo:9101"]
        assert src.execute(
            "SELECT count(*) FROM message_revisions WHERE message_uri = 'tg:msg:10/1'"
        ).fetchone()[0] == 2
    finally:
        src.close()

    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert_round_trip(db, tmp_path / "default" / "paperboy.reprojected.sqlite")


def test_reproject_reproduces_a_sha_dedup_custody_row(tmp_path, monkeypatch):
    """Two DISTINCT photo ids whose bytes are identical: the media phase only
    learns of the match after streaming the second file, writes a `duplicate`
    custody row for it, and (since #91 review) a `MediaDownload` receipt, so
    `reproject` reproduces the row. Plain `collect`, no `fetch-from-list`."""
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        fx = _collect_fixtures(10, "chan_a", {1: 101, 2: 102}) | {
            "media": {1: b"same-bytes", 2: b"same-bytes"}
        }
        asyncio.run(collect_channel(
            FakeGateway(fx), store, settings, parse_target("@chan_a"),
            ["channel", "history", "media"], LOG,
        ))
        custody = store.conn.execute(
            "SELECT source_message_uri, content_key FROM custody_log ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in custody] == [
            ("tg:msg:10/1", "photo:101"), ("tg:msg:10/2", "photo:102"),
        ]
        assert store.conn.execute("SELECT count(*) FROM media").fetchone()[0] == 1
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert_round_trip(db, out)
    assert _custody_and_media(out) == _custody_and_media(db)


def test_reproject_reproduces_a_cross_channel_repost(tmp_path, monkeypatch):
    """20/11 carries the photo that 10/1's stored file belongs to. `fetch-from-list`
    does not walk it (no download, no custody row, `already_stored`), so replay has
    no selection for it either: the reprojected store is identical."""
    settings = load_settings("default", {"data_dir": tmp_path, "media_min_free_gb": 0})
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        for cid, username, photos in ((10, "chan_a", {1: 101}), (20, "chan_b", {11: 101})):
            asyncio.run(collect_channel(
                FakeGateway(_collect_fixtures(cid, username, photos)),
                store, settings, parse_target(f"@{username}"), ["channel", "history"], LOG,
            ))
        listing = tmp_path / "list.csv"
        listing.write_text("tg:msg:10/1\ntg:msg:20/11\n", encoding="utf-8")
        gw = FakeGateway({
            "self": {"_": "user", "id": 1, "self": True},
            "get_messages": {m["id"]: m for m in _history({1: 101, 11: 101})},
            "full_channel_by_id": {10: _full(10, "chan_a"), 20: _full(20, "chan_b")},
            "media": {1: b"a1", 11: b"a1"},
        })
        summary = asyncio.run(fetch_from_list(
            gw, store, settings, classify_rows(
                store, parse_media_list(listing),
                media_store=LocalMediaStore(tmp_path / "default"),
            ), LOG,
            profile="default", report_path=tmp_path / "report.csv",
        ))
        assert summary.complete
        assert (summary.counts["downloaded"], summary.counts["already_stored"]) == (1, 1)
        assert gw.download_media_calls == [1]
        assert store.conn.execute(
            "SELECT count(*) FROM custody_log WHERE source_message_uri = 'tg:msg:20/11'"
        ).fetchone()[0] == 0
    result = _reproject(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert_round_trip(db, out)
    assert _custody_and_media(out) == _custody_and_media(db)
