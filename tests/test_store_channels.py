import json

from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store


def test_channel_flags_capture_every_boolean_not_a_fixed_allow_list(tmp_path):
    # The projection dropped 20 of the 28 boolean flags Telegram returns,
    # including the ones the --join decision rests on (issue #20). Every boolean
    # flag on the Channel and ChannelFull must survive into flags_json; the
    # `min` serialization marker is the one deliberate exclusion (it is a
    # peer-resolution artifact recorded on peers.is_min, not a channel property).
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {
            "_": "channel", "id": 5, "access_hash": 99, "title": "T",
            "broadcast": True, "join_to_send": True, "join_request": False,
            "noforwards": True, "left": False, "creator": True, "min": False,
        }
        full = {
            "_": "channelFull", "id": 5, "participants_count": 10, "pts": 1,
            "linked_chat_id": 0, "can_view_participants": True, "antispam": True,
            "hidden_prehistory": False,
        }
        r = st.add_raw("channelFull", full, "stranger", None)
        upsert_channel(st, full, chan, r, "2026-01-01T00:00:00+00:00")
        flags = json.loads(
            st.conn.execute("select flags_json from channels where id=5").fetchone()["flags_json"]
        )
        for k in ("join_to_send", "join_request", "noforwards", "left", "creator", "broadcast"):
            assert k in flags, f"{k} dropped from flags_json"
        for k in ("can_view_participants", "antispam", "hidden_prehistory"):
            assert k in flags, f"ChannelFull flag {k} dropped from flags_json"
        assert flags["join_to_send"] is True and flags["join_request"] is False
        assert "min" not in flags  # the deliberate exclusion
        # non-boolean fields are not flags
        assert "participants_count" not in flags and "title" not in flags


def test_upsert_channel_and_snapshot(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {
            "_": "channel",
            "id": 5,
            "access_hash": 99,
            "title": "Durov",
            "username": "durov",
            "broadcast": True,
            "verified": True,
        }
        full = {
            "_": "channelFull",
            "id": 5,
            "about": "Channel about text",
            "participants_count": 100,
            "pts": 42,
            "linked_chat_id": 0,
        }
        r = st.add_raw("channelFull", full, "stranger", None)
        uri = upsert_channel(st, full, chan, r, "2026-01-01T00:00:00+00:00")
        assert uri == "tg:channel:5"
        row = st.conn.execute(
            "select title, participants_count, kind, about, linked_chat_id from channels where id=5"
        ).fetchone()
        assert row["title"] == "Durov"
        assert row["participants_count"] == 100
        assert row["kind"] == "broadcast"
        assert row["about"] == "Channel about text"
        assert row["linked_chat_id"] is None  # 0 normalized to "no linked chat"

        snaps = st.conn.execute(
            "select participants_count from channel_snapshots where channel_id=5"
        ).fetchall()
        assert [s["participants_count"] for s in snaps] == [100]


def test_upsert_channel_records_a_new_snapshot_each_call(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "title": "Durov", "megagroup": True}
        full1 = {"_": "channelFull", "id": 5, "participants_count": 100}
        r1 = st.add_raw("channelFull", full1, "stranger", None)
        upsert_channel(st, full1, chan, r1, "2026-01-01T00:00:00+00:00")
        full2 = {"_": "channelFull", "id": 5, "participants_count": 150}
        r2 = st.add_raw("channelFull", full2, "stranger", None)
        upsert_channel(st, full2, chan, r2, "2026-01-02T00:00:00+00:00")

        row = st.conn.execute("select participants_count from channels where id=5").fetchone()
        assert row["participants_count"] == 150  # current state advances

        snaps = st.conn.execute(
            "select participants_count from channel_snapshots "
            "where channel_id=5 order by observed_at"
        ).fetchall()
        assert [s["participants_count"] for s in snaps] == [100, 150]  # history preserved


def test_out_of_order_observation_keeps_newest_state(tmp_path):
    # ADR-0005 §6: same order-independence contract as peers — an older
    # observation arriving after a newer one must not clobber current state,
    # but the observation window (first_seen/last_seen, via channel_snapshots
    # remaining an unconditional append) still reflects the true range.
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "title": "Durov", "broadcast": True}
        full_new = {"_": "channelFull", "id": 5, "participants_count": 200}
        r_new = st.add_raw("channelFull", full_new, "stranger", None)
        upsert_channel(st, full_new, chan, r_new, "2026-02-01T00:00:00+00:00")
        full_old = {"_": "channelFull", "id": 5, "participants_count": 100}
        r_old = st.add_raw("channelFull", full_old, "stranger", None)
        # The OLDER observation arrives second (out of order):
        upsert_channel(st, full_old, chan, r_old, "2026-01-01T00:00:00+00:00")
        row = st.conn.execute(
            "select participants_count, first_seen, last_seen from channels where id=5"
        ).fetchone()
        assert row["participants_count"] == 200  # stale data must not clobber
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen"] == "2026-02-01T00:00:00+00:00"


def test_linked_chat_id_preserved_when_present(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "title": "Durov", "broadcast": True}
        full = {"_": "channelFull", "id": 5, "linked_chat_id": 77}
        r = st.add_raw("channelFull", full, "stranger", None)
        upsert_channel(st, full, chan, r, "2026-01-01T00:00:00+00:00")
        row = st.conn.execute("select linked_chat_id from channels where id=5").fetchone()
        assert row["linked_chat_id"] == 77
