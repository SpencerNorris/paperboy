import re

import pytest
from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app
from paperboy.doctor import Check
from tests.fakes import FakeGateway

runner = CliRunner()


def _fixtures():
    return {
        "resolve": {
            "peer": {"_": "PeerChannel", "channel_id": 5},
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
    for cmd in ("auth", "doctor", "collect", "status", "export", "watch", "lookup", "fetch-media"):
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


def test_collect_media_alone_rejected_with_clear_message(tmp_path, monkeypatch):
    # Same rule as `history`: `media` also depends on `channel_id`/
    # `input_channel`, so `--phases media` without `channel` must fail fast.
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_media_only", "--phases", "media", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "channel" in result.stdout
    assert "AssertionError" not in result.stdout


def _join_fixtures():
    fx = _fixtures()
    fx["full_channel"]["full_chat"]["linked_chat_id"] = 777
    fx["full_channel"]["chats"].append(
        {"_": "channel", "id": 777, "access_hash": 4242, "title": "X Chat",
         "megagroup": True, "join_to_send": True}
    )
    fx["get_messages"] = {}
    fx["join"] = {"_": "updates", "updates": []}
    return fx


def test_collect_join_flag_joins_a_gated_group_and_records_it(tmp_path, monkeypatch):
    # End-to-end wiring: --join -> allow_join setting -> the discussion collector
    # joins the join_to_send group and records the active act in run_events.
    import sqlite3

    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        assert settings.allow_join is True, "--join must set allow_join"
        return FakeGateway(_join_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_join", "--phases", "channel,discussion",
         "--join", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    assert "active" in result.stdout.lower()  # the console warns it is an active act
    db = sqlite3.connect(tmp_path / "clitest_join" / "paperboy.sqlite")
    joins = db.execute("SELECT detail_json FROM run_events WHERE kind='join'").fetchall()
    db.close()
    assert len(joins) == 1 and "777" in joins[0][0]


def test_collect_without_join_flag_leaves_a_gated_group_unjoined(tmp_path, monkeypatch):
    import sqlite3

    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        assert settings.allow_join is False
        return FakeGateway(_join_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_nojoin", "--phases", "channel,discussion",
         "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    db = sqlite3.connect(tmp_path / "clitest_nojoin" / "paperboy.sqlite")
    joins = db.execute("SELECT 1 FROM run_events WHERE kind='join'").fetchall()
    db.close()
    assert joins == []  # never an implicit join


def test_collect_media_flag_downloads_and_stays_off_without_it(tmp_path, monkeypatch):
    fx = _fixtures()
    fx["history"] = [
        {
            "_": "message", "id": 1, "message": "", "date": 1767322445,
            "media": {
                "_": "MessageMediaDocument",
                "document": {
                    "_": "Document", "id": 1, "access_hash": 1, "mime_type": "text/plain",
                    "attributes": [{"_": "DocumentAttributeFilename", "file_name": "a.txt"}],
                },
            },
        }
    ]
    fx["media"] = {1: b"hello"}

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(fx)

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_nomedia", "--phases", "channel,history",
         "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    assert "media" not in result.stdout

    result2 = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_withmedia", "--media", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result2.exit_code == 0, result2.stdout
    assert "media" in result2.stdout
    downloaded_path = tmp_path / "clitest_withmedia" / "media"
    assert downloaded_path.exists()
    assert any(downloaded_path.rglob("*.txt"))


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


def test_doctor_missing_credentials_exits_cleanly(tmp_path, monkeypatch):
    # `build_gateway` -> `build_client` -> `resolve_api_id` raises `ConfigError`
    # when no api_id/api_hash is configured for the profile (found live during
    # VALIDATE-phase smoke testing: this previously reached `cli.py` as an
    # uncaught exception and printed a raw traceback instead of `doctor`'s own
    # actionable message).
    async def raising_build_gateway(settings, secrets, profile, store):
        del settings, secrets, store
        raise composition.ConfigError(f"No api_id configured for profile {profile!r}")

    monkeypatch.setattr(composition, "build_gateway", raising_build_gateway)

    result = runner.invoke(
        app, ["doctor", "--profile", "clitest_noconfig"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 1
    assert "No api_id configured" in result.stdout
    assert "Traceback" not in result.stdout


def test_collect_missing_credentials_exits_cleanly(tmp_path, monkeypatch):
    async def raising_build_gateway(settings, secrets, profile, store):
        del settings, secrets, store
        raise composition.ConfigError(f"No api_id configured for profile {profile!r}")

    monkeypatch.setattr(composition, "build_gateway", raising_build_gateway)

    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_noconfig2", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "No api_id configured" in result.stdout
    assert "Traceback" not in result.stdout


def test_auth_missing_credentials_exits_cleanly(tmp_path, monkeypatch):
    def raising_build_client(settings, secrets, profile):
        del settings, secrets
        raise composition.ConfigError(f"No api_id configured for profile {profile!r}")

    monkeypatch.setattr(composition, "build_client", raising_build_client)

    result = runner.invoke(
        app, ["auth", "--profile", "clitest_noconfig3"], env={"PAPERBOY_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 1
    assert "No api_id configured" in result.stdout
    assert "Traceback" not in result.stdout


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


def test_collect_media_since_skips_older_media(tmp_path, monkeypatch):
    fx = _fixtures()
    fx["history"] = [
        {
            "_": "message", "id": 1, "message": "", "date": 1767322445,  # 2026-01-02
            "media": {
                "_": "MessageMediaDocument",
                "document": {
                    "_": "Document", "id": 1, "access_hash": 1, "mime_type": "text/plain",
                    "attributes": [{"_": "DocumentAttributeFilename", "file_name": "a.txt"}],
                },
            },
        }
    ]
    fx["media"] = {1: b"hello"}

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(fx)

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_since", "--media",
         "--media-since", "2026-03-22", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    assert "out_of_window" in result.stdout
    assert not any((tmp_path / "clitest_since" / "media").rglob("*.txt"))


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain_output(result) -> str:
    """CLI output with ANSI styling stripped. CI runners force colour and
    Typer's error panel otherwise wraps/truncates long option names."""
    return _ANSI.sub("", result.output)


# Wide, colourless terminal for assertions on Typer's error panel text.
_WIDE_ENV = {"COLUMNS": "200", "TERMINAL_WIDTH": "200", "NO_COLOR": "1"}


def test_collect_media_since_rejects_bad_value(tmp_path):
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_badsince", "--media",
         "--media-since", "whenever", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), **_WIDE_ENV},
    )
    assert result.exit_code != 0
    assert "media-since" in _plain_output(result)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--media-msgs", "abc"), ("--media-max-mb", "0"), ("--media-min-free-gb", "-1")],
)
def test_collect_media_selectors_reject_bad_values(tmp_path, flag, value):
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_badsel", "--media", flag, value, "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), **_WIDE_ENV},
    )
    assert result.exit_code != 0
    assert flag.lstrip("-") in _plain_output(result)


def test_collect_media_min_free_gb_reaches_settings(tmp_path, monkeypatch):
    seen = {}

    async def fake_build_gateway(settings, secrets, profile, store):
        del secrets, profile, store
        seen["settings"] = settings
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_floor", "--media",
         "--media-min-free-gb", "0.5", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    assert seen["settings"].media_min_free_gb == 0.5


def test_collect_exits_nonzero_when_the_target_itself_cannot_be_used(tmp_path, monkeypatch):
    # Issue #56: a deleted/renamed handle (or a private channel) skips the
    # `channel` phase. Nothing is collected, so `collect` must say so and exit
    # non-zero — scripts and queues rely on the exit code — without a traceback.
    from paperboy.budget import SkipAndRecord

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        gw = FakeGateway(_fixtures())

        async def resolve(target_value: str) -> dict:
            raise SkipAndRecord("The username is not in use by anyone else yet")

        gw.resolve = resolve  # type: ignore[method-assign]
        return gw

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    result = runner.invoke(
        app,
        ["collect", "@gone_channel", "--profile", "clitest_gone", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), **_WIDE_ENV},
    )
    out = _plain_output(result)
    assert result.exit_code == 1, out
    assert "nothing was collected" in out
    assert "Traceback" not in out


