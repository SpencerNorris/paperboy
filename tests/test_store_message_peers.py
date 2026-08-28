"""#11: forward origins and mention-name users become `peers` rows with provenance."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.messages import upsert_message
from paperboy.store.peers import upsert_peer

CHANNEL_ID = 5
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"


def _seed(st, m):
    raw_id = st.add_raw("Message", m, "stranger", {"channel_id": CHANNEL_ID})
    upsert_message(st, CHANNEL_ID, m, raw_id, T1, "stranger")


def test_forward_origins_and_mentioned_users_get_provenance_rows(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, {
            "_": "Message", "id": 1, "message": "fwd", "date": 1767322445,
            "fwd_from": {
                "_": "MessageFwdHeader",
                "from_id": {"_": "PeerChannel", "channel_id": 1003099698},
            },
        })
        _seed(st, {
            "_": "Message", "id": 2, "message": "hi @x", "date": 1767322445,
            "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}},
            "entities": [
                {"_": "MessageEntityMentionName", "offset": 3, "length": 2, "user_id": 43},
                {"_": "MessageEntityBold", "offset": 0, "length": 2},
            ],
        })
        _seed(st, {"_": "Message", "id": 3, "message": "plain", "date": 1767322445})
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 3
        rows = {
            r["uri"]: (r["kind"], r["is_min"], r["seen_in_chat"], r["seen_in_msg"], r["first_seen"])
            for r in st.conn.execute("select * from peers")
        }
        assert rows["tg:channel:1003099698"] == ("channel", 1, CHANNEL_ID, 1, T1)
        assert rows["tg:user:42"] == ("user", 1, CHANNEL_ID, 2, T1)
        assert rows["tg:user:43"] == ("user", 1, CHANNEL_ID, 2, T1)


def test_backfill_is_idempotent_and_never_clobbers_a_full_row(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        full = {"_": "User", "id": 42, "access_hash": 7, "username": "real", "first_name": "R"}
        rid = st.add_raw("User", full, "stranger", None)
        upsert_peer(st, full, rid, T2, seen_in_chat=None, seen_in_msg=None)
        _seed(st, {
            "_": "Message", "id": 2, "message": "x", "date": 1767322445,
            "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 42}},
        })
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 0  # peer 42 already existed
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 0  # new-this-call: nothing new
        row = st.conn.execute(
            "select username, is_min, seen_in_msg from peers where uri='tg:user:42'"
        ).fetchone()
        assert (row["username"], row["is_min"]) == ("real", 0)
        assert row["seen_in_msg"] is None  # the older min reference does not move newer provenance
        assert st.conn.execute("select count(*) from peers").fetchone()[0] == 1


def test_backfill_never_replaces_an_existing_provenance(tmp_path):
    # An authorship context always resolves; a forward-header context only
    # if the user has not restricted forwards. Fill-only, never replace.
    with Store.open(tmp_path / "p.sqlite") as st:
        stub = {"_": "User", "id": 7, "min": True}
        rid = st.add_raw("User", stub, "stranger", None)
        # they AUTHORED msg 500 — the strong context
        upsert_peer(st, stub, rid, T1, seen_in_chat=CHANNEL_ID, seen_in_msg=500)
        _seed(st, {
            "_": "Message", "id": 100, "message": "fwd", "date": 1767322445,
            "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 7}},
        })
        assert backfill_message_referenced_peers(st, CHANNEL_ID) == 0
        row = st.conn.execute("select seen_in_msg from peers where uri='tg:user:7'").fetchone()
        assert row["seen_in_msg"] == 500
