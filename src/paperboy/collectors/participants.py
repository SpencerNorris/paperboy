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
from paperboy.ids import iso_or_none, msg_uri, namespaced_kind, peer_ref_uri, peer_stub
from paperboy.store.channels import channel_flags, upsert_channel
from paperboy.store.edges import add_edge_once
from paperboy.store.events import record_run_event
from paperboy.store.message_peers import backfill_message_referenced_peers
from paperboy.store.participants import (
    ParticipantFacts,
    add_participant_snapshot,
    add_roster_snapshot,
    membership_edges,
    participant_row,
    project_join_service_messages,
    upsert_participant,
    write_participant,
)
from paperboy.store.peers import input_user_ref, upsert_full_peer, upsert_peer
from paperboy.store.reactions import (
    REACTED_TO,
    backfill_recent_reactions,
    fetched_reaction_lists,
    reacted_message_ids,
)
from paperboy.store.sync import is_self
from paperboy.store.users import add_user_snapshot, target_user_facts, upsert_user
from paperboy.targets import Target

_PAGE_SIZE = 200  # Telegram's page size, not a total cap (spec §6.3)
_REACTIONS_PAGE = 100
# Hard per-message cap on `messages.getMessageReactionsList` pages: unlike
# `_page` (three independent stop conditions), a `next_offset` that never
# falls falsy has nothing else bounding it — a server that repeats a
# non-empty offset would otherwise spin forever, growing the DB unbounded.
_REACTIONS_MAX_PAGES = 50
METHOD_GET_PARTICIPANTS = "channels.getParticipants"
METHOD_GET_PARTICIPANT = "channels.getParticipant"
METHOD_REACTIONS = "messages.getMessageReactionsList"
METHOD_GET_FULL_CHANNEL = "channels.getFullChannel"
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

_REASON_BROADCAST = (
    "broadcast_channel: subscriber roster is never enumerable below admin "
    "(CHAT_ADMIN_REQUIRED for every filter)"
)
_REASON_HIDDEN = "participants_hidden: roster not viewable"


def _roster_wall_reason(flags: dict) -> str | None:
    """spec §6.1/§6.2: a roster that is structurally never enumerable below
    admin — a BROADCAST peer's subscriber list (`channelFull.linked_chat_id`
    is bidirectional: a group's own link points back at its broadcast, not
    only the broadcast->group direction — research
    sources/mtproto-channel-messages.md:154), or a group whose owner has
    hidden participants (`participants_hidden` / `can_view_participants`) —
    walls with ZERO enumeration RPC. Shared by the target's own roster and
    the linked group's preflight so both paths agree on one check."""
    if flags.get("broadcast") and not flags.get("megagroup"):
        return _REASON_BROADCAST
    if flags.get("participants_hidden") or flags.get("can_view_participants") is False:
        return _REASON_HIDDEN
    return None


