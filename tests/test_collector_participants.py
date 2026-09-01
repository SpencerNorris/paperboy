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
from paperboy.store.sync import set_state
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


def _seed_channel(
    st: Store, *, linked: int | None = GROUP_ID, kind: str = "broadcast",
    linked_flags: dict | None = None, target_flags: dict | None = None,
) -> None:
    raw_id = st.add_raw(
        "ChatFull", {"_": "ChatFull", "full_chat": {"id": CHANNEL_ID}}, "stranger",
        {"channel_id": CHANNEL_ID}, observed_at=T0,
    )
    chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C", "username": "c"}
    chan["broadcast" if kind == "broadcast" else "megagroup"] = True
    chan.update(target_flags or {})
    upsert_channel(
        st, {"_": "channelFull", "id": CHANNEL_ID, "pts": 1, "linked_chat_id": linked,
             "participants_count": 10},
        chan, raw_id, T0,
    )
    if linked:
        peer = {"_": "Channel", "id": linked, "access_hash": 4242, "title": "G", "megagroup": True}
        peer.update(linked_flags or {})
        upsert_peer(st, peer, raw_id, T0, seen_in_chat=None, seen_in_msg=None)


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


def _self_participant(uid: int) -> dict:
    return {"_": "ChannelParticipantSelf", "user_id": uid, "date": JOINED, "inviter_id": None}


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


def _seed_group_comment(
    st: Store, msg_id: int, user_id: int, *, reactions: dict | None = None
) -> None:
    m = {"_": "Message", "id": msg_id, "message": "c", "date": 1767322445,
         "from_id": {"_": "PeerUser", "user_id": user_id},
         "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if reactions is not None:
        m["reactions"] = reactions
    raw_id = st.add_raw("Message", m, "stranger", {"channel_id": GROUP_ID}, observed_at=T0)
    upsert_message(st, GROUP_ID, m, raw_id, T0, "stranger")
    # what `history._observe_message` does for an author: the min stub with provenance —
    # without a `peers` row `input_user_ref` is None and the oracle has nothing to ask
    upsert_peer(st, {"_": "User", "id": user_id, "min": True}, raw_id, T0,
                seen_in_chat=GROUP_ID, seen_in_msg=msg_id)


def _seed_group_comment_no_peer(st: Store, msg_id: int, user_id: int) -> None:
    """Like `_seed_group_comment`, but WITHOUT the peer stub — `from_uri`
    still makes the author an oracle candidate (the query reads `messages`
    directly), but with no `peers`/`users` row at all `input_user_ref` has
    nothing to build a ref from: an unresolvable candidate."""
    m = {"_": "Message", "id": msg_id, "message": "c", "date": 1767322445,
         "from_id": {"_": "PeerUser", "user_id": user_id},
         "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    raw_id = st.add_raw("Message", m, "stranger", {"channel_id": GROUP_ID}, observed_at=T0)
    upsert_message(st, GROUP_ID, m, raw_id, T0, "stranger")


def _answer(uid: int) -> dict:
    return {
        "_": "ChannelParticipant", "participant": _member(uid), "chats": [],
        "users": [_user(uid)],
    }


@pytest.mark.asyncio
async def test_oracle_runs_only_on_a_partial_roster_and_is_bounded(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302), (23, 303), (2, 304)):
            _seed_group_comment(st, mid, uid)  # 2 is also in the roster; 21-23 are not
        complete = _gw(
            [_page(_member(2), count=1)],
            participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}},
        )
        await ParticipantsCollector().collect(_ctx(st, complete))
        assert complete.participant_calls == []  # complete roster: no oracle spend

    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302), (23, 303), (2, 304)):
            _seed_group_comment(st, mid, uid)
        partial = _gw(
            [_page(_member(2), count=307)],
            participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}},
        )
        res = await ParticipantsCollector().collect(
            _ctx(st, partial, _settings(participant_oracle_budget=2))
        )
        assert partial.participant_calls == [(GROUP_ID, 21), (GROUP_ID, 22)]  # bounded, uri order
        assert res.counts["oracle"] == 2
        rows = {
            r["uri"]: r["status"]
            for r in st.conn.execute("select uri, status from participants")
        }
        assert rows["tg:user:21"] == "member" and rows["tg:user:22"] == "left"
        assert "tg:user:23" not in rows
        raw = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records where json_extract(context_json,'$.user_id') "
            "in (21, 22) order by id")]
        assert raw == ["channels.ChannelParticipant", "UserNotParticipant"]
        edge = st.conn.execute(
            "select evidence_json from edges where subject_uri='tg:user:21' "
            "and predicate='member_of'").fetchone()
        assert '"source": "oracle"' in edge["evidence_json"]
        assert st.conn.execute(
            "select count(*) from users where uri='tg:user:21'"
        ).fetchone()[0] == 1

        # a later run asks only about users still without an answer
        again = _gw(
            [_page(_member(2), count=307)],
            participant={GROUP_ID: {21: _answer(21), 22: None, 23: _answer(23)}},
        )
        await ParticipantsCollector().collect(
            _ctx(st, again, _settings(participant_oracle_budget=2))
        )
        assert again.participant_calls == [(GROUP_ID, 23)]


