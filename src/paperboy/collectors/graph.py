"""The `graph` collector: similar-channel recommendations, entity-derived
mentions, unjoined invite-link previews, and sponsored-message provenance —
the four sources that fill the otherwise-sparse `edges` table (spec §6, §2.6).

Each of the four sub-features is independent and wrapped in its own
`SkipAndRecord` handling: a documented per-RPC failure (`CHAT_NOT_MODIFIED`,
`CHAT_ADMIN_REQUIRED`, `PREMIUM_ACCOUNT_REQUIRED`, ...) skips *that*
sub-feature only, logged and counted as zero, so one account's lack of
access to (say) sponsored messages never blocks recommendations or the
(RPC-free) mention scan.

Follow-up, deliberately out of scope for this pass (spec instruction): bare
`MessageEntityMention`s (`@name` typed inline, no numeric id attached) are
not resolved to a peer id here — doing so would cost a `contacts.resolveUsername`
RPC per unique handle, unbounded by anything already in the message. A later
pass can batch-resolve the distinct handles this phase leaves as gaps.
"""

from __future__ import annotations

import json
import sqlite3

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.ids import (
    channel_uri,
    invite_uri,
    msg_uri,
    parse_tme_link,
    user_uri,
    username_uri,
    utc_now_iso,
    utf16_slice,
)
from paperboy.store.edges import add_edge
from paperboy.store.peers import upsert_peer
from paperboy.targets import Target

# Predicates used here, from the spec §2 edge vocabulary.
_RECOMMENDED = "recommended_with"
_MENTIONS = "mentions"
_INVITED_VIA = "invited_via"
_MEMBER_OF = "member_of"

_RESOLVED_INVITE_KINDS = {"chatinvitealready", "chatinvitepeek"}


