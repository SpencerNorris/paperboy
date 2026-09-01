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
from paperboy.ids import channel_uri, iso_or_none, msg_uri, namespaced_kind, peer_ref_uri, peer_stub
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
    reaction_resume_offsets,
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


def _roster_wall_reason(flags: dict) -> tuple[str, bool] | None:
    """spec §6.1/§6.2: a roster whose BULK enumeration is walled below admin,
    as `(reason, terminal)`. `terminal=True` for a BROADCAST peer's
    subscriber list (`channelFull.linked_chat_id` is bidirectional: a group's
    own link points back at its broadcast — research
    sources/mtproto-channel-messages.md:154): the per-user oracle is
    `CHAT_ADMIN_REQUIRED` there too (spec §13), so there is nothing left to
    do and no `_Roster` is built. `terminal=False` for a group whose owner
    hid participants (`participants_hidden` / `can_view_participants` false):
    ONLY the bulk `getParticipants` sweep is walled — the `getParticipant`
    oracle and the reaction-list vector are exactly the designated fallback
    for a hidden-member group (spec §5/§6.2, plan D9, research §1.8), so a
    `_Roster` IS built (carrying `bulk_walled`) and `_enumerate` runs those
    vectors without paging. Shared by the target's own roster and the linked
    group's preflight so both paths agree on one check."""
    if flags.get("broadcast") and not flags.get("megagroup"):
        return (_REASON_BROADCAST, True)
    if flags.get("participants_hidden") or flags.get("can_view_participants") is False:
        return (_REASON_HIDDEN, False)
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
    # The group's stored boolean flags (`store.channels.channel_flags`): for
    # the linked group, from this run's preflight `ChatFull`; for a megagroup
    # TARGET, from the `channel` phase's own row. `left` is what `--join`
    # reads — `False` means we are already a member and nothing is written.
    flags: dict
    # Set once `collect` has already joined this group this run (the
    # `join_to_send` branch): `_maybe_join` must not join it a SECOND time.
    prejoined: bool = False
    # The bulk `getParticipants` wall reason for a hidden-member group, if
    # any: `_enumerate` skips paging and runs the oracle/reaction fallback.
    bulk_walled: str | None = None


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
        target_flags: dict = {}
        target_wall: tuple[str, bool] | None = None

        if not target_is_group:
            # §6.2: the broadcast channel's OWN subscriber roster is never
            # enumerable — skip IT (recorded, zero RPC), not the collector.
            self._record_walled(
                ctx, ctx.channel_id, _REASON_BROADCAST, chan["participants_count"], run_stamp,
                counts,
            )
        else:
            # §6.1: a megagroup TARGET can be just as structurally walled
            # (hidden participants) as a broadcast peer — evaluated here,
            # zero-RPC and on the SAME side of the session gate as the
            # broadcast check above, rather than after it. Both are zero-RPC
            # facts derived from already-stored flags, so a gate-refused run
            # must record either one identically — previously the megagroup
            # check ran only after the gate, so a hidden megagroup target
            # stored no `RosterWalled` row on a gate-refused run while a
            # broadcast target did (round-3 review).
            target_flags = json.loads(chan["flags_json"] or "{}")
            target_wall = _roster_wall_reason(target_flags)
            if target_wall is not None:
                self._record_walled(
                    ctx, ctx.channel_id, target_wall[0], chan["participants_count"], run_stamp,
                    counts,
                )
        linked = linked_group(ctx, self.name)

        # Zero-RPC vectors first — they read only the store, so they run even
        # when the phase stops right below (a broadcast with no linked
        # discussion group still carries a free `recent_reactions` sample on
        # its own posts) or when the session gate further down refuses
        # enumeration. The TARGET's own id is swept unconditionally — only
        # the RPC-based roster/oracle/reaction vectors stay GROUP-only.
        zero_rpc_ids = [ctx.channel_id] + ([] if isinstance(linked, str) else [linked[0]])
        for group_id in zero_rpc_ids:
            self._zero_rpc_vectors(ctx, group_id, counts)

        if isinstance(linked, str):
            if not target_is_group:
                # No comment section => no enumerable roster (§2): the one
                # case that is a FULL phase skip — but the zero-RPC sweep
                # above has already run, so the free vectors are not lost.
                return CollectResult(
                    name=self.name, counts=counts,
                    stopped=f"{linked} — no enumerable roster (a broadcast channel's "
                            "subscribers are never enumerable); zero-RPC vectors still swept",
                )
            ctx.log.info("participants: %s", linked)

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
        if target_is_group and target_wall is None:
            # `target_flags` already carries whatever `left` the `channel`
            # phase observed — pass it through so `_maybe_join` can see
            # known membership for the TARGET exactly as it does for the
            # linked group, instead of re-joining a group we already know
            # we are in on every run (round-3 review).
            rosters.append(_Roster(
                ctx.channel_id, ctx.input_channel,
                chan["participants_count"], run_stamp, chan["source_raw_id"], target_flags,
            ))
        elif target_is_group and target_wall is not None and not target_wall[1]:
            # A HIDDEN megagroup target: bulk enumeration is walled (recorded
            # above, before the gate) but the oracle + reaction-list vectors
            # are the designated fallback — build a roster so `_enumerate`
            # runs them without paging (round-4 review).
            rosters.append(_Roster(
                ctx.channel_id, ctx.input_channel,
                chan["participants_count"], run_stamp, chan["source_raw_id"], target_flags,
                bulk_walled=target_wall[0],
            ))
        if not isinstance(linked, str):
            group_id, input_channel, needs_join = linked
            proceed = True
            prejoined = False
            if needs_join:
                # `join_to_send` is honoured the same way `discussion` treats
                # it (reading it then requires membership) — but ONLY when we
                # are not already a member and the peer is a group, not a
                # broadcast. This is the same membership check `_maybe_join`
                # makes for the roster path; without it, this second join
                # site re-joined an already-member group with an active,
                # audited `channels.joinChannel` on every run (round-4
                # review). Membership is read from `channels.flags_json`,
                # written by a PRIOR run's preflight — unknown on the first
                # run, so we join once, then never again.
                stored = self._stored_group_flags(ctx, group_id)
                if stored.get("broadcast") and not stored.get("megagroup"):
                    # A `join_to_send` flag on a BROADCAST linked peer: never
                    # join it — the preflight below walls it (terminal) with
                    # zero further RPC.
                    ctx.log.info(
                        "participants: linked peer %s is a broadcast — not joining", group_id
                    )
                elif self._already_member(ctx, group_id):
                    prejoined = True  # a prior run's preflight recorded left=false
                else:
                    skip = await join_or_skip(ctx, self.name, group_id, input_channel)
                    if skip is not None:
                        ctx.log.warning("participants: %s", skip)
                        stopped.append(skip)
                        proceed = False
                    else:
                        prejoined = True
            if proceed:
                roster = await self._preflight_group(
                    ctx, group_id, input_channel, run_stamp, counts, prejoined=prejoined,
                )
                if roster is not None:
                    rosters.append(roster)

        # `participant_oracle_budget`/`participant_reactions_budget` are
        # documented as per-RUN caps (config.py, plan D8), not per-roster —
        # a target with a linked group produces TWO rosters, so a budget read
        # fresh inside `_oracle`/`_reactions` for each one could spend up to
        # 2x the documented ceiling. One dict, decremented as each roster
        # spends it, keeps the whole run under the documented cap
        # (round-3 review).
        budgets = {
            "oracle": ctx.settings.participant_oracle_budget,
            "reactions": ctx.settings.participant_reactions_budget,
        }
        for roster in rosters:
            reason = await self._enumerate(ctx, roster, counts, budgets)
            if reason:
                stopped.append(reason)
        return CollectResult(name=self.name, counts=counts, stopped="; ".join(stopped) or None)

    # ---- preflight + gate --------------------------------------------------------

    @staticmethod
    def _stored_group_flags(ctx: CollectContext, group_id: int) -> dict:
        """The linked group's stored boolean flags from `peers` (the `channel`
        phase's row) — used BEFORE preflight to refuse joining a broadcast
        peer. `left` is not among `peers._FLAG_KEYS`, so membership is read
        from `channels` (`_already_member`), not here."""
        row = ctx.store.conn.execute(
            "SELECT flags_json FROM peers WHERE uri=?", (channel_uri(group_id),)
        ).fetchone()
        return json.loads(row["flags_json"]) if row and row["flags_json"] else {}

    @staticmethod
    def _already_member(ctx: CollectContext, group_id: int) -> bool:
        """True when a PRIOR run's preflight recorded this group in `channels`
        with `left=false`. Unknown (absent) on the first run, so the first
        `--join`/`join_to_send` join happens once and no run repeats it."""
        row = ctx.store.conn.execute(
            "SELECT flags_json FROM channels WHERE id=?", (group_id,)
        ).fetchone()
        flags = json.loads(row["flags_json"]) if row and row["flags_json"] else {}
        return flags.get("left") is False

    async def _preflight_group(
        self, ctx: CollectContext, group_id: int, input_channel: dict, run_stamp: str,
        counts: dict[str, int], *, prejoined: bool = False,
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
            record_run_event(
                ctx.store, ctx.channel_id, self.name, "preflight_mismatch",
                {"group_id": group_id, "answered_id": full_chat.get("id")},
            )
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
        if chan is None:
            # `broadcast`/`megagroup` live on the `Channel` object, not on
            # `ChannelFull` — with no Channel to read, `_roster_wall_reason`
            # cannot see them, so falling through with an incomplete flag set
            # (the old `chan or {}`) could enumerate a BROADCAST it just
            # never recognised as one. That breaks this module's headline
            # zero-enumeration-RPC guarantee, so treat it as an audited
            # preflight failure instead — the same way an unreadable
            # preflight is recorded above — rather than a silent fall-through
            # (round-3 review).
            self._record_walled(
                ctx, group_id, "preflight: no Channel object in the chats vector",
                full_chat.get("participants_count"), observed_at, counts,
            )
            return None
        flags = channel_flags(full_chat, chan)
        wall = _roster_wall_reason(flags)
        if wall is not None:
            reason, terminal = wall
            self._record_walled(
                ctx, group_id, reason, full_chat.get("participants_count"), observed_at, counts,
            )
            if terminal:
                return None  # a broadcast peer: the oracle is walled too
            # A hidden-member group: bulk paging is walled, but the oracle +
            # reaction-list fallback still run — carry the wall to `_enumerate`.
            return _Roster(
                group_id, input_channel, full_chat.get("participants_count"), observed_at,
                raw_id, flags, prejoined=prejoined, bulk_walled=reason,
            )
        return _Roster(
            group_id, input_channel, full_chat.get("participants_count"), observed_at, raw_id,
            flags, prejoined=prejoined,
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
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int], budgets: dict[str, int]
    ) -> str | None:
        """Page `Recent` (plus `Admins`/`Bots` once joined), record
        `enumerated / true_count`, then run the bounded vectors. Returns the
        §6.4 shortfall warning when the roster came back walled or partial
        and we are not a member — the phase's `stopped` reason. `budgets` is
        the RUN-level oracle/reactions spend, shared and decremented across
        every roster `_enumerate` is called for."""
        group_id = roster.group_id
        counts["rosters"] += 1
        if roster.bulk_walled is not None:
            # Bulk enumeration is walled (participants_hidden) and already
            # recorded at preflight/collect — do NOT re-record it. The oracle
            # and the reaction-list vector are the designated fallback for a
            # hidden-member group (spec §5/§6.2, plan D9): run them, page
            # nothing. `--join` cannot un-hide members for a non-admin, so
            # there is no shortfall escalation to emit.
            await self._oracle(ctx, roster, set(), counts, budgets)
            await self._reactions(ctx, roster, counts, budgets)
            return None
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
            await self._oracle(ctx, roster, enumerated, counts, budgets)
        await self._reactions(ctx, roster, counts, budgets)
        # A positive oracle answer adds the SAME kind of confirmed-member row
        # (`participants`, `member_of`) as a roster page, so it counts
        # toward the RUN's total too — deliberately NOT toward
        # `roster_enumerated` above, which stays roster-page-only (spec
        # §6.3: an accounting row for the roster RPC alone).
        counts["enumerated"] += len(enumerated)
        # `joined` is True only under `--join`; a member reached without the
        # flag (`flags.left is False`, or a `prejoined` join_to_send join)
        # is equally past the point of a `--join` suggestion. Suppress the
        # escalation for any member — the `add_roster_snapshot` shortfall row
        # above still records the gap (round-4 review).
        member = joined or roster.prejoined or roster.flags.get("left") is False
        if not partial or member:
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
        if roster.prejoined:
            return True  # `collect` already joined this group this run
        if not ctx.settings.allow_join:
            return False
        if roster.flags.get("left") is False:
            return True  # already a member (per the stored flags): nothing to write
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
        self, ctx: CollectContext, roster: _Roster, enumerated: set[str], counts: dict[str, int],
        budgets: dict[str, int],
    ) -> None:
        """`channels.getParticipant` for users REFERENCED in the group (message
        authors, provenance) that a partial/walled roster did not cover and
        that have no answer yet — bounded by `participant_oracle_budget`
        (plan D9), never one call per known commenter, and shared across every
        roster in `budgets["oracle"]` so a target+linked-group run cannot
        spend the RUN-level cap twice over (round-3 review). Confirmed
        un-joined / non-admin on the group (spec §13); a `USER_NOT_PARTICIPANT`
        answer is a definitive negative and is stored as such."""
        budget = budgets["oracle"]
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
            budgets["oracle"] -= 1
            try:
                answer = await ctx.gateway.get_participant(roster.input_channel, ref)
            except SkipAndRecord as exc:
                # CHAT_ADMIN_REQUIRED here is the wall itself, not a per-user
                # condition — stop asking this group, and record it (every
                # other wall in this module is a stored fact, round-4 review).
                ctx.log.warning("participants: oracle walled on group %s: %s", group_id, exc)
                counts["skipped"] += 1
                record_run_event(
                    ctx.store, ctx.channel_id, self.name, "oracle_walled",
                    {"group_id": group_id, "reason": str(exc)},
                )
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
        self, ctx: CollectContext, roster: _Roster, counts: dict[str, int], budgets: dict[str, int]
    ) -> None:
        """`messages.getMessageReactionsList` on reacted GROUP messages —
        newest first, bounded by `participant_reactions_budget`, resumable
        (the done-set is derived from the raw log). Reactors get a
        `reacted_to` edge, a `users` row (the response's `users` vector) and
        a `peers` row with the message as provenance. The first wall
        (`BROADCAST_FORBIDDEN` / `CHAT_ADMIN_REQUIRED`) ends the vector.
        Shared across every roster in `budgets["reactions"]` so a
        target+linked-group run cannot spend the RUN-level cap twice over
        (round-3 review)."""
        budget = budgets["reactions"]
        if budget <= 0:
            return
        group_id = roster.group_id
        done = fetched_reaction_lists(ctx.store, group_id)
        resume = reaction_resume_offsets(ctx.store, group_id)
        candidates = [m for m in reacted_message_ids(ctx.store, group_id) if m not in done][:budget]
        for msg_id in candidates:
            budgets["reactions"] -= 1
            # Resume from the last recorded page of a list truncated by the
            # page cap, rather than restarting at page 1 — so a >cap-reactor
            # list CONVERGES over runs instead of re-walking the same head
            # forever (round-4 review). A fresh message has no resume offset.
            offset: str | None = resume.get(msg_id)
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
                # The reactor's rich `User` is in the response's own `users`
                # vector (already projected by `_project_users_vector` above);
                # index it so the provenance write below is never a bare,
                # information-losing `min` stub that would NULL a `min` peer's
                # identity under `upsert_peer`'s recency rule (round-4 review).
                by_id = {
                    u["id"]: u for u in (result.get("users") or []) + (result.get("chats") or [])
                    if isinstance(u.get("id"), int)
                }
                for reaction in result.get("reactions") or []:
                    subject = peer_ref_uri(reaction.get("peer_id"))
                    stub = peer_stub(reaction.get("peer_id"))
                    if subject is None or stub is None:
                        continue
                    # Fill-only provenance, mirroring the zero-RPC vector's
                    # own rule (store/reactions.py): a reaction is not a
                    # documented `inputPeerFromMessage` context (research
                    # §8.7 lists author, forward header and mention). When a
                    # peer already carries provenance, do NOT re-upsert it —
                    # a re-write can only lose data — just guard `#12` and
                    # emit the edge. When it does not, fill it from the
                    # response's rich object (falling back to the stub only
                    # if the reactor is in neither vector).
                    known = ctx.store.conn.execute(
                        "SELECT seen_in_chat, seen_in_msg FROM peers WHERE uri=?", (subject,)
                    ).fetchone()
                    fill = (
                        known is None
                        or known["seen_in_chat"] is None
                        or known["seen_in_msg"] is None
                    )
                    if fill:
                        rich = by_id.get(stub["id"], stub)
                        if upsert_peer(
                            ctx.store, rich, raw_id, stamp,
                            seen_in_chat=group_id, seen_in_msg=msg_id,
                        ) is None:
                            continue  # the collecting account reacted (#12)
                    elif is_self(ctx.store, subject):
                        continue
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
                # A truncated reactor list is a partial observation, exactly
                # the case `add_roster_snapshot` exists to make un-silent for
                # rosters — record it the same way here, and count it as a
                # skip, or the shortfall is invisible in the store AND (via
                # `fetched_reaction_lists`, keyed on message id alone)
                # permanently un-revisited on every future run (round-3
                # review).
                ctx.log.warning(
                    "participants: reaction list for group %s message %s exceeded %d pages "
                    "without ending — stopping this message",
                    group_id, msg_id, _REACTIONS_MAX_PAGES,
                )
                record_run_event(
                    ctx.store, ctx.channel_id, self.name, "warning",
                    {"code": "reaction_list_truncated", "group_id": group_id, "msg_id": msg_id,
                     "pages": _REACTIONS_MAX_PAGES},
                )
                counts["skipped"] += 1
