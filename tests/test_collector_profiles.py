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
from paperboy.store.participants import ParticipantFacts, write_participant
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


def _gw(users: dict[int, dict | BaseException], **more) -> FakeGateway:
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
        st.begin_run()  # the dedup guard keys off Store.run_id — exercise it for real
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

        # Same run, second collect: the dedup guard must make this a no-op —
        # no further get_privacy calls, no second run_events/raw_records row.
        gw2 = _gw({}, privacy={"phone": rules, "lastseen": rules})
        await ProfilesCollector().collect(_ctx(st, gw2, _settings(tmp_path)))
        assert gw2.calls.count("get_privacy") == 0
        events = st.conn.execute(
            "select count(*) from run_events where kind='privacy_posture'"
        ).fetchone()[0]
        assert events == 1
        raw_count = st.conn.execute(
            "select count(*) from raw_records where kind like '%PrivacyRules'"
        ).fetchone()[0]
        assert raw_count == 2


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


def _full(user_id: int, **full_extra) -> dict:
    return {
        "_": "UserFull",
        "full_user": {
            "_": "UserFull", "id": user_id, "about": f"bio {user_id}", "common_chats_count": 0,
            "blocked": None, "profile_photo": None, "fallback_photo": None, **full_extra,
        },
        "chats": [], "users": [_user(user_id)],
    }


def _photos(*photo_ids: int) -> dict:
    return {"_": "Photos", "users": [], "photos": [
        {"_": "Photo", "id": pid, "access_hash": 1, "file_reference": "AQ==", "date": 1767322445,
         "dc_id": 2, "sizes": [{"_": "PhotoSize", "type": "x", "w": 640, "h": 640, "size": 1}],
         "video_sizes": None}
        for pid in photo_ids
    ]}


def _seed_population(st: Store) -> None:
    """Five discovered users with distinct priorities: 1 admin, 2 author (posted
    in the channel), 3 commenter (posted in the group), 4 and 5 others."""
    _seed_channel(st)
    for uid in (1, 2, 3, 4, 5):
        _seed_stub(st, uid, msg=uid)
    rid = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
    facts = ParticipantFacts("tg:user:1", "admin", None, None, None, None)
    write_participant(st, GROUP_ID, facts, rid, T0)
    post = {"_": "Message", "id": 900, "message": "post", "date": 1767322445,
            "from_id": {"_": "PeerUser", "user_id": 2}}
    post_raw = st.add_raw("Message", post, "stranger", {"channel_id": CHANNEL_ID})
    upsert_message(st, CHANNEL_ID, post, post_raw, T0, "stranger")
    comment = {"_": "Message", "id": 901, "message": "comment", "date": 1767322445,
               "from_id": {"_": "PeerUser", "user_id": 3}}
    comment_raw = st.add_raw("Message", comment, "stranger", {"channel_id": GROUP_ID})
    upsert_message(st, GROUP_ID, comment, comment_raw, T0, "stranger")


def _enrich_gw(ids=(1, 2, 3, 4, 5), **more) -> FakeGateway:
    return FakeGateway({
        "users": {i: _user(i) for i in ids},
        "full_user": {i: _full(i) for i in ids},
        **more,
    })


@pytest.mark.asyncio
async def test_profiles_flag_enriches_in_priority_order_within_budget(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        gw = _enrich_gw()
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=2))
        )
        assert gw.full_user_calls == [1, 2]  # admin, then author — budget 2
        assert gw.user_photos_calls == [1, 2]
        assert res.counts["enriched"] == 2 and res.counts["triaged"] == 5
        row = st.conn.execute(
            "select about, enriched_at from users where uri='tg:user:1'"
        ).fetchone()
        assert row["about"] == "bio 1" and row["enriched_at"] is not None
        user3_enriched = st.conn.execute(
            "select enriched_at from users where uri='tg:user:3'"
        ).fetchone()[0]
        assert user3_enriched is None
        kinds = [r["kind"] for r in st.conn.execute(
            "select kind from raw_records "
            "where json_extract(context_json, '$.user_id')=1 order by id"
        )]
        assert kinds == ["User", "users.UserFull", "photos.Photos"]
        snaps = [s["method"] for s in st.conn.execute(
            "select method from user_snapshots where uri='tg:user:1' order by id"
        )]
        assert snaps == ["users.getUsers", "users.getFullUser"]
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None
        assert summary["pass"] == "initial"
        assert summary["fully_enriched"] == 2 and summary["population"] == 5