class GraphCollector:
    name = "graph"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.input_channel is None or ctx.channel_id is None:
            raise PhaseStop(
                "graph skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {"edges": 0, "peers": 0, "raw": 0, "skipped": 0}

        await self._collect_recommendations(ctx, counts)

        rows = ctx.store.conn.execute(
            "SELECT channel_id, msg_id, text, entities_json, source_raw_id FROM messages "
            "WHERE channel_id=? AND entities_json IS NOT NULL",
            (ctx.channel_id,),
        ).fetchall()
        mention_edges, invite_hashes = _scan_message_entities(rows)
        self._write_mention_edges(ctx, mention_edges, counts)

        await self._collect_invite_previews(ctx, invite_hashes, counts)
        await self._collect_sponsored(ctx, counts)

        return CollectResult(name=self.name, counts=counts)

    async def _collect_recommendations(self, ctx: CollectContext, counts: dict) -> None:
        assert ctx.input_channel is not None and ctx.channel_id is not None
        try:
            result = await ctx.gateway.get_channel_recommendations(ctx.input_channel)
        except SkipAndRecord as exc:
            ctx.log.warning("graph recommendations skipped: %s", exc)
            counts["skipped"] += 1
            return

        observed_at = utc_now_iso()
        raw_id = ctx.store.add_raw(
            result.get("_", "ChatsSlice"), result, ctx.tier, {"channel_id": ctx.channel_id}
        )
        counts["raw"] += 1
        chats = result.get("chats", [])
        # `messages.ChatsSlice.count` is the true total even when Telegram
        # only returns ~10 `chats`; plain `messages.Chats` means the list
        # returned *is* the full set, so its own length is the true count.
        true_count = result.get("count", len(chats))

        subject = channel_uri(ctx.channel_id)
        for chat in chats:
            if chat.get("id") == ctx.channel_id:
                continue
            peer_uri = upsert_peer(
                ctx.store, chat, raw_id, observed_at, seen_in_chat=None, seen_in_msg=None
            )
            counts["peers"] += 1
            add_edge(
                ctx.store, subject, _RECOMMENDED, peer_uri, observed_at, ctx.tier, raw_id,
                {"total_count": true_count},
            )
            counts["edges"] += 1

    def _write_mention_edges(
        self,
        ctx: CollectContext,
        mention_edges: list[tuple[str, str, dict, int | None]],
        counts: dict,
    ) -> None:
        observed_at = utc_now_iso()
        for subject, object_, evidence, source_raw_id in mention_edges:
            add_edge(
                ctx.store, subject, _MENTIONS, object_, observed_at, ctx.tier, source_raw_id,
                evidence,
            )
            counts["edges"] += 1

    async def _collect_invite_previews(
        self, ctx: CollectContext, invite_hashes: dict[str, list[str]], counts: dict
    ) -> None:
        for hash_, msg_uris in invite_hashes.items():
            try:
                preview = await ctx.gateway.check_chat_invite(hash_)
            except SkipAndRecord as exc:
                ctx.log.warning("graph invite preview skipped for %s: %s", hash_, exc)
                counts["skipped"] += 1
                continue

            observed_at = utc_now_iso()
            raw_id = ctx.store.add_raw(
                preview.get("_", "ChatInvite"), preview, ctx.tier, {"hash": hash_}
            )
            counts["raw"] += 1

            kind = preview.get("_", "").lower()
            chat = preview.get("chat")
            if kind in _RESOLVED_INVITE_KINDS and chat:
                # Already-known chat (member, or a public/peekable one) —
                # link to the real peer instead of the hash pseudo-URI.
                object_uri = upsert_peer(
                    ctx.store, chat, raw_id, observed_at, seen_in_chat=None, seen_in_msg=None
                )
                counts["peers"] += 1
                evidence = {"resolved": True, "chat_id": chat.get("id")}
            else:
                # Unjoined preview: no numeric chat id, so the edge targets
                # the invite-hash pseudo-URI, evidenced by what the preview
                # *does* reveal (spec §6: title, participants_count, photo).
                object_uri = invite_uri(hash_)
                evidence = {
                    "resolved": False,
                    "title": preview.get("title"),
                    "participants_count": preview.get("participants_count"),
                    "has_photo": preview.get("photo") is not None,
                }

            # `chatInvite.participants` is a *sample* of the members — the only
            # roster data Telegram hands an account that has not joined (see
            # docs/research/sources/mtproto-participants-users.md). It rotates
            # between calls, so projecting it on every run accumulates real
            # membership over time while collection stays passive. An unjoined
            # invite exposes no numeric chat id, so the ChatInvite raw record —
            # whose context carries the hash — is the only provenance there is.
            for participant in preview.get("participants") or []:
                peer_uri = upsert_peer(
                    ctx.store, participant, raw_id, observed_at,
                    seen_in_chat=None, seen_in_msg=None,
                )
                counts["peers"] += 1
                add_edge(
                    ctx.store, peer_uri, _MEMBER_OF, object_uri, observed_at, ctx.tier, raw_id,
                    # `sampled` marks these as a handful out of
                    # `participants_count`, so no reader mistakes the rows
                    # present for the whole membership.
                    {"sampled": True, "participants_count": preview.get("participants_count")},
                )
                counts["edges"] += 1

            for m_uri in msg_uris:
                add_edge(
                    ctx.store, m_uri, _INVITED_VIA, object_uri, observed_at, ctx.tier, raw_id,
                    evidence,
                )
                counts["edges"] += 1

    async def _collect_sponsored(self, ctx: CollectContext, counts: dict) -> None:
        assert ctx.input_channel is not None and ctx.channel_id is not None
        try:
            result = await ctx.gateway.get_sponsored_messages(ctx.input_channel)
        except SkipAndRecord as exc:
            ctx.log.warning("graph sponsored messages skipped: %s", exc)
            counts["skipped"] += 1
            return

        kind = result.get("_", "").lower()
        if kind == "sponsoredmessagesempty":
            return

        observed_at = utc_now_iso()
        subject = channel_uri(ctx.channel_id)
        for sponsored in result.get("messages", []):
            # No dedicated table (spec instruction) — raw + edges only. Every
            # sponsored message is raw-logged individually (not just the
            # envelope) so `sponsor_info`/`url`/`title` are each queryable
            # from `raw_records` without re-parsing the batch.
            raw_id = ctx.store.add_raw(
                sponsored.get("_", "SponsoredMessage"), sponsored, ctx.tier,
                {"channel_id": ctx.channel_id},
            )
            counts["raw"] += 1

            url = sponsored.get("url", "")
            parsed = parse_tme_link(url)
            if parsed is None:
                continue
            link_kind, value = parsed
            object_uri = invite_uri(value) if link_kind == "invite" else username_uri(value)
            add_edge(
                ctx.store, subject, _MENTIONS, object_uri, observed_at, ctx.tier, raw_id,
                {
                    "source": "sponsored",
                    "url": url,
                    "sponsor_info": sponsored.get("sponsor_info"),
                    "title": sponsored.get("title"),
                },
            )
            counts["edges"] += 1


def _scan_message_entities(
    rows: list[sqlite3.Row],
) -> tuple[list[tuple[str, str, dict, int | None]], dict[str, list[str]]]:
    """Walk stored messages' `entities_json` for `MentionName`/`TextUrl`/`Url`
    entities, without any RPC. Returns `(mention_edges, invite_hashes)`:
    `mention_edges` is `(subject_uri, object_uri, evidence, source_raw_id)`
    ready for `add_edge` (provenance borrowed from the message's own raw
    record); `invite_hashes` maps each distinct invite hash found to the
    list of message URIs that referenced it, for `_collect_invite_previews`
    to dedupe the `checkChatInvite` RPC by hash rather than by mention.
    """
    mention_edges: list[tuple[str, str, dict, int | None]] = []
    invite_hashes: dict[str, list[str]] = {}

    for row in rows:
        entities = json.loads(row["entities_json"]) or []
        text = row["text"] or ""
        uri = msg_uri(row["channel_id"], row["msg_id"])
        source_raw_id = row["source_raw_id"]

        for entity in entities:
            kind = (entity.get("_") or "").lower()
            if kind == "messageentitymentionname":
                user_id = entity.get("user_id")
                if user_id is not None:
                    mention_edges.append((
                        uri, user_uri(user_id),
                        {"entity": "MessageEntityMentionName"}, source_raw_id,
                    ))
            elif kind == "messageentitytexturl":
                _record_link(
                    uri, entity.get("url", ""), "MessageEntityTextUrl", source_raw_id,
                    mention_edges, invite_hashes,
                )
            elif kind == "messageentityurl":
                offset, length = entity.get("offset"), entity.get("length")
                if offset is None or length is None:
                    continue
                url = utf16_slice(text, offset, length)
                _record_link(
                    uri, url, "MessageEntityUrl", source_raw_id, mention_edges, invite_hashes
                )
            # else: MessageEntityMention (bare @name) and every other entity
            # kind (bold, code, custom emoji, ...) carry nothing graph-shaped.

    return mention_edges, invite_hashes


def _record_link(
    subject_uri: str,
    url: str,
    entity_name: str,
    source_raw_id: int | None,
    mention_edges: list[tuple[str, str, dict, int | None]],
    invite_hashes: dict[str, list[str]],
) -> None:
    parsed = parse_tme_link(url)
    if parsed is None:
        return
    kind, value = parsed
    object_uri = invite_uri(value) if kind == "invite" else username_uri(value)
    mention_edges.append((
        subject_uri, object_uri, {"entity": entity_name, "url": url}, source_raw_id,
    ))
    if kind == "invite":
        invite_hashes.setdefault(value, []).append(subject_uri)
