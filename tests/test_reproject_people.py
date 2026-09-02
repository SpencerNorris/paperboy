"""Spec §11: round-trip identity extends to the person layer — collect ->
reproject -> users/participants/snapshots/photos identical, one and two runs,
--profiles and triage-only."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.graph import GraphCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.participants import ParticipantsCollector
from paperboy.collectors.profiles import ProfilesCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_reproject import assert_round_trip

runner = CliRunner()
CHANNEL_ID = 5
GROUP_ID = 77
PHASES = ["channel", "history", "discussion", "participants", "profiles", "graph"]


def _user(uid: int, **extra) -> dict:
    return {
        "_": "User", "id": uid, "access_hash": uid * 10, "first_name": f"U{uid}",
        "username": f"u{uid}", "phone": None, "photo": None,
        "status": {"_": "UserStatusRecently", "by_me": None},
        "restriction_reason": [], "usernames": [], **extra,
    }


def _photo(pid: int) -> dict:
    return {
        "_": "Photo", "id": pid, "access_hash": 1, "file_reference": "AQ==",
        "date": 1767322445, "dc_id": 2,
        "sizes": [{"_": "PhotoSize", "type": "x", "w": 640, "h": 640, "size": 1}],
        "video_sizes": None,
    }


def people_fixtures() -> dict:
    chan = {
        "_": "Channel", "id": CHANNEL_ID, "access_hash": 99, "title": "C", "username": "c",
        "broadcast": True,
    }
    group = {
        "_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "C Chat",
        "megagroup": True, "left": True,
    }
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
        "recent_reactions": [{
            "_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 12},
            "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
        }],
    }
    return {
        "self": {"_": "user", "id": 1, "self": True},
        "resolve": {
            "peer": {"_": "PeerChannel", "channel_id": CHANNEL_ID}, "chats": [chan], "users": [],
        },
        "full_channel": {
            "full_chat": {"_": "channelFull", "id": CHANNEL_ID, "participants_count": 10,
                          "pts": 1, "linked_chat_id": GROUP_ID},
            "chats": [chan, group], "users": [],
        },
        "full_channel_by_id": {
            GROUP_ID: {
                "full_chat": {"_": "channelFull", "id": GROUP_ID, "participants_count": 3,
                              "pts": 1, "can_view_participants": True,
                              "participants_hidden": False},
                "chats": [group], "users": [],
            },
        },
        "history": [
            {"_": "message", "id": 3, "message": "comment", "date": 1767322445,
             "from_id": {"_": "PeerUser", "user_id": 12}, "reactions": reacted,
             "reply_to": {"_": "MessageReplyHeader", "reply_to_msg_id": 2, "reply_to_top_id": 2}},
            {"_": "message", "id": 2, "message": "", "date": 1767322445,
             "fwd_from": {"_": "MessageFwdHeader", "channel_post": 1,
                          "from_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}}},
            {"_": "message", "id": 1, "message": "post", "date": 1767322400,
             "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 15}}},
        ],
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
        "get_messages": {},
        "participants": {
            GROUP_ID: {"channelParticipantsRecent": [
                {"_": "ChannelParticipants", "count": 3, "chats": [],
                 "users": [_user(11), _user(13)],
                 "participants": [
                     {"_": "ChannelParticipant", "user_id": 11, "date": 1735689600, "rank": None,
                      "subscription_until_date": None},
                     {"_": "ChannelParticipantAdmin", "user_id": 13, "promoted_by": 13,
                      "date": 1735689600, "admin_rights": {"_": "ChatAdminRights"}, "rank": "mod",
                      "is_self": None, "inviter_id": None},
                 ]},
            ]},
        },
        "participant": {
            GROUP_ID: {
                12: None,
                15: {"_": "ChannelParticipant", "chats": [], "users": [_user(15)],
                     "participant": {"_": "ChannelParticipant", "user_id": 15, "date": 1735689700,
                                     "rank": None, "subscription_until_date": None}},
            },
        },
        "reactions": {
            GROUP_ID: {
                3: {"_": "MessageReactionsList", "count": 1, "chats": [], "next_offset": None,
                    "users": [_user(12)],
                    "reactions": [{
                        "_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 12},
                        "date": 1767322500,
                        "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
                    }]},
            },
        },
        "users": {11: _user(11), 12: _user(12, phone="+15550001212"), 13: _user(13), 15: _user(15)},
        "full_user": {
            11: {"_": "UserFull", "chats": [], "users": [_user(11)],
                 "full_user": {"_": "UserFull", "id": 11, "about": "bio 11",
                               "common_chats_count": 0,
                               "fallback_photo": {"_": "Photo", "id": 5}}},
            12: {"_": "UserFull", "chats": [], "users": [_user(12, phone="+15550001212")],
                 "full_user": {"_": "UserFull", "id": 12, "about": None,
                               "common_chats_count": 1}},
            13: {"_": "UserFull", "chats": [], "users": [_user(13)],
                 "full_user": {"_": "UserFull", "id": 13, "about": "bio 13",
                               "common_chats_count": 0}},
            15: {"_": "UserFull", "chats": [], "users": [_user(15)],
                 "full_user": {"_": "UserFull", "id": 15, "about": None,
                               "common_chats_count": 0}},
        },
        "user_photos": {11: {"_": "PhotosSlice", "count": 1, "photos": [_photo(701)], "users": []}},
        "avatar": {701: b"jpeg-bytes"},
        "privacy": {
            k: {"_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}],
                "chats": [], "users": []}
            for k in ("phone", "lastseen", "photo")
        },
        "channel_recommendations": {"_": "messages.chats", "chats": []},
        "sponsored_messages": {"_": "messages.sponsoredMessagesEmpty"},
        "chat_invite": {},
    }


async def run_people_collect(data_dir: Path, *, enrich: bool = True, mutate=None) -> Path:
    settings = load_settings(
        "default", {"data_dir": data_dir, "unsafe": True, "enrich_profiles": enrich}
    )
    db = data_dir / "default" / "paperboy.sqlite"
    fixtures = people_fixtures()
    if mutate is not None:
        fixtures = mutate(fixtures)
    collectors = [
        ChannelCollector(), HistoryCollector(), DiscussionCollector(),
        ParticipantsCollector(), ProfilesCollector(), GraphCollector(),
    ]
    with Store.open(db) as store:
        await collect_channel(
            FakeGateway(fixtures), store, settings, parse_target("@c"), phases=PHASES,
            log=logging.getLogger("people"), collectors=collectors,
        )
    return db


def _reproject(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    return tmp_path / "default" / "paperboy.reprojected.sqlite"


def test_people_round_trip_identity(tmp_path, monkeypatch):
    db1 = asyncio.run(run_people_collect(tmp_path))
    src = sqlite3.connect(db1)
    # the fixture really exercised every vector before we trust the identity.
    # 11, 13 come from the roster page; 12 comes from the oracle (referenced
    # in the GROUP's own thread, per D9) as a definitive `left`. 15 is only
    # ever referenced as a forward-origin on the TARGET channel's own post —
    # `backfill_message_referenced_peers` is fill-only (store/message_peers.py)
    # and the target channel is scoped before the linked group, so 15's peer
    # keeps `seen_in_chat=CHANNEL_ID` and is never a candidate for GROUP_ID's
    # oracle; it is still enriched below via the peers-wide profiles sweep.
    assert src.execute("select count(*) from participants").fetchone()[0] == 3
    assert (
        src.execute("select count(*) from users where enriched_at is not null").fetchone()[0]
        == 4
    )
    assert (
        src.execute("select count(*) from user_photos where sha256 is not null").fetchone()[0]
        == 1
    )
    # 2, not 1: FakeGateway.iter_history serves the same fixture history to
    # BOTH the target channel and the linked group (this fake has no
    # per-channel history), so message id=3 is stored as two distinct
    # message URIs (tg:msg:5/3 and tg:msg:77/3) — the zero-RPC
    # `recent_reactions` sample projects a `reacted_to` edge per message URI.
    assert (
        src.execute("select count(*) from edges where predicate='reacted_to'").fetchone()[0]
        == 2
    )
    assert (
        src.execute("select count(*) from raw_records where kind='RosterWalled'").fetchone()[0]
        == 1
    )
    src.close()
    out = _reproject(tmp_path, monkeypatch)
    assert_round_trip(db1, out)


def test_two_run_people_round_trip(tmp_path, monkeypatch):
    asyncio.run(run_people_collect(tmp_path))

    def second(fx: dict) -> dict:
        fx = {**fx, "users": {**fx["users"], 11: _user(11, first_name="Renamed")}}
        fx["user_photos"] = {
            11: {"_": "PhotosSlice", "count": 2, "photos": [_photo(702), _photo(701)], "users": []},
        }
        fx["avatar"] = {**fx["avatar"], 702: b"new-avatar"}
        return fx

    db1 = asyncio.run(run_people_collect(tmp_path, mutate=second))
    src = sqlite3.connect(db1)
    assert src.execute(
        "select count(*) from user_snapshots where uri='tg:user:11' and method='users.getUsers'"
    ).fetchone()[0] == 2
    assert src.execute(
        "select count(*) from user_photos where uri='tg:user:11'"
    ).fetchone()[0] == 2
    src.close()
    assert_round_trip(db1, _reproject(tmp_path, monkeypatch))


def test_triage_only_source_reprojects_triage_only(tmp_path, monkeypatch):
    db1 = asyncio.run(run_people_collect(tmp_path, enrich=False))
    out = _reproject(tmp_path, monkeypatch)
    assert_round_trip(db1, out)
    rep = sqlite3.connect(out)
    assert rep.execute(
        "select count(*) from users where enriched_at is not null"
    ).fetchone()[0] == 0
    assert rep.execute(
        "select count(*) from raw_records where kind='users.UserFull'"
    ).fetchone()[0] == 0
    rep.close()


def test_detect_phases_sees_the_person_layer(tmp_path):
    db = asyncio.run(run_people_collect(tmp_path))
    src = ReplaySource.open(db, tmp_path / "default" / "media")
    phases = detect_phases(src, src.runs()[0])
    assert phases.index("participants") == phases.index("discussion") + 1
    assert phases.index("profiles") == phases.index("participants") + 1