@dataclass
class _Roster:
    """One enumerable group: the linked discussion group, or the target itself
    when it is a supergroup — never a broadcast peer or a hidden roster, both
    walled by `_roster_wall_reason` before construction. `stamp`/
    `source_raw_id` are the ChatFull observation that established the flags —
    every zero-RPC row derived from this roster is stamped from them, never
    from "now" (plan D5)."""

    group_id: int
    input_channel: dict
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
                ctx, ctx.channel_id, _REASON_BROADCAST, chan["participants_count"], run_stamp,
                counts,
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
        # when the session gate below refuses enumeration. The TARGET's own
        # id is swept unconditionally (its posts still carry a free
        # `recent_reactions` sample even when the target is a broadcast whose
        # subscriber roster and RPC-based reaction-list vector are both
        # walled) — only the RPC-based roster/oracle/reaction vectors below
        # stay GROUP-only.
        zero_rpc_ids = [ctx.channel_id] + ([] if isinstance(linked, str) else [linked[0]])
        for group_id in zero_rpc_ids:
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
        stopped: list[str] = []
        if target_is_group:
            target_flags = json.loads(chan["flags_json"] or "{}")
            target_wall = _roster_wall_reason(target_flags)
            if target_wall is not None:
                # §6.1/§6.2: a megagroup TARGET can be just as structurally
                # walled (hidden participants) as a broadcast — recorded with
                # zero enumeration RPC exactly like the linked group's own
                # preflight wall, rather than discovering it the expensive
                # way via a `CHAT_ADMIN_REQUIRED` on the first page.
                self._record_walled(
                    ctx, ctx.channel_id, target_wall, chan["participants_count"], run_stamp,
                    counts,
                )
            else:
                rosters.append(_Roster(
                    ctx.channel_id, ctx.input_channel,
                    chan["participants_count"], run_stamp, chan["source_raw_id"], None,
                ))
        if not isinstance(linked, str):
            group_id, input_channel, needs_join = linked
            proceed = True
            if needs_join:
                # `join_to_send` is honoured the same way `discussion` treats
                # it for the SAME group (its docstring: reading it then
                # requires membership) — a roster read is not exempt from
                # that just because it is not a message read.
                skip = await join_or_skip(ctx, self.name, group_id, input_channel)
                if skip is not None:
                    ctx.log.warning("participants: %s", skip)
                    stopped.append(skip)
                    proceed = False
            if proceed:
                roster = await self._preflight_group(
                    ctx, group_id, input_channel, run_stamp, counts,
                )
                if roster is not None:
                    rosters.append(roster)

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
        GROUP's channelFull, which no phase has fetched before — a hidden
        roster, or a peer that turns out to be a BROADCAST (`linked_chat_id`
        is bidirectional: a discussion group's own link points back at its
        broadcast), walls with zero further RPC via `_roster_wall_reason`.
        Also projects the group into `channels` (+ snapshot) and its vectors
        into peers."""
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
        self._project_users_vector(
            ctx, full, raw_id, observed_at, counts, METHOD_GET_FULL_CHANNEL,
        )
        flags = channel_flags(full_chat, chan or {})
        wall = _roster_wall_reason(flags)
        if wall is not None:
            self._record_walled(
                ctx, group_id, wall, full_chat.get("participants_count"), observed_at, counts,
            )
            return None
        return _Roster(
            group_id, input_channel, full_chat.get("participants_count"), observed_at, raw_id,
            chan,
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
        self_seen = False
        last_stamp, last_raw = roster.stamp, roster.source_raw_id
        true_count = roster.true_count
        walled: str | None = None
        filters = [FILTER_RECENT] + ([FILTER_ADMINS, FILTER_BOTS] if joined else [])
        for filter_ in filters:
            try:
                count, last_stamp, last_raw, page_self_seen = await self._page(
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
            self_seen = self_seen or page_self_seen
            if count is not None and filter_ is FILTER_RECENT:
                true_count = count  # an Admins/Bots page's `count` is only its own filter's
        # `roster_enumerated`: what THIS run's roster PAGES alone found —
        # captured now, before the oracle mutates `enumerated` below, and
        # used for every roster-page-scoped consumer (the walled/snapshot
        # row, the `roster` event, the shortfall predicate, its warning text
        # and its event) so all of them agree on one number (round-2 review:
        # the predicate and the rendered warning previously read the set at
        # two different points and could disagree). Self is never written to
        # `participants` (issue #12) so `write_participant` drops it from
        # `enumerated`, but Telegram's own `true_count` DOES include self
        # when self is a member — added back in here, or a completely
        # enumerated roster reports a permanent phantom shortfall of
        # exactly 1 on every run after any --join.
        roster_enumerated = len(enumerated) + (1 if self_seen else 0)
        if walled is not None:
            self._record_walled(
                ctx, group_id, walled, true_count, last_stamp, counts,
                enumerated=roster_enumerated,
            )
        else:
            add_roster_snapshot(
                ctx.store, group_id, last_stamp, enumerated=roster_enumerated,
                true_count=true_count, reason=None, source_raw_id=last_raw,
            )
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "roster",
            {"group_id": group_id, "enumerated": roster_enumerated, "true_count": true_count,
             "walled": walled, "joined": joined},
        )
        counts["true_count"] += true_count or 0

        partial = (
            walled is not None or (true_count is not None and roster_enumerated < true_count)
        )
        if partial:
            await self._oracle(ctx, roster, enumerated, counts)
        await self._reactions(ctx, roster, counts)
        # A positive oracle answer adds the SAME kind of confirmed-member row
        # (`participants`, `member_of`) as a roster page, so it counts
        # toward the RUN's total too — deliberately NOT toward
        # `roster_enumerated` above, which stays roster-page-only (spec
        # §6.3: an accounting row for the roster RPC alone).
        counts["enumerated"] += len(enumerated)
        if not partial or joined:
            return None
        warning = JOIN_SHORTFALL_WARNING.format(
            enumerated=roster_enumerated, total=true_count if true_count is not None else "?",
            group_id=group_id,
        )
        ctx.log.warning(warning)
        record_run_event(
            ctx.store, ctx.channel_id, self.name, "warning",
            {"code": "roster_partial", "group_id": group_id, "enumerated": roster_enumerated,
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
    ) -> tuple[int | None, str, int | None, bool]:
        """Page one filter until the server stops adding members: an empty
        or short page, or a page that adds nothing new (a capped server
        repeats itself). Returns `(count, stamp, raw_id, self_seen)` — the
        last page's, except `self_seen`, which is True if ANY page across
        this filter carried the collecting account as a participant."""
        offset = 0
        count: int | None = None
        self_seen = False
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
            new, page_self_seen = self._project_page(
                ctx, roster.group_id, page, last_raw, last_stamp, enumerated, counts
            )
            self_seen = self_seen or page_self_seen
            got = len(page.get("participants") or [])
            if got == 0 or new == 0 or got < _PAGE_SIZE:
                break
            offset += got
        return count, last_stamp, last_raw, self_seen

    def _project_page(
        self, ctx: CollectContext, group_id: int, page: dict, raw_id: int, stamp: str,
        enumerated: set[str], counts: dict[str, int],
    ) -> tuple[int, bool]:
        """Spec §6.5 for one page: participants rows + snapshots, member_of/
        admin_of edges, and the free full `User` objects. Returns
        `(new, self_seen)`: how many participants were NEW to this run's
        `enumerated` set, and whether the collecting account itself appeared
        as a participant on this page — `write_participant` refuses to store
        self (issue #12), so that fact would otherwise vanish rather than
        being reported back to the caller, which needs it to reconcile
        against Telegram's own `count` (spec-6.4 boundary; round-2 review)."""
        self._project_users_vector(ctx, page, raw_id, stamp, counts, METHOD_GET_PARTICIPANTS)
        for chat in page.get("chats") or []:
            upsert_peer(ctx.store, chat, raw_id, stamp, seen_in_chat=None, seen_in_msg=None)
        new = 0
        self_seen = False
        for participant in page.get("participants") or []:
            facts = participant_row(participant)
            if facts is None:
                continue  # unknown constructor — never guessed at
            if is_self(ctx.store, facts.uri):
                self_seen = True
                continue
            if write_participant(ctx.store, group_id, facts, raw_id, stamp) is None:
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
        return new, self_seen

    def _project_users_vector(
        self, ctx: CollectContext, envelope: dict, raw_id: int, stamp: str, counts: dict[str, int],
        method: str,
    ) -> None:
        """Roster RPCs enrich DURING discovery (spec §3): every full `User` in
        the response's `users` vector lands in `users` (+ snapshot) and
        `peers` (provenance preserved) for free. `method` is the RPC that
        produced `envelope` — `user_snapshots.method` is both provenance and
        the dedupe partition (`add_user_snapshot` keys on `(uri, method)`),
        so each of the four call sites (the group preflight, roster pages,
        the oracle, reaction lists) must pass its own, not share one
        hard-coded value."""
        for user in envelope.get("users") or []:
            if (user.get("_") or "").lower() != "user":
                continue
            upsert_full_peer(ctx.store, user, raw_id, stamp)
            uri = upsert_user(ctx.store, user, raw_id, stamp, ctx.tier)
            if uri is None:
                continue
            counts["users"] += 1
            add_user_snapshot(
                ctx.store, uri, stamp, ctx.tier, method, {"user": target_user_facts(user)}, raw_id,
            )

    async def _oracle(
        self, ctx: CollectContext, roster: _Roster, enumerated: set[str], counts: dict[str, int]
    ) -> None:
        """`channels.getParticipant` for users REFERENCED in the group (message
        authors, provenance) that a partial/walled roster did not cover and
        that have no answer yet — bounded by `participant_oracle_budget`
        (plan D9), never one call per known commenter. Confirmed un-joined /
        non-admin on the group (spec §13); a `USER_NOT_PARTICIPANT` answer is a
        definitive negative and is stored as such."""
        budget = ctx.settings.participant_oracle_budget
        if budget <= 0:
            return
        group_id = roster.group_id
        rows = ctx.store.conn.execute(
            """
            SELECT DISTINCT uri FROM (
                SELECT from_uri AS uri FROM messages
                WHERE channel_id = ? AND from_uri LIKE 'tg:user:%'
                UNION
                SELECT uri FROM peers WHERE kind = 'user' AND seen_in_chat = ?
            )
            WHERE uri NOT IN (SELECT uri FROM participants WHERE group_id = ?)
            ORDER BY uri
            """,
            (group_id, group_id, group_id),
        ).fetchall()
        # Resolve to an `input_user_ref` and drop the budget onto THAT
        # filtered list, not the raw uri list: slicing first (as before)
        # let an unresolvable candidate occupy a budget slot forever — the
        # same `ORDER BY uri` query re-selects it in the same position every
        # run, and the oracle never reaches anyone past it.
        candidates: list[tuple[str, dict]] = []
        for r in rows:
            uri = r["uri"]
            if uri in enumerated:
                continue
            ref = input_user_ref(ctx.store, uri)
            if ref is None:
                continue
            candidates.append((uri, ref))
            if len(candidates) >= budget:
                break
        for uri, ref in candidates:
            try:
                answer = await ctx.gateway.get_participant(roster.input_channel, ref)
            except SkipAndRecord as exc:
                # CHAT_ADMIN_REQUIRED here is the wall itself, not a per-user
                # condition — stop asking this group.
                ctx.log.warning("participants: oracle walled on group %s: %s", group_id, exc)
                counts["skipped"] += 1
                return
            counts["oracle"] += 1
            if answer is None:
                payload = {"_": "UserNotParticipant", "user_id": ref["user_id"]}
                stamp = ctx.clock.for_payload(payload)
                raw_id = ctx.store.add_raw(
                    "UserNotParticipant", payload, ctx.tier,
                    {"channel_id": group_id, "user_id": ref["user_id"]}, observed_at=stamp,
                )
                facts = ParticipantFacts(uri, "left", None, None, None, None)
                if write_participant(ctx.store, group_id, facts, raw_id, stamp):
                    add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
                continue
            stamp = ctx.clock.for_payload(answer)
            raw_id = ctx.store.add_raw(
                namespaced_kind("channels", answer, "ChannelParticipant"), answer, ctx.tier,
                {"channel_id": group_id, "user_id": ref["user_id"]}, observed_at=stamp,
            )
            self._project_users_vector(ctx, answer, raw_id, stamp, counts, METHOD_GET_PARTICIPANT)
            facts = upsert_participant(
                ctx.store, group_id, answer.get("participant") or {}, raw_id, stamp
            )
            if facts is None:
                continue
            counts["participants"] += 1
            enumerated.add(facts.uri)
            add_participant_snapshot(ctx.store, group_id, facts, stamp, raw_id)
            counts["edges"] += membership_edges(
                ctx.store, group_id, facts, stamp, ctx.tier, raw_id,
                {"source": "oracle", "status": facts.status},
            )

    async def _reactions(
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int]
    ) -> None:
        """`messages.getMessageReactionsList` on reacted GROUP messages —
        newest first, bounded by `participant_reactions_budget`, resumable
        (the done-set is derived from the raw log). Reactors get a
        `reacted_to` edge, a `users` row (the response's `users` vector) and
        a `peers` row with the message as provenance. The first wall
        (`BROADCAST_FORBIDDEN` / `CHAT_ADMIN_REQUIRED`) ends the vector."""
        budget = ctx.settings.participant_reactions_budget
        if budget <= 0:
            return
        group_id = roster.group_id
        done = fetched_reaction_lists(ctx.store, group_id)
        candidates = [m for m in reacted_message_ids(ctx.store, group_id) if m not in done][:budget]
        for msg_id in candidates:
            offset: str | None = None
            for _ in range(_REACTIONS_MAX_PAGES):
                try:
                    result = await ctx.gateway.get_message_reactions_list(
                        roster.input_channel, msg_id, offset=offset, limit=_REACTIONS_PAGE
                    )
                except SkipAndRecord as exc:
                    ctx.log.warning(
                        "participants: reaction lists skipped for group %s: %s", group_id, exc
                    )
                    counts["skipped"] += 1
                    return
                stamp = ctx.clock.for_payload(result)
                raw_id = ctx.store.add_raw(
                    namespaced_kind("messages", result, "MessageReactionsList"), result, ctx.tier,
                    {"channel_id": group_id, "msg_id": msg_id, "offset": offset or ""},
                    observed_at=stamp,
                )
                counts["reaction_lists"] += 1
                self._project_users_vector(ctx, result, raw_id, stamp, counts, METHOD_REACTIONS)
                for reaction in result.get("reactions") or []:
                    subject = peer_ref_uri(reaction.get("peer_id"))
                    stub = peer_stub(reaction.get("peer_id"))
                    if subject is None or stub is None:
                        continue
                    # Fill-only provenance, mirroring the zero-RPC vector's
                    # own rule (store/reactions.py): a reaction is not a
                    # documented `inputPeerFromMessage` context (research
                    # §8.7 lists author, forward header and mention), so it
                    # must never replace stronger provenance a message
                    # authorship already recorded for this same peer.
                    known = ctx.store.conn.execute(
                        "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (subject,)
                    ).fetchone()
                    known_chat = known["seen_in_chat"] if known is not None else None
                    known_msg = known["seen_in_msg"] if known is not None else None
                    fill = known_chat is None or known_msg is None
                    if upsert_peer(
                        ctx.store, stub, raw_id, stamp,
                        seen_in_chat=group_id if fill else known_chat,
                        seen_in_msg=msg_id if fill else known_msg,
                    ) is None:
                        continue  # the collecting account reacted (#12)
                    if add_edge_once(
                        ctx.store, subject, REACTED_TO, msg_uri(group_id, msg_id), stamp, ctx.tier,
                        raw_id,
                        {"source": "reactions_list", "reaction": reaction.get("reaction"),
                         "date": iso_or_none(reaction.get("date"))},
                    ):
                        counts["edges"] += 1
                offset = result.get("next_offset")
                if not offset:
                    break
            else:
                ctx.log.warning(
                    "participants: reaction list for group %s message %s exceeded %d pages "
                    "without ending — stopping this message",
                    group_id, msg_id, _REACTIONS_MAX_PAGES,
                )