def test_collect_exits_zero_when_the_channel_phase_succeeds(tmp_path, monkeypatch):
    # Guard for the non-zero rule above: it keys on the `channel` phase only,
    # so a normal run over a usable target still exits 0.
    fx = _fixtures()

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(fx)

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    result = runner.invoke(
        app,
        ["collect", "@x", "--profile", "clitest_ok", "--phases", "channel,history", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), **_WIDE_ENV},
    )
    assert result.exit_code == 0, _plain_output(result)


def test_collect_reports_other_phases_when_the_channel_phase_is_skipped(tmp_path, monkeypatch):
    # Review finding on #56: `web` works from the handle alone, so it can still
    # archive a deleted channel's t.me/s and Wayback pages after `channel` was
    # skipped. The exit code stays 1 (the target itself was unusable), but the
    # message must not claim "nothing was collected" — it names what was.
    import paperboy.cli as cli_mod
    from paperboy.collectors.base import CollectResult

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    async def fake_collect_channel(*args, **kwargs):
        del args, kwargs
        return [
            CollectResult(name="channel", counts={}, stopped="skip"),
            CollectResult(name="participants", counts={"enumerated": 0}),
            CollectResult(name="web", counts={"tme_posts": 3, "wayback_rows": 12}),
        ]

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    monkeypatch.setattr(cli_mod, "collect_channel", fake_collect_channel)
    result = runner.invoke(
        app,
        ["collect", "@gone_channel", "--profile", "clitest_gone_web", "--web", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path), **_WIDE_ENV},
    )
    out = _plain_output(result)
    assert result.exit_code == 1, out
    assert "nothing was collected" not in out
    assert "web" in out and "tme_posts" in out
    # An all-zero phase saved nothing and is not listed as having collected.
    assert "participants {" not in out
    assert "Traceback" not in out