@pytest.mark.asyncio
async def test_resume_to_convergence_then_refresh_wraps_stalest_first(tmp_path):
    # Spec §11: budget 2 over 5 people — run 1 enriches [1,2], run 2 [3,4],
    # run 3 enriches the tail [5] and THEN wraps to refresh the stalest
    # already-enriched user (1). No head re-enriched before the tail is reached.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        settings = _settings(tmp_path, enrich_profiles=True, profile_budget=2)
        seen: list[list[int]] = []
        for _ in range(3):
            gw = _enrich_gw()
            await ProfilesCollector().collect(_ctx(st, gw, settings))
            seen.append(gw.full_user_calls)
        assert seen == [[1, 2], [3, 4], [5, 1]]
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None and summary["pass"] == "refresh"
        fully = st.conn.execute(
            "select count(*) from users where enriched_at is not null"
        ).fetchone()[0]
        assert fully == 5


@pytest.mark.asyncio
async def test_refresh_floor_skips_recently_enriched_users(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        first = _enrich_gw()
        first_settings = _settings(tmp_path, enrich_profiles=True, profile_budget=5)
        await ProfilesCollector().collect(_ctx(st, first, first_settings))
        assert first.full_user_calls == [1, 2, 3, 4, 5]
        second = _enrich_gw()
        second_settings = _settings(
            tmp_path, enrich_profiles=True, profile_budget=5, profile_refresh_after=7 * 86400
        )
        res = await ProfilesCollector().collect(_ctx(st, second, second_settings))
        assert second.full_user_calls == []
        assert res.counts["fresh_skipped"] == 5 and res.counts["refreshed"] == 0


@pytest.mark.asyncio
async def test_photo_history_and_avatar_download_are_content_addressed(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        photo_bytes = {701: b"jpeg-1", 702: b"jpeg-2"}
        gw = _enrich_gw(ids=(1,), user_photos={1: _photos(701, 702)}, avatar=photo_bytes)
        photo_settings = _settings(tmp_path, enrich_profiles=True)
        res = await ProfilesCollector().collect(_ctx(st, gw, photo_settings))
        assert gw.avatar_calls == [701, 702]
        assert res.counts["photos"] == 2 and res.counts["avatars"] == 2
        rows = st.conn.execute(
            "select photo_id, date, sha256 from user_photos order by photo_id"
        ).fetchall()
        assert [r["photo_id"] for r in rows] == [701, 702]
        assert all(r["sha256"] for r in rows) and rows[0]["date"] == "2026-01-02T02:54:05+00:00"
        media = st.conn.execute("select kind, message_uri, path, mime_type from media").fetchall()
        assert {m["kind"] for m in media} == {"avatar"}
        assert all(m["message_uri"] is None for m in media)
        media_dir = str(tmp_path / "p" / "media")
        assert all(m["path"].startswith(media_dir) and m["path"].endswith(".jpg") for m in media)
        custody = st.conn.execute(
            "select count(*) from custody_log where source_message_uri is null"
        ).fetchone()[0]
        assert custody == 2
        avatar_raw = st.conn.execute(
            "select count(*) from raw_records where kind='AvatarDownload'"
        ).fetchone()[0]
        assert avatar_raw == 2
        # a second run re-lists the history but never re-downloads a known photo
        again = _enrich_gw(ids=(1,), user_photos={1: _photos(701, 702)}, avatar=photo_bytes)
        await ProfilesCollector().collect(_ctx(st, again, photo_settings))
        assert again.avatar_calls == []
        assert st.conn.execute("select count(*) from custody_log").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_restricted_users_avatars_are_listed_but_not_downloaded(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        restricted = _user(1, restricted=True, restriction_reason=[
            {"_": "RestrictionReason", "platform": "all", "reason": "porn", "text": "x"}])
        gw = FakeGateway({"users": {1: restricted},
                          "full_user": {1: {**_full(1), "users": [restricted]}},
                          "user_photos": {1: _photos(701)}, "avatar": {701: b"bytes"}})
        settings = _settings(tmp_path, enrich_profiles=True)
        res = await ProfilesCollector().collect(_ctx(st, gw, settings))
        assert gw.avatar_calls == []
        assert res.counts["photos"] == 1
        assert res.counts["restricted_skipped"] == 1 and res.counts["avatars"] == 0


@pytest.mark.asyncio
async def test_unresolvable_users_are_not_double_counted_during_enrichment(tmp_path):
    # Round-2 review: `_gather` counts every unresolvable peer once; `_enrich`'s
    # candidate loop reaches the same peers again (nothing between the two
    # passes changes resolvability) and must not count them a second time.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1, chat=None, msg=None)  # unresolvable: no provenance at all
        _seed_stub(st, 2)
        gw = _enrich_gw(ids=(2,))
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=10))
        )
        assert res.counts["gathered"] == 2
        assert res.counts["unresolvable"] == 1
        assert res.counts["enriched"] == 1


