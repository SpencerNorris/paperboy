from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from paperboy.config import (
    Settings,
    load_settings,
    parse_duration,
    parse_msg_ids,
    parse_since,
    profile_dir,
)


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


def test_person_layer_defaults():
    s = load_settings("default", {})
    assert s.unsafe is False
    assert s.enrich_profiles is False
    assert s.profile_interval is None
    assert s.profile_refresh_after is None
    assert s.profile_budget == 2000
    assert s.participant_oracle_budget == 100
    assert s.participant_reactions_budget == 200


def test_parse_duration_units():
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("12h") == 12 * 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("45s") == 45
    assert parse_duration("3600") == 3600
    for bad in ("", "7x", "-1d", "d"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_parse_since_duration_is_relative_to_now():
    now = datetime(2026, 9, 22, 12, 30, 45, 999, tzinfo=UTC)
    # Truncated to whole seconds so the cutoff's ISO form matches stored dates.
    assert parse_since("180d", now) == datetime(2026, 3, 26, 12, 30, 45, tzinfo=UTC)
    assert parse_since("12h", now) == datetime(2026, 9, 22, 0, 30, 45, tzinfo=UTC)


def test_parse_since_absolute_date_and_datetime_are_utc():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    assert parse_since("2026-03-22", now) == datetime(2026, 3, 22, tzinfo=UTC)
    assert parse_since("2026-03-22T06:00:00+02:00", now) == datetime(2026, 3, 22, 4, tzinfo=UTC)
    # A naive datetime is taken as UTC, never local time.
    assert parse_since("2026-03-22T06:00:00", now) == datetime(2026, 3, 22, 6, tzinfo=UTC)


def test_parse_since_rejects_garbage():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    for bad in ("", "soon", "7x", "2026-13-01", "-5d"):
        with pytest.raises(ValueError):
            parse_since(bad, now)


def test_media_since_setting_defaults_to_none():
    assert load_settings("default", {}).media_since is None
    cutoff = datetime(2026, 3, 22, tzinfo=UTC) - timedelta(0)
    assert load_settings("default", {"media_since": cutoff}).media_since == cutoff


def test_parse_msg_ids_lists_and_ranges():
    assert parse_msg_ids("8554") == [8554]
    assert parse_msg_ids("8665, 8554,8600-8602") == [8554, 8600, 8601, 8602, 8665]
    assert parse_msg_ids("5,5,4-5") == [4, 5]


def test_parse_msg_ids_rejects_garbage():
    for bad in ("", "abc", "5-", "9-3", "0", "-4", "1,,2"):
        with pytest.raises(ValueError):
            parse_msg_ids(bad)
