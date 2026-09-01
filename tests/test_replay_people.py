"""`RawReplayGateway` serves the person layer's raw kinds back (spec §10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.gateway import FILTER_RECENT
from paperboy.replay import RawReplayGateway, ReplaySource
from paperboy.store.db import Store

GROUP_ID = 77
IC = {"channel_id": GROUP_ID, "access_hash": 4242}
T1 = "2026-01-01T00:00:01+00:00"
T2 = "2026-01-01T00:00:02+00:00"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "src.sqlite"
    media_root = tmp_path / "media"
    with Store.open(db) as st:
        st.begin_run("r1")
        st.add_raw(
            "User", {"_": "User", "id": 1, "is_self": True}, "self", None, observed_at=T1
        )
        st.add_raw(
            "ResolvedPeer",
            {"_": "ResolvedPeer", "peer": {"_": "PeerChannel", "channel_id": 5},
             "chats": [], "users": []},
            "stranger", {"target": "@x"}, observed_at=T1,
        )
        page = {
            "_": "ChannelParticipants", "count": 2,
            "participants": [{"_": "ChannelParticipant", "user_id": 11}],
            "chats": [], "users": [],
        }
        st.add_raw(
            "channels.ChannelParticipants", page, "stranger",
            {"channel_id": GROUP_ID, "filter": "channelParticipantsRecent", "offset": 0},
            observed_at=T1,
        )
        st.add_raw(
            "channels.ChannelParticipant",
            {"_": "ChannelParticipant", "participant": {"_": "ChannelParticipant", "user_id": 12},
             "chats": [], "users": []},
            "stranger", {"channel_id": GROUP_ID, "user_id": 12}, observed_at=T1,
        )
        st.add_raw(
            "UserNotParticipant", {"_": "UserNotParticipant", "user_id": 13}, "stranger",
            {"channel_id": GROUP_ID, "user_id": 13}, observed_at=T2,
        )
        st.add_raw(
            "User", {"_": "User", "id": 11, "first_name": "A"}, "stranger",
            {"channel_id": 5, "method": "users.getUsers", "user_id": 11}, observed_at=T1,
        )
        st.add_raw(
            "UserEmpty", {"_": "UserEmpty", "id": 14}, "stranger",
            {"channel_id": 5, "method": "users.getUsers", "user_id": 14}, observed_at=T1,
        )
        st.add_raw(
            "users.UserFull",
            {"_": "UserFull", "full_user": {"_": "UserFull", "id": 11, "about": "bio"},
             "chats": [], "users": [{"_": "User", "id": 11}]},
            "stranger", {"channel_id": 5, "user_id": 11, "method": "users.getFullUser"},
            observed_at=T2,
        )
        photos = {
            "_": "Photos",
            "photos": [{"_": "Photo", "id": 701, "access_hash": 1, "file_reference": "AQ==",
                        "date": 1767322445, "dc_id": 2, "sizes": [], "video_sizes": None}],
            "users": [],
        }
        st.add_raw(
            "photos.Photos", photos, "stranger",
            {"channel_id": 5, "user_id": 11, "method": "photos.getUserPhotos"}, observed_at=T2,
        )
        sha = "ab" * 32
        avatar_path = media_root / "ab" / f"{sha}.jpg"
        avatar_path.parent.mkdir(parents=True)
        avatar_path.write_bytes(b"jpeg")
        st.add_raw(
            "AvatarDownload",
            {"sha256": sha, "path": str(tmp_path / "elsewhere" / f"{sha}.jpg"),
             "size": 4, "user_uri": "tg:user:11", "photo_id": 701},
            "stranger", {"channel_id": 5, "user_id": 11, "photo_id": 701}, observed_at=T2,
        )
        st.add_raw(
            "messages.MessageReactionsList",
            {"_": "MessageReactionsList", "count": 1, "reactions": [],
             "chats": [], "users": [], "next_offset": "p2"},
            "stranger", {"channel_id": GROUP_ID, "msg_id": 40, "offset": ""}, observed_at=T1,
        )
        st.add_raw(
            "account.PrivacyRules",
            {"_": "account.PrivacyRules", "rules": [], "chats": [], "users": []},
            "self", {"key": "phone"}, observed_at=T1,
        )
    return db, media_root


def _gateway(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    clock = ReplayClock()
    return RawReplayGateway(src, clock, src.runs()[0]), clock


@pytest.mark.asyncio
async def test_participants_served_by_channel_filter_offset(tmp_path):
    gw, clock = _gateway(tmp_path)
    page = await gw.get_participants(IC, FILTER_RECENT, 0, 200)
    assert page["participants"][0]["user_id"] == 11
    assert clock.for_payload(page) == T1
    with pytest.raises(SkipAndRecord):
        await gw.get_participants(IC, FILTER_RECENT, 200, 200)
    with pytest.raises(SkipAndRecord):
        await gw.get_participants(IC, {"_": "channelParticipantsAdmins"}, 0, 200)


@pytest.mark.asyncio
async def test_participant_oracle_serves_answers_and_definitive_negatives(tmp_path):
    gw, clock = _gateway(tmp_path)
    answer = await gw.get_participant(IC, {"user_id": 12, "access_hash": 1})
    assert answer is not None and answer["participant"]["user_id"] == 12
    assert await gw.get_participant(IC, {"user_id": 13, "access_hash": 1}) is None
    assert clock.for_payload({"_": "UserNotParticipant", "user_id": 13}) == T2
    with pytest.raises(SkipAndRecord):
        await gw.get_participant(IC, {"user_id": 99, "access_hash": 1})


@pytest.mark.asyncio
async def test_get_users_serves_per_id_with_placeholders_for_unknown(tmp_path):
    gw, clock = _gateway(tmp_path)
    users = await gw.get_users([
        {"user_id": 11, "access_hash": 1},
        {"user_id": 14, "access_hash": 1},
        {"user_id": 99, "access_hash": 1},
    ])
    assert [u["_"] for u in users] == ["User", "UserEmpty", "ReplayUnknownUser"]
    assert users[2]["id"] == 99
    assert clock.for_payload(users[0]) == T1


@pytest.mark.asyncio
async def test_full_user_photos_and_avatar_bytes(tmp_path):
    gw, clock = _gateway(tmp_path)
    full = await gw.get_full_user({"user_id": 11, "access_hash": 1})
    assert full["full_user"]["about"] == "bio" and clock.for_payload(full) == T2
    photos = await gw.get_user_photos(
        {"user_id": 11, "access_hash": 1}, offset=0, max_id=0, limit=100
    )
    assert photos["photos"][0]["id"] == 701
    # bytes come from THIS source's media root even though the stored path is foreign
    assert await gw.download_user_photo(photos["photos"][0]) == b"jpeg"
    assert await gw.download_user_photo({"id": 999}) is None
    with pytest.raises(SkipAndRecord):
        await gw.get_full_user({"user_id": 99, "access_hash": 1})


@pytest.mark.asyncio
async def test_reaction_lists_by_msg_and_offset_and_privacy_by_key(tmp_path):
    gw, _ = _gateway(tmp_path)
    first = await gw.get_message_reactions_list(IC, 40, offset=None, limit=100)
    assert first["next_offset"] == "p2"
    with pytest.raises(SkipAndRecord):
        await gw.get_message_reactions_list(IC, 40, offset="p2", limit=100)
    assert (await gw.get_privacy("phone"))["rules"] == []
    with pytest.raises(SkipAndRecord):
        await gw.get_privacy("photo")


def test_has_context_value(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    run = src.runs()[0]
    assert src.has_context_value(run, "method", "users.getUsers")
    assert not src.has_context_value(run, "method", "nope")
