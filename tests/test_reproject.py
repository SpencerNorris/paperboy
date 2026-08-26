"""CLI/orchestration tests for `reproject` (Task 6). The round-trip identity
+ guardrail battery (spec §7) is appended in Task 7, in the same file."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paperboy.budget import PhaseStop
from paperboy.cli import app
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource
from paperboy.reproject import detect_phases
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_reproject_parity import full_collect_fixtures, run_full_collect

runner = CliRunner()


@pytest.mark.asyncio
async def test_detect_phases_reflects_recorded_raw_kinds(tmp_path):
    db = await run_full_collect(tmp_path)
    src = ReplaySource.open(db, tmp_path / "default" / "media")
    phases = detect_phases(src)
    assert phases[:2] == ["channel", "history"]
    assert "graph" in phases and "web" in phases and "media" in phases


@pytest.mark.asyncio
async def test_detect_phases_minimal_source(tmp_path):
    # channel+history-only raw log -> no graph/web/media/discussion phases.
    settings = load_settings("default", {"data_dir": tmp_path})
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        await collect_channel(
            FakeGateway(full_collect_fixtures()), store, settings,
            parse_target("@durov"), phases=["channel", "history"],
            log=logging.getLogger("t"),
        )
    src = ReplaySource.open(db, tmp_path / "default" / "media")
    assert detect_phases(src) == ["channel", "history"]


def test_cli_reproject_writes_fresh_db_and_prints_diff(tmp_path, monkeypatch):
    # NOT `async def`: `runner.invoke` runs the CLI command, which calls
    # `asyncio.run()` internally — nesting that inside a pytest-asyncio
    # test's own running loop raises "asyncio.run() cannot be called from a
    # running event loop". `run_full_collect` is a plain coroutine, driven
    # here with its own asyncio.run() instead of `await`.
    asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    out_db = tmp_path / "default" / "paperboy.reprojected.sqlite"
    assert out_db.exists()
    assert "channels" in result.output and "messages" in result.output


def test_cli_reproject_refuses_existing_out(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    out.write_bytes(b"")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 1
    assert "refusing" in result.output.lower()


def test_cli_reproject_empty_source_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    (tmp_path / "default").mkdir(parents=True)
    with Store.open(tmp_path / "default" / "paperboy.sqlite"):
        pass  # schema only, no raws
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 1
    assert "no resolve records" in result.output


def test_cli_reproject_cleans_up_out_on_unexpected_source_error(tmp_path, monkeypatch):
    # A corrupted/unreadable source DB fails deep inside
    # `ReplaySource.resolve_targets` (a bare `sqlite3.DatabaseError`), not
    # `ReprojectError` — found running reproject against a hand-corrupted
    # file during DoD smoke testing. `build_reproject` already created and
    # migrated `out_path` by this point; the CLI's cleanup must not be
    # narrowed to only the `ReprojectError` branch, or a half-made file is
    # left behind and a retry against the default --out spuriously hits
    # "refusing to overwrite existing" for a file that was never usable.
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    (tmp_path / "default").mkdir(parents=True)
    (tmp_path / "default" / "paperboy.sqlite").write_bytes(b"not a real sqlite file")
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code != 0
    assert not (tmp_path / "default" / "paperboy.reprojected.sqlite").exists()


def test_cli_reproject_phases_override(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(
        app, ["reproject", "--profile", "default", "--phases", "channel,history"]
    )
    assert result.exit_code == 0, result.output
    out = tmp_path / "default" / "paperboy.reprojected.sqlite"
    with Store.open(out) as store:
        edges_n = store.conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    # graph never ran, so no mention edges were reprojected either.
    assert edges_n == 0


def test_cli_reproject_custom_out_path(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    custom_out = tmp_path / "custom.sqlite"
    result = runner.invoke(
        app, ["reproject", "--profile", "default", "--out", str(custom_out)]
    )
    assert result.exit_code == 0, result.output
    assert custom_out.exists()
    assert not (tmp_path / "default" / "paperboy.reprojected.sqlite").exists()


def test_one_bad_historical_target_does_not_abort_other_targets(tmp_path, monkeypatch):
    # A source archive can carry more than one resolved target across its
    # history (successive `collect` runs against different inputs, found
    # exercising this against a real archive — DoD smoke, see the feature
    # doc). One later turning out not to be a channel (an accidental collect
    # against a user, say) crashes `channel.collect`'s `_resolved_channel_id`
    # with a bare ValueError even on a live run; reproject's multi-target
    # loop must not let that discard every other target's already-committed
    # projections the way a single crashed `collect` run naturally would.
    db = tmp_path / "default" / "paperboy.sqlite"
    with Store.open(db) as st:
        resolve = json.loads(Path("tests/fixtures/tl/resolve_durov.json").read_text())
        full_channel = json.loads(Path("tests/fixtures/tl/full_channel.json").read_text())
        st.add_raw(resolve.get("_"), resolve, "stranger", {"target": "@durov"})
        st.add_raw(full_channel.get("_"), full_channel, "stranger", {"channel_id": 5})
        st.add_raw("user", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw(
            "contacts.resolvedPeer",
            {"_": "contacts.resolvedPeer", "peer": {"_": "PeerUser", "user_id": 999},
             "chats": [], "users": [{"_": "User", "id": 999}]},
            "stranger", {"target": "@notachannel"},
        )
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    out = sqlite3.connect(tmp_path / "default" / "paperboy.reprojected.sqlite")
    # The good target's channel landed despite the bad target existing too.
    assert out.execute("SELECT count(*) FROM channels WHERE id=5").fetchone()[0] == 1
    out.close()
    assert "notachannel" in result.output


# ---------------------------------------------------------------------------
# Task 7: the correctness battery — round-trip identity + guardrails (spec §7)
# ---------------------------------------------------------------------------

# D5 (plan): equality modulo autoincrement pks and source_raw_id, compared as
# DISTINCT row sets — replay legitimately serves one observation through two
# paths (getHistory + getChannelDifference), duplicating byte-identical rows.
ROUND_TRIP_EXCLUDE = {
    "raw_records": {"id"},
    "channels": {"source_raw_id"},
    "channel_snapshots": {"id", "source_raw_id"},
    "peers": {"source_raw_id"},
    "messages": {"source_raw_id"},
    "message_revisions": {"id", "source_raw_id"},
    "message_metrics": {"id"},
    "message_tombstones": {"id"},
    "edges": {"id", "source_raw_id"},
    "media": set(),
    "custody_log": {"id"},
    "web_snapshots": {"id"},
}


def _table_set(conn: sqlite3.Connection, table: str, exclude: set[str]) -> set[tuple]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in exclude]
    sql = f"SELECT {', '.join(cols)} FROM {table}"
    return {tuple(row) for row in conn.execute(sql)}


def assert_round_trip(
    db1: Path, db2: Path, *, skip_tables: frozenset[str] = frozenset()
) -> None:
    c1, c2 = sqlite3.connect(db1), sqlite3.connect(db2)
    try:
        for table, exclude in ROUND_TRIP_EXCLUDE.items():
            if table in skip_tables:
                continue
            s1, s2 = _table_set(c1, table, exclude), _table_set(c2, table, exclude)
            assert s1 == s2, (
                f"{table} diverged:\n only in source: {sorted(s1 - s2)[:5]}\n"
                f" only in reprojected: {sorted(s2 - s1)[:5]}"
            )
    finally:
        c1.close()
        c2.close()


def test_round_trip_identity(tmp_path, monkeypatch):
    """Spec §7: collect -> reproject -> projections identical, timestamps
    included. Runs with the REAL clock (no freezing) — timestamp equality is
    exactly what the observed-at seam exists to guarantee."""
    db1 = asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db1, tmp_path / "default" / "paperboy.reprojected.sqlite")


def test_reproject_is_incapable_of_network_or_keychain(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))

    def _forbidden(*a, **k):
        raise AssertionError("reproject touched a forbidden constructor")

    import keyring

    import paperboy.gateway as gw
    import paperboy.web.client as wc

    monkeypatch.setattr(gw.TelethonGateway, "__init__", _forbidden)
    monkeypatch.setattr(wc.WebClient, "__init__", _forbidden)
    monkeypatch.setattr(keyring, "get_password", _forbidden)
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output


async def _collect_with_fixtures(
    data_dir: Path, fixtures: dict, phases: list[str]
) -> Path:
    settings = load_settings("default", {"data_dir": data_dir})
    db = data_dir / "default" / "paperboy.sqlite"
    with Store.open(db) as store:
        await collect_channel(
            FakeGateway(fixtures), store, settings, parse_target("@durov"),
            phases=phases, log=logging.getLogger("t"),
        )
    return db


def test_reproject_corrects_old_code_projections(tmp_path, monkeypatch):
    """A DB whose projections were written by OLD code (a reduced-flag
    channel, a stored self peer, a lost edge) reprojects to the current
    correct shape — raw is the system of record."""
    fx = full_collect_fixtures()
    # A richer channel than the shared parity fixture (which carries only
    # `broadcast`), so "old code dropped flags" is a meaningful scenario.
    fx["full_channel"] = {
        **fx["full_channel"],
        "full_chat": {
            **fx["full_channel"]["full_chat"],
            "can_view_participants": True, "antispam": True,
        },
        "chats": [{
            **fx["full_channel"]["chats"][0],
            "verified": True, "noforwards": True, "signatures": True,
        }],
    }
    # channel+history+graph: graph is what turns msg 3's URL entity into a
    # mention edge, giving the "lost edge" simulation below something real
    # to restore.
    db1 = asyncio.run(_collect_with_fixtures(tmp_path, fx, ["channel", "history", "graph"]))
    conn = sqlite3.connect(db1)
    good_flags = conn.execute("SELECT flags_json FROM channels").fetchone()[0]
    assert len(json.loads(good_flags)) > 3
    conn.execute("UPDATE channels SET flags_json = ?",
                 (json.dumps({"broadcast": True}),))          # old 1-flag shape
    conn.execute(
        "INSERT INTO peers (uri, kind, id, is_min, first_seen, last_seen) "
        "VALUES ('tg:user:1', 'user', 1, 0, 'x', 'x')")       # self row (pre-#12)
    conn.execute("DELETE FROM edges")                         # lost projections
    conn.commit()
    conn.close()

    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    out = sqlite3.connect(tmp_path / "default" / "paperboy.reprojected.sqlite")
    assert json.loads(out.execute("SELECT flags_json FROM channels").fetchone()[0]) \
        == json.loads(good_flags)
    assert out.execute("SELECT count(*) FROM peers WHERE uri='tg:user:1'").fetchone()[0] == 0
    assert out.execute("SELECT count(*) FROM edges").fetchone()[0] > 0
    out.close()


def test_source_without_graph_reprojects_without_graph(tmp_path, monkeypatch):
    # channel+history-only DB1 (no graph/web/media raws) -> DB2 must not grow
    # mention edges the original never projected (plan D4.5).
    db1 = asyncio.run(
        _collect_with_fixtures(tmp_path, full_collect_fixtures(), ["channel", "history"])
    )
    src = ReplaySource.open(db1, tmp_path / "default" / "media")
    assert detect_phases(src) == ["channel", "history"]
    src.close()

    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db1, tmp_path / "default" / "paperboy.reprojected.sqlite")


def _diff_page(pts: int, *, final: bool, mid: int) -> dict:
    return {
        "_": "updates.channelDifference", "final": final, "pts": pts,
        "new_messages": [
            {"_": "message", "id": mid, "message": f"c{mid}", "date": 1767322445}
        ],
        "other_updates": [],
    }


def test_partial_interrupted_source_reprojects_to_same_partial_state(tmp_path, monkeypatch):
    # DB1 whose catch_up PhaseStopped mid-backlog: the channel_difference
    # fixture is [non_final_page(pts 50), PhaseStop("flood")]. Reproject must
    # land on the same partial projections — raw_records is excluded here:
    # replay closes the log with one synthetic final empty diff the
    # interrupted original never wrote (plan D4.4).
    fx = full_collect_fixtures()
    fx["channel_difference"] = [_diff_page(50, final=False, mid=99), PhaseStop("flood")]

    async def _collect() -> Path:
        settings = load_settings("default", {"data_dir": tmp_path})
        db = tmp_path / "default" / "paperboy.sqlite"
        with Store.open(db) as store:
            results = await collect_channel(
                FakeGateway(fx), store, settings, parse_target("@durov"),
                phases=["channel", "history"], log=logging.getLogger("t"),
            )
        history = next(r for r in results if r.name == "history")
        assert history.stopped == "phase_stop"
        return db

    db1 = asyncio.run(_collect())
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(
        db1, tmp_path / "default" / "paperboy.reprojected.sqlite",
        skip_tables=frozenset({"raw_records"}),
    )


def test_reproject_never_rewrites_media_files(tmp_path, monkeypatch):
    asyncio.run(run_full_collect(tmp_path))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))

    def _no_write(self, data):
        raise AssertionError(f"reproject wrote a media file: {self}")

    monkeypatch.setattr(Path, "write_bytes", _no_write)
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Revision R (ADR-0005): the two-run round-trip gate (#33)
# ---------------------------------------------------------------------------


def _second_run_fixtures(fx: dict) -> dict:
    """Run 2 observes one NEW message carrying NEW media, on top of
    everything run 1 saw — so run 2 exercises incremental backfill, repeat
    snapshots/metrics/web captures, AND a fresh MediaDownload (which is what
    makes run 2's media phase raw-detectable, spec D4.5/ADR-0005 residual)."""
    fx = {**fx, "history": [
        {
            "_": "message", "id": 4, "message": "new in run 2",
            "date": 1769322400, "views": 3,
            "media": {
                "_": "MessageMediaDocument",
                "document": {
                    "_": "Document", "id": 77, "access_hash": 1,
                    "mime_type": "text/plain",
                    "attributes": [
                        {"_": "DocumentAttributeFilename", "file_name": "b.txt"}
                    ],
                },
            },
        },
        *fx["history"],
    ]}
    fx["media"] = {**fx["media"], 4: b"second run bytes"}
    return fx


@pytest.mark.xfail(
    reason="#33 / ADR-0005: multi-run replay lands in tasks R2-R5",
    strict=True,
)
def test_two_run_round_trip_identity(tmp_path, monkeypatch):
    """ADR-0005 gate: a source built from TWO collect passes — the ordinary
    real-archive shape — must round-trip identically, time series included."""
    asyncio.run(run_full_collect(tmp_path))
    db1 = asyncio.run(run_full_collect(tmp_path, mutate_fixtures=_second_run_fixtures))
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    assert_round_trip(db1, tmp_path / "default" / "paperboy.reprojected.sqlite")
