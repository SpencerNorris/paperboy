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


# --- #96: Telegram re-issues `file_reference` on every fetch; it is not content.

_T1 = "2026-01-01T00:00:00+00:00"
_T2 = "2026-01-02T00:00:00+00:00"


def _photo(photo_id=1, ref="aa", **extra):
    return {"_": "messageMediaPhoto", "photo": {"_": "Photo", "id": photo_id,
                                                "file_reference": ref, "dc_id": 2}, **extra}


def _media_msg(mid, media, text="cap"):
    m = _msg(mid, text)
    m["media"] = media
    return m


def _observe(st, msg, at):
    raw = st.add_raw("message", msg, "stranger", None)
    return upsert_message(st, 7, msg, raw, at, "stranger")


def _revision_count(st, uri):
    return st.conn.execute(
        "select count(*) as n from message_revisions where message_uri=?", (uri,)
    ).fetchone()["n"]


def test_content_hash_ignores_file_reference_at_any_depth():
    a = '{"photo": {"id": 1, "file_reference": "aa"}, "alt": [{"file_reference": "x", "id": 2}]}'
    b = '{"photo": {"id": 1, "file_reference": "bb"}, "alt": [{"file_reference": "y", "id": 2}]}'
    assert content_hash("t", a) == content_hash("t", b)
    c = '{"photo": {"id": 3, "file_reference": "aa"}, "alt": [{"file_reference": "x", "id": 2}]}'
    assert content_hash("t", a) != content_hash("t", c)


def test_reobserving_photo_with_new_file_reference_adds_no_revision(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        u = _observe(st, _media_msg(1, _photo(ref="aa")), _T1)
        _observe(st, _media_msg(1, _photo(ref="bb")), _T2)
        assert _revision_count(st, u) == 1
        # The stored media stays verbatim: the current row carries the latest reference.
        row = st.conn.execute("select media_json from messages where uri=?", (u,)).fetchone()
        assert '"bb"' in row["media_json"]


def test_nested_file_reference_changes_add_no_revision(tmp_path):
    def wp(ref):
        return {"_": "messageMediaWebPage", "webpage": {
            "_": "WebPage", "url": "https://x", "photo": {"id": 5, "file_reference": ref},
            "cached_page": {"photos": [{"id": 6, "file_reference": ref}]},
            "alt_documents": [{"id": 7, "file_reference": ref}]}}

    with Store.open(tmp_path / "p.sqlite") as st:
        u = _observe(st, _media_msg(2, wp("aa")), _T1)
        _observe(st, _media_msg(2, wp("bb")), _T2)
        assert _revision_count(st, u) == 1


def test_different_photo_id_or_text_still_adds_revision(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        u = _observe(st, _media_msg(3, _photo(photo_id=1)), _T1)
        _observe(st, _media_msg(3, _photo(photo_id=2)), _T2)
        assert _revision_count(st, u) == 2
        v = _observe(st, _media_msg(4, _photo()), _T1)
        _observe(st, _media_msg(4, _photo(), text="edited"), _T2)
        assert _revision_count(st, v) == 2


def test_latest_revision_hashed_under_old_scheme_is_not_a_new_revision(tmp_path):
    import hashlib

    with Store.open(tmp_path / "p.sqlite") as st:
        u = _observe(st, _media_msg(5, _photo(ref="aa")), _T1)
        # Rewrite the stored revision as an older release would have hashed it
        # (verbatim media_json, file_reference included).
        row = st.conn.execute(
            "select id, text, media_json from message_revisions where message_uri=?", (u,)
        ).fetchone()
        old = hashlib.sha256(f"{row['text']}\x00{row['media_json']}".encode()).hexdigest()
        st.conn.execute("update message_revisions set content_hash=? where id=?", (old, row["id"]))
        assert old != content_hash(row["text"], row["media_json"])
        _observe(st, _media_msg(5, _photo(ref="aa")), _T2)  # unchanged
        assert _revision_count(st, u) == 1
        _observe(st, _media_msg(5, _photo(ref="zz")), _T2)  # only the reference rotated
        assert _revision_count(st, u) == 1