@pytest.mark.asyncio
async def test_full_user_chats_vector_preserves_chat_provenance(tmp_path):
    # Round-2 review: a `Chat`-tagged object in `users.UserFull.chats` (a
    # `Vector<Chat>`, so basic groups are as legal a member as channels) was
    # classified by a hand-rolled `user_uri`/`channel_uri` branch that never
    # matches `chat*` — filing it under `tg:channel:88` instead of
    # `tg:chat:88`, missing the real row's stored provenance and (by the
    # recency rule) nulling it out. Fixed to reuse `classify_peer`, the same
    # classifier `upsert_peer` uses.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        chat_raw = st.add_raw(
            "Message", {"_": "Message", "id": 55}, "stranger", {"channel_id": GROUP_ID}
        )
        upsert_peer(
            st, {"_": "Chat", "id": 88, "min": True}, chat_raw, T0,
            seen_in_chat=GROUP_ID, seen_in_msg=55,
        )
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = {
            **_full(1), "chats": [{"_": "Chat", "id": 88, "title": "Fan Chat"}],
        }
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        rows = st.conn.execute(
            "select uri, seen_in_chat, seen_in_msg, is_min, title from peers where id=88"
        ).fetchall()
        assert [r["uri"] for r in rows] == ["tg:chat:88"]  # never tg:channel:88
        assert (rows[0]["seen_in_chat"], rows[0]["seen_in_msg"]) == (GROUP_ID, 55)
        assert rows[0]["is_min"] == 0 and rows[0]["title"] == "Fan Chat"


@pytest.mark.asyncio
async def test_malformed_full_user_envelope_is_skipped_not_crashed(tmp_path):
    # Round-2 review: `full_user = full.get("full_user") or {}` then passed to
    # `upsert_user`, which raises ValueError on any full_user/user id mismatch
    # — and `{}` always mismatches. A malformed `users.UserFull` envelope must
    # be counted and skipped like the sibling missing-`user` case, never crash
    # the run.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = {
            "_": "UserFull", "full_user": None, "users": [_user(1)], "chats": None,
        }
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True))
        )
        assert res.counts["skipped"] == 1 and res.counts["enriched"] == 0
        assert st.conn.execute(
            "select enriched_at from users where uri='tg:user:1'"
        ).fetchone()[0] is None
        assert st.conn.execute(
            "select count(*) from raw_records where kind='users.UserFull'"
        ).fetchone()[0] == 1  # recorded raw, never guessed at


