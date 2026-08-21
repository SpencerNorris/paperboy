"""`WebClient` allow-list enforcement — the tested invariant from issue #1.

No real network anywhere here: every case uses `httpx.MockTransport`.
"""

import httpx
import pytest

from paperboy.web.client import ALLOWED_HOSTS, DisallowedHostError, WebClient


def test_allowed_hosts_is_exactly_the_three_hosts():
    assert {"t.me", "www.t.me", "web.archive.org"} == ALLOWED_HOSTS


def test_get_rejects_a_non_allowlisted_host():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="should never be reached")

    client = WebClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedHostError):
        client.get("https://evil.example.com/steal")
    assert calls == []  # rejected before any request was sent


def test_get_allows_an_allowlisted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    client = WebClient(transport=httpx.MockTransport(handler))
    response = client.get("https://t.me/s/durov")
    assert response.status_code == 200
    assert response.text == "ok"


def test_get_rejects_a_redirect_to_a_non_allowlisted_host():
    """t.me answering 302 to an external host must not be followed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "t.me":
            return httpx.Response(302, headers={"location": "https://evil.example.com/phish"})
        return httpx.Response(200, text="should never be reached")

    client = WebClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedHostError):
        client.get("https://t.me/s/durov")


def test_get_follows_a_redirect_to_an_allowlisted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "t.me":
            return httpx.Response(302, headers={"location": "https://www.t.me/s/durov"})
        return httpx.Response(200, text="landed")

    client = WebClient(transport=httpx.MockTransport(handler))
    response = client.get("https://t.me/s/durov")
    assert response.status_code == 200
    assert response.text == "landed"


def test_get_rejects_a_relative_redirect_resolving_off_allowlisted_host():
    """A redirect chain: t.me -> www.t.me (ok) -> evil host (rejected), each
    hop checked independently — allowing the first hop must not grant the
    second a free pass.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "t.me":
            return httpx.Response(302, headers={"location": "https://www.t.me/s/durov"})
        if request.url.host == "www.t.me":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x"})
        return httpx.Response(200, text="should never be reached")

    client = WebClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedHostError):
        client.get("https://t.me/s/durov")


def test_too_many_redirects_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://t.me/s/durov"})

    client = WebClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DisallowedHostError):
        client.get("https://t.me/s/durov", max_redirects=2)
