"""Composition root: wires `Settings` + `SecretStore` into a live client,
`Gateway`, `Store`, and `Budget`. `cli.py` is a thin Typer layer over this —
every real object gets built here so tests can monkeypatch one seam
(`build_gateway`) instead of reaching into Telethon.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import keyring

from paperboy.budget import Budget
from paperboy.config import profile_dir
from paperboy.gateway import TelethonGateway
from paperboy.logging_setup import register_secret
from paperboy.secrets import SERVICE, KeyringSecrets
from paperboy.store.db import Store

if TYPE_CHECKING:
    from telethon import TelegramClient

    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.secrets import SecretStore


class ConfigError(Exception):
    """Missing credentials/config the operator must fix (not a bug to catch and hide)."""


def resolve_api_id(settings: Settings, profile: str) -> int:
    """`api_id` is not a secret (spec §9) — env/config is the documented
    source (`PAPERBOY_API_ID`). As a convenience for operators who already
    ran `scripts/store_api.py` (Task 0's bootstrap helper), fall back to the
    same keychain entry that script wrote, so `--profile` alone is enough to
    pick up a previously stored api_id without also exporting the env var.
    """
    if settings.api_id is not None:
        return settings.api_id
    stored = keyring.get_password(SERVICE, f"{profile}:api_id")
    if stored is None:
        raise ConfigError(
            f"No api_id configured for profile {profile!r}: set PAPERBOY_API_ID or run "
            f"`uv run python scripts/store_api.py --profile {profile}`."
        )
    return int(stored)


def build_secrets(profile: str) -> SecretStore:
    return KeyringSecrets(profile)


def _parse_proxy(proxy: str) -> tuple:
    """Parse `socks5://[user:pass@]host:port` into a PySocks-style tuple.

    `mtproxy://` is recognized but not yet implemented — it needs a distinct
    Telethon connection class, not just a `proxy=` tuple, and that wiring
    hasn't been built/verified against the installed layer.
    """
    if proxy.startswith("mtproxy://"):
        raise NotImplementedError(
            "mtproxy:// proxies are not yet implemented; use socks5:// for now."
        )
    if not proxy.startswith("socks5://"):
        raise ValueError(f"unsupported proxy scheme: {proxy!r}")
    import socks

    rest = proxy.removeprefix("socks5://")
    auth, _, hostport = rest.rpartition("@") if "@" in rest else ("", "", rest)
    host, _, port_s = hostport.partition(":")
    port = int(port_s) if port_s else 1080
    if auth:
        user, _, password = auth.partition(":")
        return (socks.SOCKS5, host, port, True, user, password)
    return (socks.SOCKS5, host, port, True)


def build_client(settings: Settings, secrets: SecretStore, profile: str) -> TelegramClient:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = resolve_api_id(settings, profile)
    api_hash = secrets.get_api_hash()
    if api_hash is None:
        raise ConfigError(
            f"No api_hash stored for profile {profile!r}; run "
            f"`uv run python scripts/store_api.py --profile {profile}`."
        )
    register_secret(api_hash)
    session = secrets.get_session() or ""
    if session:
        register_secret(session)

    client_kwargs: dict = {
        "device_model": settings.device.device_model,
        "system_version": settings.device.system_version,
        "app_version": settings.device.app_version,
        "lang_code": settings.device.lang_code,
        # paperboy's own Budget owns all flood pacing/persistence (ADR-0003);
        # a nonzero threshold here would let Telethon silently sleep through
        # a FLOOD_WAIT before Budget.call ever sees the exception to record.
        "flood_sleep_threshold": 0,
    }
    if settings.proxy:
        client_kwargs["proxy"] = _parse_proxy(settings.proxy)

    return TelegramClient(StringSession(session), api_id, api_hash, **client_kwargs)


async def build_gateway(
    settings: Settings, secrets: SecretStore, profile: str, store: Store
) -> Gateway:
    """Build a connected `TelethonGateway` — the one seam `cli.py` calls to
    get "the thing that talks to Telegram", so tests substitute this single
    async function with a stub returning a `FakeGateway` and never touch
    Telethon, the keychain, or the network.
    """
    client = build_client(settings, secrets, profile)
    budget = Budget(settings, store)
    await client.connect()
    return TelethonGateway(client, budget)


def build_store(settings: Settings, profile: str) -> Store:
    path = profile_dir(settings, profile) / "paperboy.sqlite"
    return Store.open(path)
