"""Unit tests for `RawReplayGateway`/`ReplaySource` (spec §2–§3): each Gateway
method is tested in isolation against a hand-built raw log — no collector
involved."""

from __future__ import annotations

import sqlite3

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayGateway, ReplaySource
from paperboy.store.db import Store

CID = 100
IC = {"channel_id": CID, "access_hash": 7}


def _seed(tmp_path):
    """A minimal raw log: self, resolve, full, three messages (one edited),
    a probe MessageEmpty, one diff, one recommendation set, one MediaDownload."""
    db = tmp_path / "src.sqlite"
    media_root = tmp_path / "media"
    with Store.open(db) as st:
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None,
                   observed_at="2026-01-01T00:00:00+00:00")
        st.add_raw("ResolvedPeer",
                   {"_": "contacts.ResolvedPeer",
                    "peer": {"_": "PeerChannel", "channel_id": CID},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"target": "@durov"},
                   observed_at="2026-01-01T00:00:01+00:00")
        st.add_raw("ChatFull",
                   {"_": "messages.ChatFull",
                    "full_chat": {"_": "ChannelFull", "id": CID, "pts": 40,
                                  "linked_chat_id": 555},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:00:02+00:00")
        for mid, text, t in [
            (3, "m3", "2026-01-01T00:01:03+00:00"),
            (2, "m2", "2026-01-01T00:01:02+00:00"),
            (1, "m1", "2026-01-01T00:01:01+00:00"),
            (1, "m1 edited", "2026-01-01T00:02:00+00:00"),  # later revision
        ]:
            st.add_raw("Message", {"_": "message", "id": mid, "message": text},
                       "stranger", {"channel_id": CID}, observed_at=t)
        st.add_raw("MessageEmpty", {"_": "MessageEmpty", "id": 4}, "stranger",
                   {"channel_id": CID}, observed_at="2026-01-01T00:03:00+00:00")
        st.add_raw("ChannelDifference",
                   {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:04:00+00:00")
        st.add_raw("Chats", {"_": "messages.chats",
                             "chats": [{"_": "Channel", "id": 200, "access_hash": 9}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:05:00+00:00")
        sha = "ab" + "0" * 62
        path = media_root / sha[:2] / f"{sha}.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"file contents")
        st.add_raw("MediaDownload",
                   {"sha256": sha, "kind": "document", "size": 13,
                    "mime_type": "text/plain", "file_name": "a.txt",
                    "path": str(path), "message_uri": f"tg:msg:{CID}/2"},
                   "stranger", {"channel_id": CID, "msg_id": 2},
                   observed_at="2026-01-01T00:06:00+00:00")
    return db, media_root


def _gateway(tmp_path):
    db, media_root = _seed(tmp_path)
    clock = ReplayClock()
    return RawReplayGateway(ReplaySource.open(db, media_root), clock), clock


@pytest.mark.asyncio
async def test_resolve_matches_target_and_stamps_clock(tmp_path):
    gw, clock = _gateway(tmp_path)
    resolved = await gw.resolve("durov")
    assert resolved["peer"]["channel_id"] == CID
    assert clock.for_payload(resolved) == "2026-01-01T00:00:01+00:00"


@pytest.mark.asyncio
async def test_resolve_unknown_target_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    with pytest.raises(SkipAndRecord):
        await gw.resolve("someone_else")


@pytest.mark.asyncio
async def test_get_self_serves_self_tier_record(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_self())["id"] == 1


@pytest.mark.asyncio
async def test_iter_history_pages_newest_first_excluding_empties(tmp_path):
    gw, clock = _gateway(tmp_path)
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=100)]
    # id DESC; both revisions of msg 1 in capture order; MessageEmpty excluded.
    assert [(m["id"], m["message"]) for m in page] == [
        (3, "m3"), (2, "m2"), (1, "m1"), (1, "m1 edited"),
    ]
    assert clock.for_payload(page[3]) == "2026-01-01T00:02:00+00:00"
    assert clock.for_payload(page[1]) == "2026-01-01T00:01:02+00:00"


@pytest.mark.asyncio
async def test_iter_history_never_splits_an_id_group_across_pages(tmp_path):
    gw, _ = _gateway(tmp_path)
    # limit=3 would cut between msg 1's two revisions; the page extends.
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=3)]
    assert [m["id"] for m in page] == [3, 2, 1, 1]
    next_page = [m async for m in gw.iter_history(IC, offset_id=1, limit=3)]
    assert next_page == []


@pytest.mark.asyncio
async def test_get_messages_serves_stored_and_placeholder(tmp_path):
    gw, _ = _gateway(tmp_path)
    out = await gw.get_messages(IC, [4, 99])
    assert out[0]["_"] == "MessageEmpty"          # stored probe result
    assert out[1] == {"_": "ReplayUnknownMessage", "id": 99}  # D4.1: no fabricated evidence


@pytest.mark.asyncio
async def test_channel_difference_serves_stored_then_synthetic_final(tmp_path):
    gw, _ = _gateway(tmp_path)
    first = await gw.get_channel_difference(IC, 40, 100)
    assert first["pts"] == 41 and first["final"]
    again = await gw.get_channel_difference(IC, 41, 100)
    assert again == {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41}


@pytest.mark.asyncio
async def test_recommendations_served_and_missing_raw_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    recs = await gw.get_channel_recommendations(IC)
    assert recs["chats"][0]["id"] == 200
    with pytest.raises(SkipAndRecord):
        await gw.get_channel_recommendations({"channel_id": 999, "access_hash": 0})


@pytest.mark.asyncio
async def test_sponsored_reconstructs_envelope_or_empty(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_sponsored_messages(IC))["_"] == "sponsoredMessagesEmpty"


@pytest.mark.asyncio
async def test_download_media_reads_content_addressed_file(tmp_path):
    gw, clock = _gateway(tmp_path)
    del clock
    data = await gw.download_media(IC, {"id": 2})
    assert data == b"file contents"
    assert await gw.download_media(IC, {"id": 3}) is None  # no record -> unavailable


@pytest.mark.asyncio
async def test_join_channel_is_synthetic_and_offline(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.join_channel(IC))["_"] == "Updates"


@pytest.mark.asyncio
async def test_doctor_methods_are_not_replayable(tmp_path):
    gw, _ = _gateway(tmp_path)
    for coro in (gw.get_authorizations(), gw.get_password_state(), gw.get_privacy("phone")):
        with pytest.raises(SkipAndRecord):
            await coro


def test_source_helpers(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    assert src.resolve_targets() == ["@durov"]
    assert src.linked_group_ids() == {555}
    assert src.has_kind("mediadownload") and not src.has_kind("tme_page")


def test_source_is_read_only(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    with pytest.raises(sqlite3.OperationalError):
        src.conn.execute("DELETE FROM raw_records")
