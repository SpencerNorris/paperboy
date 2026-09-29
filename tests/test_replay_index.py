"""The per-run raw index behind every replay lookup (#75): `kind_matches` is
the Python twin of the SQL `_kind_clause`, and `RunIndex` is built by exactly
one walk of the run's rowid window."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest

from paperboy import replay
from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayGateway, ReplaySource, kind_matches
from paperboy.store.db import Store

SPELLINGS = [
    "Message", "message", "contacts.resolvedPeer", "ResolvedPeer", "x.y.chatfull",
    "chatfullx", "users.UserFull", "UserFull", ".chatfull", "tme_page",
    "messages.Chats", "Chats", "channels.channelParticipant",
]
KIND_TUPLES = [
    ("message", "messageservice", "messageempty"),
    ("resolvedpeer",),
    ("chatfull",),
    ("users.userfull",),
    ("chats", "chatsslice"),
    ("channels.channelparticipant", "usernotparticipant"),
    ("tme_page",),
]


@pytest.mark.parametrize("stored", SPELLINGS)
@pytest.mark.parametrize("kinds", KIND_TUPLES)
def test_kind_matches_equals_kind_clause_sql(stored, kinds):
    conn = sqlite3.connect(":memory:")
    sql, params = replay._kind_clause(kinds)
    row = conn.execute(
        f"SELECT 1 FROM (SELECT ? AS kind) WHERE {sql}", (stored, *params)
    ).fetchone()
    assert kind_matches(stored.lower(), kinds) == (row is not None)


def _source(tmp_path, populate):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.begin_run("r1")
        populate(st)
    return ReplaySource.open(db, tmp_path)


def _basic(st):
    st.add_raw("User", {"_": "user", "id": 1}, "self", None, observed_at="t1")
    st.add_raw("contacts.ResolvedPeer", {"_": "x"}, "stranger", {"target": "@a"}, observed_at="t2")
    st.add_raw("Message", {"_": "message", "id": "12"}, "stranger", {"channel_id": 5},
               observed_at="t3")
    st.add_raw("Message", {"_": "message", "id": 12, "v": 2}, "stranger", {"channel_id": 5},
               observed_at="t4")
    st.add_raw("Message", {"_": "message", "id": 13}, "stranger", None, observed_at="t5")
    st.add_raw("tme_page", {"url": "http://x/1", "status_code": 200, "text": ""}, "stranger",
               None, observed_at="t6")


def test_index_entries_are_id_ordered_and_bucketed_by_lowercased_kind(tmp_path):
    with _source(tmp_path, _basic) as src:
        run = src.runs()[0]
        idx = src.index(run)
        assert [e.id for e in idx.all_entries] == sorted(e.id for e in idx.all_entries)
        assert set(idx.by_kind) == {"user", "contacts.resolvedpeer", "message", "tme_page"}
        assert [e.observed_at for e in idx.entries(("message",), "suffix")] == ["t3", "t4", "t5"]
        assert [e.kind for e in idx.entries(("resolvedpeer",), "suffix")] == [
            "contacts.resolvedpeer"
        ]
        assert idx.entries(("resolvedpeer",), "exact") == []
        assert [e.kind for e in idx.entries(("resolvedpeer",), "contains")] == [
            "contacts.resolvedpeer"
        ]


def test_lookup_none_value_never_matches(tmp_path):
    with _source(tmp_path, _basic) as src:
        idx = src.index(src.runs()[0])
        # Message 13 has NULL context: a None query must not match its missing key.
        assert idx.lookup(("message",), ("channel_id",), (None,)) == []
        assert len(idx.lookup(("message",), ("channel_id",), (5,))) == 2


def test_lookup_payload_id_keeps_sql_cast_semantics(tmp_path):
    with _source(tmp_path, _basic) as src:
        idx = src.index(src.runs()[0])
        hits = idx.lookup(("message",), ("channel_id", "@payload_id"), (5, 12))
        # payload id "12" (text) and 12 (int) both CAST to 12; id ASC order.
        assert [e.observed_at for e in hits] == ["t3", "t4"]


def test_index_is_built_once_per_run_and_replaced_on_the_next(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.begin_run("r1")
        _basic(st)
        st.begin_run("r2")
        st.add_raw("User", {"_": "user", "id": 1}, "self", None, observed_at="u1")
    with ReplaySource.open(db, tmp_path) as src:
        r1, r2 = src.runs()
        walks: list[str] = []
        src.conn.set_trace_callback(
            lambda s: walks.append(s) if "FROM raw_records WHERE id BETWEEN" in s else None
        )
        try:
            a = src.index(r1)
            assert src.index(r1) is a
            assert len(walks) == 1
            src.index(r2)
            assert len(walks) == 2
            assert src.index(r2) is not a
            assert len(walks) == 2
        finally:
            src.conn.set_trace_callback(None)


def test_index_logs_rows_and_size(tmp_path, caplog):
    with (
        _source(tmp_path, _basic) as src,
        caplog.at_level(logging.INFO, logger="paperboy.replay"),
    ):
        src.index(src.runs()[0])
    msgs = [r.getMessage() for r in caplog.records if r.name == "paperboy.replay"]
    assert any("rows=6" in m and "bytes" in m for m in msgs), msgs


def test_index_warns_over_the_memory_threshold(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(replay, "INDEX_WARN_BYTES", 1)
    with (
        _source(tmp_path, _basic) as src,
        caplog.at_level(logging.INFO, logger="paperboy.replay"),
    ):
        src.index(src.runs()[0])
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_payloads_missing_id_raises(tmp_path):
    with _source(tmp_path, _basic) as src:
        ids = [e.id for e in src.index(src.runs()[0]).all_entries]
        got = src.payloads(ids)
        assert set(got) == set(ids)
        with pytest.raises(replay.ReprojectSourceError):
            src.payloads([ids[-1] + 1000])


# --- Gateway lookups run no range query ---------------------------------------


def _media_run(st):
    st.add_raw("User", {"_": "user", "id": 1}, "self", None, observed_at="t1")
    for mid in range(1, 4):
        st.add_raw("MediaDownload", {"sha256": "ab" * 32, "path": "media/ab/x"}, "stranger",
                   {"channel_id": 5, "msg_id": mid}, observed_at=f"t{mid}")


def test_gateway_lookups_issue_no_sql_beyond_a_point_fetch(tmp_path):
    with _source(tmp_path, _media_run) as src:
        run = src.runs()[0]
        gw = RawReplayGateway(src, ReplayClock(), run)
        src.index(run)  # the one walk happens before we start counting
        stmts: list[str] = []
        src.conn.set_trace_callback(stmts.append)
        try:
            for mid in range(100, 150):  # misses never touch the sink
                assert not asyncio.run(gw.download_media({"channel_id": 5}, {"id": mid}, None))  # type: ignore[arg-type]
            asyncio.run(gw.get_messages({"channel_id": 5}, [1]))  # miss -> placeholder
            assert [s for s in stmts if "raw_records" in s] == []
            asyncio.run(gw.get_self())  # a hit: exactly one point fetch
            fetches = [s for s in stmts if "raw_records" in s]
            assert len(fetches) == 1
        finally:
            src.conn.set_trace_callback(None)
        plan = " ".join(
            r[3] for r in src.conn.execute(
                "EXPLAIN QUERY PLAN SELECT id, observed_at, payload_json FROM raw_records "
                "WHERE id IN (?)", (1,)
            )
        )
        assert "USING INTEGER PRIMARY KEY (rowid=?)" in plan


def _phase_run(st):
    st.add_raw("User", {"_": "user", "id": 1}, "self", None, observed_at="t1")
    st.add_raw("ResolvedPeer", {"_": "x"}, "stranger", {"target": "@a"}, observed_at="t2")
    st.add_raw("ResolvedPeer", {"_": "x"}, "stranger", {"target": "@b"}, observed_at="t3")
    st.add_raw("ResolvedPeer", {"_": "x"}, "stranger", {"target": "@a"}, observed_at="t4")
    st.add_raw("messages.ChatFull", {"full_chat": {"linked_chat_id": 555}}, "stranger",
               {"channel_id": 5}, observed_at="t5")
    st.add_raw("ChatFull", {"full_chat": {"linked_chat_id": 0}}, "stranger",
               {"channel_id": 6}, observed_at="t6")
    st.add_raw("User", {"_": "user", "id": 2}, "stranger",
               {"method": "users.getUsers", "user_id": 2}, observed_at="t7")


def test_source_phase_helpers_read_the_index_not_the_run(tmp_path):
    with _source(tmp_path, _phase_run) as src:
        run = src.runs()[0]
        src.index(run)
        stmts: list[str] = []
        src.conn.set_trace_callback(stmts.append)
        try:
            assert src.resolve_targets(run) == ["@a", "@b"]
            assert src.linked_group_ids(run) == {555}
            assert src.has_kind(run, "chatfull") and not src.has_kind(run, "mediadownload")
            assert src.has_context_channel(run, {5}) and not src.has_context_channel(run, {9})
            assert src.has_context_value(run, "method", "users.getUsers")
            assert not src.has_context_value(run, "method", "nope")
        finally:
            src.conn.set_trace_callback(None)
        assert not [s for s in stmts if "BETWEEN" in s or "json_extract" in s], stmts