@pytest.mark.asyncio
async def test_oracle_wall_ends_the_oracle_loop_for_that_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for uid, mid in ((21, 301), (22, 302)):
            _seed_group_comment(st, mid, uid)
        gw = _gw(
            [_page(_member(2), count=307)],
            participant={GROUP_ID: {21: SkipAndRecord("CHAT_ADMIN_REQUIRED"), 22: _answer(22)}},
        )
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participant_calls == [(GROUP_ID, 21)]
        assert res.counts["skipped"] == 1 and res.counts["oracle"] == 0


@pytest.mark.asyncio
async def test_oracle_confirmed_members_count_toward_enumerated(tmp_path):
    """A positive oracle answer writes the SAME kind of confirmed-member row
    (`participants`, `member_of`) as a roster page, so it must count toward
    the run's `enumerated` total too -- found running the Leg 3 DoD smoke.
    The shortfall warning stays scoped to the roster PAGE alone (spec §6.3;
    round-2 review) -- it must not silently switch to the oracle-mutated
    set, which would make it disagree with the `roster` event and the
    `participant_snapshots` row for the very same run."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 301, 21)
        gw = _gw(
            [_page(_member(2), count=3)],
            participant={GROUP_ID: {21: _answer(21)}},
        )
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.counts["oracle"] == 1
        assert res.counts["enumerated"] == 2, "the roster's 1 + the oracle's confirmed 21"
        # the warning/roster accounting stays roster-page-only: 1 (not 2) of 3
        assert res.stopped is not None and "1 of 3" in res.stopped
        roster_event = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='roster'").fetchone()[0])
        warning_event = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'").fetchone()[0])
        snapshot = st.conn.execute(
            "select enumerated from participant_snapshots where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert roster_event["enumerated"] == warning_event["enumerated"] == snapshot["enumerated"]


@pytest.mark.asyncio
async def test_oracle_completing_the_roster_never_claims_a_complete_roster_is_short(tmp_path):
    """Boundary the previous fix (dd01185) missed by one case: when the
    roster PAGE alone found 1 of a declared 2 and the oracle confirms the
    other 1, the run's total IS complete (2 of 2) -- but the shortfall
    warning must never say so while still telling the operator to spend the
    tool's one documented write for a roster that is, by its own numbers,
    already done. It reports the roster-page figure (1 of 2) instead, which
    stays self-consistent with the `roster` event and the roster snapshot."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 301, 21)
        gw = _gw(
            [_page(_member(2), count=2)],
            full_channel_by_id={GROUP_ID: _group_full(2)},
            participant={GROUP_ID: {21: _answer(21)}},
        )
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.counts["oracle"] == 1
        assert res.counts["enumerated"] == 2  # the run DID find everyone, combined
        assert res.stopped is not None
        assert "2 of 2" not in res.stopped  # never claim complete while still warning
        roster_event = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='roster'").fetchone()[0])
        warning_event = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning'").fetchone()[0])
        snapshot = st.conn.execute(
            "select enumerated from participant_snapshots where group_id=? and uri is null",
            (GROUP_ID,)).fetchone()
        assert roster_event["enumerated"] == warning_event["enumerated"] == snapshot["enumerated"]


