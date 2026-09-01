"""The `profiles` collector, spec §7: triage always; full enrichment behind --profiles."""

from __future__ import annotations  # noqa: I001

import json
import logging

import pytest
from paperboy.collectors.profiles import ProfilesCollector

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import get_state, set_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77
T0 = "2026-01-01T00:00:00+00:00"


def _settings(tmp_path, **over):
    return load_settings("default", {"data_dir": tmp_path, **over})


def _ctx(st, gw, settings, tier="stranger"):
    return CollectContext(
        gw, st, settings, parse_target("@x"),
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, tier, logging.getLogger("t"), "p",
    )


def _seed_channel(st: Store, linked: int | None = GROUP_ID) -> None:
    raw_id = st.add_raw("ChatFull", {"_": "ChatFull"}, "stranger", None)
    upsert_channel(
        st,
        {
            "_": "channelFull", "id": CHANNEL_ID, "pts": 1,
            "linked_chat_id": linked, "participants_count": 10,
        },
        {
            "_": "Channel", "id": CHANNEL_ID, "access_hash": 9,
            "title": "C", "username": "c", "broadcast": True,
        },
        raw_id, T0,
    )
    # `upsert_channel` only writes `channels`; the real `channel` collector
    # ALSO writes a `peers` row for the target from its `resolve()` vector
    # (channel.py's `for obj in (*payload.get("chats", []), ...)`), which is
    # what makes a from-message ref into the target itself resolvable. A
    # fixture that skips this understates what `channel` always establishes
    # before `profiles` ever runs.
    upsert_peer(
        st,
        {
            "_": "Channel", "id": CHANNEL_ID, "access_hash": 9,
            "title": "C", "username": "c", "broadcast": True,
        },
        raw_id, T0, seen_in_chat=None, seen_in_msg=None,
    )
    if linked:
        upsert_peer(
            st,
            {"_": "Channel", "id": linked, "access_hash": 4242, "title": "G", "megagroup": True},
            raw_id, T0, seen_in_chat=None, seen_in_msg=None,
        )


def _seed_stub(
    st: Store, user_id: int, *, chat: int | None = GROUP_ID, msg: int | None = 200
) -> None:
    raw_id = st.add_raw(
        "Message", {"_": "Message", "id": msg or 0}, "stranger", {"channel_id": chat}
    )
    upsert_peer(
        st, {"_": "User", "id": user_id, "min": True}, raw_id, T0,
        seen_in_chat=chat, seen_in_msg=msg,
    )


def _user(user_id: int, **extra) -> dict:
    return {"_": "User", "id": user_id, "access_hash": user_id * 10, "first_name": f"U{user_id}",
            "username": f"u{user_id}", "phone": None, "photo": None, "status": None,
            "restriction_reason": [], "usernames": [], **extra}


def _gw(users: dict[int, dict | SkipAndRecord], **more) -> FakeGateway:
    return FakeGateway({"users": users, **more})


