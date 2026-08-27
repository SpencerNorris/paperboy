"""Runtime configuration.

`Settings` is a `pydantic-settings` model, `PAPERBOY_*`-env-prefixed, with the
defaults from spec §9. Precedence is CLI > env > `config.toml` > defaults;
`pydantic-settings` already prioritises constructor keyword arguments over
environment variables (its default source order), so `load_settings` gets the
right precedence for free by passing CLI overrides as kwargs.
"""

from __future__ import annotations

import re
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
