from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store


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


def test_linked_chat_id_preserved_when_present(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "title": "Durov", "broadcast": True}
        full = {"_": "channelFull", "id": 5, "linked_chat_id": 77}
        r = st.add_raw("channelFull", full, "stranger", None)
        upsert_channel(st, full, chan, r, "2026-01-01T00:00:00+00:00")
        row = st.conn.execute("select linked_chat_id from channels where id=5").fetchone()
        assert row["linked_chat_id"] == 77
