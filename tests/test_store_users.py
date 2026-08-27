"""`users` / `user_snapshots` / `user_photos` projection + the tri-state shape (spec §4, §4.3)."""

from __future__ import annotations

import json

import pytest

from paperboy.store.db import Store
from paperboy.store.sync import set_state
from paperboy.store.users import (
    add_user_snapshot,
    field_states,
    merge_field_states,
    set_user_photo_sha,
    target_full_facts,
    target_user_facts,
    upsert_user,
    upsert_user_photo,
    user_photo_sha,
)

T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"
T3 = "2026-01-03T00:00:00+00:00"


def _full_user_obj(**extra) -> dict:
    return {
        "_": "User", "id": 9, "access_hash": 111, "first_name": "Real", "last_name": "Person",
        "username": None,
        "usernames": [
            {"_": "Username", "username": "bought", "editable": False, "active": True},
            {"_": "Username", "username": "chosen", "editable": True, "active": True},
        ],
        "phone": "+15550001111", "verified": True, "premium": True, "scam": None,
        "photo": {"_": "UserProfilePhoto", "photo_id": 77, "dc_id": 2, "has_video": False,
                  "personal": None, "stripped_thumb": None},
        "status": {"_": "UserStatusOffline", "was_online": 1767322445},
        "emoji_status": {"_": "EmojiStatus", "document_id": 5, "until": None},
        "restriction_reason": [],
        **extra,
    }


def _store(tmp_path) -> Store:
    return Store.open(tmp_path / "p.sqlite")


def _row(st, uri="tg:user:9"):
    return st.conn.execute("select * from users where uri=?", (uri,)).fetchone()


def test_upsert_user_projects_triage_columns(tmp_path):
    with _store(tmp_path) as st:
        u = _full_user_obj()
        rid = st.add_raw("User", u, "stranger", None)
        assert upsert_user(st, u, rid, T1, "stranger") == "tg:user:9"
        row = _row(st)
        assert row["username"] == "chosen"  # the editable (self-chosen) handle wins
        # the full multi-username structure survives — `editable: False` = Fragment/collectible
        assert [e["username"] for e in json.loads(row["usernames_json"])] == ["bought", "chosen"]
        assert row["phone"] == "+15550001111"
        assert row["status_kind"] == "offline"
        assert row["status_value"] == "2026-01-02T02:54:05+00:00"  # 1767322445
        assert json.loads(row["photo_ref"])["photo_id"] == 77
        assert json.loads(row["flags_json"]) == {"verified": True, "premium": True}
        assert json.loads(row["emoji_status"])["document_id"] == 5
        assert row["is_min"] == 0 and row["enriched_at"] is None
        assert row["first_seen"] == row["last_seen"] == T1
        states = json.loads(row["field_states_json"])
        assert states["phone"] == {"state": "present"}
        assert states["photo"] == {"state": "present"}
        assert states["status"] == {"state": "present", "granularity": "exact"}
        assert "about" not in states  # triage cannot observe full-only fields


def test_collecting_account_is_never_written(tmp_path):
    with _store(tmp_path) as st:
        set_state(st, "account", "self", {"uri": "tg:user:9", "id": 9})
        u = _full_user_obj()
        rid = st.add_raw("User", u, "self", None)
        assert upsert_user(st, u, rid, T1, "self") is None
        assert _row(st) is None


def test_min_never_clobbers_a_full_row_but_widens_the_window(tmp_path):
    with _store(tmp_path) as st:
        full = _full_user_obj()
        r1 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r1, T1, "stranger")
        mn = {"_": "User", "id": 9, "min": True, "first_name": "Min", "phone": "",
              "status": {"_": "UserStatusRecently", "by_me": None}}
        r2 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r2, T2, "stranger")
        row = _row(st)
        assert row["first_name"] == "Real" and row["phone"] == "+15550001111"
        assert row["is_min"] == 0
        assert row["status_kind"] == "offline"  # cached status is not empty -> min status ignored
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)


