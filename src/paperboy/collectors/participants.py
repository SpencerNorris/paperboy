"""The `participants` collector: roster discovery for the person layer
(spec §6) — within Telegram's hard walls, passive by default.

For a BROADCAST channel a non-admin can enumerate nothing about subscribers
(research §1.3, settled live: `CHAT_ADMIN_REQUIRED` for every filter), and
joining buys nothing. That wall is a first-class stored outcome — a
`RosterWalled` raw record + a `participant_snapshots` accounting row, with
ZERO enumeration RPC against the channel — never a silent zero. The people
live in the linked discussion supergroup, whose public roster IS enumerable
un-joined (spec §13, live probe): `channels.getParticipants(Recent)` is paged
to the server's depth and `enumerated / true_count` is recorded every run
(spec §6.3 — 200 is a page size, not a total; the real ceiling is Telegram's
and a shortfall is labelled, with the `--join` escalation named, §6.4).

Unioned with zero new RPC: join/leave service messages already in the
captured history (`store.participants.project_join_service_messages`) and
the `recent_reactions` sample inside stored messages
(`store.reactions.backfill_recent_reactions`). Bounded RPC vectors (Task 9):
the `channels.getParticipant` oracle for known users a partial roster
missed, and `messages.getMessageReactionsList` on reacted group messages.

Guardrails: the per-phase session-age gate (spec §6.1) refuses enumeration
on a young session unless `--unsafe`; `--join` joins only a group we have
not joined, through the shared audited `join_or_skip`; admin-only
sub-methods (boosts, invite importers, admin log) are never attempted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.channel import pick_channel
from paperboy.collectors.discussion import join_or_skip, linked_group
from paperboy.collectors.posture import record_privacy_posture
from paperboy.doctor import session_age_days
from paperboy.gateway import FILTER_ADMINS, FILTER_BOTS, FILTER_RECENT
from paperboy.ids import namespaced_kind
from paperboy.store.channels import channel_flags, upsert_channel
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.participants import (
    add_participant_snapshot,
    add_roster_snapshot,
    membership_edges,
    project_join_service_messages,
    upsert_participant,
)
from paperboy.store.peers import upsert_full_peer, upsert_peer
from paperboy.store.reactions import backfill_recent_reactions
from paperboy.store.users import add_user_snapshot, target_user_facts, upsert_user
from paperboy.targets import Target

_PAGE_SIZE = 200  # Telegram's page size, not a total cap (spec §6.3)
METHOD_GET_PARTICIPANTS = "channels.getParticipants"
# Admin-only sub-methods: detected via rights and SKIPPED, never attempted (spec §6.5).
_ADMIN_ONLY_METHODS = (
    "channels.getAdminLog", "premium.getBoostsList", "messages.getChatInviteImporters",
    "channels.getParticipants(channelParticipantsKicked/Banned)",
)

JOIN_SHORTFALL_WARNING = (
    "participants: enumerated {enumerated} of {total} members of group {group_id}; the full "
    "roster requires membership — re-run with --join to join and enumerate (an active, "
    "audited write)"
)


@dataclass
class _Roster:
    """One enumerable group: the linked discussion group, or the target itself
    when it is a supergroup. `stamp`/`source_raw_id` are the ChatFull
    observation that established the flags — every zero-RPC row derived
    from this roster is stamped from them, never from "now" (plan D5)."""

    group_id: int
    input_channel: dict
    flags: dict
    true_count: int | None
    stamp: str
    source_raw_id: int | None
    chan: dict | None  # the group's `Channel` object (its `left` flag drives --join)


class ParticipantsCollector:
    name = "participants"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None or ctx.input_channel is None:
            raise PhaseStop(
                "participants skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "rosters": 0, "walled": 0, "enumerated": 0, "true_count": 0, "participants": 0,
            "users": 0, "edges": 0, "oracle": 0, "backfilled_peers": 0, "service_joins": 0,
            "service_leaves": 0, "reactors": 0, "reaction_lists": 0, "skipped": 0,
        }
        chan = ctx.store.conn.execute(
            "SELECT kind, participants_count, flags_json, last_seen, source_raw_id "
            "FROM channels WHERE id=?",
            (ctx.channel_id,),
        ).fetchone()
        if chan is None:
            raise PhaseStop(
                "participants skipped: no channels row (channel phase did not complete)"
            )
        run_stamp: str = chan["last_seen"]
        target_is_group = chan["kind"] != "broadcast"

        if not target_is_group:
            # §6.2: the broadcast channel's OWN subscriber roster is never
            # enumerable — skip IT (recorded, zero RPC), not the collector.
            self._record_walled(
                ctx, ctx.channel_id, "broadcast_channel: subscriber roster is never enumerable "
                "below admin (CHAT_ADMIN_REQUIRED for every filter)",
                chan["participants_count"], run_stamp, counts,
            )
        linked = linked_group(ctx)
        if isinstance(linked, str):
            if not target_is_group:
                # No comment section => no person vector at all (§2): the one
                # case that is a FULL phase skip.
                return CollectResult(
                    name=self.name, counts=counts,
                    stopped=f"{linked} — no person vector (a broadcast channel's subscribers "
                            "are never enumerable)",
                )
            ctx.log.info("participants: %s", linked)

        # Zero-RPC vectors first — they read only the store, so they run even
        # when the session gate below refuses enumeration.
        group_ids = ([ctx.channel_id] if target_is_group else []) + (
            [] if isinstance(linked, str) else [linked[0]]
        )
        for group_id in group_ids:
            self._zero_rpc_vectors(ctx, group_id, counts)

        # The gate comes BEFORE the first RPC against any group (spec §6.1) —
        # including the linked group's preflight getFullChannel.
        gate = await self._session_gate(ctx)
        if gate is not None:
            ctx.log.warning(gate)
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "warning",
                {"code": "session_age_gate", "message": gate},
            )
            return CollectResult(name=self.name, counts=counts, stopped=gate)
        # The roster's free `users` vectors carry `userStatus*`, so the account's
        # own posture is recorded here too (once per run — a no-op if `profiles`
        # already did it this run; spec §4.3).
        await record_privacy_posture(ctx, self.name)

        rosters: list[_Roster] = []
        if target_is_group:
            rosters.append(_Roster(
                ctx.channel_id, ctx.input_channel, json.loads(chan["flags_json"] or "{}"),
                chan["participants_count"], run_stamp, chan["source_raw_id"], None,
            ))
        if not isinstance(linked, str):
            group_id, input_channel, _needs_join = linked
            roster = await self._preflight_group(ctx, group_id, input_channel, run_stamp, counts)
            if roster is not None:
                rosters.append(roster)

        stopped: list[str] = []
        for roster in rosters:
            reason = await self._enumerate(ctx, roster, counts)
            if reason:
                stopped.append(reason)
        return CollectResult(name=self.name, counts=counts, stopped="; ".join(stopped) or None)

    # ---- preflight + gate --------------------------------------------------------

    async def _preflight_group(
        self, ctx: CollectContext, group_id: int, input_channel: dict, run_stamp: str,
        counts: dict[str, int],
    ) -> _Roster | None:
        """§6.1: `can_view_participants` / `participants_hidden` live on the
        GROUP's channelFull, which no phase has fetched before. Also projects
        the group into `channels` (+ snapshot) and its vectors into peers."""
        try:
            full = await ctx.gateway.get_full_channel(input_channel)
        except SkipAndRecord as exc:
            self._record_walled(ctx, group_id, f"preflight: {exc}", None, run_stamp, counts)
            return None
        observed_at = ctx.clock.for_payload(full)
        # A SECOND `ChatFull` in the same pass. `ReplaySource.runs()` treats
        # `chatfull` rows as opening-cluster markers only for LEGACY (NULL
        # run_id) rows; every record this collector writes is stamped, so this
        # never splits a run — keep that invariant if that branch ever changes.
        raw_id = ctx.store.add_raw(
            full.get("_", "ChatFull"), full, ctx.tier, {"channel_id": group_id},
            observed_at=observed_at,
        )
        full_chat = full.get("full_chat") or {}
        if full_chat.get("id") != group_id:
            ctx.log.warning(
                "participants: getFullChannel for group %s answered for %s — not enumerating",
                group_id, full_chat.get("id"),
            )
            counts["skipped"] += 1
            return None
        chats = full.get("chats") or []
        try:
            chan = pick_channel(chats, group_id) if chats else None
        except ValueError:
            chan = None
        if chan is not None:
            upsert_channel(ctx.store, full_chat, chan, raw_id, observed_at)
        if not (chan or {}).get("admin_rights") and not (chan or {}).get("creator"):
            # Spec §6.5: admin-only sub-methods (boosts, invite importers, admin
            # log, kicked/banned) are detected via rights and SKIPPED, never
            # attempted — recorded so their absence is a stored decision.
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "admin_only_skipped",
                {"group_id": group_id, "methods": list(_ADMIN_ONLY_METHODS),
                 "reason": "no admin_rights on the group"},
            )
        for obj in chats:
            upsert_peer(ctx.store, obj, raw_id, observed_at, seen_in_chat=None, seen_in_msg=None)
        self._project_users_vector(ctx, full, raw_id, observed_at, counts)
        return _Roster(
            group_id, input_channel, channel_flags(full_chat, chan or {}),
            full_chat.get("participants_count"), observed_at, raw_id, chan,
        )

    async def _session_gate(self, ctx: CollectContext) -> str | None:
        """Spec §6.1 MUST: no participant sweep on a session younger than
        `min_session_age_days` without `--unsafe` — enforced per phase here,
        not only run-level by `doctor`."""
        if ctx.settings.unsafe:
            return None
        try:
            authorizations = await ctx.gateway.get_authorizations()
        except SkipAndRecord as exc:
            return (
                f"participants: roster enumeration refused — session age unknown ({exc}); "
                "pass --unsafe to enumerate anyway"
            )
        age = session_age_days(authorizations)
        minimum = ctx.settings.min_session_age_days
        if age is None or age < minimum:
            shown = "unknown" if age is None else f"{age:.1f} days"
            return (
                f"participants: roster enumeration refused — session age {shown} is below "
                f"min_session_age_days={minimum}; pass --unsafe to enumerate anyway"
            )
        return None

    # ---- zero-RPC vectors -----------------------------------------------------------

    def _zero_rpc_vectors(self, ctx: CollectContext, group_id: int, counts: dict[str, int]) -> None:
        """Everything derivable from the store alone: #11's message-referenced
        peers (so forward origins/mentions become oracle candidates too),
        join/leave service messages, and the `recent_reactions` sample."""
        counts["backfilled_peers"] += backfill_message_referenced_peers(ctx.store, group_id)
        service = project_join_service_messages(ctx.store, group_id, ctx.tier)
        counts["service_joins"] += service["joins"]
        counts["service_leaves"] += service["leaves"]
        counts["edges"] += service["edges"]
        counts["reactors"] += backfill_recent_reactions(ctx.store, group_id, ctx.tier)

    def _record_walled(
        self, ctx: CollectContext, group_id: int, reason: str, true_count: int | None,
        observed_at: str, counts: dict[str, int], *, enumerated: int = 0,
    ) -> None:
        """A walled roster is a stored observation (§6.2): a synthetic raw
        record (so a reproject detects and reproduces it) + the accounting
        row + an audit event. Stamped from the observation that established
        the wall, never from the wall clock (plan D5)."""
        payload = {
            "_": "RosterWalled", "group_id": group_id, "reason": reason,
            "participants_count": true_count, "enumerated": enumerated,
        }
        raw_id = ctx.store.add_raw(
            "RosterWalled", payload, ctx.tier, {"channel_id": group_id}, observed_at=observed_at
        )
        add_roster_snapshot(
            ctx.store, group_id, observed_at, enumerated=enumerated, true_count=true_count,
            reason=reason, source_raw_id=raw_id,
        )
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "roster_walled",
            {"group_id": group_id, "reason": reason, "participants_count": true_count},
        )
        counts["walled"] += 1

    # ---- enumeration ----------------------------------------------------------------

    async def _enumerate(
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]
    ) -> str | None:
        """Page `Recent` (plus `Admins`/`Bots` once joined), record
        `enumerated / true_count`, then run the bounded vectors. Returns the
        §6.4 shortfall warning when the roster came back walled or partial
        and we are not a member — the phase's `stopped` reason."""
        group_id = roster.group_id
        counts["rosters"] += 1
        joined = await self._maybe_join(ctx, roster)
        enumerated: set[str] = set()
        last_stamp, last_raw = roster.stamp, roster.source_raw_id
        true_count = roster.true_count
        walled: str | None = None
        filters = [FILTER_RECENT] + ([FILTER_ADMINS, FILTER_BOTS] if joined else [])
        for filter_ in filters:
            try:
                count, last_stamp, last_raw = await self._page(
                    ctx, roster, filter_, enumerated, counts, last_stamp, last_raw
                )
            except SkipAndRecord as exc:
                if filter_ is FILTER_RECENT:
                    walled = str(exc)
                else:
                    ctx.log.warning(
                        "participants: %s skipped for group %s: %s", filter_["_"], group_id, exc
                    )
                    counts["skipped"] += 1
                continue
            if count is not None and filter_ is FILTER_RECENT:
                true_count = count  # an Admins/Bots page's `count` is only its own filter's
        if walled is not None:
            self._record_walled(
                ctx, group_id, walled, true_count, last_stamp, counts, enumerated=len(enumerated)
            )
        else:
            add_roster_snapshot(
                ctx.store, group_id, last_stamp, enumerated=len(enumerated),
                true_count=true_count, reason=None, source_raw_id=last_raw,
            )
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "roster",
            {"group_id": group_id, "enumerated": len(enumerated), "true_count": true_count,
             "walled": walled, "joined": joined},
        )
        counts["enumerated"] += len(enumerated)
        counts["true_count"] += true_count or 0

        partial = walled is not None or (true_count is not None and len(enumerated) < true_count)
        if partial:
            await self._oracle(ctx, roster, enumerated, counts)
        await self._reactions(ctx, roster, counts)
        if not partial or joined:
            return None
        warning = JOIN_SHORTFALL_WARNING.format(
            enumerated=len(enumerated), total=true_count if true_count is not None else "?",
            group_id=group_id,
        )
        ctx.log.warning(warning)
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "warning",
            {"code": "roster_partial", "group_id": group_id, "enumerated": len(enumerated),
             "true_count": true_count, "walled": walled, "hint": "--join"},
        )
        return warning

    async def _maybe_join(self, ctx: CollectContext, roster: _Roster) -> bool:
        """Under `--join`, join a group we are not a member of (`Channel.left`
        true, or membership unknown) through the shared audited path; a
        refused join falls back to the un-joined branch. Never without the
        flag (plan D11)."""
        if not ctx.settings.allow_join:
            return False
        if roster.chan is not None and roster.chan.get("left") is False:
            return True  # already a member: nothing to write
        skip = await join_or_skip(ctx, self.name, roster.group_id, roster.input_channel)
        if skip is not None:
            ctx.log.warning("participants: %s", skip)
            return False
        return True

    async def _page(
        self, ctx: CollectContext, roster: _Roster, filter_: dict, enumerated: set[str],
        counts: dict[str, int], last_stamp: str, last_raw: int | None,
    ) -> tuple[int | None, str, int | None]:
        """Page one filter until the server stops adding members: an empty
        or short page, or a page that adds nothing new (a capped server
        repeats itself). Returns `(count, stamp, raw_id)` of the last page."""
        offset = 0
        count: int | None = None
        while True:
            page = await ctx.gateway.get_participants(
                roster.input_channel, filter_, offset, _PAGE_SIZE, 0
            )
            last_stamp = ctx.clock.for_payload(page)
            last_raw = ctx.store.add_raw(
                namespaced_kind("channels", page, "ChannelParticipants"), page, ctx.tier,
                {"channel_id": roster.group_id, "filter": filter_["_"], "offset": offset},
                observed_at=last_stamp,
            )
            if (page.get("_") or "").lower().endswith("notmodified"):
                break
            if page.get("count") is not None:
                count = page["count"]
            new = self._project_page(
                ctx, roster.group_id, page, last_raw, last_stamp, enumerated, counts
            )
            got = len(page.get("participants") or [])
            if got == 0 or new == 0 or got < _PAGE_SIZE:
                break
            offset += got
        return count, last_stamp, last_raw

    def _project_page(
        self, ctx: CollectContext, group_id: int, page: dict, raw_id: int, stamp: str,
        enumerated: set[str], counts: dict[str, int],
    ) -> int:
        """Spec §6.5 for one page: participants rows + snapshots, member_of/
        admin_of edges, and the free full `User` objects. Returns how many
        participants were NEW to this run's enumerated set."""
        self._project_users_vector(ctx, page, raw_id, stamp, counts)
        for chat in page.get("chats") or []:
            upsert_peer(ctx.store, chat, raw_id, stamp, seen_in_chat=None, seen_in_msg=None)
        new = 0
        for participant in page.get("participants") or []:
            facts = upsert_participant(ctx.store, group_id, participant, raw_id, stamp)
            if facts is None:
                continue
            counts["participants"] += 1
            if facts.uri not in enumerated:
                enumerated.add(facts.uri)
                new += 1
            add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
            counts["edges"] += membership_edges(
                ctx.store, group_id, facts, stamp, ctx.tier, raw_id,
                {"source": "roster", "status": facts.status},
            )
        return new

    def _project_users_vector(
        self, ctx: CollectContext, envelope: dict, raw_id: int, stamp: str, counts: dict[str, int]
    ) -> None:
        """Roster RPCs enrich DURING discovery (spec §3): every full `User` in
        the response's `users` vector lands in `users` (+ snapshot) and
        `peers` (provenance preserved) for free."""
        for user in envelope.get("users") or []:
            if (user.get("_") or "").lower() != "user":
                continue
            upsert_full_peer(ctx.store, user, raw_id, stamp)
            uri = upsert_user(ctx.store, user, raw_id, stamp, ctx.tier)
            if uri is None:
                continue
            counts["users"] += 1
            add_user_snapshot(
                ctx.store, uri, stamp, ctx.tier, METHOD_GET_PARTICIPANTS,
                {"user": target_user_facts(user)}, raw_id,
            )

    async def _oracle(
        self, ctx: CollectContext, roster: _Roster, enumerated: set[str], counts: dict[str, int]
    ) -> None:
        return None  # Task 9

    async def _reactions(
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]
    ) -> None:
        return None  # Task 9
