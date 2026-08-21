from paperboy.store.db import Store
from paperboy.store.edges import add_edge


def test_add_edge_round_trip(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("channelFull", {"id": 5}, "stranger", None)
        add_edge(
            st, "tg:channel:5", "linked_group", "tg:chat:77",
            "2026-01-01T00:00:00+00:00", "stranger", r, evidence={"field": "linked_chat_id"},
        )
        row = st.conn.execute(
            "select subject_uri, predicate, object_uri, tier, evidence_json from edges"
        ).fetchone()
        assert row["subject_uri"] == "tg:channel:5"
        assert row["predicate"] == "linked_group"
        assert row["object_uri"] == "tg:chat:77"
        assert row["tier"] == "stranger"
        assert '"field"' in row["evidence_json"]


def test_add_edge_without_evidence(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("message", {"id": 1}, "stranger", None)
        add_edge(
            st, "tg:msg:5/1", "forwarded_from", "tg:channel:9",
            "2026-01-01T00:00:00+00:00", "stranger", r, None,
        )
        row = st.conn.execute("select evidence_json from edges").fetchone()
        assert row["evidence_json"] is None