@pytest.mark.asyncio
async def test_triage_resolves_min_stubs_via_from_message_and_writes_users(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        _seed_stub(st, 12, msg=201)
        gw = _gw({11: _user(11), 12: _user(12, phone="+15550002222")})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.users_calls == [[11, 12]]
        # case-2 refs: built from the stub's stored provenance + the group's hash
        assert gw.calls.count("get_users") == 1
        rows = {r["uri"]: r for r in st.conn.execute("select * from users")}
        assert rows["tg:user:11"]["first_name"] == "U11"
        assert rows["tg:user:11"]["enriched_at"] is None
        assert rows["tg:user:12"]["phone"] == "+15550002222"
        assert json.loads(rows["tg:user:12"]["field_states_json"])["phone"] == {"state": "present"}
        peer = st.conn.execute(
            "select is_min, access_hash, seen_in_chat, seen_in_msg "
            "from peers where uri='tg:user:11'"
        ).fetchone()
        assert (peer["is_min"], peer["access_hash"]) == (0, 110)  # now a full peer, real hash
        assert (peer["seen_in_chat"], peer["seen_in_msg"]) == (GROUP_ID, 200)  # provenance kept
        raw = st.conn.execute(
            "select kind, context_json from raw_records "
            "where json_extract(context_json, '$.method')='users.getUsers' order by id"
        ).fetchall()
        assert [r["kind"] for r in raw] == ["User", "User"]
        assert json.loads(raw[0]["context_json"]) == {
            "channel_id": CHANNEL_ID, "method": "users.getUsers", "user_id": 11,
        }
        snaps = st.conn.execute("select uri, method from user_snapshots order by uri").fetchall()
        assert [(s["uri"], s["method"]) for s in snaps] == [
            ("tg:user:11", "users.getUsers"), ("tg:user:12", "users.getUsers"),
        ]
        assert res.counts["gathered"] == 2
        assert res.counts["triaged"] == 2 and res.counts["snapshots"] == 2
        assert res.counts["enriched"] == 0 and gw.full_user_calls == []


@pytest.mark.asyncio
async def test_triage_batches_at_most_100_refs_per_call(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for i in range(1, 231):
            _seed_stub(st, i, msg=i)
        gw = _gw({i: _user(i) for i in range(1, 231)})
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert [len(c) for c in gw.users_calls] == [100, 100, 30]


@pytest.mark.asyncio
async def test_a_failed_batch_is_bisected_to_isolate_the_stale_ref(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for i in (1, 2, 3, 4):
            _seed_stub(st, i, msg=i)
        gw = _gw({1: _user(1), 2: _user(2), 3: SkipAndRecord("MSG_ID_INVALID"), 4: _user(4)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        # [1,2,3,4] fails -> [1,2] ok -> [3,4] fails -> [3] fails (skipped) -> [4] ok
        assert gw.users_calls == [[1, 2, 3, 4], [1, 2], [3, 4], [3], [4]]
        assert res.counts["triaged"] == 3 and res.counts["skipped"] == 1
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_unresolvable_stubs_are_counted_and_never_sent(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1, chat=None, msg=None)  # no provenance at all
        _seed_stub(st, 2, chat=999, msg=5)  # provenance into a channel with no known hash
        _seed_stub(st, 3)
        gw = _gw({3: _user(3)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.users_calls == [[3]]
        assert res.counts["unresolvable"] == 2 and res.counts["gathered"] == 3


@pytest.mark.asyncio
async def test_forward_origin_users_are_backfilled_then_triaged(tmp_path):
    # Issue #11: the forwarded_from endpoint had no peers row, so no sweep
    # could ever reach it. Now it is backfilled (provenance = the message)
    # and triaged in the same pass.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        m = {"_": "Message", "id": 300, "message": "fwd", "date": 1767322445,
             "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}}}
        raw_id = st.add_raw("Message", m, "stranger", {"channel_id": CHANNEL_ID})
        upsert_message(st, CHANNEL_ID, m, raw_id, T0, "stranger")
        gw = _gw({42: _user(42)})
        res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert res.counts["backfilled_peers"] == 1
        assert gw.users_calls == [[42]]
        name = st.conn.execute(
            "select first_name from users where uri='tg:user:42'"
        ).fetchone()[0]
        assert name == "U42"


@pytest.mark.asyncio
async def test_without_profiles_flag_no_full_user_call_and_a_warning_is_recorded(tmp_path, caplog):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        gw = _gw(
            {11: _user(11)},
            full_user={11: {"full_user": {"_": "UserFull", "id": 11}, "users": [_user(11)]}},
        )
        with caplog.at_level(logging.WARNING):
            res = await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.full_user_calls == [] and gw.user_photos_calls == []
        assert res.stopped is None  # triage-only is the documented default, not a stop
        event = st.conn.execute(
            "select detail_json from run_events where phase='profiles' and kind='warning'"
        ).fetchone()
        detail = json.loads(event["detail_json"])
        assert detail["code"] == "profiles_enrichment_off" and detail["triaged"] == 1
        assert any(
            "--profiles" in r.getMessage() and "triaged 1" in r.getMessage()
            for r in caplog.records
        )
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None and summary["pass"] == "triage_only"


@pytest.mark.asyncio
async def test_privacy_posture_is_recorded_once_per_run(tmp_path):
    rules = {
        "_": "account.PrivacyRules", "rules": [{"_": "PrivacyValueAllowContacts"}],
        "chats": [], "users": [],
    }
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        gw = _gw({}, privacy={"phone": rules, "lastseen": rules})  # `photo` deliberately missing
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert gw.calls.count("get_privacy") == 3
        raw = st.conn.execute(
            "select kind, tier, context_json from raw_records "
            "where kind like '%PrivacyRules' order by id"
        ).fetchall()
        assert [(r["kind"], r["tier"]) for r in raw] == [("account.PrivacyRules", "self")] * 2
        assert json.loads(raw[0]["context_json"]) == {"key": "phone"}
        posture = json.loads(st.conn.execute(
            "select detail_json from run_events where kind='privacy_posture'"
        ).fetchone()["detail_json"])
        assert posture["phone"] == ["PrivacyValueAllowContacts"]
        assert "unavailable" in posture["photo"]


@pytest.mark.asyncio
async def test_user_empty_is_recorded_raw_but_never_projected(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 11)
        res = await ProfilesCollector().collect(_ctx(st, _gw({}), _settings(tmp_path)))
        assert res.counts["empty"] == 1 and res.counts["triaged"] == 0
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 0
        empty_raw = st.conn.execute(
            "select count(*) from raw_records where kind='UserEmpty'"
        ).fetchone()[0]
        assert empty_raw == 1


@pytest.mark.asyncio
async def test_collecting_account_in_a_users_vector_is_never_projected(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:1", "id": 1})
        _seed_channel(st)
        # a peer row for self cannot exist (#12), but a fixture may still answer it
        _seed_stub(st, 11)
        gw = _gw({11: _user(1, is_self=True)})  # fake answers the WRONG user: self
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert st.conn.execute("select count(*) from users").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_phase_stop_when_channel_context_is_missing(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = _ctx(st, _gw({}), _settings(tmp_path))
        ctx.channel_id = None
        with pytest.raises(PhaseStop):
            await ProfilesCollector().collect(ctx)


def test_applies_to_channel_like_targets():
    assert ProfilesCollector().applies_to(parse_target("@durov"))
    assert not ProfilesCollector().applies_to(parse_target("#osint"))