@pytest.mark.asyncio
async def test_avatar_unavailable_is_logged_not_silently_dropped(tmp_path, caplog):
    # Round-2 review: `download_user_photo` returning `None` (server-side
    # unavailable, not a raised `SkipAndRecord`) was dropped with no count and
    # no log line — invisible in both the counts dict and the run log.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,), user_photos={1: _photos(701)}, avatar={})  # no bytes for 701
        with caplog.at_level(logging.WARNING):
            res = await ProfilesCollector().collect(
                _ctx(st, gw, _settings(tmp_path, enrich_profiles=True))
            )
        assert res.counts["photos"] == 1 and res.counts["avatars"] == 0
        assert res.counts["unavailable"] == 1  # round-3 review: was invisible in counts
        assert any(
            "avatar" in r.getMessage() and "701" in r.getMessage()
            and "unavailable" in r.getMessage()
            for r in caplog.records
        )


@pytest.mark.asyncio
async def test_phase_stop_during_triage_still_reports_partial_counts(tmp_path):
    # Correctness review: a PhaseStop escaping `_triage` (FLOOD_WAIT above
    # threshold on `users.getUsers`) must not surface an empty result for a
    # run that already gathered/triaged real work.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        for i in (1, 2, 3):
            _seed_stub(st, i, msg=i)
        gw = _gw({1: _user(1), 2: _user(2), 3: PhaseStop("FLOOD_WAIT_OF_300_SECONDS")})
        with pytest.raises(PhaseStop) as excinfo:
            await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert excinfo.value.counts["gathered"] == 3
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None and summary["pass"] == "triage_only"


@pytest.mark.asyncio
async def test_phase_stop_mid_enrichment_preserves_counts_and_summary(tmp_path):
    # Correctness review: a PhaseStop escaping `_enrich` (FLOOD_WAIT above
    # threshold on `users.getFullUser`) must carry the enrichment already done
    # this run, and `_record_summary` must still run — otherwise the operator
    # reads an empty result and the persisted convergence summary silently
    # keeps the previous run's figures.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)  # priority order: admin 1, author 2, commenter 3, others 4/5
        gw = _enrich_gw()
        gw._fx["full_user"][3] = PhaseStop("FLOOD_WAIT_OF_300_SECONDS")
        with pytest.raises(PhaseStop) as excinfo:
            await ProfilesCollector().collect(
                _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=5))
            )
        assert excinfo.value.counts["enriched"] == 2  # 1, 2 landed before the stop
        assert excinfo.value.counts["triaged"] == 5
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None and summary["fully_enriched"] == 2


@pytest.mark.asyncio
async def test_full_user_skip_is_counted_spends_budget_and_continues(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        gw = _enrich_gw()
        gw._fx["full_user"][1] = SkipAndRecord("USER_ID_INVALID")
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=2))
        )
        assert gw.full_user_calls == [1, 2]  # the failed attempt still spent budget
        assert res.counts["skipped"] == 1 and res.counts["enriched"] == 1


@pytest.mark.asyncio
async def test_same_run_triage_skip_does_not_starve_lower_priority_users(tmp_path):
    # Round-3 review (blocking finding #1): a peer whose stale
    # (seen_in_chat, seen_in_msg) provenance fails `users.getUsers` fails
    # `users.getFullUser` identically (both raise on the same
    # ChannelInvalidError-shaped condition). Before this fix, `_enrich`
    # re-spent budget confirming what `_triage` already proved unusable THIS
    # SAME run — with the stale ref at the head of the priority order
    # (admin, rank 0) and a tight budget, it consumed the entire budget every
    # run forever and no one else was ever enriched.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)  # 1 admin(rank0), 2 author(rank1), 3 commenter(rank2), 4/5 others
        settings = _settings(tmp_path, enrich_profiles=True, profile_budget=1)
        seen: list[list[int]] = []
        for _ in range(3):
            gw = FakeGateway({
                "users": {
                    1: SkipAndRecord("MSG_ID_INVALID"),  # stale provenance: fails triage too
                    2: _user(2), 3: _user(3), 4: _user(4), 5: _user(5),
                },
                "full_user": {2: _full(2), 3: _full(3), 4: _full(4), 5: _full(5)},
            })
            await ProfilesCollector().collect(_ctx(st, gw, settings))
            seen.append(gw.full_user_calls)
        # user 1's stale ref is proven unusable by triage every run and NEVER
        # spent on getFullUser — the single-slot budget lands on a real,
        # healthy candidate each run instead of being wasted.
        assert seen == [[2], [3], [4]]
        assert st.conn.execute(
            "select count(*) from users where enriched_at is not null"
        ).fetchone()[0] == 3
        assert st.conn.execute(
            "select count(*) from users where uri='tg:user:1'"
        ).fetchone()[0] == 0  # never triages successfully — no `users` row at all