@pytest.mark.asyncio
async def test_join_flag_joins_a_left_group_then_pages_admins_and_bots(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        admins = {"_": "ChannelParticipants", "count": 1, "chats": [], "users": [_user(9)],
                  "participants": [{"_": "ChannelParticipantCreator", "user_id": 9,
                                    "admin_rights": {"_": "ChatAdminRights"}, "rank": "founder"}]}
        bots = _page(_member(30), count=1, users=[_user(30, bot=True)])
        gw = FakeGateway({
            "full_channel_by_id": {GROUP_ID: _group_full(left=True)},
            "participants": {GROUP_ID: {"channelParticipantsRecent": [_page(_member(2), count=3)],
                                        "channelParticipantsAdmins": [admins],
                                        "channelParticipantsBots": [bots]}},
            "join": {"_": "Updates", "updates": []},
        })
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1
        assert [c[1] for c in gw.participants_calls] == [
            "channelParticipantsRecent", "channelParticipantsAdmins", "channelParticipantsBots",
        ]
        assert st.conn.execute(
            "select status from participants where uri='tg:user:9'"
        ).fetchone()[0] == "creator"
        assert st.conn.execute(
            "select count(*) from run_events where kind='join'"
        ).fetchone()[0] == 1
        assert res.stopped is None  # joined: the shortfall is not a --join warning any more

    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = FakeGateway({
            "full_channel_by_id": {GROUP_ID: _group_full(left=False)},
            "participants": {GROUP_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]}},
        })
        await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 0  # already a member: never re-joined
        assert [c[1] for c in gw.participants_calls][1:] == [
            "channelParticipantsAdmins", "channelParticipantsBots",
        ]


@pytest.mark.asyncio
async def test_join_never_fires_without_the_flag_and_a_refused_join_falls_back(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2), count=1)], join={"_": "Updates", "updates": []})
        await ParticipantsCollector().collect(_ctx(st, gw))
        assert "join_channel" not in gw.calls
    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2), count=1)], join=SkipAndRecord("INVITE_REQUEST_SENT"))
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1
        assert [c[1] for c in gw.participants_calls] == ["channelParticipantsRecent"]  # un-joined
        assert res.counts["enumerated"] == 1


@pytest.mark.asyncio
async def test_reaction_lists_are_bounded_newest_first_and_resumable(tmp_path):
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2, "reaction": {}}],
    }

    def _list(*uids: int, next_offset=None) -> dict:
        return {
            "_": "MessageReactionsList", "count": len(uids), "chats": [],
            "next_offset": next_offset,
            "users": [_user(u) for u in uids],
            "reactions": [
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": u},
                 "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "🔥"}}
                for u in uids
            ],
        }

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for mid in (401, 402, 403):
            _seed_group_comment(st, mid, 2, reactions=reacted)
        _seed_group_comment(st, 404, 2)  # no reactions: never asked
        gw = _gw([_page(_member(2), count=1)], reactions={GROUP_ID: {
            403: [_list(31, 32, next_offset="p2"), _list(33)], 402: _list(34), 401: _list(35),
        }})
        res = await ParticipantsCollector().collect(
            _ctx(st, gw, _settings(participant_reactions_budget=2))
        )
        assert gw.reactions_calls == [
            (GROUP_ID, 403, None), (GROUP_ID, 403, "p2"), (GROUP_ID, 402, None),
        ]
        assert res.counts["reaction_lists"] == 3
        edges = {(e[0], e[2]) for e in st.conn.execute(
            "select subject_uri, predicate, object_uri from edges where predicate='reacted_to'")}
        assert edges == {
            ("tg:user:31", "tg:msg:77/403"), ("tg:user:32", "tg:msg:77/403"),
            ("tg:user:33", "tg:msg:77/403"), ("tg:user:34", "tg:msg:77/402"),
        }
        assert st.conn.execute(
            "select count(*) from users where uri in ('tg:user:31','tg:user:34')"
        ).fetchone()[0] == 2
        peer = st.conn.execute(
            "select seen_in_chat, seen_in_msg from peers where uri='tg:user:31'"
        ).fetchone()
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 403)

        again = _gw([_page(_member(2), count=1)], reactions={GROUP_ID: {401: _list(35)}})
        await ParticipantsCollector().collect(
            _ctx(st, again, _settings(participant_reactions_budget=2))
        )
        assert again.reactions_calls == [(GROUP_ID, 401, None)]  # 403/402 fetched: resumable


