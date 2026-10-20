"""`paperboy doctor`: the operational-security preflight (spec §3).

Checks the account's controllable opsec posture — proxy presence, session
age, 2FA, restrictive privacy keys, a minimal profile — before `collect` is
allowed to run. A `fail` blocks `collect` unless the operator passes
`--unsafe`; a `warn` (currently only the minimal-profile check) never blocks
— a non-minimal profile is a hygiene issue for the *account*, not a risk to
the current run the way a missing proxy or a fresh session is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from paperboy import media_store as _media_store
from paperboy.config import parse_media_store_url

if TYPE_CHECKING:
    from paperboy.config import Settings
    from paperboy.gateway import Gateway

Severity = Literal["fail", "warn"]

_PRIVACY_KEYS = ("phone", "lastseen", "photo")
# A privacy rule of AllowAll is the permissive default; anything else (and no
# rules at all is *not* actually possible from a real getPrivacy response,
# but an empty list is treated the same as AllowAll — Telegram's own default)
# counts as restrictive.
# Telethon's to_dict() uses the PascalCase class name, not the lowercase TL
# constructor name — compared case-insensitively below.
_PERMISSIVE_RULE = "privacyvalueallowall"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: Severity


def session_age_days(authorizations: dict) -> float | None:
    for auth in authorizations.get("authorizations", []):
        if not auth.get("current"):
            continue
        created = auth.get("date_created")
        if created is None:
            return None
        if not isinstance(created, datetime):
            raise TypeError(
                f"expected an aware datetime for date_created, got {type(created).__name__}"
            )
        return (datetime.now(UTC) - created).total_seconds() / 86400
    return None



def _check_proxy(settings: Settings) -> Check:
    if not settings.require_proxy:
        return Check("proxy", True, "require_proxy is disabled", "fail")
    if settings.proxy:
        return Check("proxy", True, f"proxy configured ({settings.proxy})", "fail")
    return Check("proxy", False, "require_proxy is set but no proxy is configured", "fail")


def _check_session_age(authorizations: dict, settings: Settings) -> Check:
    age = session_age_days(authorizations)
    if age is None:
        return Check("session_age", False, "no current session found", "fail")
    if age < settings.min_session_age_days:
        return Check(
            "session_age", False,
            f"session is {age:.1f} days old, below min_session_age_days="
            f"{settings.min_session_age_days}",
            "fail",
        )
    return Check("session_age", True, f"session is {age:.1f} days old", "fail")


def _check_two_factor(password_state: dict) -> Check:
    if password_state.get("has_password"):
        return Check("two_factor_auth", True, "2FA password is set", "fail")
    return Check("two_factor_auth", False, "no 2FA password set", "fail")


def _check_privacy(key: str, rules: dict) -> Check:
    rule_list = rules.get("rules", [])
    permissive = not rule_list or rule_list[0].get("_", "").lower() == _PERMISSIVE_RULE
    if permissive:
        return Check(f"privacy_{key}", False, f"{key} privacy is Everyone (AllowAll)", "fail")
    return Check(f"privacy_{key}", True, f"{key} privacy is restricted", "fail")


def _check_minimal_profile(self_user: dict) -> Check:
    exposed = [
        field for field in ("username", "photo", "about") if self_user.get(field)
    ]
    if exposed:
        return Check(
            "minimal_profile", False,
            f"self profile exposes: {', '.join(exposed)}",
            "warn",
        )
    return Check("minimal_profile", True, "self profile is minimal", "warn")


_STORE_PERMISSIONS = (
    "storage.objects.create", "storage.objects.get", "storage.objects.delete",
)


def media_store_checks(settings: Settings, client_factory: Callable[[], Any]) -> list[Check]:
    """Preflight for a GCS media store (#63, ADR-0008): credentials, the
    permissions the run needs, a least-privilege warning, and the bucket's
    retention. Nothing here writes to the bucket or is recorded in raw.

    `delete` being granted only warns (the operator's Mac is a project owner):
    paperboy never deletes whether or not it is allowed to.
    """
    assert settings.media_store is not None
    bucket_name, _ = parse_media_store_url(settings.media_store)
    try:
        bucket = client_factory().bucket(bucket_name)
    except Exception as exc:  # noqa: BLE001 - any credential failure blocks; class name only
        return [Check(
            "media_store_credentials", False,
            f"Application Default Credentials unavailable ({type(exc).__name__}); "
            "run `gcloud auth application-default login`",
            "fail",
        )]
    checks = [
        Check("media_store_credentials", True, "Application Default Credentials found", "fail")
    ]
    try:
        granted = set(bucket.test_iam_permissions(list(_STORE_PERMISSIONS)))
    except Exception as exc:  # noqa: BLE001 - network/auth failure; class name only
        checks.append(Check(
            "media_store_permissions", False,
            f"cannot check bucket permissions ({type(exc).__name__})", "fail",
        ))
        return checks
    missing = [p for p in _STORE_PERMISSIONS[:2] if p not in granted]
    if missing:
        checks.append(Check(
            "media_store_permissions", False, f"missing {', '.join(missing)}", "fail"
        ))
    else:
        checks.append(Check(
            "media_store_permissions", True, "storage.objects.create and get granted", "fail"
        ))
    if "storage.objects.delete" in granted:
        checks.append(Check(
            "media_store_least_privilege", False,
            "storage.objects.delete is granted; paperboy never deletes, but a "
            "create-only role (roles/storage.objectCreator) is safer",
            "warn",
        ))
    else:
        checks.append(Check("media_store_least_privilege", True, "no delete permission", "warn"))
    try:
        bucket.reload()
    except Exception as exc:  # noqa: BLE001 - typically 403 on storage.buckets.get
        checks.append(Check(
            "media_store_retention", False,
            f"not readable (needs storage.buckets.get; {type(exc).__name__})", "warn",
        ))
    else:
        period = bucket.retention_period
        retention = (
            f"retention {period} s ({period / 86400:.0f} days)" if period else "no retention"
        )
        versioning = "on" if bucket.versioning_enabled else "off"
        checks.append(Check(
            "media_store_retention", True, f"{retention}, versioning {versioning}", "warn"
        ))
    return checks


async def run_doctor(
    gateway: Gateway,
    settings: Settings,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> list[Check]:
    self_user = await gateway.get_self()
    authorizations = await gateway.get_authorizations()
    password_state = await gateway.get_password_state()

    checks = [
        _check_proxy(settings),
        _check_session_age(authorizations, settings),
        _check_two_factor(password_state),
    ]
    for key in _PRIVACY_KEYS:
        rules = await gateway.get_privacy(key)
        checks.append(_check_privacy(key, rules))
    checks.append(_check_minimal_profile(self_user))
    if settings.media_store is not None:
        factory = client_factory or (lambda: _media_store.default_client_factory())
        checks.extend(media_store_checks(settings, factory))
    return checks


def doctor_blocks(checks: list[Check]) -> bool:
    """True if any `fail`-severity check did not pass. `warn`s never block."""
    return any(not c.ok and c.severity == "fail" for c in checks)
