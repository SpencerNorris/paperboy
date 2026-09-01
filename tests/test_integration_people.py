"""Default-set + --profiles gating (spec §11), read-only guardrail, CLI wiring."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter

import pytest
from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app as cli_app
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_integration_discussion import _GATEWAY_READ_METHODS
from tests.test_reproject_people import GROUP_ID, people_fixtures

runner = CliRunner()


@pytest.mark.asyncio
async def test_plain_collect_runs_both_person_phases_but_never_getfulluser(tmp_path):
    gw = FakeGateway(people_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {"data_dir": tmp_path, "unsafe": True}),
            parse_target("@c"), phases=None, log=logging.getLogger("t"),
        )
        assert [r.name for r in results] == [
            "channel", "history", "discussion", "participants", "profiles", "graph",
        ]
        assert gw.full_user_calls == [] and gw.user_photos_calls == [] and gw.avatar_calls == []
        assert gw.calls.count("get_users") >= 1  # triage IS default-on
        assert st.conn.execute("select count(*) from participants").fetchone()[0] >= 2
        warning = st.conn.execute(
            "select detail_json from run_events where phase='profiles' and kind='warning'"
        ).fetchone()
        assert json.loads(warning["detail_json"])["code"] == "profiles_enrichment_off"
        assert set(gw.calls) <= _GATEWAY_READ_METHODS | {"join_channel"}
        assert "join_channel" not in gw.calls


@pytest.mark.asyncio
async def test_profiles_setting_enables_the_full_sweep(tmp_path):
    gw = FakeGateway(people_fixtures())
    settings = load_settings(
        "default", {"data_dir": tmp_path, "unsafe": True, "enrich_profiles": True}
    )
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            gw, st, settings, parse_target("@c"), phases=None, log=logging.getLogger("t"),
        )
        assert Counter(gw.calls)["get_full_user"] == 4
        enriched = st.conn.execute(
            "select count(*) from users where enriched_at is not null"
        ).fetchone()[0]
        assert enriched == 4


def _patch_gateway(monkeypatch, captured: dict) -> None:
    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        captured["settings"] = settings
        return FakeGateway(people_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)


def test_cli_profiles_flags_reach_settings(tmp_path, monkeypatch):
    captured: dict = {}
    _patch_gateway(monkeypatch, captured)
    result = runner.invoke(
        cli_app,
        [
            "collect", "@c", "--profile", "people1", "--unsafe",
            # people to enrich need the sweeps
            "--phases", "channel,history,discussion,participants,profiles",
            "--profiles", "--profile-budget", "3",
            "--profile-interval", "2.5", "--profile-refresh-after", "7d",
        ],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    s = captured["settings"]
    assert (
        s.enrich_profiles, s.profile_budget, s.profile_interval,
        s.profile_refresh_after, s.unsafe,
    ) == (True, 3, 2.5, 7 * 86400, True)
    assert "--profiles" in result.stdout  # the console names the expensive sweep it is about to run
    db = sqlite3.connect(tmp_path / "people1" / "paperboy.sqlite")
    assert db.execute("select count(*) from users where enriched_at is not null").fetchone()[0] == 3
    db.close()


def test_cli_rejects_a_bad_refresh_duration(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, {})
    result = runner.invoke(
        cli_app,
        ["collect", "@c", "--profile", "people2", "--unsafe", "--profile-refresh-after", "7x"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0 and "7x" in result.output


def test_cli_participants_alone_is_rejected_and_with_channel_works(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, {})
    alone = runner.invoke(
        cli_app,
        ["collect", "@c", "--profile", "people3", "--phases", "participants", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert alone.exit_code == 1 and "channel" in alone.stdout.lower()
    ok = runner.invoke(
        cli_app,
        ["collect", "@c", "--profile", "people4", "--phases", "channel,participants", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert ok.exit_code == 0, ok.stdout
    db = sqlite3.connect(tmp_path / "people4" / "paperboy.sqlite")
    n = db.execute(
        "select count(*) from participants where group_id=?", (GROUP_ID,)
    ).fetchone()[0]
    assert n >= 2
    db.close()
    status = runner.invoke(
        cli_app, ["status", "--profile", "people4"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert status.exit_code == 0
    assert "participants" in status.stdout and "users" in status.stdout