@pytest.mark.asyncio
async def test_reaction_list_wall_is_a_recorded_skip(tmp_path):
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2, "reaction": {}}],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 401, 2, reactions=reacted)
        _seed_group_comment(st, 402, 2, reactions=reacted)
        gw = _gw(
            [_page(_member(2), count=1)],
            reactions={GROUP_ID: {402: SkipAndRecord("BROADCAST_FORBIDDEN")}},
        )
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.reactions_calls == [(GROUP_ID, 402, None)]  # the wall ends the vector
        assert res.counts["skipped"] == 1 and res.stopped is None


@pytest.mark.asyncio
async def test_a_complete_roster_including_self_reports_no_shortfall(tmp_path):
    """`true_count` (Telegram's own `channelParticipants.count`) includes the
    collecting account when it is a member, but `write_participant` refuses
    to store self (issue #12) so self can never land in `enumerated` the
    same way. Left unreconciled, a completely enumerated roster with self as
    one of its members reports a permanent phantom shortfall of exactly 1 on
    every run — structural, not transient (correctness round-2 review)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:1", "id": 1})
        _seed_channel(st)
        page = _page(_member(2), _member(3), _self_participant(1), count=3)
        gw = _gw([page], full_channel_by_id={GROUP_ID: _group_full(3)})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.stopped is None
        roster = st.conn.execute(
            "select enumerated, true_count from participant_snapshots "
            "where group_id=? and uri is null", (GROUP_ID,)).fetchone()
        assert (roster["enumerated"], roster["true_count"]) == (3, 3)
        assert st.conn.execute(
            "select count(*) from participants where uri='tg:user:1'"
        ).fetchone()[0] == 0  # self is still never a stored subject (#12)
        assert st.conn.execute(
            "select count(*) from users where uri='tg:user:1'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_linked_broadcast_peer_is_never_enumerated_or_joined(tmp_path):
    """`channelFull.linked_chat_id` is bidirectional (research
    sources/mtproto-channel-messages.md:154): from a discussion SUPERGROUP it
    points back at its BROADCAST channel. That broadcast's subscriber roster
    is never enumerable below admin regardless of which side of the link
    discovered it — enumerating it, or joining it under --join, would
    violate the module's own zero-enumeration-RPC promise for broadcasts."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=GROUP_ID, kind="megagroup")
        broadcast_full = {
            "_": "ChatFull",
            "full_chat": {"_": "channelFull", "id": GROUP_ID, "participants_count": 50000,
                          "pts": 1, "can_view_participants": True, "participants_hidden": False},
            "chats": [{"_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "B",
                       "broadcast": True, "left": True}],
            "users": [],
        }
        gw = _gw([_page(_member(2))], full_channel_by_id={GROUP_ID: broadcast_full})
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert not any(c[0] == GROUP_ID for c in gw.participants_calls)
        # the TARGET (a megagroup with unknown membership) is legitimately
        # joined under --join (`_maybe_join`'s documented "membership
        # unknown" branch) — what must NEVER happen is the LINKED BROADCAST
        # being joined, so check the join audit trail by group, not the
        # gateway's blanket join_channel call count.
        joins = [
            json.loads(r[0])["group_id"]
            for r in st.conn.execute("select detail_json from run_events where kind='join'")
        ]
        assert GROUP_ID not in joins
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert any(w["group_id"] == GROUP_ID and "broadcast" in w["reason"] for w in walled)
        assert res.counts["walled"] == 1


