"""Runtime configuration.

`Settings` is a `pydantic-settings` model, `PAPERBOY_*`-env-prefixed, with the
defaults from spec §9. Precedence is CLI > env > `config.toml` > defaults;
`pydantic-settings` already prioritises constructor keyword arguments over
environment variables (its default source order), so `load_settings` gets the
right precedence for free by passing CLI overrides as kwargs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DURATION_RE = re.compile(r"^(\d+)([dhms]?)$")
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 1}


def parse_duration(text: str) -> int:
    """`7d` / `12h` / `30m` / `45s` / bare seconds → seconds. Used by the
    `--profile-refresh-after` CLI flag (spec §7.2)."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (expected e.g. 7d, 12h, 30m, 45s)")
    value, unit = match.groups()
    return int(value) * _DURATION_UNITS[unit]

def parse_since(text: str, now: datetime) -> datetime:
    """A `--media-since` value → an absolute, aware UTC cutoff (issue #52).

    Accepts a duration relative to `now` (`180d`, `12h`, any `parse_duration`
    form) or an ISO-8601 date / datetime (`2026-03-22`,
    `2026-03-22T06:00:00+02:00`). A naive datetime is read as UTC, never local
    time, so the same flag means the same window on any machine. The result is
    truncated to whole seconds so its `isoformat()` has the exact shape of the
    stored `messages.date` strings, keeping the SQL string comparison sound.
    """
    text = text.strip()
    try:
        cutoff = now - timedelta(seconds=parse_duration(text))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"not a duration or ISO date: {text!r} (expected e.g. 180d or 2026-03-22)"
            ) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        cutoff = parsed
    return cutoff.astimezone(UTC).replace(microsecond=0)


_MSG_ID_ITEM_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_msg_ids(text: str) -> list[int]:
    """A `--media-msgs` value → sorted, de-duplicated message ids (issue #55).

    Comma-separated ids and inclusive ranges: `8554,8600-8602`. Ids are
    positive; a range must run low→high. Anything else is a `ValueError` —
    a typo must fail loudly, never silently select nothing.
    """
    ids: set[int] = set()
    for item in text.split(","):
        match = _MSG_ID_ITEM_RE.match(item.strip())
        if match is None:
            raise ValueError(f"not a message id or range: {item!r} (expected e.g. 8554,8600-8602)")
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        if low < 1 or high < low:
            raise ValueError(f"bad message id range: {item!r}")
        ids.update(range(low, high + 1))
    return sorted(ids)


# Repo-relative by default so collected data lands in `./data/` next to the
# code, not somewhere on the filesystem you have to hunt for. `./data` is
# gitignored. Override with `PAPERBOY_DATA_DIR` (absolute or `~`-relative) to
# put it elsewhere.
DEFAULT_DATA_DIR = Path("data")


class DeviceIdentity(BaseModel):
    """A stable, generic device fingerprint.

    Deliberately does NOT resemble an official Telegram client string (see
    docs/opsec.md: "we do not impersonate an official client") — a consistent,
    generic identity across runs is the goal, not blending in as a specific
    real client.
    """

    device_model: str = "PC"
    system_version: str = "Linux"
    app_version: str = "paperboy"
    lang_code: str = "en"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAPERBOY_", extra="ignore")

    api_id: int | None = None
    data_dir: Path = DEFAULT_DATA_DIR
    proxy: str | None = None
    require_proxy: bool = True
    device: DeviceIdentity = Field(default_factory=DeviceIdentity)
    min_session_age_days: int = 7
    flood_sleep_threshold: int = 60
    max_rpc_per_run: int = 20000
    profile_budget: int = 2000
    discussion_page_budget: int = 500
    # `catch_up` loops getChannelDifference until the server says `final`; this
    # bounds the pages one run will pull (each is up to _CHANNEL_DIFFERENCE_LIMIT
    # updates) so a huge backlog stops politely and resumes next run, rather than
    # holding the connection open indefinitely. The pts-advance guard, not this,
    # is what prevents an infinite loop; this is the politeness/size bound.
    # `ge=1`: the budget is checked only after a page is fetched and applied, so
    # a value <1 would still pull one page — it never means "no catch-up".
    catchup_page_budget: int = Field(default=1000, ge=1)
    allow_join: bool = False
    allow_phone_lookup: bool = False
    # --unsafe: skip the doctor preflight AND the per-phase session-age gate
    # on roster enumeration (spec §6.1). Env `PAPERBOY_UNSAFE` is the same
    # operator override.
    unsafe: bool = False
    # Person layer (spec §7.2). `profile_budget` above bounds getFullUser
    # fetches per run; these parameterize the rest of the enrichment pass.
    enrich_profiles: bool = False
    profile_interval: float | None = Field(default=None, ge=0)
    profile_refresh_after: int | None = Field(default=None, ge=0)  # seconds
    # `--media-since` (issue #52): only download media for messages dated at or
    # after this aware-UTC cutoff. None = no window (every stored message).
    media_since: datetime | None = None
    # `--media-msgs` (issue #55): download media only for these message ids.
    media_msgs: list[int] | None = None
    # `--media-max-mb` (issue #53): skip media whose size, as recorded in the
    # stored message, exceeds this many MB (10^6 bytes). None = no cap.
    media_max_mb: int | None = Field(default=None, ge=1)
    participant_oracle_budget: int = Field(default=100, ge=0)
    participant_reactions_budget: int = Field(default=200, ge=0)

    @field_validator("data_dir", mode="after")
    @classmethod
    def _expand_data_dir(cls, v: Path) -> Path:
        return v.expanduser()


def load_settings(profile: str, overrides: dict) -> Settings:
    """Build `Settings` for one profile.

    `profile` selects *where* data lives (see `profile_dir`) — it is not a
    `Settings` field, so it is not threaded into the model. `overrides`
    (typically parsed CLI flags) are passed as constructor kwargs, which
    `pydantic-settings` prioritises over environment variables.
    """
    del profile
    return Settings(**overrides)


def profile_dir(settings: Settings, profile: str) -> Path:
    """The per-profile data directory: `<data_dir>/<profile>/`."""
    return settings.data_dir / profile
