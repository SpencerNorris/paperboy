from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app
from paperboy.doctor import Check
from tests.fakes import FakeGateway

runner = CliRunner()


def _fixtures():
    return {
        "resolve": {
            "chats": [
                {
                    "_": "channel", "id": 5, "access_hash": 99, "title": "X", "username": "x",
                    "broadcast": True,
                }
            ],
            "users": [],
        },
        "full_channel": {
            "full_chat": {"_": "channelFull", "id": 5, "participants_count": 1, "pts": 1},
            "chats": [
                {"_": "channel", "id": 5, "access_hash": 99, "title": "X", "username": "x"}
            ],
            "users": [],
        },
        "self": {"_": "user", "id": 1, "self": True},
        "history": [],
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
    }


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("auth", "doctor", "collect", "status", "export", "watch", "lookup"):
        assert cmd in result.stdout


def test_collect_writes_sqlite_and_exits_zero(tmp_path, monkeypatch):
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest", "--phases", "channel,history", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    db_path = tmp_path / "clitest" / "paperboy.sqlite"
    assert db_path.exists()


def test_collect_unsupported_target_exits_nonzero(tmp_path, monkeypatch):
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "", "--profile", "clitest2"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0


def test_collect_history_alone_rejected_with_clear_message(tmp_path, monkeypatch):
    # `history` depends on `channel_id`/`input_channel`, which only the
    # `channel` collector populates *within one run* (access_hash isn't
    # persisted between runs — see cli.py). Selecting `--phases history`
    # alone must fail fast with an actionable message, not the raw
    # `AssertionError` `HistoryCollector.collect` would otherwise raise.
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_history_only", "--phases", "history", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "channel" in result.stdout
    assert "AssertionError" not in result.stdout


def test_doctor_noncompliant_exits_nonzero(tmp_path, monkeypatch):
    async def fake_run_doctor(gateway, settings):
        del gateway, settings
        return [Check("proxy", False, "no proxy configured", "fail")]

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    monkeypatch.setattr("paperboy.cli.run_doctor", fake_run_doctor)

    result = runner.invoke(
        app, ["doctor", "--profile", "clitest3"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code != 0
    assert "BLOCKED" in result.stdout


def test_doctor_compliant_exits_zero(tmp_path, monkeypatch):
    async def fake_run_doctor(gateway, settings):
        del gateway, settings
        return [Check("proxy", True, "proxy configured", "fail")]

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    monkeypatch.setattr("paperboy.cli.run_doctor", fake_run_doctor)

    result = runner.invoke(
        app, ["doctor", "--profile", "clitest4"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 0
    assert "PASS" in result.stdout


def test_watch_and_lookup_are_phase_2_stubs():
    result = runner.invoke(app, ["watch", "@x"])
    assert result.exit_code != 0
    assert "Phase 2" in result.stdout

    result = runner.invoke(app, ["lookup", "phone", "+15551234567"])
    assert result.exit_code != 0
    assert "Phase 2" in result.stdout


def test_status_on_empty_profile(tmp_path):
    result = runner.invoke(
        app, ["status", "--profile", "clitest5"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 0
    assert "channels" in result.stdout


def test_export_without_prior_collect_exits_nonzero(tmp_path):
    result = runner.invoke(
        app,
        ["export", "@nosuchchannel", "--profile", "clitest6"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