@pytest.mark.asyncio
async def test_hidden_linked_roster_is_walled_before_any_enumeration_rpc(tmp_path):
    """spec §6.1: an owner-hidden roster (`participants_hidden` /
    `can_view_participants`) is exactly as structural a wall as a broadcast
    peer — walled with zero enumeration RPC, not discovered the expensive
    way via a `CHAT_ADMIN_REQUIRED` on the first page."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw([_page(_member(2))], full_channel_by_id={GROUP_ID: _group_full(hidden=True)})
        await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == []
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert any(
            w["group_id"] == GROUP_ID and "participants_hidden" in w["reason"] for w in walled
        )


@pytest.mark.asyncio
async def test_a_hidden_megagroup_target_is_walled_from_its_own_stored_flags(tmp_path):
    """The same §6.1 wall applies to the TARGET's own roster when its stored
    `channels.flags_json` (from an earlier `channel` phase observation)
    already carries `participants_hidden` — zero enumeration RPC, exactly
    like the linked group's own preflight wall (`_roster_wall_reason` is
    shared by both paths)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        raw_id = st.add_raw(
            "ChatFull", {"_": "ChatFull", "full_chat": {"id": CHANNEL_ID}}, "stranger",
            {"channel_id": CHANNEL_ID}, observed_at=T0,
        )
        chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C",
                "username": "c", "megagroup": True}
        upsert_channel(
            st, {"_": "channelFull", "id": CHANNEL_ID, "pts": 1, "linked_chat_id": None,
                 "participants_count": 10, "participants_hidden": True,
                 "can_view_participants": False},
            chan, raw_id, T0,
        )
        gw = FakeGateway({
            "participants": {CHANNEL_ID: {"channelParticipantsRecent": [_page(_member(2))]}},
        })
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == []
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert any(
            w["group_id"] == CHANNEL_ID and "participants_hidden" in w["reason"] for w in walled
        )
        assert res.stopped is None


@pytest.mark.asyncio
async def test_reaction_list_peer_write_is_fill_only_and_never_overwrites_authorship(tmp_path):
    """A reaction is not a documented `inputPeerFromMessage` context (research
    §8.7 lists author, forward header and mention) — a user's reaction to
    someone else's message must never replace their authorship provenance,
    mirroring `store.reactions.backfill_recent_reactions`'s own fill-only
    rule for the zero-RPC vector (correctness round-2 review)."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 301, 21)  # user 21 authored 301 -> provenance (77, 301)
        _seed_group_comment(st, 401, 2, reactions=reacted)  # someone else's message, reacted
        gw = _gw(
            [_page(_member(2), count=1)],
            reactions={GROUP_ID: {401: {
                "_": "MessageReactionsList", "count": 1, "chats": [], "next_offset": None,
                "users": [_user(21)],
                "reactions": [
                    {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 21},
                     "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "🔥"}},
                ],
            }}},
        )
        await ParticipantsCollector().collect(_ctx(st, gw))
        peer = st.conn.execute(
            "select seen_in_chat, seen_in_msg from peers where uri='tg:user:21'"
        ).fetchone()
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 301)
        # the reaction is still recorded as an edge — only the provenance write is fill-only
        assert ("tg:user:21", "reacted_to", "tg:msg:77/401") in {
            (e[0], e[1], e[2])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }


@pytest.mark.asyncio
async def test_reaction_list_peer_write_still_fills_provenance_for_a_new_peer(tmp_path):
    """The fill-only guard must not regress the ordinary case: a reactor with
    NO prior provenance still gets the reaction's `(chat, msg)` recorded."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 401, 2, reactions=reacted)
        gw = _gw(
            [_page(_member(2), count=1)],
            reactions={GROUP_ID: {401: {
                "_": "MessageReactionsList", "count": 1, "chats": [], "next_offset": None,
                "users": [_user(31)],
                "reactions": [
                    {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 31},
                     "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "🔥"}},
                ],
            }}},
        )
        await ParticipantsCollector().collect(_ctx(st, gw))
        peer = st.conn.execute(
            "select seen_in_chat, seen_in_msg from peers where uri='tg:user:31'"
        ).fetchone()
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 401)


