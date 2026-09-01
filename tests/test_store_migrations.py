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
