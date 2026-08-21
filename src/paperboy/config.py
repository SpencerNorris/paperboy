"""Runtime configuration.

`Settings` is a `pydantic-settings` model, `PAPERBOY_*`-env-prefixed, with the
defaults from spec §9. Precedence is CLI > env > `config.toml` > defaults;
`pydantic-settings` already prioritises constructor keyword arguments over
environment variables (its default source order), so `load_settings` gets the
right precedence for free by passing CLI overrides as kwargs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = Path("~/.local/share/paperboy")


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
    allow_join: bool = False
    allow_phone_lookup: bool = False

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
