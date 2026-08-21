from paperboy.store.db import Store
from paperboy.store.sync import add_range, get_state, missing_ids, set_state


def test_range_merge_and_gap(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        add_range(st, 7, 1, 5)
        add_range(st, 7, 6, 10)  # adjacent -> merges to 1..10
        add_range(st, 7, 20, 25)
        assert missing_ids(st, 7, 1, 25) == list(range(11, 20))
        rows = st.conn.execute(
            "select lo,hi from sync_ranges where channel_id=7 order by lo"
        ).fetchall()
        assert [(r["lo"], r["hi"]) for r in rows] == [(1, 10), (20, 25)]


def test_range_merge_bridges_a_gap_from_both_sides(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        add_range(st, 7, 1, 5)
        add_range(st, 7, 8, 10)
        add_range(st, 7, 6, 7)  # fills the gap between the two -> single 1..10
        rows = st.conn.execute(
            "select lo,hi from sync_ranges where channel_id=7 order by lo"
        ).fetchall()
        assert [(r["lo"], r["hi"]) for r in rows] == [(1, 10)]


def test_overlapping_ranges_merge(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        add_range(st, 7, 1, 10)
        add_range(st, 7, 5, 15)
        rows = st.conn.execute(
            "select lo,hi from sync_ranges where channel_id=7 order by lo"
        ).fetchall()
        assert [(r["lo"], r["hi"]) for r in rows] == [(1, 15)]


def test_missing_ids_with_no_ranges_is_everything(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        assert missing_ids(st, 7, 1, 5) == [1, 2, 3, 4, 5]


def test_channels_have_independent_ranges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        add_range(st, 7, 1, 10)
        assert missing_ids(st, 8, 1, 10) == list(range(1, 11))


def test_state_round_trip(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        assert get_state(st, "channel", "5") is None
        set_state(st, "channel", "5", {"pts": 42})
        assert get_state(st, "channel", "5") == {"pts": 42}
        set_state(st, "channel", "5", {"pts": 99})
        assert get_state(st, "channel", "5") == {"pts": 99}
