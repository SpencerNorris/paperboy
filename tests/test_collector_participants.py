"""The `participants` collector, spec §6: roster discovery within Telegram's walls."""

from __future__ import annotations  # noqa: I001

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from paperboy.collectors.participants import ParticipantsCollector

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.peers import upsert_peer
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77
T0 = "2026-01-01T00:00:00+00:00"
JOINED = 1735689600


def _settings(**over):
    return load_settings("default", {"unsafe": True, **over})


def _ctx(st, gw, settings=None, tier="stranger"):
    return CollectContext(
        gw, st, settings or _settings(), parse_target("@x"),
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, tier, logging.getLogger("t"),
    )


def _seed_channel(st: Store, *, linked: int | None = GROUP_ID, kind: str = "broadcast") -> None:
    raw_id = st.add_raw(
        "ChatFull", {"_": "ChatFull", "full_chat": {"id": CHANNEL_ID}}, "stranger",
        {"channel_id": CHANNEL_ID}, observed_at=T0,
    )
    chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C", "username": "c"}
    chan["broadcast" if kind == "broadcast" else "megagroup"] = True
    upsert_channel(
        st, {"_": "channelFull", "id": CHANNEL_ID, "pts": 1, "linked_chat_id": linked,
             "participants_count": 10},
        chan, raw_id, T0,
    )
    if linked:
        upsert_peer(
            st,
            {"_": "Channel", "id": linked, "access_hash": 4242, "title": "G", "megagroup": True},
            raw_id, T0, seen_in_chat=None, seen_in_msg=None,
        )


def _group_full(count: int = 3, *, left: bool = True, hidden: bool = False, users=()) -> dict:
    return {
        "_": "ChatFull",
        "full_chat": {"_": "channelFull", "id": GROUP_ID, "participants_count": count, "pts": 1,
                      "can_view_participants": not hidden, "participants_hidden": hidden},
        "chats": [{"_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "G",
                   "megagroup": True, "left": left}],
        "users": list(users),
    }


def _user(uid: int, **extra) -> dict:
    return {"_": "User", "id": uid, "access_hash": uid * 10, "first_name": f"U{uid}", **extra}


def _member(uid: int, **extra) -> dict:
    return {"_": "ChannelParticipant", "user_id": uid, "date": JOINED + uid, "rank": None,
            "subscription_until_date": None, **extra}


def _page(*participants: dict, count: int | None = None, users=None) -> dict:
    return {
        "_": "ChannelParticipants",
        "count": count if count is not None else len(participants),
        "participants": list(participants), "chats": [],
        "users": (
            users if users is not None
            else [_user(p["user_id"]) for p in participants if "user_id" in p]
        ),
    }


def _gw(pages=None, **more) -> FakeGateway:
    fx = {"full_channel_by_id": {GROUP_ID: _group_full()}}
    if pages is not None:
        fx["participants"] = {GROUP_ID: {"channelParticipantsRecent": pages}}
    fx.update(more)
    return FakeGateway(fx)