@pytest.mark.asyncio
async def test_linked_group_honours_join_to_send_like_discussion_does(tmp_path):
    """The linked group is shared with `discussion`, whose own docstring
    states that reading it requires membership once `join_to_send` is set
    (issue #20). A roster read is not exempt from that just because it is
    not a message read — honour the same flag the same way, for the SAME
    group, instead of silently reading it un-joined regardless."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked_flags={"join_to_send": True})
        gw = _gw([_page(_member(2))])
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert all(c[0] != GROUP_ID for c in gw.participants_calls)
        assert gw.calls.count("get_full_channel") == 0
        assert gw.calls.count("join_channel") == 0
        assert res.stopped is not None and "join_to_send" in res.stopped
        assert "participants group" in res.stopped  # named for THIS phase, not "discussion"

    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st, linked_flags={"join_to_send": True})
        gw = _gw(
            [_page(_member(2), count=1)],
            full_channel_by_id={GROUP_ID: _group_full(left=False)},
        )
        res = await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1  # joined once, via the needs_join gate
        assert gw.calls.count("get_full_channel") == 1
        assert gw.participants_calls[0] == (GROUP_ID, "channelParticipantsRecent", 0)
        assert res.counts["rosters"] == 1
        assert st.conn.execute(
            "select count(*) from run_events where kind='join'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_broadcast_targets_own_recent_reactions_sample_is_swept(tmp_path):
    """The zero-RPC `recent_reactions` sample on a BROADCAST's own posts is
    free — it costs no RPC and is not the walled `getMessageReactionsList`
    vector — so it must not be dropped just because the target itself is
    never enumerated via `getParticipants` (correctness round-2 review)."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
        "recent_reactions": [
            {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 51},
             "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}},
        ],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)  # target is a broadcast (default kind)
        post = {"_": "Message", "id": 900, "message": "post", "date": 1767322000,
                "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}, "reactions": reacted}
        raw_id = st.add_raw("Message", post, "stranger", {"channel_id": CHANNEL_ID}, observed_at=T0)
        upsert_message(st, CHANNEL_ID, post, raw_id, T0, "stranger")
        gw = _gw()
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.counts["reactors"] == 1
        assert ("tg:user:51", "reacted_to", "tg:msg:5/900") in {
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }


@pytest.mark.asyncio
async def test_broadcast_with_no_linked_group_still_sweeps_its_own_recent_reactions(tmp_path):
    """Round-3 review (blocking): a BROADCAST target with NO linked discussion
    group — the single most common paperboy target — must still sweep its
    own zero-RPC `recent_reactions` sample before the phase reports its full
    skip. The previous fix moved the sweep above the `if isinstance(linked,
    str):` block textually but the `return` for the no-comment-section case
    was still ABOVE it, so `zero_rpc_ids` was never reached on this exact
    path; the existing `test_broadcast_targets_own_recent_reactions_sample_is_swept`
    never caught it because `_seed_channel`'s default `linked=GROUP_ID`
    skipped the `return` branch entirely."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
        "recent_reactions": [
            {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 51},
             "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}},
        ],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None)  # broadcast target, no linked discussion group
        post = {"_": "Message", "id": 900, "message": "post", "date": 1767322000,
                "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}, "reactions": reacted}
        raw_id = st.add_raw("Message", post, "stranger", {"channel_id": CHANNEL_ID}, observed_at=T0)
        upsert_message(st, CHANNEL_ID, post, raw_id, T0, "stranger")
        gw = _gw()
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.counts["reactors"] == 1
        assert ("tg:user:51", "reacted_to", "tg:msg:5/900") in {
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }
        # the phase still reports its full skip — the fix does not change
        # that, it only ensures the free vectors ran first.
        assert res.stopped is not None and "zero-RPC vectors still swept" in res.stopped
        assert gw.calls.count("get_full_channel") == 0  # still zero-RPC


@pytest.mark.asyncio
async def test_reaction_list_pagination_is_hard_capped_against_a_repeating_offset(tmp_path):
    """Unlike `_page` (three independent stop conditions: empty page, short
    page, no-new-members), a reaction list's only natural stop is a falsy
    `next_offset` — a server that always returns one would otherwise spin
    forever, growing the DB unbounded."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
    }
    pages = [
        {"_": "MessageReactionsList", "count": 0, "chats": [], "users": [],
         "reactions": [], "next_offset": f"p{i}"}
        for i in range(60)
    ]
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 401, 2, reactions=reacted)
        gw = _gw([_page(_member(2), count=1)], reactions={GROUP_ID: {401: pages}})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert res.counts["reaction_lists"] == 50  # capped (`_REACTIONS_MAX_PAGES`), not 60
        assert res.stopped is None
        # Round-3 review: a truncated reactor list is a partial observation —
        # recorded, counted, and NOT treated as done by the next run.
        from paperboy.store.reactions import fetched_reaction_lists

        assert res.counts["skipped"] == 1
        truncated = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='warning' order by id desc limit 1"
        ).fetchone()[0])
        assert (truncated["code"], truncated["msg_id"]) == ("reaction_list_truncated", 401)
        assert fetched_reaction_lists(st, GROUP_ID) == set()


