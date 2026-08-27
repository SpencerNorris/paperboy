"""`paperboy doctor`: the operational-security preflight (spec §3).

Checks the account's controllable opsec posture — proxy presence, session
age, 2FA, restrictive privacy keys, a minimal profile — before `collect` is
allowed to run. A `fail` blocks `collect` unless the operator passes
`--unsafe`; a `warn` (currently only the minimal-profile check) never blocks
— a non-minimal profile is a hygiene issue for the *account*, not a risk to
the current run the way a missing proxy or a fresh session is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

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


# Back-compat alias: this was private (`_session_age_days`) before the
# `participants` collector's per-phase session-age gate (plan D10) needed it
# as public API too. Existing tests still reference the old name.
_session_age_days = session_age_days


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


async def run_doctor(gateway: Gateway, settings: Settings) -> list[Check]:
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
    return checks


def doctor_blocks(checks: list[Check]) -> bool:
    """True if any `fail`-severity check did not pass. `warn`s never block."""
    return any(not c.ok and c.severity == "fail" for c in checks)
