from pathlib import Path

from paperboy.config import Settings, load_settings, profile_dir


def test_env_override(monkeypatch):
    monkeypatch.setenv("PAPERBOY_MAX_RPC_PER_RUN", "5")
    s = load_settings("default", {})
    assert s.max_rpc_per_run == 5
    assert s.require_proxy is True


def test_cli_override_beats_env(monkeypatch):
    monkeypatch.setenv("PAPERBOY_PROFILE_BUDGET", "10")
    s = load_settings("default", {"profile_budget": 3})
    assert s.profile_budget == 3


def test_defaults_match_spec(monkeypatch):
    for var in (
        "PAPERBOY_MAX_RPC_PER_RUN",
        "PAPERBOY_PROFILE_BUDGET",
        "PAPERBOY_MIN_SESSION_AGE_DAYS",
        "PAPERBOY_FLOOD_SLEEP_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)
    s = load_settings("default", {})
    assert s.min_session_age_days == 7
    assert s.flood_sleep_threshold == 60
    assert s.max_rpc_per_run == 20000
    assert s.profile_budget == 2000
    assert s.allow_join is False
    assert s.allow_phone_lookup is False
    assert s.api_id is None
    assert s.proxy is None


def test_data_dir_expands_user():
    s = load_settings("default", {"data_dir": "~/somewhere"})
    assert s.data_dir == Path.home() / "somewhere"


def test_device_identity_is_generic_not_official_client():
    s = Settings()
    d = s.device.device_model + s.device.system_version + s.device.app_version
    for banned in ("Telegram Desktop", "TelegramAndroid", "iOS"):
        assert banned not in d


def test_profile_dir_scopes_data_dir(tmp_path):
    s = load_settings("default", {"data_dir": str(tmp_path)})
    assert profile_dir(s, "investigation-1") == tmp_path / "investigation-1"


def test_two_profiles_dont_share_a_dir(tmp_path):
    s = load_settings("default", {"data_dir": str(tmp_path)})
    assert profile_dir(s, "a") != profile_dir(s, "b")