@pytest.mark.asyncio
async def test_previously_failed_enrich_only_sorts_after_never_attempted(tmp_path):
    # Round-3 review (blocking finding #1, second half): a ref that fails
    # ONLY `getFullUser` (triage succeeds, so it is never in this run's
    # triage-skip set) must not consume every run's budget forever just
    # because it is always highest-ranked. A durable `ProfileFetchSkipped`
    # raw record, read back by `_enrichment_candidates`, sorts it behind a
    # never-attempted candidate on the next run.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_population(st)
        settings = _settings(tmp_path, enrich_profiles=True, profile_budget=1)
        seen: list[list[int]] = []
        for _ in range(3):
            gw = _enrich_gw()  # triage succeeds for everyone
            gw._fx["full_user"][1] = SkipAndRecord("USER_ID_INVALID")  # admin always fails
            await ProfilesCollector().collect(_ctx(st, gw, settings))
            seen.append(gw.full_user_calls)
        # Run 1: the admin (rank 0) is tried first, as always, and fails —
        # recorded as a durable skip. Run 2: that prior failure now sorts the
        # admin behind every never-attempted candidate, so the author (a
        # real, healthy user) gets the single slot instead of the admin
        # failing again. Run 3: same reasoning reaches the commenter.
        assert seen == [[1], [2], [3]]
        assert st.conn.execute(
            "select count(*) from raw_records where kind='ProfileFetchSkipped'"
        ).fetchone()[0] == 1  # the admin is never retried once healthier candidates remain
        assert st.conn.execute(
            "select enriched_at from users where uri='tg:user:1'"
        ).fetchone()[0] is None
        assert st.conn.execute(
            "select count(*) from users where enriched_at is not null"
        ).fetchone()[0] == 2  # 2 and 3 eventually enriched despite the permanently-stuck admin


@pytest.mark.asyncio
async def test_admin_of_unrelated_group_does_not_outrank_this_targets_author(tmp_path):
    # Round-3 review (blocking finding #2): the profile DB is per-profile,
    # not per-target — an admin/creator of a group entirely UNRELATED to this
    # target (neither `ctx.channel_id` nor its linked group) must not rank 0
    # ("admin") for this target's sweep and pre-empt this target's own
    # message author.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1, msg=1)
        _seed_stub(st, 2, msg=2)
        rid = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        unrelated_group_id = 999
        facts = ParticipantFacts("tg:user:1", "admin", None, None, None, None)
        write_participant(st, unrelated_group_id, facts, rid, T0)
        post = {"_": "Message", "id": 900, "message": "post", "date": 1767322445,
                "from_id": {"_": "PeerUser", "user_id": 2}}
        post_raw = st.add_raw("Message", post, "stranger", {"channel_id": CHANNEL_ID})
        upsert_message(st, CHANNEL_ID, post, post_raw, T0, "stranger")
        gw = _enrich_gw(ids=(1, 2))
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True, profile_budget=1))
        )
        assert gw.full_user_calls == [2]  # this target's own author, never the sibling's admin
        assert res.counts["enriched"] == 1