def test_min_status_applied_to_column_is_reflected_in_field_states(tmp_path):
    """Blocking regression: the min branch's status_kind/status_value write
    must be matched by a field_states update, or the row contradicts itself
    (a column reading 'present' while field_states says 'absent' — the exact
    false negative D2's tri-state map exists to prevent). The existing
    min-status test never exercises this because its cached status is not
    empty, so the min branch's status write never fires there."""
    with _store(tmp_path) as st:
        full = _full_user_obj(status={"_": "UserStatusEmpty"})
        r1 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r1, T1, "stranger")
        assert json.loads(_row(st)["field_states_json"])["status"] == {"state": "absent"}
        mn = {
            "_": "User", "id": 9, "min": True,
            "status": {"_": "UserStatusRecently", "by_me": None},
        }
        r2 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r2, T2, "stranger")
        row = _row(st)
        assert row["status_kind"] == "recently"
        states = json.loads(row["field_states_json"])
        assert states["status"] == {
            "state": "present", "granularity": "coarse", "coarse_cause": "target_privacy",
        }


def test_min_photo_applied_to_column_is_reflected_in_field_states(tmp_path):
    """Blocking regression: same as above, for the min branch's
    `apply_min_photo` photo_ref write."""
    with _store(tmp_path) as st:
        full = _full_user_obj(photo=None)
        r1 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r1, T1, "stranger")
        assert _row(st)["photo_ref"] is None
        assert json.loads(_row(st)["field_states_json"])["photo"] == {"state": "absent"}
        mn = {
            "_": "User", "id": 9, "min": True, "apply_min_photo": True,
            "photo": {"_": "UserProfilePhoto", "photo_id": 3, "dc_id": 2, "has_video": False,
                      "personal": None, "stripped_thumb": None},
        }
        r2 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r2, T2, "stranger")
        row = _row(st)
        assert row["photo_ref"] is not None
        assert json.loads(row["photo_ref"])["photo_id"] == 3
        states = json.loads(row["field_states_json"])
        assert states["photo"] == {"state": "present"}


def test_min_first_then_full_applies_even_when_the_full_is_older(tmp_path):
    # Richness beats recency (ADR-0005 §6's min<-full cell, applied here too).
    with _store(tmp_path) as st:
        mn = {"_": "User", "id": 9, "min": True, "first_name": "Min"}
        r1 = st.add_raw("User", mn, "stranger", None)
        upsert_user(st, mn, r1, T2, "stranger")
        full = _full_user_obj()
        r2 = st.add_raw("User", full, "stranger", None)
        upsert_user(st, full, r2, T1, "stranger")
        row = _row(st)
        assert row["first_name"] == "Real" and row["is_min"] == 0
        assert (row["first_seen"], row["last_seen"]) == (T1, T2)


def test_triage_after_full_keeps_about_birthday_and_enriched_at(tmp_path):
    with _store(tmp_path) as st:
        user = _full_user_obj()
        full_user = {"_": "UserFull", "id": 9, "about": "bio text",
                     "birthday": {"_": "Birthday", "day": 4, "month": 7, "year": None},
                     "profile_photo": {"_": "Photo", "id": 77}}
        r1 = st.add_raw(
            "users.UserFull", {"full_user": full_user, "users": [user]}, "stranger", None
        )
        upsert_user(st, user, r1, T1, "stranger", full_user=full_user)
        row = _row(st)
        assert row["about"] == "bio text"
        assert json.loads(row["birthday"]) == {"day": 4, "month": 7, "year": None}
        assert row["enriched_at"] == T1
        states = json.loads(row["field_states_json"])
        assert states["about"] == {"state": "present"}
        assert states["birthday"] == {"state": "present"}

        r2 = st.add_raw("User", user, "stranger", None)
        upsert_user(st, {**user, "first_name": "Renamed"}, r2, T2, "stranger")
        row = _row(st)
        assert row["first_name"] == "Renamed"
        assert row["about"] == "bio text" and row["enriched_at"] == T1  # triage never blanks these
        assert json.loads(row["field_states_json"])["about"] == {"state": "present"}


