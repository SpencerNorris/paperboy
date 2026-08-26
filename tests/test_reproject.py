"""CLI/orchestration tests for `reproject` (Task 6). The round-trip identity
+ guardrail battery (spec §7) is appended in Task 7, in the same file."""

from __future__ import annotations

import asyncio
import logging

import pytest
from typer.testing import CliRunner

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
