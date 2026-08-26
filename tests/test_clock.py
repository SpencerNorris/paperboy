"""tests/test_clock.py"""

from __future__ import annotations

import pytest

from paperboy.clock import LiveClock, ReplayClock, ReplayClockError
from paperboy.store.db import Store, dumps


def test_live_clock_returns_fresh_iso_utc():
    t = LiveClock().for_payload({"_": "Message", "id": 1})
    assert t.endswith("+00:00")


def test_replay_clock_returns_served_payloads_stamp():
    clock = ReplayClock()
    m1, m2 = {"_": "Message", "id": 1}, {"_": "Message", "id": 2}
    clock.serve("2026-01-01T00:00:01+00:00", m1)
    clock.serve("2026-01-01T00:00:02+00:00", m2)
    # Payload-keyed: order of lookup does not matter (history consumes a
    # whole page before projecting each message).
    assert clock.for_payload(m2) == "2026-01-01T00:00:02+00:00"
    assert clock.for_payload(m1) == "2026-01-01T00:00:01+00:00"
    # Lookup is by value, not object identity — a re-parsed equal dict hits.
    assert clock.for_payload({"_": "Message", "id": 1}) == "2026-01-01T00:00:01+00:00"


def test_replay_clock_serve_json_matches_dict_lookup():
    clock = ReplayClock()
    payload = {"sha256": "ab", "kind": "photo"}
    clock.serve_json("2026-01-01T00:00:03+00:00", dumps(payload))
    assert clock.for_payload(payload) == "2026-01-01T00:00:03+00:00"


def test_replay_clock_unknown_payload_falls_back_to_last_served():
    clock = ReplayClock()
    clock.serve("2026-01-01T00:00:04+00:00", {"_": "ChannelDifference"})
    # A nested dict with no individually-stored record inherits its
    # envelope's stamp.
    assert clock.for_payload({"_": "novel"}) == "2026-01-01T00:00:04+00:00"


def test_replay_clock_raises_before_anything_served():
    with pytest.raises(ReplayClockError):
        ReplayClock().for_payload({"_": "x"})


def test_begin_batch_clears_registry_but_keeps_current():
    clock = ReplayClock()
    clock.serve("2026-01-01T00:00:05+00:00", {"_": "a"})
    clock.begin_batch()
    assert clock.for_payload({"_": "a"}) == "2026-01-01T00:00:05+00:00"  # fallback


def test_add_raw_accepts_explicit_observed_at(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger",
                      {"channel_id": 5}, observed_at="2026-01-01T00:00:06+00:00")
        row = store.conn.execute("SELECT observed_at FROM raw_records").fetchone()
        assert row["observed_at"] == "2026-01-01T00:00:06+00:00"


def test_add_raw_defaults_to_now(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        row = store.conn.execute("SELECT observed_at FROM raw_records").fetchone()
        assert row["observed_at"].endswith("+00:00")


def test_add_raw_stamps_the_begun_run(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        rid = store.begin_run()
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        row = store.conn.execute("SELECT run_id FROM raw_records").fetchone()
        assert row["run_id"] == rid and len(rid) == 32


def test_add_raw_without_begin_run_leaves_null(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        store.add_raw("Message", {"_": "Message", "id": 1}, "stranger", None)
        assert store.conn.execute(
            "SELECT run_id FROM raw_records"
        ).fetchone()["run_id"] is None


def test_begin_run_accepts_injected_id(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as store:
        assert store.begin_run("legacy-0001") == "legacy-0001"
