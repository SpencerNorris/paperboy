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


# --- GCS media store preflight (#63) -----------------------------------------


def _store_settings(**over):
    return load_settings(
        "default",
        {"media_store": "gs://bkt/p/x", "media_store_buckets": "bkt", **over},
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_media_store_checks_pass_warn_and_fail():
    from google.api_core.exceptions import Forbidden
    from google.auth.exceptions import DefaultCredentialsError

    from paperboy.doctor import media_store_checks
    from tests.fake_gcs import FakeGcsClient

    client = FakeGcsClient()
    bucket = client.bucket("bkt")
    settings = _store_settings()

    # Least privilege: create+get only -> everything ok, retention is reported.
    checks = _by_name(media_store_checks(settings, lambda: client))
    assert set(checks) == {
        "media_store_credentials", "media_store_permissions",
        "media_store_least_privilege", "media_store_retention",
    }
    assert all(c.ok for c in checks.values())
    assert "8035200" in checks["media_store_retention"].detail
    assert "93" in checks["media_store_retention"].detail  # days
    assert "versioning on" in checks["media_store_retention"].detail

    # delete granted (the operator's Mac): a warning, never a block.
    bucket.granted.add("storage.objects.delete")
    out = media_store_checks(settings, lambda: client)
    assert not _by_name(out)["media_store_least_privilege"].ok
    assert _by_name(out)["media_store_least_privilege"].severity == "warn"
    assert not doctor_blocks(out)

    # create missing -> fail, and it blocks.
    bucket.granted.discard("storage.objects.create")
    out = media_store_checks(settings, lambda: client)
    perms = _by_name(out)["media_store_permissions"]
    assert not perms.ok and perms.severity == "fail" and "storage.objects.create" in perms.detail
    assert doctor_blocks(out)

    # no ADC -> a single failing credentials check, nothing else is attempted.
    def no_adc():
        raise DefaultCredentialsError("no ADC")

    out = media_store_checks(settings, no_adc)
    assert [c.name for c in out] == ["media_store_credentials"]
    assert not out[0].ok and out[0].severity == "fail" and doctor_blocks(out)

    # storage.buckets.get not granted (a VM service account): a warning naming it.
    bucket.granted.add("storage.objects.create")
    bucket.reload_error = Forbidden("no")
    out = media_store_checks(settings, lambda: client)
    retention = _by_name(out)["media_store_retention"]
    assert not retention.ok and retention.severity == "warn"
    assert "storage.buckets.get" in retention.detail
    assert not doctor_blocks(out)


@pytest.mark.asyncio
async def test_run_doctor_adds_store_checks_only_when_a_store_is_configured(monkeypatch):
    from tests.fake_gcs import FakeGcsClient

    fx, proxy_ok = _fixtures()
    plain = await run_doctor(FakeGateway(fx), _settings(proxy_ok))
    assert not any(c.name.startswith("media_store") for c in plain)  # also: test_doctor_without_...

    client = FakeGcsClient()
    monkeypatch.setattr("paperboy.media_store.default_client_factory", lambda: client)
    with_store = await run_doctor(
        FakeGateway(fx), _settings(proxy_ok, media_store="gs://bkt/p", media_store_buckets="bkt")
    )
    assert len(with_store) == len(plain) + 4


@pytest.mark.asyncio
async def test_doctor_without_media_store_builds_no_gcs_client(monkeypatch):
    def boom():
        raise AssertionError("no store configured: no GCS client")

    monkeypatch.setattr("paperboy.media_store.default_client_factory", boom)
    fx, proxy_ok = _fixtures()
    await run_doctor(FakeGateway(fx), _settings(proxy_ok))