@pytest.mark.asyncio
async def test_oracle_budget_is_spent_on_resolvable_candidates_not_wasted_on_dead_ones(tmp_path):
    """The budget slices AFTER filtering to resolvable `input_user_ref`s, not
    before: an unresolvable candidate must never occupy a budget slot the
    same `ORDER BY uri` query would otherwise hand to someone the oracle can
    actually ask — and, unfixed, that starvation is PERMANENT (the dead
    candidate sorts first on every future run too, since it never gets
    written to `participants` either)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment_no_peer(st, 300, 20)  # authored, but unresolvable: no peer row
        _seed_group_comment(st, 301, 21)  # authored AND resolvable via inputUserFromMessage
        gw = _gw(
            [_page(_member(2), count=3)],
            participant={GROUP_ID: {21: _answer(21)}},
        )
        res = await ParticipantsCollector().collect(
            _ctx(st, gw, _settings(participant_oracle_budget=1))
        )
        assert gw.participant_calls == [(GROUP_ID, 21)]
        assert res.counts["oracle"] == 1


@pytest.mark.asyncio
async def test_user_snapshot_method_matches_the_producing_rpc(tmp_path):
    """`user_snapshots.method` is both provenance and the dedupe partition
    (`add_user_snapshot` keys on `(uri, method)`) — each vector must record
    the RPC that actually produced the observation, not share one
    hard-coded value (correctness round-2 review)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_group_comment(st, 301, 21)
        gw = _gw(
            [_page(_member(2), count=3)],
            participant={GROUP_ID: {21: _answer(21)}},
        )
        await ParticipantsCollector().collect(_ctx(st, gw))
        methods = {
            r["uri"]: r["method"]
            for r in st.conn.execute("select uri, method from user_snapshots")
        }
        assert methods["tg:user:2"] == "channels.getParticipants"
        assert methods["tg:user:21"] == "channels.getParticipant"


