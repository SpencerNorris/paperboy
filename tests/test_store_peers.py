import json

from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer


def test_min_does_not_clobber_full(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        full = {"_": "user", "id": 9, "access_hash": 111, "username": "real", "first_name": "Real"}
        r1 = st.add_raw("user", full, "member", None)
        upsert_peer(st, full, r1, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        mn = {"_": "user", "id": 9, "min": True, "first_name": "MinName"}
        r2 = st.add_raw("user", mn, "stranger", None)
        upsert_peer(st, mn, r2, "2026-01-02T00:00:00+00:00", seen_in_chat=7, seen_in_msg=34)
        row = st.conn.execute(
            "select username, first_name, is_min, seen_in_msg from peers where uri='tg:user:9'"
        ).fetchone()
        assert row["username"] == "real"
        assert row["first_name"] == "Real"  # not clobbered
        assert row["seen_in_msg"] == 34  # provenance updated
        assert row["is_min"] == 0  # the full row's is_min flag is untouched


def test_first_observation_min_is_stored_as_is(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        mn = {"_": "user", "id": 42, "min": True, "first_name": "OnlyMin"}
        r = st.add_raw("user", mn, "stranger", None)
        uri = upsert_peer(st, mn, r, "2026-01-01T00:00:00+00:00", seen_in_chat=7, seen_in_msg=1)
        assert uri == "tg:user:42"
        row = st.conn.execute("select is_min, first_name from peers where uri=?", (uri,)).fetchone()
        assert row["is_min"] == 1
        assert row["first_name"] == "OnlyMin"


def test_full_observation_overwrites_a_prior_full_observation(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        old = {"_": "user", "id": 9, "username": "old_handle", "first_name": "Old"}
        r1 = st.add_raw("user", old, "member", None)
        upsert_peer(st, old, r1, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        new = {"_": "user", "id": 9, "username": "new_handle", "first_name": "New"}
        r2 = st.add_raw("user", new, "member", None)
        upsert_peer(st, new, r2, "2026-01-02T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None)
        row = st.conn.execute(
            "select username, first_name from peers where uri='tg:user:9'"
        ).fetchone()
        assert row["username"] == "new_handle"
        assert row["first_name"] == "New"


def test_channel_typed_peer_stores_title(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {"_": "channel", "id": 5, "access_hash": 99, "title": "Durov", "username": "durov"}
        r = st.add_raw("channel", chan, "stranger", None)
        uri = upsert_peer(
            st, chan, r, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None
        )
        assert uri == "tg:channel:5"
        row = st.conn.execute("select kind, title from peers where uri=?", (uri,)).fetchone()
        assert row["kind"] == "channel"
        assert row["title"] == "Durov"


def test_out_of_order_observation_keeps_seen_window_and_newest_state(tmp_path):
    # ADR-0005 §6: replay serves records in RECORDED order, not necessarily
    # observed_at order across runs — an older observation can arrive at
    # upsert_peer AFTER a newer one already landed. first_seen/last_seen must
    # widen to the true window regardless of arrival order, and stale data
    # must never clobber a newer value.
    with Store.open(tmp_path / "p.sqlite") as store:
        raw_new = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        raw_old = store.add_raw("User", {"_": "user", "id": 9}, "stranger", None)
        upsert_peer(store, {"_": "user", "id": 9, "username": "new_name"},
                    raw_new, "2026-02-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        # The OLDER observation arrives second (out of order):
        upsert_peer(store, {"_": "user", "id": 9, "username": "old_name"},
                    raw_old, "2026-01-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)
        row = store.conn.execute(
            "SELECT username, first_seen, last_seen FROM peers"
        ).fetchone()
        assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert row["last_seen"] == "2026-02-01T00:00:00+00:00"
        assert row["username"] == "new_name"   # stale data must not clobber


def test_join_to_send_flag_is_persisted(tmp_path):
    """`join_to_send` is the passivity guardrail the `discussion` collector's
    preflight relies on to decide whether reading a linked group requires
    membership (design spec §4.3) — `docs/data-model.md` already documents
    it as a stored flag. `_FLAG_KEYS` currently omits it, so it is silently
    dropped here: `upsert_peer` of a `Channel` carrying `join_to_send: True`
    stores `flags_json` with no trace of it, which is a production gap
    (`_FLAG_KEYS` needs `"join_to_send"` added), not a test-fixture problem.
    Pinning it here means the discussion-collector test that seeds this
    flag through `upsert_peer` cannot be made to pass by writing
    `flags_json` directly in that test's own helper instead of fixing this
    projection — see `tests/test_collector_discussion.py::
    test_skips_when_the_group_requires_joining_to_read`."""
    with Store.open(tmp_path / "p.sqlite") as st:
        chan = {
            "_": "Channel", "id": 77, "access_hash": 4242, "title": "C Chat",
            "megagroup": True, "join_to_send": True,
        }
        r = st.add_raw("channel", chan, "stranger", None)
        uri = upsert_peer(
            st, chan, r, "2026-01-01T00:00:00+00:00", seen_in_chat=None, seen_in_msg=None
        )
        row = st.conn.execute("select flags_json from peers where uri=?", (uri,)).fetchone()
        flags = json.loads(row["flags_json"]) if row["flags_json"] else {}
        assert flags.get("join_to_send") is True