def test_triage_after_full_keeps_bot_only_surface(tmp_path):
    # A triage-level bot_json (bare `User.bot_*` flags only) must not replace
    # the richer bot_json built from a `UserFull` (bot_info, admin rights,
    # ...) — the projection must not go permanently lossy just because the
    # LAST observation of a bot happened to be triage-level (correctness
    # review, regression for the FILTER_BOTS enumeration path).
    with _store(tmp_path) as st:
        bot_user = {
            "_": "User", "id": 9, "access_hash": 111, "first_name": "Real", "bot": True,
            "bot_chat_history": True, "bot_info_version": 3,
        }
        full_user = {
            "_": "UserFull", "id": 9,
            "bot_info": {"_": "BotInfo", "description": "does things", "commands": []},
            "bot_broadcast_admin_rights": {"_": "ChatAdminRights", "change_info": True},
        }
        r1 = st.add_raw(
            "users.UserFull", {"full_user": full_user, "users": [bot_user]}, "stranger", None
        )
        upsert_user(st, bot_user, r1, T1, "stranger", full_user=full_user)
        row = _row(st)
        bot = json.loads(row["bot_json"])
        assert bot["bot_chat_history"] is True
        assert bot["bot_info"]["description"] == "does things"
        assert bot["bot_broadcast_admin_rights"]["change_info"] is True

        # A later triage-only observation (no full_user) of the SAME bot.
        r2 = st.add_raw("User", bot_user, "stranger", None)
        upsert_user(st, bot_user, r2, T2, "stranger")
        row = _row(st)
        bot = json.loads(row["bot_json"])
        assert bot["bot_chat_history"] is True  # still there
        assert bot["bot_info"]["description"] == "does things"  # NOT wiped by triage
        assert bot["bot_broadcast_admin_rights"]["change_info"] is True  # NOT wiped by triage
        assert row["enriched_at"] == T1  # a triage pass never looks like an enrichment


def test_field_states_phone_empty_string_is_the_min_wire_state():
    states = field_states({"_": "User", "id": 1, "min": True, "phone": ""})
    assert states["phone"] == {"state": "absent", "why": "min_empty_string"}
    assert field_states({"_": "User", "id": 1, "phone": None})["phone"] == {"state": "absent"}
    assert field_states({"_": "User", "id": 1, "phone": "+1"})["phone"] == {"state": "present"}


def test_field_states_fallback_photo_proves_hidden_from_you():
    user = {"_": "User", "id": 1, "photo": None}
    full = {"_": "UserFull", "id": 1, "profile_photo": None,
            "fallback_photo": {"_": "Photo", "id": 3}}
    assert field_states(user, full)["photo"] == {
        "state": "hidden_from_you", "why": "fallback_photo",
    }
    # Absence alone stays ambiguous — never "not set".
    assert field_states(user, {"_": "UserFull", "id": 1})["photo"] == {"state": "absent"}
    assert field_states(user)["photo"] == {"state": "absent"}


def test_field_states_by_me_is_self_privacy_not_target_opsec():
    ours = field_states(
        {"_": "User", "id": 1, "status": {"_": "UserStatusRecently", "by_me": True}}
    )
    theirs = field_states(
        {"_": "User", "id": 1, "status": {"_": "UserStatusLastWeek", "by_me": None}}
    )
    assert ours["status"] == {
        "state": "present", "granularity": "coarse", "coarse_cause": "self_privacy",
    }
    assert theirs["status"] == {
        "state": "present", "granularity": "coarse", "coarse_cause": "target_privacy",
    }
    empty = field_states({"_": "User", "id": 1, "status": {"_": "UserStatusEmpty"}})
    assert empty["status"] == {"state": "absent"}


def test_field_states_forwards_and_stories():
    full = {"_": "UserFull", "id": 1, "private_forward_name": "Anon", "stories": None}
    s = field_states({"_": "User", "id": 1, "stories_unavailable": True}, full)
    assert s["forwards"] == {"state": "hidden_from_you", "why": "private_forward_name"}
    assert s["stories"] == {"state": "absent", "why": "stories_unavailable"}