@pytest.mark.asyncio
async def test_join_flag_never_rejoins_a_megagroup_target_already_a_member(tmp_path):
    """Round-3 review (blocking): the TARGET's roster carried no membership
    flags, so `--join` on a megagroup target we already belong to issued an
    active `channels.joinChannel` on EVERY run."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, linked=None, kind="megagroup", target_flags={"left": False})
        gw = FakeGateway({
            "participants": {
                CHANNEL_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]},
            },
            "join": {"_": "Updates", "updates": []},
        })
        for _ in range(2):
            await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert "join_channel" not in gw.calls
        joins = st.conn.execute("select count(*) from run_events where kind='join'")
        assert joins.fetchone()[0] == 0
        # known membership still unlocks the member-only filters
        assert [c[1] for c in gw.participants_calls[:3]] == [
            "channelParticipantsRecent", "channelParticipantsAdmins", "channelParticipantsBots",
        ]
    with Store.open(tmp_path / "q.sqlite") as st:
        _seed_channel(st, linked=None, kind="megagroup", target_flags={"left": True})
        gw = FakeGateway({
            "participants": {
                CHANNEL_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]},
            },
            "join": {"_": "Updates", "updates": []},
        })
        await ParticipantsCollector().collect(_ctx(st, gw, _settings(allow_join=True)))
        assert gw.calls.count("join_channel") == 1  # a group we have left IS joined, once
        joins = st.conn.execute("select count(*) from run_events where kind='join'")
        assert joins.fetchone()[0] == 1


@pytest.mark.asyncio
async def test_run_level_budgets_are_shared_across_rosters(tmp_path):
    """Round-3 review: `participant_reactions_budget` is a per-RUN cap; a
    megagroup target with a linked group has TWO rosters and must not spend
    it twice."""
    reacted = {
        "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
    }
    def _list(uid: int) -> dict:
        return {"_": "MessageReactionsList", "count": 1, "chats": [], "next_offset": None,
                "users": [_user(uid)],
                "reactions": [{
                    "_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": uid},
                    "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "x"},
                }]}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, kind="megagroup", target_flags={"left": False})
        for mid in (701, 702):
            post = {"_": "Message", "id": mid, "message": "p", "date": 1767322000,
                    "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}, "reactions": reacted}
            rid = st.add_raw(
                "Message", post, "stranger", {"channel_id": CHANNEL_ID}, observed_at=T0
            )
            upsert_message(st, CHANNEL_ID, post, rid, T0, "stranger")
        _seed_group_comment(st, 801, 2, reactions=reacted)
        _seed_group_comment(st, 802, 3, reactions=reacted)
        gw = FakeGateway({
            "full_channel_by_id": {GROUP_ID: _group_full(left=False)},
            "participants": {
                CHANNEL_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]},
                GROUP_ID: {"channelParticipantsRecent": [_page(_member(2), count=1)]},
            },
            "reactions": {CHANNEL_ID: {701: _list(31), 702: _list(32)},
                          GROUP_ID: {801: _list(33), 802: _list(34)}},
        })
        res = await ParticipantsCollector().collect(
            _ctx(st, gw, _settings(participant_reactions_budget=3))
        )
        assert res.counts["rosters"] == 2
        assert len(gw.reactions_calls) == 3  # 2 on the target + 1 on the group, never 4
        assert [c[0] for c in gw.reactions_calls] == [CHANNEL_ID, CHANNEL_ID, GROUP_ID]


@pytest.mark.asyncio
async def test_preflight_without_a_channel_object_is_walled_not_enumerated(tmp_path):
    """Round-3 review: `broadcast`/`megagroup` live on the `Channel` object;
    with no Channel in the chats vector the wall check could not see them,
    so a broadcast could have been paged. Now an audited preflight wall."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        no_channel = {**_group_full(), "chats": []}
        gw = _gw([_page(_member(2))], full_channel_by_id={GROUP_ID: no_channel})
        res = await ParticipantsCollector().collect(_ctx(st, gw))
        assert gw.participants_calls == []
        walled = [json.loads(r[0]) for r in st.conn.execute(
            "select payload_json from raw_records where kind='RosterWalled' order by id")]
        assert walled[-1]["group_id"] == GROUP_ID and "no Channel object" in walled[-1]["reason"]
        assert res.counts["walled"] == 2  # the broadcast target + the unreadable group


@pytest.mark.asyncio
async def test_no_access_hash_reason_names_the_running_phase(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        st.conn.execute("update peers set access_hash=NULL where uri='tg:channel:77'")
        res = await ParticipantsCollector().collect(_ctx(st, _gw()))
        assert res.stopped is not None
        assert "participants group 77: no access hash known" in res.stopped
        assert "discussion group" not in res.stopped
