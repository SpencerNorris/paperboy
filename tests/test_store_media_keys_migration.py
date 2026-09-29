"""Migration 0006: legacy media locations become profile-relative keys (#62, ADR-0007)."""

from paperboy.store.db import Store

A, B, C = "aa" + "1" * 62, "bb" + "2" * 62, "cc" + "3" * 62


def _unapply(db_path, name):
    """Recreate a pre-migration store: forget that `name` was applied."""
    with Store.open(db_path) as st:
        st.conn.execute("DELETE FROM schema_migrations WHERE name=?", (name,))


def _put(st, table, sha, path):
    if table == "media":
        st.conn.execute("INSERT INTO media(sha256, path) VALUES (?, ?)", (sha, path))
    else:
        st.conn.execute(
            "INSERT INTO custody_log(path, sha256, recorded_at) VALUES (?, ?, 't')", (path, sha)
        )


def _paths(st, table):
    return [r[0] for r in st.conn.execute(f"SELECT path FROM {table} ORDER BY rowid")]


def test_0006_media_keys_rewrites_both_legacy_forms(tmp_path):
    db = tmp_path / "p.sqlite"
    with Store.open(db) as st:
        _put(st, "media", A, f"data/default/media/aa/{A}.jpg")
        _put(st, "media", B, f"/mnt/x/data/default/media/bb/{B}.MP4")
        _put(st, "media", C, f"media/cc/{C}.pdf")
        _put(st, "custody_log", A, f"data/default/media/aa/{A}.jpg")
        _put(st, "custody_log", A, f"/mnt/x/data/default/media/aa/{A}.jpg")
        _put(st, "custody_log", B, f"/mnt/x/data/default/media/bb/{B}.MP4")
        _put(st, "custody_log", C, f"media/cc/{C}.pdf")
    _unapply(db, "0006_media_keys")
    with Store.open(db) as st:
        assert _paths(st, "media") == [
            f"media/aa/{A}.jpg",
            f"media/bb/{B}.MP4",
            f"media/cc/{C}.pdf",
        ]
        assert _paths(st, "custody_log") == [
            f"media/aa/{A}.jpg",
            f"media/aa/{A}.jpg",
            f"media/bb/{B}.MP4",
            f"media/cc/{C}.pdf",
        ]
        names = {r[0] for r in st.conn.execute("SELECT name FROM schema_migrations")}
        assert "0006_media_keys" in names


def test_0006_leaves_rows_without_sha_untouched_and_warns(tmp_path, caplog):
    db = tmp_path / "p.sqlite"
    with caplog.at_level("WARNING", logger="paperboy.store"):
        with Store.open(db) as st:
            assert not [r for r in caplog.records if r.name == "paperboy.store"]
            _put(st, "media", A, "elsewhere/nothing.bin")
        _unapply(db, "0006_media_keys")
        caplog.clear()
        with Store.open(db) as st:
            assert _paths(st, "media") == ["elsewhere/nothing.bin"]
    warnings = [r.getMessage() for r in caplog.records if r.name == "paperboy.store"]
    assert len(warnings) == 1
    assert "media=1" in warnings[0] and "custody_log=0" in warnings[0]
    assert "nothing.bin" not in warnings[0]


def test_0006_is_idempotent(tmp_path):
    db = tmp_path / "p.sqlite"
    with Store.open(db) as st:
        _put(st, "media", A, f"/mnt/x/data/default/media/aa/{A}.jpg")
        _put(st, "custody_log", A, f"data/default/media/aa/{A}.jpg")
    snapshots = []
    for _ in range(2):
        _unapply(db, "0006_media_keys")
        with Store.open(db) as st:
            snapshots.append((_paths(st, "media"), _paths(st, "custody_log")))
    assert snapshots[0] == snapshots[1] == ([f"media/aa/{A}.jpg"], [f"media/aa/{A}.jpg"])


def test_unnormalised_count_uses_the_key_grammar(tmp_path):
    """A legacy suffix that is traversal-safe (". 5") is a valid key; one that is
    not (a backslash) stays counted so the operator is warned."""
    db = tmp_path / "p.sqlite"
    with Store.open(db) as st:
        _put(st, "media", A, f"data/default/media/aa/{A}. 5")
        _put(st, "media", B, f"data/default/media/bb/{B}.a\\b")
        _put(st, "custody_log", A, f"data/default/media/aa/{A}. 5")
    _unapply(db, "0006_media_keys")
    with Store.open(db) as st:
        assert st.unnormalised_media_counts() == {"media": 1, "custody_log": 0}