@pytest.mark.asyncio
async def test_full_user_users_vector_userempty_subject_is_skipped_not_crashed(tmp_path):
    # Round-3 review (minor finding): `users.UserFull.users` is a
    # `Vector<User>` whose union includes `userEmpty` — the id-only lookup
    # can match one, and without checking its `_` kind this reached
    # `upsert_user`'s `raise ValueError("not a User object: ...")`, which
    # `recipes.py` does not catch, crashing the whole run.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = {
            "_": "UserFull",
            "full_user": {"_": "UserFull", "id": 1, "about": "bio 1"},
            "chats": [], "users": [{"_": "UserEmpty", "id": 1}],
        }
        res = await ProfilesCollector().collect(
            _ctx(st, gw, _settings(tmp_path, enrich_profiles=True))
        )
        assert res.counts["skipped"] == 1 and res.counts["enriched"] == 0
        assert st.conn.execute(
            "select enriched_at from users where uri='tg:user:1'"
        ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_phase_stop_during_privacy_posture_still_reports_backfill_counts(tmp_path):
    # Round-3 review (minor finding): `record_privacy_posture` sat OUTSIDE the
    # `try/except PhaseStop` — `account.getPrivacy` is a budgeted RPC that can
    # raise `PhaseStop` (FLOOD_WAIT above threshold), which would discard the
    # backfill work already done this run and skip the convergence summary.
    class _PostureStopGateway(FakeGateway):
        async def get_privacy(self, key: str) -> dict:
            raise PhaseStop("FLOOD_WAIT_OF_300_SECONDS")

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        m = {"_": "Message", "id": 300, "message": "fwd", "date": 1767322445,
             "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}}}
        raw_id = st.add_raw("Message", m, "stranger", {"channel_id": CHANNEL_ID})
        upsert_message(st, CHANNEL_ID, m, raw_id, T0, "stranger")
        gw = _PostureStopGateway({"users": {}})
        with pytest.raises(PhaseStop) as excinfo:
            await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path)))
        assert excinfo.value.counts["backfilled_peers"] == 1
        summary = get_state(st, "profiles", str(CHANNEL_ID))
        assert summary is not None and summary["pass"] == "triage_only"


@pytest.mark.asyncio
async def test_full_user_chats_vector_empty_or_forbidden_does_not_null_known_peer(tmp_path):
    # Round-3 review (minor finding): `Vector<Chat>` also legally carries
    # `ChatEmpty`/`ChatForbidden` (id + title only, no `access_hash`, no
    # `min`) — upserting one as a full observation would null out a known
    # peer's identity columns under `upsert_peer`'s recency rule.
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        chat_raw = st.add_raw(
            "Message", {"_": "Message", "id": 55}, "stranger", {"channel_id": GROUP_ID}
        )
        upsert_peer(
            st, {"_": "Chat", "id": 88, "access_hash": 7, "title": "Fan Chat"}, chat_raw, T0,
            seen_in_chat=GROUP_ID, seen_in_msg=55,
        )
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = {**_full(1), "chats": [{"_": "ChatEmpty", "id": 88}]}
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        row = st.conn.execute("select title, access_hash from peers where id=88").fetchone()
        assert row["title"] == "Fan Chat" and row["access_hash"] == 7


@pytest.mark.asyncio
async def test_full_profile_disambiguates_a_hidden_photo(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st)
        _seed_stub(st, 1)
        gw = _enrich_gw(ids=(1,))
        gw._fx["full_user"][1] = _full(
            1, fallback_photo={"_": "Photo", "id": 5}, private_forward_name="Anon"
        )
        await ProfilesCollector().collect(_ctx(st, gw, _settings(tmp_path, enrich_profiles=True)))
        states_json = st.conn.execute(
            "select field_states_json from users where uri='tg:user:1'"
        ).fetchone()[0]
        states = json.loads(states_json)
        assert states["photo"] == {"state": "hidden_from_you", "why": "fallback_photo"}
        assert states["forwards"] == {"state": "hidden_from_you", "why": "private_forward_name"}
        fields_json = st.conn.execute(
            "select fields_json from user_snapshots where method='users.getFullUser'"
        ).fetchone()[0]
        bundle = json.loads(fields_json)
        assert "common_chats_count" not in bundle["full_user"]
        assert "blocked" not in bundle["full_user"]
