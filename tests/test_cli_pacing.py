"""`--pacing-factor` / `--max-flood-sleep` reach `Settings` (#69)."""

import re

import pytest
from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture
def captured(monkeypatch):
    """Replace `build_gateway` with a stub that records the settings it was given
    and aborts with the clean missing-credentials exit (no network, no keychain)."""
    seen: list = []

    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        seen.append(settings)
        raise composition.ConfigError("stop here")

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    return seen


def test_collect_flags_reach_settings(tmp_path, captured):
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "p", "--unsafe", "--phases", "channel",
         "--pacing-factor", "3", "--max-flood-sleep", "120"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1, result.stdout
    assert captured[0].pacing_factor == 3.0
    assert captured[0].flood_sleep_threshold == 120


def test_collect_rejects_factor_below_one_without_building_a_gateway(tmp_path, captured):
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "p", "--unsafe", "--pacing-factor", "0.5"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 2
    assert captured == []


def test_env_pacing_factor_reaches_settings(tmp_path, captured):
    runner.invoke(
        app,
        ["collect", "@x", "--profile", "p", "--unsafe", "--phases", "channel"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), "PAPERBOY_PACING_FACTOR": "1.5"},
    )
    assert captured[0].pacing_factor == 1.5


def test_doctor_accepts_the_flags(tmp_path, captured):
    result = runner.invoke(
        app,
        ["doctor", "--profile", "p", "--pacing-factor", "4", "--max-flood-sleep", "30"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1, result.stdout
    assert captured[0].pacing_factor == 4.0
    assert captured[0].flood_sleep_threshold == 30


@pytest.mark.asyncio
async def test_build_gateway_logs_the_pacing_line(tmp_path, monkeypatch, caplog):
    import logging

    from paperboy.config import load_settings
    from paperboy.store.db import Store

    class _Client:
        async def connect(self):
            return None

    monkeypatch.setattr(composition, "build_client", lambda *a, **k: _Client())
    caplog.set_level(logging.INFO, logger="paperboy.app")
    with Store.open(tmp_path / "p.sqlite") as store:
        await composition.build_gateway(load_settings("p", {}), None, "p", store)
    line = next(r.message for r in caplog.records if r.message.startswith("pacing:"))
    assert "factor=2.0" in line and "contacts.resolveUsername=10.0s" in line


@pytest.mark.parametrize("command", ["collect", "doctor"])
def test_help_lists_both_flags(command):
    out = _plain(runner.invoke(app, [command, "--help"]).stdout)
    assert "--pacing-factor" in out
    assert "--max-flood-sleep" in out
