"""tests/test_replay_web.py"""

from __future__ import annotations

from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayWebClient, ReplaySource
from paperboy.store.db import Store


def _seed(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        for i, t in enumerate(["2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00"]):
            st.add_raw(
                "tme_page",
                {"url": "https://t.me/s/durov", "status_code": 200,
                 "text": f"<html>page capture {i}</html>"},
                "stranger", {"channel_username": "durov"}, observed_at=t,
            )
        st.add_raw("wayback_cdx",
                   {"url": "https://web.archive.org/cdx/search/cdx?url=t.me/s/durov*"
                           "&output=json&filter=statuscode:200&collapse=digest&limit=10000",
                    "status_code": 200, "text": "[]"},
                   "stranger", {"channel_username": "durov"},
                   observed_at="2026-01-01T00:00:03+00:00")
    return db


def _client(tmp_path):
    clock = ReplayClock()
    return RawReplayWebClient(ReplaySource.open(_seed(tmp_path), tmp_path / "media"), clock), clock


def test_serves_stored_response_and_stamps_clock(tmp_path):
    client, clock = _client(tmp_path)
    resp = client.get("https://t.me/s/durov")
    assert resp.status_code == 200
    assert clock.for_payload(
        {"url": "https://t.me/s/durov", "status_code": 200, "text": resp.text}
    ) == "2026-01-01T00:00:01+00:00"


def test_same_url_serves_captures_in_order(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("https://t.me/s/durov").text == "<html>page capture 0</html>"
    assert client.get("https://t.me/s/durov").text == "<html>page capture 1</html>"


def test_unrecorded_url_is_a_definitive_404(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("https://t.me/s/durov?before=5")
    # 404, not 5xx: an unambiguous "nothing there", so the collector's page
    # loop stops cleanly instead of reporting a failure (web.py's
    # _is_ambiguous_failure treats 404 as an answer).
    assert resp.status_code == 404 and resp.text == ""


def test_web_client_satisfies_web_getter():
    from paperboy.web.client import WebClient, WebGetter
    client: WebGetter = WebClient()  # structural check exercised by pyright too
    client.close()
