import pytest

from paperboy.store.db import Store


def test_migrations_and_raw(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        rid = st.add_raw(
            "channelFull", {"id": 1, "title": "x"}, tier="stranger", context={"target": "@x"}
        )
        assert isinstance(rid, int)
        row = st.conn.execute(
            "select kind, payload_json, tier from raw_records where id=?", (rid,)
        ).fetchone()
        assert row["kind"] == "channelFull"
        assert '"title"' in row["payload_json"]
        assert row["tier"] == "stranger"
        applied = [
            r["name"]
            for r in st.conn.execute("select name from schema_migrations order by name")
        ]
        assert "0001_init" in applied
        assert st.conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
        assert st.conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_add_raw_without_context(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        rid = st.add_raw("message", {"id": 5}, tier="member", context=None)
        row = st.conn.execute("select context_json from raw_records where id=?", (rid,)).fetchone()
        assert row["context_json"] is None


def test_reopen_does_not_reapply_migrations(tmp_path):
    path = tmp_path / "p.sqlite"
    with Store.open(path) as st:
        first_open_count = st.conn.execute("select count(*) from schema_migrations").fetchone()[0]
        st.add_raw("x", {}, tier="stranger", context=None)
    with Store.open(path) as st:
        count = st.conn.execute("select count(*) from schema_migrations").fetchone()[0]
        assert count == first_open_count  # not reapplied / duplicated
        rows = st.conn.execute("select count(*) from raw_records").fetchone()[0]
        assert rows == 1


def test_expected_tables_exist(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        names = {
            r["name"]
            for r in st.conn.execute("select name from sqlite_master where type='table'")
        }
        for expected in (
            "raw_records",
            "channels",
            "peers",
            "messages",
            "media",
            "channel_snapshots",
            "message_revisions",
            "message_metrics",
            "message_tombstones",
            "edges",
            "sync_state",
            "sync_ranges",
            "flood_log",
            "custody_log",
            "run_events",
            "schema_migrations",
            "web_snapshots",
        ):
            assert expected in names, f"missing table {expected}"


def test_0005_flood_applied_column(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        applied = {r["name"] for r in st.conn.execute("select name from schema_migrations")}
        assert "0005_flood_applied" in applied
        cols = {r["name"] for r in st.conn.execute("pragma table_info(flood_log)")}
        assert "applied_seconds" in cols
        # Pre-#69 rows have NULL applied_seconds and must still be insertable/readable.
        st.conn.execute(
            "insert into flood_log(method, until, seconds, recorded_at) "
            "values ('m', '2026-01-01T00:00:00+00:00', 3, '2026-01-01T00:00:00+00:00')"
        )
        row = st.conn.execute("select applied_seconds from flood_log").fetchone()
        assert row["applied_seconds"] is None


def test_0002_web_migration_applied(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        applied = {
            r["name"] for r in st.conn.execute("select name from schema_migrations")
        }
        assert "0002_web" in applied


def test_messages_fts_tracks_inserts(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        st.conn.execute(
            "insert into messages"
            "(uri, channel_id, msg_id, text, content_hash, first_seen, last_seen) "
            "values ('tg:msg:1/1', 1, 1, 'hello world', 'h', 'now', 'now')"
        )
        hits = st.conn.execute(
            "select messages.uri from messages "
            "join messages_fts on messages.rowid = messages_fts.rowid "
            "where messages_fts match 'hello'"
        ).fetchall()
        assert len(hits) == 1


def test_0004_people_tables_exist(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        applied = {r["name"] for r in st.conn.execute("select name from schema_migrations")}
        assert "0004_people" in applied
        names = {
            r["name"]
            for r in st.conn.execute("select name from sqlite_master where type='table'")
        }
        for expected in (
            "users", "user_snapshots", "user_photos", "participants", "participant_snapshots",
            "profile_attempts",
        ):
            assert expected in names, f"missing table {expected}"


def test_participants_status_is_constrained(tmp_path):
    import sqlite3

    with Store.open(tmp_path / "p.sqlite") as st, pytest.raises(sqlite3.IntegrityError):
        st.conn.execute(
            "insert into participants (group_id, uri, status, first_seen, last_seen) "
            "values (1, 'tg:user:1', 'lurker', 'now', 'now')"
        )


def test_user_photos_unique_per_user_and_photo(tmp_path):
    import sqlite3

    with Store.open(tmp_path / "p.sqlite") as st:
        st.conn.execute(
            "insert into user_photos (uri, photo_id, observed_at) values ('tg:user:1', 7, 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            st.conn.execute(
                "insert into user_photos (uri, photo_id, observed_at) "
                "values ('tg:user:1', 7, 'later')"
            )


def test_run_id_property_reflects_begin_run(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        assert st.run_id is None
        assert st.begin_run("r1") == "r1" and st.run_id == "r1"


def test_0007_custody_log_store_column(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        applied = {r["name"] for r in st.conn.execute("select name from schema_migrations")}
        assert "0007_media_stores" in applied
        cols = {r["name"]: r for r in st.conn.execute("pragma table_info(custody_log)")}
        assert cols["store"]["notnull"] == 1
        assert cols["store"]["dflt_value"] == "'local'"
        # A row inserted the pre-0007 way (no store) reads back as local.
        st.conn.execute(
            "insert into custody_log(path, sha256, recorded_at) values ('media/ab/x', 'ab', 'now')"
        )
        row = st.conn.execute("select store from custody_log").fetchone()
        assert row["store"] == "local"


def test_0008_backfills_custody_content_key_only_where_certain(tmp_path):
    """A sighting gets its content key when its message never carried another
    key; an edited message's sightings stay NULL (unknown), never guessed (#91)."""
    import json as _json

    from paperboy.store.db import _MIGRATIONS_DIR

    def photo(pid, fref="x"):
        return _json.dumps(
            {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": pid, "file_reference": fref}}
        )

    with Store.open(tmp_path / "p.sqlite") as st:
        conn = st.conn
        for uri, mid in (("tg:msg:1/1", 1), ("tg:msg:1/2", 2)):
            conn.execute(
                "insert into messages (uri, channel_id, msg_id, media_json, media_kind, "
                "content_hash, first_seen, last_seen) "
                "values (?, 1, ?, ?, 'MessageMediaPhoto', 'h', 't', 't')",
                (uri, mid, photo(100 if mid == 1 else 300)),
            )
        # 1/1: two revisions, SAME photo (a refreshed file_reference). 1/2: edited A -> B.
        for uri, pid, fref in (
            ("tg:msg:1/1", 100, "a"), ("tg:msg:1/1", 100, "b"),
            ("tg:msg:1/2", 200, "a"), ("tg:msg:1/2", 300, "a"),
        ):
            conn.execute(
                "insert into message_revisions "
                "(message_uri, observed_at, content_hash, media_json) values (?, 't', ?, ?)",
                (uri, f"{uri}{pid}{fref}", photo(pid, fref)),
            )
        conn.execute(  # the file 'ab' was stored for message 1/1
            "insert into media (sha256, message_uri, kind, size, path, downloaded_at) "
            "values ('ab', 'tg:msg:1/1', 'photo', 1, 'media/ab/x', 'now')"
        )
        for uri in ("tg:msg:1/1", "tg:msg:1/2", None):
            conn.execute(
                "insert into custody_log (path, sha256, recorded_at, source_message_uri) "
                "values ('media/ab/x', 'ab', 'now', ?)", (uri,),
            )
        conn.execute("alter table custody_log drop column content_key")
        conn.executescript((_MIGRATIONS_DIR / "0008_custody_content_key.sql").read_text())
        keys = [
            r[0] for r in conn.execute("select content_key from custody_log order by id")
        ]
        assert keys == ["photo:100", None, None]


def test_0008_does_not_stamp_a_dedup_sighting_of_a_file_stored_for_an_edited_post(tmp_path):
    """The pre-0008 index filed a file under its message's CURRENT media. Post P was
    downloaded as photo 1001 (file A) and later edited to 2002; Q, stable on 2002,
    then got a dedup sighting naming A. A is NOT photo 2002's file, so Q's sighting
    must stay NULL (unknown), though Q itself never changed (#91, review F4)."""
    import json as _json

    from paperboy.store.db import _MIGRATIONS_DIR

    def photo(pid):
        return _json.dumps(
            {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": pid}}
        )

    with Store.open(tmp_path / "p.sqlite") as st:
        conn = st.conn
        for uri, mid, pid in (("tg:msg:1/1", 1, 2002), ("tg:msg:1/2", 2, 2002)):
            conn.execute(
                "insert into messages (uri, channel_id, msg_id, media_json, media_kind, "
                "content_hash, first_seen, last_seen) "
                "values (?, 1, ?, ?, 'MessageMediaPhoto', 'h', 't', 't')",
                (uri, mid, photo(pid)),
            )
        # P (1/1) was photo 1001, then edited to 2002; Q (1/2) was always 2002.
        for uri, pid in (("tg:msg:1/1", 1001), ("tg:msg:1/1", 2002), ("tg:msg:1/2", 2002)):
            conn.execute(
                "insert into message_revisions "
                "(message_uri, observed_at, content_hash, media_json) values (?, 't', ?, ?)",
                (uri, f"{uri}{pid}", photo(pid)),
            )
        conn.execute(  # file A, downloaded for P while it was photo 1001
            "insert into media (sha256, message_uri, kind, size, path, downloaded_at) "
            "values ('aa', 'tg:msg:1/1', 'photo', 1, 'media/aa/x', 'now')"
        )
        for uri in ("tg:msg:1/1", "tg:msg:1/2"):  # P's download, Q's dedup hit
            conn.execute(
                "insert into custody_log (path, sha256, recorded_at, source_message_uri) "
                "values ('media/aa/x', 'aa', 'now', ?)", (uri,),
            )
        conn.execute("alter table custody_log drop column content_key")
        conn.executescript((_MIGRATIONS_DIR / "0008_custody_content_key.sql").read_text())
        assert [r[0] for r in conn.execute(
            "select content_key from custody_log order by id")] == [None, None]


def test_migration_runner_logs_start_and_duration(tmp_path, caplog):
    """A slow migration must be visible: each applied migration logs its name and
    how long it took (#91: 0008 was quadratic and silent for minutes)."""
    import logging

    with caplog.at_level(logging.INFO, logger="paperboy.store"):
        Store.open(tmp_path / "p.sqlite").close()
    messages = [r.getMessage() for r in caplog.records]
    assert "migration 0008_custody_content_key: applying" in messages
    assert any(
        m.startswith("migration 0008_custody_content_key: applied in ") for m in messages
    )
