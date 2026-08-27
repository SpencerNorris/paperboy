"""`participants` / `participant_snapshots` writers +
the zero-RPC join/leave service-message vector."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.store.participants import (
    ParticipantFacts,
    add_participant_snapshot,
    add_roster_snapshot,
    membership_edges,
    participant_row,
    project_join_service_messages,
    upsert_participant,
    write_participant,
)
from paperboy.store.sync import set_state

GROUP_ID = 77
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"
JOINED = 1735689600  # 2025-01-01T00:00:00+00:00


def test_participant_row_parses_every_constructor():
    member = participant_row({
        "_": "ChannelParticipant", "user_id": 1, "date": JOINED,
        "subscription_until_date": None, "rank": "scout",
    })
    assert member == ParticipantFacts(
        "tg:user:1", "member", "2025-01-01T00:00:00+00:00", "scout", None, None
    )
    me = participant_row({
        "_": "ChannelParticipantSelf", "user_id": 2, "inviter_id": 9, "date": JOINED,
        "via_request": True, "subscription_until_date": None, "rank": None,
    })
    assert me is not None and (me.status, me.inviter_id) == ("member", 9)
    creator = participant_row({
        "_": "ChannelParticipantCreator", "user_id": 3,
        "admin_rights": {"_": "ChatAdminRights"}, "rank": "founder",
    })
    assert creator == ParticipantFacts("tg:user:3", "creator", None, "founder", None, None)
    admin = participant_row({
        "_": "ChannelParticipantAdmin", "user_id": 4, "promoted_by": 3, "date": JOINED,
        "admin_rights": {"_": "ChatAdminRights"}, "can_edit": None, "is_self": None,
        "inviter_id": None, "rank": None,
    })
    assert admin is not None
    assert (admin.status, admin.join_date) == ("admin", "2025-01-01T00:00:00+00:00")
    banned = participant_row({
        "_": "ChannelParticipantBanned", "peer": {"_": "PeerChannel", "channel_id": 5},
        "kicked_by": 3, "date": JOINED, "banned_rights": {}, "left": True, "rank": None,
    })
    # ban date != join date, so it never lands in ParticipantFacts.join_date
    assert banned == ParticipantFacts("tg:channel:5", "banned", None, None, None, None)
    restricted = participant_row({
        "_": "ChannelParticipantBanned", "peer": {"_": "PeerUser", "user_id": 5},
        "kicked_by": 3, "date": JOINED, "banned_rights": {}, "left": False, "rank": None,
    })
    # `left` unset (or explicitly False) on Banned = restricted but STILL A
    # MEMBER, not kicked out — the single constructor carries both meanings.
    assert restricted == ParticipantFacts("tg:user:5", "member", None, None, None, None)
    left = participant_row({"_": "ChannelParticipantLeft", "peer": {"_": "PeerUser", "user_id": 6}})
    assert left == ParticipantFacts("tg:user:6", "left", None, None, None, None)
    assert participant_row({"_": "SomethingElse", "user_id": 7}) is None


def test_upsert_writes_current_state_and_a_newer_observation_wins_keeping_join_date(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r1 = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        facts = upsert_participant(
            st, GROUP_ID,
            {"_": "ChannelParticipant", "user_id": 1, "date": JOINED, "rank": "scout"},
            r1, T1,
        )
        assert facts is not None and facts.uri == "tg:user:1"
        r2 = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        upsert_participant(
            st, GROUP_ID,
            {"_": "ChannelParticipantBanned", "peer": {"_": "PeerUser", "user_id": 1},
             "kicked_by": 3, "date": JOINED + 86400, "banned_rights": {}, "left": True,
             "rank": None},
            r2, T2,
        )
        row = st.conn.execute("select * from participants where uri='tg:user:1'").fetchone()
        assert row["status"] == "banned"
        assert row["join_date"] == "2025-01-01T00:00:00+00:00"  # a known join date is never blanked
        assert row["rank"] is None  # rank DID move: the newer observation carries none
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)
        assert row["source_raw_id"] == r2


def test_older_observation_never_overrides_status(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        write_participant(
            st, GROUP_ID, ParticipantFacts("tg:user:1", "left", None, None, None, None), r, T2
        )
        write_participant(
            st, GROUP_ID,
            ParticipantFacts("tg:user:1", "member", "2025-01-01T00:00:00+00:00", None, None, None),
            r, T1,
        )
        row = st.conn.execute("select status, join_date, first_seen from participants").fetchone()
        assert (row["status"], row["join_date"], row["first_seen"]) == (
            "left", "2025-01-01T00:00:00+00:00", T1,
        )


def test_collecting_account_is_never_a_participant_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:2", "id": 2})
        r = st.add_raw("channels.ChannelParticipants", {}, "member", None)
        assert upsert_participant(
            st, GROUP_ID,
            {"_": "ChannelParticipantSelf", "user_id": 2, "inviter_id": 9, "date": JOINED},
            r, T1,
        ) is None
        assert st.conn.execute("select count(*) from participants").fetchone()[0] == 0


def test_snapshots_member_rows_and_roster_accounting_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        facts = ParticipantFacts(
            "tg:user:1", "member", "2025-01-01T00:00:00+00:00", None, None, None
        )
        assert add_participant_snapshot(st, GROUP_ID, facts, T1, r)
        assert add_participant_snapshot(st, GROUP_ID, facts, T1, r)  # append-only by default
        assert not add_participant_snapshot(st, GROUP_ID, facts, T1, r, once=True)
        # `once=True` dedupes on observation identity (group_id, uri,
        # observed_at, status), NOT on source_raw_id: a re-observed history
        # page gives the underlying message a fresh source_raw_id every run
        # (upsert_message's ON CONFLICT), so the same fact re-scanned with a
        # DIFFERENT source_raw_id must still be recognized as a duplicate.
        r_other = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        assert r_other != r
        assert not add_participant_snapshot(st, GROUP_ID, facts, T1, r_other, once=True)
        add_roster_snapshot(
            st, GROUP_ID, T1, enumerated=1, true_count=307, reason=None, source_raw_id=r
        )
        rows = st.conn.execute(
            "select uri, enumerated, true_count from participant_snapshots order by id"
        ).fetchall()
        assert [tuple(x) for x in rows] == [
            ("tg:user:1", None, None), ("tg:user:1", None, None), (None, 1, 307),
        ]


def test_restricted_member_without_left_still_gets_a_member_of_edge(tmp_path):
    # A moderated group's Recent-sourced ChannelParticipantBanned for a
    # muted/restricted (but still-present) member must not be dropped from
    # the roster/graph: `left` unset means member, and `member_of` must fire.
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        facts = upsert_participant(
            st, GROUP_ID,
            {"_": "ChannelParticipantBanned", "peer": {"_": "PeerUser", "user_id": 5},
             "kicked_by": 3, "date": JOINED, "banned_rights": {}, "left": False, "rank": None},
            r, T1,
        )
        assert facts is not None and facts.status == "member"
        row = st.conn.execute("select status from participants where uri='tg:user:5'").fetchone()
        assert row["status"] == "member"
        assert membership_edges(st, GROUP_ID, facts, T1, "stranger", r, {"source": "roster"}) == 1
        edges = {
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }
        assert ("tg:user:5", "member_of", "tg:channel:77") in edges


def test_membership_edges_only_for_member_statuses(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channels.ChannelParticipants", {}, "stranger", None)
        admin = ParticipantFacts("tg:user:1", "admin", None, None, None, None)
        evidence = {"source": "roster"}
        assert membership_edges(st, GROUP_ID, admin, T1, "stranger", r, evidence) == 2
        assert membership_edges(st, GROUP_ID, admin, T1, "stranger", r, evidence) == 0  # once
        banned = ParticipantFacts("tg:user:2", "banned", None, None, None, None)
        assert membership_edges(st, GROUP_ID, banned, T1, "stranger", r, {}) == 0
        preds = sorted(
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        )
        assert preds == [
            ("tg:user:1", "admin_of", "tg:channel:77"), ("tg:user:1", "member_of", "tg:channel:77"),
        ]


def _service(msg_id: int, action: dict, from_user: int | None, date: int) -> dict:
    m = {"_": "MessageService", "id": msg_id, "date": date, "action": action,
         "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if from_user is not None:
        m["from_id"] = {"_": "PeerUser", "user_id": from_user}
    return m


def _seed(st: Store, m: dict) -> None:
    raw_id = st.add_raw(m["_"], m, "stranger", {"channel_id": GROUP_ID})
    upsert_message(st, GROUP_ID, m, raw_id, T1, "stranger")


def test_join_service_messages_project_membership_and_invite_edges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, _service(10, {"_": "MessageActionChatAddUser", "users": [5, 6]}, 4, JOINED))
        _seed(st, _service(
            11, {"_": "MessageActionChatJoinedByLink", "inviter_id": 4}, 7, JOINED + 60
        ))
        _seed(st, _service(12, {"_": "MessageActionChatJoinedByRequest"}, 8, JOINED + 120))
        _seed(st, _service(13, {"_": "MessageActionChatDeleteUser", "user_id": 6}, 6, JOINED + 180))
        # not a membership fact:
        _seed(st, _service(14, {"_": "MessageActionPinMessage"}, 4, JOINED + 240))
        counts = project_join_service_messages(st, GROUP_ID, "stranger")
        # 4 member_of (5, 6, 7, 8) + 2 added_by (5, 6 <- 4) + 1 invited_by (7 <- 4)
        assert counts == {"joins": 4, "leaves": 1, "edges": 7}
        rows = {
            r["uri"]: (r["status"], r["join_date"], r["inviter_id"])
            for r in st.conn.execute("select uri, status, join_date, inviter_id from participants")
        }
        assert rows["tg:user:5"] == ("member", "2025-01-01T00:00:00+00:00", None)
        assert rows["tg:user:6"] == ("left", "2025-01-01T00:00:00+00:00", None)  # joined, then left
        assert rows["tg:user:7"] == ("member", "2025-01-01T00:01:00+00:00", 4)
        assert rows["tg:user:8"] == ("member", "2025-01-01T00:02:00+00:00", None)
        edges = {
            (e["subject_uri"], e["predicate"], e["object_uri"])
            for e in st.conn.execute("select subject_uri, predicate, object_uri from edges")
        }
        assert ("tg:user:5", "added_by", "tg:user:4") in edges
        assert ("tg:user:6", "added_by", "tg:user:4") in edges
        assert ("tg:user:7", "invited_by", "tg:user:4") in edges
        assert ("tg:user:5", "member_of", "tg:channel:77") in edges
        assert ("tg:user:8", "member_of", "tg:channel:77") in edges
        # the membership observation is stamped with the FACT's time (the message
        # date), so a later roster observation correctly wins over it (plan D5)
        snap = st.conn.execute(
            "select observed_at from participant_snapshots where uri='tg:user:7'"
        ).fetchone()
        assert snap["observed_at"] == "2025-01-01T00:01:00+00:00"

        # idempotent: a re-run adds nothing — including `joins`/`leaves`, not
        # just `edges` (regression: `write_participant` always upserts and
        # returns non-None, so a naive `joins += 1`/`leaves += 1` gated only on
        # that would recount every historical join/leave as "new" on EVERY
        # scan; a caller accumulating these into `run_events` would then
        # misreport N "new" service_joins on every future `collect` run
        # against an unchanged channel, forever). The gate must be the
        # `add_participant_snapshot(once=True)` result, the one signal that
        # actually distinguishes "seen for the first time" from a re-scan.
        again = project_join_service_messages(st, GROUP_ID, "stranger")
        assert again == {"joins": 0, "leaves": 0, "edges": 0}
        assert st.conn.execute("select count(*) from participant_snapshots").fetchone()[0] == 5
        assert st.conn.execute("select count(*) from edges").fetchone()[0] == 7
