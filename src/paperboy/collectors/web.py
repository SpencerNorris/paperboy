"""The `web` collector: `t.me/s/<name>` feed capture + Wayback CDX index
enumeration into `web_snapshots` (spec §2.8, §6). Pure HTTP via `WebClient`
— no MTProto, no `Gateway`/`Budget` involvement; `web.client.ALLOWED_HOSTS`
is this collector's only outbound-request guardrail.

Deleted-post recovery: every parsed `t.me/s/` post whose id is already
tombstoned in `message_tombstones` gets `meta_json.tombstoned_in_store =
true` and counts toward `deleted_recovered` — the web page can still be
serving text the MTProto API no longer will.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

import httpx

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import msg_uri, utc_now_iso
from paperboy.store.sync import get_state, set_state
from paperboy.store.web import insert_tme_snapshot, insert_wayback_snapshot
from paperboy.targets import Target, TargetKind
from paperboy.web.client import WebClient
from paperboy.web.tme_parser import TmePost, parse_tme_page
from paperboy.web.wayback import cdx_timestamp_to_iso, parse_cdx_rows

# Safety bound on `?before=` pagination depth and the pacing delay between
# HTTP requests — code constants (spec: not user config), like the allow-list.
_MAX_TME_PAGES = 50
_DEFAULT_MIN_INTERVAL_SECONDS = 1.0


def _text_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_username(ctx: CollectContext) -> str:
    """The `t.me/s/<name>` username: from the target if it's username-shaped,
    else looked up in `channels` by `ctx.channel_id` (set when `channel` ran
    earlier in this recipe run). Raises `SkipAndRecord` if neither yields
    one — invite-hash/peer-id/msg-link targets and private channels have no
    public `t.me/s/` page at all.
    """
    if ctx.target.kind is TargetKind.USERNAME:
        return ctx.target.value
    if ctx.channel_id is not None:
        row = ctx.store.conn.execute(
            "SELECT username FROM channels WHERE id=?", (ctx.channel_id,)
        ).fetchone()
        if row and row["username"]:
            return str(row["username"])
    raise SkipAndRecord(
        f"web: no public username available for target {ctx.target.raw!r} "
        "— t.me/s/ requires one"
    )


def _resolve_channel_id(ctx: CollectContext, username: str) -> int | None:
    if ctx.channel_id is not None:
        return ctx.channel_id
    row = ctx.store.conn.execute(
        "SELECT id FROM channels WHERE username=?", (username,)
    ).fetchone()
    return row["id"] if row else None


def _is_tombstoned(ctx: CollectContext, channel_id: int | None, msg_id: int | None) -> bool:
    if channel_id is None or msg_id is None:
        return False
    uri = msg_uri(channel_id, msg_id)
    row = ctx.store.conn.execute(
        "SELECT 1 FROM message_tombstones WHERE message_uri=? LIMIT 1", (uri,)
    ).fetchone()
    return row is not None


class WebCollector:
    """`t.me/s/<name>` feed capture + Wayback CDX enumeration into `web_snapshots`.

    `client` is injectable (tests build a `WebClient` over an
    `httpx.MockTransport`); left `None`, one is built lazily from
    `ctx.settings.proxy` on first use. `sleep` is injectable for the same
    reason `Budget` makes its sleeper injectable — tests never actually
    block on the polite inter-request pacing.
    """

    name = "web"

    def __init__(
        self,
        *,
        client: WebClient | None = None,
        min_interval: float = _DEFAULT_MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._min_interval = min_interval
        self._sleep: Callable[[float], None] = sleep or time.sleep

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    def _get_client(self, ctx: CollectContext) -> WebClient:
        if self._client is None:
            self._client = WebClient(proxy=ctx.settings.proxy)
        return self._client

    def _paced_get(self, client: WebClient, url: str) -> httpx.Response:
        self._sleep(self._min_interval)
        return client.get(url)

    async def collect(self, ctx: CollectContext) -> CollectResult:
        username = _resolve_username(ctx)
        client = self._get_client(ctx)
        channel_id = _resolve_channel_id(ctx, username)

        counts = self._collect_tme(ctx, client, username, channel_id)
        counts["wayback_rows"] = self._collect_wayback(ctx, client, username)

        return CollectResult(name=self.name, counts=counts)

    def _collect_tme(
        self, ctx: CollectContext, client: WebClient, username: str, channel_id: int | None
    ) -> dict[str, int]:
        counts = {"tme_posts": 0, "deleted_recovered": 0}
        resume = get_state(ctx.store, "web_tme", username) or {}
        before_cursor: int | None = resume.get("before")

        for _ in range(_MAX_TME_PAGES):
            url = f"https://t.me/s/{username}"
            if before_cursor is not None:
                url += f"?before={before_cursor}"

            response = self._paced_get(client, url)
            fetched_at = utc_now_iso()
            ctx.store.add_raw(
                "tme_page",
                {"url": url, "status_code": response.status_code, "text": response.text},
                ctx.tier,
                {"channel_username": username},
            )

            posts = parse_tme_page(response.text)
            if not posts:
                break

            new_min: int | None = None
            for post in posts:
                self._store_post(ctx, fetched_at, username, channel_id, post, counts)
                if post.msg_id is not None:
                    new_min = post.msg_id if new_min is None else min(new_min, post.msg_id)

            if new_min is None or (before_cursor is not None and new_min >= before_cursor):
                break
            before_cursor = new_min
            set_state(ctx.store, "web_tme", username, {"before": before_cursor})

        return counts

    def _store_post(
        self,
        ctx: CollectContext,
        fetched_at: str,
        username: str,
        channel_id: int | None,
        post: TmePost,
        counts: dict[str, int],
    ) -> None:
        tombstoned = _is_tombstoned(ctx, channel_id, post.msg_id)
        meta = {
            "views": post.views,
            "author_signature": post.author_signature,
            "forwarded_from": post.forwarded_from,
            "tombstoned_in_store": tombstoned,
        }
        insert_tme_snapshot(
            ctx.store,
            url=f"https://t.me/{post.post_id}",
            fetched_at=fetched_at,
            channel_username=username,
            msg_id=post.msg_id,
            timestamp=post.datetime,
            content_hash=_text_content_hash(post.text),
            raw={
                "post_id": post.post_id,
                "text": post.text,
                "datetime": post.datetime,
                "views": post.views,
                "author_signature": post.author_signature,
                "forwarded_from": post.forwarded_from,
            },
            meta=meta,
        )
        counts["tme_posts"] += 1
        if tombstoned:
            counts["deleted_recovered"] += 1

    def _collect_wayback(self, ctx: CollectContext, client: WebClient, username: str) -> int:
        url = f"https://web.archive.org/cdx/search/cdx?url=t.me/s/{username}*&output=json"
        response = self._paced_get(client, url)
        fetched_at = utc_now_iso()
        ctx.store.add_raw(
            "wayback_cdx",
            {"url": url, "status_code": response.status_code, "text": response.text},
            ctx.tier,
            {"channel_username": username},
        )

        try:
            payload = response.json()
        except ValueError:
            payload = []
        rows = parse_cdx_rows(payload) if isinstance(payload, list) else []

        n = 0
        for row in rows:
            timestamp = row.get("timestamp", "")
            insert_wayback_snapshot(
                ctx.store,
                url=row.get("original", url),
                fetched_at=fetched_at,
                channel_username=username,
                timestamp=cdx_timestamp_to_iso(timestamp) or (timestamp or None),
                content_hash=row.get("digest"),
                raw=row,
                meta={
                    "statuscode": row.get("statuscode"),
                    "length": row.get("length"),
                    "mimetype": row.get("mimetype"),
                },
            )
            n += 1
        return n
