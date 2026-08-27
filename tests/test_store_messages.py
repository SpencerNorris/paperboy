from paperboy.store.db import Store
from paperboy.store.messages import content_hash, mark_deleted, upsert_message


def _msg(mid, text, views=None, edit=None, channel_id=7):
    m = {
        "_": "message",
        "id": mid,
        "date": 1767322445,
        "message": text,
        "peer_id": {"channel_id": channel_id},
    }
    if views is not None:
        m["views"] = views
    if edit is not None:
        m["edit_date"] = edit
    return m


def test_edit_appends_revision(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r1 = st.add_raw("message", _msg(10, "hello"), "stranger", None)
        u = upsert_message(
            st, 7, _msg(10, "hello", views=5), r1, "2026-01-01T00:00:00+00:00", "stranger"
        )
        r2 = st.add_raw("message", _msg(10, "hello EDITED", edit=1767322500), "stranger", None)
        upsert_message(
            st, 7, _msg(10, "hello EDITED", views=9, edit=1767322500), r2,
            "2026-01-02T00:00:00+00:00", "stranger",
        )
        revs = st.conn.execute(
            "select text from message_revisions where message_uri=? order by observed_at", (u,)
        ).fetchall()
        assert [r["text"] for r in revs] == ["hello", "hello EDITED"]
        metrics = st.conn.execute(
            "select views from message_metrics where message_uri=? order by observed_at", (u,)
        ).fetchall()
        assert [m["views"] for m in metrics] == [5, 9]


def test_unchanged_content_does_not_append_revision(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r1 = st.add_raw("message", _msg(12, "same"), "stranger", None)
        u = upsert_message(st, 7, _msg(12, "same"), r1, "2026-01-01T00:00:00+00:00", "stranger")
        r2 = st.add_raw("message", _msg(12, "same"), "stranger", None)
        upsert_message(st, 7, _msg(12, "same"), r2, "2026-01-02T00:00:00+00:00", "stranger")
        revs = st.conn.execute(
            "select count(*) as n from message_revisions where message_uri=?", (u,)
        ).fetchone()
        assert revs["n"] == 1
        # last_seen still advances even without a content change.
        row = st.conn.execute(
            "select last_seen, first_seen from messages where uri=?", (u,)
        ).fetchone()
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen"] == "2026-01-02T00:00:00+00:00"


def test_no_metrics_row_when_no_counters_present(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("message", _msg(13, "x"), "stranger", None)
        u = upsert_message(st, 7, _msg(13, "x"), r, "2026-01-01T00:00:00+00:00", "stranger")
        n = st.conn.execute(
            "select count(*) as n from message_metrics where message_uri=?", (u,)
        ).fetchone()["n"]
        assert n == 0


def test_tombstone_only_sets_deleted_for_update(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        r = st.add_raw("message", _msg(11, "x"), "stranger", None)
        u = upsert_message(st, 7, _msg(11, "x"), r, "2026-01-01T00:00:00+00:00", "stranger")
        mark_deleted(st, 7, 11, "gap", "2026-01-03T00:00:00+00:00")

        def _deleted_at():
            return st.conn.execute(
                "select deleted_at from messages where uri=?", (u,)
            ).fetchone()["deleted_at"]

        assert _deleted_at() is None
        mark_deleted(st, 7, 11, "update", "2026-01-04T00:00:00+00:00")
        assert _deleted_at() is not None
        tombstones = st.conn.execute(
            "select evidence from message_tombstones where message_uri=? order by observed_at", (u,)
        ).fetchall()
        assert [t["evidence"] for t in tombstones] == ["gap", "update"]


def test_mark_deleted_for_never_seen_message_still_records_tombstone(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        mark_deleted(st, 7, 999, "empty", "2026-01-01T00:00:00+00:00")
        rows = st.conn.execute("select evidence from message_tombstones").fetchall()
        assert [r["evidence"] for r in rows] == ["empty"]


def test_content_hash_changes_with_text():
    a = content_hash("hello", None)
    b = content_hash("hello!", None)
    assert a != b
    assert content_hash("hello", None) == a


def test_content_hash_includes_media():
    a = content_hash("hello", None)
    b = content_hash("hello", '{"_": "messageMediaPhoto"}')
    assert a != b


def test_metrics_row_is_written_when_only_reactions_are_present(tmp_path):
    # Group messages carry no `views`/`forwards`; before this fix their
    # reactions never reached `message_metrics` at all (found building the
    # reaction-candidate query in the person layer, no-shed).
    with Store.open(tmp_path / "p.sqlite") as st:
        m = {"_": "Message", "id": 1, "message": "m", "date": 1767322445,
             "reactions": {
                 "_": "MessageReactions", "results": [{"_": "ReactionCount", "count": 2}],
             }}
        rid = st.add_raw("Message", m, "stranger", {"channel_id": 77})
        upsert_message(st, 77, m, rid, "2026-01-01T00:00:00+00:00", "stranger")
        row = st.conn.execute("select views, reactions_json from message_metrics").fetchone()
        assert row is not None and row["views"] is None and '"count": 2' in row["reactions_json"]
