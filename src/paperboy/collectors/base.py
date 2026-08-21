"""The `Collector` Protocol and the mutable context every collector shares.

A recipe (`recipes.py`) builds one `CollectContext` per target and threads it
through an ordered list of collectors: `channel` populates `input_channel`/
`channel_id`/`tier` for `history` (and later Phase 2 collectors) to consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from logging import Logger
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from paperboy.config import Settings
    from paperboy.gateway import Gateway
    from paperboy.store.db import Store
    from paperboy.targets import Target


@dataclass
class CollectContext:
    gateway: Gateway
    store: Store
    settings: Settings
    target: Target
    input_channel: dict | None
    channel_id: int | None
    tier: str
    log: Logger
    # Which profile this run is collecting into — only `media` needs it (to
    # derive `<data_dir>/<profile>/media/...`, matching `profile_dir()` in
    # config.py); every other field above is required, so this is appended
    # at the end with a default rather than threaded through every existing
    # positional `CollectContext(...)` call site (recipes.py, every test).
    profile: str = "default"


@dataclass
class CollectResult:
    name: str
    counts: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None


class Collector(Protocol):
    name: str

    def applies_to(self, target: Target) -> bool: ...

    async def collect(self, ctx: CollectContext) -> CollectResult: ...
