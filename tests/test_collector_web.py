import logging
from pathlib import Path

import httpx
import pytest

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.collectors.web import WebCollector
from paperboy.config import load_settings
from paperboy.ids import msg_uri, utc_now_iso
from paperboy.store.db import Store
from paperboy.store.sync import get_state
from paperboy.targets import parse_target
from paperboy.web.client import WebClient
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/web")


def _mock_client(handler) -> WebClient:
    return WebClient(transport=httpx.MockTransport(handler))


def _ctx(store, target_str="@durov", channel_id=None):
    return CollectContext(
        FakeGateway({}), store, load_settings("default", {}), parse_target(target_str),
        None, channel_id, "stranger", logging.getLogger("t"),
    )


def _handler_factory(tme_html: str, cdx_json: str, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host in ("t.me", "www.t.me"):
            if request.url.params.get("before") is not None:
                return httpx.Response(200, text="<html><body>no more posts</body></html>")
            return httpx.Response(200, text=tme_html)
        if request.url.host == "web.archive.org":
            return httpx.Response(200, text=cdx_json)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_web_collector_stores_tme_posts(tmp_path):
    tme_html = (FX / "tme_durov_page1.html").read_text()
    calls: list[str] = []
    client = _mock_client(_handler_factory(tme_html, "[]", calls))
    with Store.open(tmp_path / "p.sqlite") as st:
        collector = WebCollector(client=client, sleep=lambda s: None)
        res = await collector.collect(_ctx(st))
        assert res.counts["tme_posts"] == 2
        rows = st.conn.execute(
            "SELECT url, channel_username, msg_id, timestamp, content_hash "
            "FROM web_snapshots WHERE source='tme' ORDER BY msg_id"
        ).fetchall()
        assert [r["msg_id"] for r in rows] == [523, 524]
        assert rows[0]["channel_username"] == "durov"
        assert rows[0]["url"] == "https://t.me/durov/523"
        assert rows[0]["timestamp"] == "2026-08-20T12:00:00+00:00"
        assert rows[0]["content_hash"]


@pytest.mark.asyncio
async def test_web_collector_stores_wayback_rows(tmp_path):
    cdx_json = (FX / "wayback_cdx.json").read_text()
    calls: list[str] = []
    client = _mock_client(_handler_factory("<html><body>no feed</body></html>", cdx_json, calls))
    with Store.open(tmp_path / "p.sqlite") as st:
        collector = WebCollector(client=client, sleep=lambda s: None)
        res = await collector.collect(_ctx(st))
        assert res.counts["wayback_rows"] == 2
        # the CDX query must be BOUNDED (unbounded returns 100+ MB on a
        # heavily-archived channel and fails to parse)
        cdx_calls = [c for c in calls if "web.archive.org/cdx" in c]
        assert cdx_calls and "limit=" in cdx_calls[0] and "collapse=" in cdx_calls[0]
        rows = st.conn.execute(
            "SELECT url, timestamp, content_hash, meta_json FROM web_snapshots "
            "WHERE source='wayback' ORDER BY timestamp"
        ).fetchall()
        assert rows[0]["timestamp"] == "2019-03-01T12:00:00+00:00"
        assert rows[0]["url"] == "http://t.me/s/durov"
        assert rows[0]["content_hash"] == "AAAABBBBCCCCDDDD"
        assert '"statuscode": "200"' in rows[0]["meta_json"]


@pytest.mark.asyncio
async def test_web_collector_flags_deleted_post_recovery(tmp_path):
    tme_html = (FX / "tme_durov_page1.html").read_text()
    calls: list[str] = []
    client = _mock_client(_handler_factory(tme_html, "[]", calls))
    with Store.open(tmp_path / "p.sqlite") as st:
        channel_id = 5
        st.conn.execute(
            "INSERT INTO channels (id, uri, username, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, "tg:channel:5", "durov", utc_now_iso(), utc_now_iso()),
        )
        # post 523 is tombstoned (deleted per the MTProto side) but still
        # visible on the t.me/s/ page -> deleted-post recovery.
        st.conn.execute(
            "INSERT INTO message_tombstones (message_uri, observed_at, evidence) "
            "VALUES (?, ?, ?)",
            (msg_uri(channel_id, 523), utc_now_iso(), "update"),
        )
        collector = WebCollector(client=client, sleep=lambda s: None)
        res = await collector.collect(_ctx(st, channel_id=channel_id))
        assert res.counts["deleted_recovered"] == 1
        tombstoned = st.conn.execute(
            "SELECT meta_json FROM web_snapshots WHERE source='tme' AND msg_id=523"
        ).fetchone()
        assert '"tombstoned_in_store": true' in tombstoned["meta_json"]
        not_tombstoned = st.conn.execute(
            "SELECT meta_json FROM web_snapshots WHERE source='tme' AND msg_id=524"
        ).fetchone()
        assert '"tombstoned_in_store": false' in not_tombstoned["meta_json"]


@pytest.mark.asyncio
async def test_web_collector_paginates_with_before_and_stops_on_empty_page(tmp_path):
    tme_html = (FX / "tme_durov_page1.html").read_text()
    calls: list[str] = []
    client = _mock_client(_handler_factory(tme_html, "[]", calls))
    with Store.open(tmp_path / "p.sqlite") as st:
        collector = WebCollector(client=client, sleep=lambda s: None)
        await collector.collect(_ctx(st))
        tme_calls = [c for c in calls if c.startswith("https://t.me/s/durov")]
        assert len(tme_calls) == 2
        # first call fetches the NEWEST posts (no ?before=), then pages backward
        assert "?before" not in tme_calls[0]
        assert "before=523" in tme_calls[1]
        # state is a newest-seen high-water mark, not a backward resume cursor —
        # so a later run re-fetches the newest posts rather than only continuing
        # deeper into the past.
        state = get_state(st, "web_tme", "durov")
        assert state is not None
        assert "before" not in state and "newest_seen" in state


def test_applies_to_channel_like_targets():
    assert WebCollector().applies_to(parse_target("@durov"))
    assert not WebCollector().applies_to(parse_target("#osint"))


@pytest.mark.asyncio
async def test_web_collector_skips_target_with_no_public_username(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request should be made for an unresolvable target")

    with Store.open(tmp_path / "p.sqlite") as st:
        collector = WebCollector(client=_mock_client(handler), sleep=lambda s: None)
        with pytest.raises(SkipAndRecord):
            await collector.collect(_ctx(st, target_str="-100123456789"))