def test_status_and_export_accept_channel_id_forms(tmp_path, monkeypatch):
    # #84: after a collect, a channel is addressable offline by its id in any
    # of the accepted forms, not only by username.
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    env = {"PAPERBOY_DATA_DIR": str(tmp_path)}
    collected = runner.invoke(
        app, ["collect", "@x", "--profile", "cliid", "--phases", "channel", "--unsafe"], env=env
    )
    assert collected.exit_code == 0, collected.stdout
    # The marked form starts with `-`, so the shell needs `--` before it.
    for args in (["5"], ["--", "-1005"], ["t.me/c/5/9"]):
        status = runner.invoke(app, ["status", "--profile", "cliid", *args], env=env)
        assert status.exit_code == 0, (args, status.stdout)
    out = tmp_path / "export-out"
    exported = runner.invoke(
        app, ["export", "5", "--profile", "cliid", "--out", str(out)], env=env
    )
    assert exported.exit_code == 0, exported.stdout
    unknown = runner.invoke(app, ["status", "6", "--profile", "cliid"], env=env)
    assert unknown.exit_code == 1
    assert "No local data" in unknown.stdout


@pytest.mark.parametrize("cmd", ["status", "collect", "export"])
def test_negative_non_channel_id_is_rejected_without_a_traceback(tmp_path, cmd):
    result = runner.invoke(
        app,
        [cmd, "--profile", "clitest_badid", "--", "-123"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "basic group or user id" in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("cmd", ["status", "collect", "export"])
@pytest.mark.parametrize("target", ["99999999999999999999", "-10099999999999999999999"])
def test_out_of_range_id_is_rejected_without_a_traceback(tmp_path, cmd, target):
    result = runner.invoke(
        app,
        [cmd, "--profile", "clitest_bigid", "--", target],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "64-bit" in result.stdout
    assert "Traceback" not in result.stdout


def test_collect_media_store_flag_and_bad_bucket_exit_1(tmp_path, monkeypatch):
    from tests.fake_gcs import FakeGcsClient

    async def fake_build_gateway(settings, secrets, profile, store):
        raise AssertionError("a config error must stop before any gateway is built")

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    env = {"PAPERBOY_DATA_DIR": str(tmp_path)}
    bad = runner.invoke(
        app,
        ["collect", "@x", "--profile", "c", "--media-store", "gs://not-allowed/p", "--unsafe"],
        env=env,
    )
    assert bad.exit_code == 1
    assert "media_store_buckets" in bad.stdout and "Traceback" not in bad.stdout

    async def ok_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures())

    monkeypatch.setattr(composition, "build_gateway", ok_gateway)
    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    good = runner.invoke(
        app,
        [
            "collect", "@x", "--profile", "c", "--phases", "channel,history",
            "--media-store", "gs://bkt/p/x", "--unsafe",
        ],
        env={**env, "PAPERBOY_MEDIA_STORE_BUCKETS": "bkt"},
    )
    assert good.exit_code == 0, good.stdout
