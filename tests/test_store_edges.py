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


def test_add_edge_is_append_only_not_deduped(tmp_path):
    """`add_edge` itself must never dedupe on `(subject, predicate, object)`.

    Amendment 4 (docs/superpowers/plans/2026-08-21-discussion-collector.md)
    scopes idempotence to exactly two predicates it calls "structural facts"
    — `commented_on` and `replied_to` — and says the skip-if-triple-exists
    guard belongs inside their producers (`discussion.py`,
    `repliers.py`), never inside `store/edges.py::add_edge` itself, because
    `channel` (`linked_group`), `history` (`forwarded_from`), and `graph`
    (`recommends`, `mentions`, `member_of`) all rely on `add_edge` staying an
    append-only observation log (ADR-0002) so a repeated observation of the
    same fact over time is preserved rather than collapsed into a set. A
    global dedup inside `add_edge` would silently destroy that longitudinal
    evidence for every other predicate while still satisfying the two
    discussion-specific tests that pin idempotence at the producer level.
    """
    with Store.open(tmp_path / "p.sqlite") as st:
        r1 = st.add_raw("channelFull", {"id": 5}, "stranger", None)
        r2 = st.add_raw("channelFull", {"id": 5}, "stranger", None)
        add_edge(
            st, "tg:channel:5", "recommends", "tg:channel:9",
            "2026-01-01T00:00:00+00:00", "stranger", r1, None,
        )
        add_edge(
            st, "tg:channel:5", "recommends", "tg:channel:9",
            "2026-02-01T00:00:00+00:00", "stranger", r2, None,
        )
        count = st.conn.execute(
            "select count(*) c from edges where subject_uri='tg:channel:5' "
            "and predicate='recommends' and object_uri='tg:channel:9'"
        ).fetchone()["c"]
        assert count == 2