@pytest.mark.asyncio
async def test_broadcast_roster_is_a_stored_walled_outcome_and_the_group_is_enumerated(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        admin = {"_": "ChannelParticipantAdmin", "user_id": 1, "promoted_by": 9, "date": JOINED,
                 "admin_rights": {"_": "ChatAdminRights"}, "rank": "mod", "is_self": None,
                 "inviter_id": None}
        gw = _gw([_page(admin, _member(2), _member(3, rank="vip"))])
        res = await ParticipantsCollector().collect(_ctx(st, gw))

        # 1. the broadcast channel's OWN roster: zero enumeration RPC, first-class stored outcome
        assert all(c[0] == GROUP_ID for c in gw.participants_calls)
        assert (CHANNEL_ID, "channelParticipantsRecent", 0) not in gw.participants_calls
        assert gw.calls.count("get_participants") == 1  # the group's one Recent page (§11 zero-RPC)
        walled = st.conn.execute(
            "select payload_json, observed_at from raw_records where kind='RosterWalled'"
        ).fetchone()
        payload = json.loads(walled["payload_json"])
        assert payload["group_id"] == CHANNEL_ID and payload["participants_count"] == 10
        assert "broadcast" in payload["reason"]
        assert walled["observed_at"] == T0  # from the ChatFull observation, never "now" (D5)
        acct = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots "
            "where group_id=? and uri is null",
            (CHANNEL_ID,)).fetchone()
        assert (acct["enumerated"], acct["true_count"]) == (0, 10) and "broadcast" in acct["reason"]
        assert res.counts["walled"] == 1

        # 2. the linked group: preflight ChatFull recorded, roster paged, projected
        assert gw.calls.count("get_full_channel") == 1
        assert gw.participants_calls == [(GROUP_ID, "channelParticipantsRecent", 0)]
        rows = {
            r["uri"]: r for r in
            st.conn.execute("select * from participants where group_id=?", (GROUP_ID,))
        }
        assert rows["tg:user:1"]["status"] == "admin" and rows["tg:user:1"]["rank"] == "mod"
        assert rows["tg:user:2"]["join_date"] == "2025-01-01T00:00:02+00:00"
        assert rows["tg:user:3"]["rank"] == "vip"
        users = {r["uri"] for r in st.conn.execute("select uri from users")}
        assert users == {"tg:user:1", "tg:user:2", "tg:user:3"}  # the free full User objects
        edges = {
            (e["subject_uri"], e["predicate"])
            for e in st.conn.execute("select subject_uri, predicate from edges")
        }
        assert ("tg:user:1", "admin_of") in edges and ("tg:user:2", "member_of") in edges
        roster = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots "
            "where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"], roster["reason"]) == (3, 3, None)
        assert st.conn.execute(
            "select count(*) from participant_snapshots where group_id=? and uri is not null",
            (GROUP_ID,)).fetchone()[0] == 3
        group_row = st.conn.execute(
            "select kind, participants_count from channels where id=?", (GROUP_ID,)
        ).fetchone()
        assert (group_row["kind"], group_row["participants_count"]) == ("megagroup", 3)
        kinds = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records where json_extract(context_json,'$.channel_id')=? "
            "order by id", (GROUP_ID,))]
        assert kinds == ["ChatFull", "channels.ChannelParticipants"]
        event = json.loads(
            st.conn.execute("select detail_json from run_events where kind='roster'").fetchone()[0]
        )
        assert (
            event["group_id"], event["enumerated"], event["true_count"], event["walled"]
        ) == (GROUP_ID, 3, 3, None)
        assert res.stopped is None
        assert (
            res.counts["enumerated"] == 3
            and res.counts["participants"] == 3
            and res.counts["users"] == 3
        )
        # §6.5: admin-only sub-methods are detected via rights and SKIPPED — a recorded decision
        skipped = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='admin_only_skipped'").fetchone()[0])
        assert skipped["group_id"] == GROUP_ID and "channels.getAdminLog" in skipped["methods"]
        assert st.conn.execute(
            "select count(*) from run_events where kind='privacy_posture'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_no_linked_group_records_the_walled_channel_then_skips_the_phase(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None)
        gw = _gw()
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.stopped is not None and "linked" in res.stopped
        assert gw.participants_calls == [] and gw.calls.count("get_full_channel") == 0
        assert st.conn.execute(
            "select count(*) from raw_records where kind='RosterWalled'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_recent_pages_until_a_short_page_and_labels_the_shortfall(tmp_path):
    # The 78k->12 reality (spec §6.3): a full first page, then a short one,
    # then STOP; enumerated / true_count is recorded, never presented as complete.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        first = _page(*[_member(i) for i in range(1, 201)], count=78000)
        second = _page(*[_member(i) for i in range(201, 213)], count=78000)
        gw = _gw([first, second], full_channel_by_id={GROUP_ID: _group_full(78000)})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == [
            (GROUP_ID, "channelParticipantsRecent", 0),
            (GROUP_ID, "channelParticipantsRecent", 200),
        ]
        roster = st.conn.execute(
            "select enumerated, true_count from participant_snapshots "
            "where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"]) == (212, 78000)
        assert res.stopped is not None and "212 of 78000" in res.stopped and "--join" in res.stopped
        warning = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'").fetchone()[0])
        assert warning["code"] == "roster_partial" and warning["hint"] == "--join"


@pytest.mark.asyncio
async def test_a_repeating_page_ends_the_loop(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        same = _page(*[_member(i) for i in range(1, 201)], count=1000)
        gw = _gw([same, same, same])
        await ParticipantsCollector().collect(_ctx(st, gw))
        assert len(gw.participants_calls) == 2  # page 2 added nothing new -> stop


@pytest.mark.asyncio
async def test_walled_group_is_recorded_with_its_reason_and_the_join_warning(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw(SkipAndRecord("CHAT_ADMIN_REQUIRED"))
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert [w["group_id"] for w in walled] == [CHANNEL_ID, GROUP_ID]
        assert "CHAT_ADMIN_REQUIRED" in walled[1]["reason"]
        roster = st.conn.execute(
            "select enumerated, true_count, reason from participant_snapshots "
            "where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert (
            (roster["enumerated"], roster["true_count"]) == (0, 3)
            and "CHAT_ADMIN_REQUIRED" in roster["reason"]
        )
        assert res.stopped is not None and "--join" in res.stopped
        assert res.counts["walled"] == 2


@pytest.mark.asyncio
async def test_session_age_gate_refuses_enumeration_without_unsafe(tmp_path):
    young = {
        "authorizations": [{"current": True, "date_created": datetime.now(UTC) - timedelta(days=1)}]
    }
    old_date = datetime.now(UTC) - timedelta(days=30)
    old = {"authorizations": [{"current": True, "date_created": old_date}]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2))], authorizations=young)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.stopped is not None and "--unsafe" in res.stopped
        assert gw.participants_calls == []
        assert gw.calls.count("get_full_channel") == 0  # the gate runs BEFORE any group RPC (§6.1)
        assert st.conn.execute(
            "select count(*) from run_events where kind='warning'"
        ).fetchone()[0] == 1
        assert json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'"
        ).fetchone()[0])["code"] == "session_age_gate"
    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2))], authorizations=old)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.stopped is None and gw.participants_calls != []


