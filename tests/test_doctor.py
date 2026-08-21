from datetime import UTC, datetime, timedelta

import pytest

from paperboy.config import load_settings
from paperboy.doctor import doctor_blocks, run_doctor
from tests.fakes import FakeGateway


def _fixtures(
    *, proxy_ok=True, session_age_days=30, has_password=True, restrictive=True, minimal=True
):
    now = datetime.now(UTC)
    created = now - timedelta(days=session_age_days)
    self_user = {"_": "user", "id": 1, "self": True}
    if not minimal:
        self_user["username"] = "notminimal"
        self_user["photo"] = {"_": "userProfilePhoto"}
        self_user["about"] = "hello, this is my bio"

    rule = {"_": "privacyValueDisallowAll"} if restrictive else {"_": "privacyValueAllowAll"}

    return {
        "self": self_user,
        "authorizations": {
            "authorizations": [
                {"_": "authorization", "current": True, "date_created": created, "date_active": now}
            ]
        },
        "password_state": {"_": "account.password", "has_password": has_password},
        "privacy": {
            "phone": {"rules": [rule]},
            "lastseen": {"rules": [rule]},
            "photo": {"rules": [rule]},
        },
    }, proxy_ok


def _settings(proxy_ok, **overrides):
    overrides.setdefault("require_proxy", True)
    if proxy_ok:
        overrides.setdefault("proxy", "socks5://127.0.0.1:9050")
    return load_settings("default", overrides)


@pytest.mark.asyncio
async def test_compliant_account_passes_everything():
    fx, proxy_ok = _fixtures()
    gw = FakeGateway(fx)
    settings = _settings(proxy_ok)
    checks = await run_doctor(gw, settings)
    assert checks
    assert not doctor_blocks(checks)
    assert all(c.ok for c in checks)


@pytest.mark.asyncio
async def test_no_proxy_and_young_session_both_fail():
    fx, _ = _fixtures(session_age_days=1)
    gw = FakeGateway(fx)
    settings = _settings(proxy_ok=False)
    checks = await run_doctor(gw, settings)
    by_name = {c.name: c for c in checks}
    assert by_name["proxy"].ok is False
    assert by_name["proxy"].severity == "fail"
    assert by_name["session_age"].ok is False
    assert doctor_blocks(checks) is True


@pytest.mark.asyncio
async def test_no_2fa_fails():
    fx, proxy_ok = _fixtures(has_password=False)
    gw = FakeGateway(fx)
    checks = await run_doctor(gw, _settings(proxy_ok))
    by_name = {c.name: c for c in checks}
    assert by_name["two_factor_auth"].ok is False
    assert doctor_blocks(checks) is True


@pytest.mark.asyncio
async def test_permissive_privacy_fails():
    fx, proxy_ok = _fixtures(restrictive=False)
    gw = FakeGateway(fx)
    checks = await run_doctor(gw, _settings(proxy_ok))
    privacy_checks = [c for c in checks if c.name.startswith("privacy_")]
    assert len(privacy_checks) == 3
    assert all(not c.ok for c in privacy_checks)
    assert doctor_blocks(checks) is True


@pytest.mark.asyncio
async def test_non_minimal_profile_warns_but_does_not_block():
    fx, proxy_ok = _fixtures(minimal=False)
    gw = FakeGateway(fx)
    checks = await run_doctor(gw, _settings(proxy_ok))
    by_name = {c.name: c for c in checks}
    assert by_name["minimal_profile"].ok is False
    assert by_name["minimal_profile"].severity == "warn"
    assert doctor_blocks(checks) is False


def test_doctor_blocks_is_false_for_no_checks():
    assert doctor_blocks([]) is False
