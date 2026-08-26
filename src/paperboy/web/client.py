"""`WebClient`: the only seam through which paperboy makes outbound HTTP.

Spec §2 guardrail (issue #1): outbound HTTP is allowed to exactly three
hosts — `t.me`, `www.t.me`, `web.archive.org` — through the configured
proxy when one is set, and paperboy must never dereference a URL found
*inside* collected content (only URLs this codebase itself constructs from
a known username/query are ever fetched).

`WebClient.get` enforces the allow-list on the initial URL *and* on every
redirect hop: the underlying `httpx.Client` is built with
`follow_redirects=False` and this class walks the redirect chain itself,
checking each `Location` target's host before ever issuing that next
request. A disallowed host — initial or redirected-to — raises
`DisallowedHostError` instead of being followed.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlsplit

import httpx

# Deliberately a code constant, not user config (spec: the allow-list host
# set is not configurable).
ALLOWED_HOSTS = frozenset({"t.me", "www.t.me", "web.archive.org"})

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_REDIRECTS = 5


class DisallowedHostError(Exception):
    """Raised for a URL (initial or a redirect target) whose host isn't allow-listed."""


class WebGetter(Protocol):
    """The shape `WebCollector` needs from an HTTP client — satisfied by the
    real `WebClient` and by `replay.RawReplayWebClient` (spec §4)."""

    def get(self, url: str) -> httpx.Response: ...


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _check_allowed(url: str) -> None:
    host = _host_of(url)
    if host not in ALLOWED_HOSTS:
        raise DisallowedHostError(f"host not allow-listed: {host!r} (url={url!r})")


class WebClient:
    """A thin `httpx.Client` wrapper that enforces `ALLOWED_HOSTS` on every hop."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        kwargs: dict = {"follow_redirects": False, "timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.Client(**kwargs)

    def get(self, url: str, *, max_redirects: int = _MAX_REDIRECTS) -> httpx.Response:
        """GET `url`, manually following redirects so every hop is host-checked.

        Never call this with a URL sourced from collected message/page
        content (spec §2) — only with a URL this module built itself from a
        known username or CDX query.
        """
        _check_allowed(url)
        current = url
        for _ in range(max_redirects + 1):
            response = self._client.get(current)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = str(httpx.URL(current).join(location))
            _check_allowed(next_url)
            current = next_url
        raise DisallowedHostError(f"too many redirects starting at {url!r}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WebClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
