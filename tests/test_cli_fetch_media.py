"""`paperboy fetch-media LIST` (#68). Synthetic channels/messages only."""

import csv

from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app
from paperboy.doctor import Check
from paperboy.store.db import Store
from tests.test_fetch_media import BYTES, LIST, _gateway, _seed_store

runner = CliRunner()


def _prepare(tmp_path, list_text=LIST):
    with Store.open(tmp_path / "p" / "paperboy.sqlite") as st:
        _seed_store(st)
    path = tmp_path / "list.csv"
    path.write_text(list_text, encoding="utf-8")
    return path


def _env(tmp_path):
    return {"PAPERBOY_DATA_DIR": str(tmp_path)}


def _forbid_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("dry run must not build secrets or a gateway")

    monkeypatch.setattr(composition, "build_secrets", boom)
    monkeypatch.setattr(composition, "build_gateway", boom)


def test_dry_run_builds_no_gateway_and_no_secrets(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    _forbid_network(monkeypatch)
    result = runner.invoke(
        app, ["fetch-media", str(path), "--profile", "p", "--dry-run"], env=_env(tmp_path)
    )
    assert result.exit_code == 0, result.stdout
    assert "pending" in result.stdout and "priority" in result.stdout.lower()
    # 4 segments over 2 channels; ids only, never usernames.
    assert "chan_a" not in result.stdout
    assert not list((tmp_path / "p").glob("fetch-media-*.csv"))


def test_malformed_list_exits_1_listing_lines(tmp_path, monkeypatch):
    _forbid_network(monkeypatch)
    path = tmp_path / "bad.csv"
    path.write_text("uri\ntg:msg:1/1\nnope\ntg:msg:1/x\n", encoding="utf-8")
    result = runner.invoke(
        app, ["fetch-media", str(path), "--profile", "p", "--dry-run"], env=_env(tmp_path)
    )
    assert result.exit_code == 1
    assert "3" in result.stdout and "4" in result.stdout
    assert not (tmp_path / "p").exists()  # nothing touched before the list parsed


def test_live_run_exit_codes_and_default_report_path(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    gw = _gateway(BYTES)

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return gw

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--unsafe", "--media-min-free-gb", "0"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.stdout
    reports = list((tmp_path / "p").glob("fetch-media-*.csv"))
    assert len(reports) == 1
    with reports[0].open(newline="") as fh:
        assert [r["outcome"] for r in csv.DictReader(fh)] == ["downloaded"] * 5

    # Second run: everything is already stored -> exit 0, no gateway built.
    _forbid_network(monkeypatch)
    again = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--unsafe", "--report",
         str(tmp_path / "again.csv")],
        env=_env(tmp_path),
    )
    assert again.exit_code == 0, again.stdout
    assert "already_stored" in again.stdout


def test_stopped_run_exits_1_and_still_writes_the_report(tmp_path, monkeypatch):
    from paperboy.budget import HardStop

    path = _prepare(tmp_path)
    gw = _gateway({**BYTES, 11: HardStop("boom")})

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return gw

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    report = tmp_path / "r.csv"
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--unsafe", "--media-min-free-gb", "0",
         "--report", str(report)],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    with report.open(newline="") as fh:
        outcomes = [r["outcome"] for r in csv.DictReader(fh)]
    assert outcomes[0] == "downloaded" and outcomes.count("not_attempted") == 4


def test_doctor_block_exits_1_before_any_segment(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    gw = _gateway(BYTES)

    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return gw

    async def fake_run_doctor(gateway, settings):
        del gateway, settings
        return [Check("proxy", False, "no proxy configured", "fail")]

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)
    monkeypatch.setattr("paperboy.cli.run_doctor", fake_run_doctor)
    report = tmp_path / "r.csv"
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--report", str(report)],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert "doctor preflight failed" in result.stdout
    assert gw.download_media_calls == []
    with report.open(newline="") as fh:
        assert {r["outcome"] for r in csv.DictReader(fh)} == {"not_attempted"}


def test_unwritable_report_fails_before_any_segment(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    _forbid_network(monkeypatch)
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--unsafe", "--report",
         str(tmp_path / "no" / "such" / "dir" / "r.csv")],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert "report" in result.stdout.lower()


def test_gateway_build_failure_still_writes_the_report(tmp_path, monkeypatch):
    path = _prepare(tmp_path)

    async def failing_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        raise composition.ConfigError("no credentials")

    monkeypatch.setattr(composition, "build_gateway", failing_build_gateway)
    report = tmp_path / "r.csv"
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--report", str(report)],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    with report.open(newline="") as fh:
        assert {r["outcome"] for r in csv.DictReader(fh)} == {"not_attempted"}


def test_dry_run_prints_per_channel_counts_and_excludes(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    _forbid_network(monkeypatch)
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--dry-run", "--exclude-target", "10"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "per channel" in out.lower()
    # Channel 10 has 3 of the 5 rows, all excluded; channel 20's 2 are pending.
    per_channel = out[out.lower().index("per channel"):]
    header = next(ln for ln in per_channel.splitlines() if "excluded" in ln and "pending" in ln)
    cols = [c.strip() for c in header.strip("┃│ ").replace("┃", "│").split("│")]
    rows = {}
    for ln in per_channel.splitlines():
        cells = [c.strip() for c in ln.strip().strip("│").split("│")]
        if len(cells) == len(cols) and cells[0] in ("10", "20"):
            rows[cells[0]] = dict(zip(cols, cells, strict=True))
    assert rows["10"]["excluded"] == "3" and rows["10"]["pending"] == "0"
    assert rows["20"]["pending"] == "2" and rows["20"]["excluded"] == "0"
    assert "chan_a" not in out
    assert "unresolvable" not in out


def test_unknown_exclude_target_exits_1_before_anything_else(tmp_path, monkeypatch):
    path = _prepare(tmp_path)
    _forbid_network(monkeypatch)
    result = runner.invoke(
        app,
        ["fetch-media", str(path), "--profile", "p", "--dry-run", "--exclude-target", "999"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert "999" in result.stdout