def test_triage_absent_never_downgrades_a_hidden_proof():
    existing = {"photo": {"state": "hidden_from_you", "why": "fallback_photo"}}
    triage = {"photo": {"state": "absent"}}
    kept = merge_field_states(existing, triage, full=False)
    assert kept["photo"]["state"] == "hidden_from_you"
    assert merge_field_states(existing, triage, full=True)["photo"] == {"state": "absent"}
    overwritten = merge_field_states(existing, {"photo": {"state": "present"}}, full=False)
    assert overwritten["photo"] == {"state": "present"}


def test_personal_photo_is_never_ingested_as_target_data(tmp_path):
    with _store(tmp_path) as st:
        u = _full_user_obj(photo={"_": "UserProfilePhoto", "photo_id": 5, "dc_id": 1,
                                  "has_video": False, "personal": True, "stripped_thumb": None})
        rid = st.add_raw("User", u, "stranger", None)
        upsert_user(st, u, rid, T1, "stranger")
        row = _row(st)
        assert row["photo_ref"] is None
        assert json.loads(row["field_states_json"])["photo"] == {
            "state": "absent", "why": "personal_photo_shadows",
        }


def test_target_facts_strip_facts_about_us_and_empties():
    full = {"_": "UserFull", "id": 9, "about": "bio", "common_chats_count": 3, "blocked": True,
            "personal_photo": {"_": "Photo", "id": 1}, "note": {"text": "mine"},
            "settings": {"_": "PeerSettings"}, "notify_settings": {"_": "PeerNotifySettings"},
            "personal_channel_id": 42, "birthday": None}
    assert target_full_facts(full) == {"id": 9, "about": "bio", "personal_channel_id": 42}
    user = {"_": "User", "id": 9, "contact": True, "mutual_contact": None, "first_name": "R",
            "restriction_reason": [], "usernames": [], "phone": "", "min": True, "premium": True}
    assert target_user_facts(user) == {"id": 9, "first_name": "R", "premium": True}


def test_snapshot_dedupes_by_uri_and_method_hash(tmp_path):
    with _store(tmp_path) as st:
        rid = st.add_raw("User", {"_": "User", "id": 9}, "stranger", None)
        uri, m1, m2 = "tg:user:9", "users.getUsers", "users.getFullUser"
        assert add_user_snapshot(st, uri, T1, "stranger", m1, {"a": 1}, rid)
        assert not add_user_snapshot(st, uri, T2, "stranger", m1, {"a": 1}, rid)
        # a different METHOD with the same bundle is a distinct observation stream
        assert add_user_snapshot(st, uri, T2, "stranger", m2, {"a": 1}, rid)
        assert add_user_snapshot(st, uri, T3, "stranger", m1, {"a": 2}, rid)
        n = st.conn.execute("select count(*) from user_snapshots").fetchone()[0]
        assert n == 3


def test_user_photos_upsert_and_sha_link(tmp_path):
    with _store(tmp_path) as st:
        rid = st.add_raw("photos.Photos", {"_": "Photos"}, "stranger", None)
        photo = {"_": "Photo", "id": 77, "access_hash": 1, "date": 1767322445, "dc_id": 2,
                 "video_sizes": None, "sizes": []}
        upsert_user_photo(st, "tg:user:9", photo, T1, rid)
        upsert_user_photo(st, "tg:user:9", photo, T2, rid)  # idempotent, observed_at widens
        row = st.conn.execute("select * from user_photos where uri='tg:user:9'").fetchone()
        assert row["photo_id"] == 77 and row["date"] == "2026-01-02T02:54:05+00:00"
        assert row["observed_at"] == T2 and row["has_video"] == 0
        assert user_photo_sha(st, "tg:user:9", 77) is None
        st.conn.execute(
            "insert into media (sha256, kind, mime_type, size, path, downloaded_at) "
            "values ('ab', 'avatar', 'image/jpeg', 1, '/x', ?)", (T1,)
        )
        set_user_photo_sha(st, "tg:user:9", 77, "ab")
        assert user_photo_sha(st, "tg:user:9", 77) == "ab"


def test_upsert_user_rejects_non_user_objects(tmp_path):
    with _store(tmp_path) as st, pytest.raises(ValueError):
        upsert_user(st, {"_": "Channel", "id": 1}, 1, T1, "stranger")