@pytest.mark.asyncio
async def test_zero_rpc_vectors_run_even_when_the_gate_refuses(tmp_path):
    young = {"authorizations": [{"current": True, "date_created": datetime.now(UTC)}]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        join = {"_": "MessageService", "id": 40, "date": JOINED,
                "from_id": {"_": "PeerUser", "user_id": 8},
                "action": {"_": "MessageActionChatJoinedByLink", "inviter_id": 4},
                "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
        upsert_message(
            st, GROUP_ID, join,
            st.add_raw("MessageService", join, "stranger", {"channel_id": GROUP_ID}),
            T0, "stranger",
        )
        gw = _gw([_page(_member(2))], authorizations=young)
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(unsafe=False)))
        assert res.counts["service_joins"] == 1
        assert st.conn.execute(
            "select status from participants where uri='tg:user:8'"
        ).fetchone()[0] == "member"
        assert ("tg:user:8", "invited_by", "tg:user:4") in {
            (e[0], e[1], e[2])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }


@pytest.mark.asyncio
async def test_privacy_posture_is_recorded_once_per_run_across_both_person_phases(tmp_path):
    from paperboy.collectors.profiles import ProfilesCollector

    rules = {
        "_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}],
        "chats": [], "users": [],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        st.begin_run("run-1")
        _seed_channel(st)
        gw = _gw([_page(_member(2))], privacy={k: rules for k in ("phone", "lastseen", "photo")})
        ctx = _ctx(st, gw)
        await ParticipantsCollector().collect(ctx)
        await ProfilesCollector().collect(ctx)
        assert gw.calls.count("get_privacy") == 3  # participants recorded; profiles didn't repeat
        assert st.conn.execute(
            "select count(*) from run_events where kind='privacy_posture'"
        ).fetchone()[0] == 1
        st.begin_run("run-2")
        await ProfilesCollector().collect(ctx)
        assert gw.calls.count("get_privacy") == 6  # a new run records again


@pytest.mark.asyncio
async def test_preflight_answering_for_another_channel_does_not_enumerate(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        wrong = _group_full()
        wrong["full_chat"]["id"] = 999
        gw = _gw([_page(_member(2))], full_channel_by_id={GROUP_ID: wrong})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == [] and res.counts["skipped"] == 1
        assert st.conn.execute("select count(*) from participants").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_megagroup_target_enumerates_its_own_roster_from_stored_flags(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None, kind="megagroup")
        pages = {"channelParticipantsRecent": [_page(_member(2), count=10)]}
        gw = FakeGateway({"participants": {CHANNEL_ID: pages}})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.calls.count("get_full_channel") == 0  # target's ChatFull already in `channels`
        assert gw.participants_calls == [(CHANNEL_ID, "channelParticipantsRecent", 0)]
        assert st.conn.execute(
            "select count(*) from raw_records where kind='RosterWalled'"
        ).fetchone()[0] == 0
        assert res.counts["rosters"] == 1 and res.counts["enumerated"] == 1


@pytest.mark.asyncio
async def test_phase_stop_without_channel_context(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _ctx(st, _gw())
        ctx.channel_id = None
        with pytest.raises(PhaseStop):
            await ParticipantsCollector().collect(ctx)


def test_applies_to_channel_like_targets():
    assert ParticipantsCollector().applies_to(parse_target("@durov"))
    assert not ParticipantsCollector().applies_to(parse_target("+15551234567"))
