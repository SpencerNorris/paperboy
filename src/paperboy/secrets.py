"""Secret storage — `api_hash` and the MTProto session string.

Per spec §3/§9, secrets live only in the OS keychain, never in `config.toml`,
env, or logs. `SecretStore` is a small Protocol so collectors/CLI code never
depend on `keyring` directly, and tests use `MemorySecrets` instead of
touching the real keychain.
"""

from __future__ import annotations

from typing import Protocol

import keyring

SERVICE = "paperboy"


class SecretStore(Protocol):
    def get_api_hash(self) -> str | None: ...
    def set_api_hash(self, value: str) -> None: ...
    def get_session(self) -> str | None: ...
    def set_session(self, value: str) -> None: ...


class KeyringSecrets:
    """Secrets backed by the OS keychain, scoped to one profile."""

    def __init__(self, profile: str) -> None:
        self._profile = profile

    def _key(self, name: str) -> str:
        return f"{self._profile}:{name}"

    def get_api_hash(self) -> str | None:
        return keyring.get_password(SERVICE, self._key("api_hash"))

    def set_api_hash(self, value: str) -> None:
        keyring.set_password(SERVICE, self._key("api_hash"), value)

    def get_session(self) -> str | None:
        return keyring.get_password(SERVICE, self._key("session"))

    def set_session(self, value: str) -> None:
        keyring.set_password(SERVICE, self._key("session"), value)


class MemorySecrets:
    """In-memory `SecretStore` for tests — never touches the real keychain."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    def get_api_hash(self) -> str | None:
        return self._values.get("api_hash")

    def set_api_hash(self, value: str) -> None:
        self._values["api_hash"] = value

    def get_session(self) -> str | None:
        return self._values.get("session")

    def set_session(self, value: str) -> None:
        self._values["session"] = value
