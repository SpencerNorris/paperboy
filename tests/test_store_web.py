from paperboy.ids import utc_now_iso
from paperboy.store.db import Store
from paperboy.store.web import insert_tme_snapshot, insert_wayback_snapshot


def test_insert_tme_snapshot_round_trips(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        rid = insert_tme_snapshot(
            st,
            url="https://t.me/durov/523",
            fetched_at=utc_now_iso(),
            channel_username="durov",
            msg_id=523,
            timestamp="2026-08-20T12:00:00+00:00",
            content_hash="abc123",
            raw={"text": "hi"},
            meta={"views": "1.42M"},
        )
        row = st.conn.execute(
            "SELECT source, url, msg_id, channel_username, meta_json FROM web_snapshots WHERE id=?",
            (rid,),
        ).fetchone()
        assert row["source"] == "tme"
        assert row["msg_id"] == 523
        assert row["channel_username"] == "durov"
        assert '"views": "1.42M"' in row["meta_json"]


def test_insert_tme_snapshot_is_append_only(tmp_path):
    """Two observations of the same post are two rows (an observation log,
    like `channel_snapshots`), not upserted current-state.
    """
    with Store.open(tmp_path / "p.sqlite") as st:
        for _ in range(2):
            insert_tme_snapshot(
                st,
                url="https://t.me/durov/523",
                fetched_at=utc_now_iso(),
                channel_username="durov",
                msg_id=523,
                timestamp=None,
                content_hash=None,
                raw={},
                meta=None,
            )
        n = st.conn.execute("SELECT count(*) AS n FROM web_snapshots").fetchone()["n"]
        assert n == 2


def test_insert_wayback_snapshot_has_no_msg_id(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        rid = insert_wayback_snapshot(
            st,
            url="http://t.me/s/durov",
            fetched_at=utc_now_iso(),
            channel_username="durov",
            timestamp="2019-03-01T12:00:00+00:00",
            content_hash="digest123",
            raw={"timestamp": "20190301120000"},
            meta={"statuscode": "200"},
        )
        row = st.conn.execute(
            "SELECT source, msg_id, content_hash FROM web_snapshots WHERE id=?", (rid,)
        ).fetchone()
        assert row["source"] == "wayback"
        assert row["msg_id"] is None
        assert row["content_hash"] == "digest123"
