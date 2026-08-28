"""Roster projection: current membership facts (`participants`) + append-only
observations (`participant_snapshots`), the membership edges, and the
zero-RPC join/leave service-message vector (spec §6.2, §8).

`join_date` is stored only where the constructor's `date` MEANS "joined"
(`channelParticipant`, `Self`, `Admin` — research §1.6); `Creator` carries no
date and `Banned.date` is the BAN date, which stays in raw. Membership facts
are newest-observation-wins keyed `(group_id, uri)`, except that a known
`join_date` is never blanked by a later observation that lacks one (a member
who is later banned still joined when they joined). `inviter_id` comes from
`channelParticipantSelf`/`channelParticipantAdmin(is_self)` on the roster
path, and from `messageActionChatJoinedByLink.inviter_id` on the
service-message path (the link creator is itself an observed fact there,
for an arbitrary joiner, not just self) — never otherwise inferred.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from paperboy.ids import channel_uri, iso_or_none, peer_ref_uri, user_uri
from paperboy.store.db import Store
from paperboy.store.edges import add_edge_once
from paperboy.store.sync import is_self

PARTICIPANT_STATUSES = ("member", "admin", "creator", "banned", "left")
MEMBER_STATUSES = ("member", "admin", "creator")
_ADMIN_STATUSES = ("admin", "creator")
MEMBER_OF = "member_of"
ADMIN_OF = "admin_of"
INVITED_BY = "invited_by"
ADDED_BY = "added_by"


@dataclass(frozen=True)
class ParticipantFacts:
    uri: str
    status: str
    join_date: str | None
    rank: str | None
    subscription_until_date: str | None
    inviter_id: int | None


def participant_row(participant: dict) -> ParticipantFacts | None:
    """Parse one `channelParticipant*` dict (Telethon `to_dict()`, PascalCase
    `_`, matched case-insensitively) into the facts we store; `None` for an
    unknown constructor — never guessed at."""
    kind = (participant.get("_") or "").lower()
    rank = participant.get("rank") or None
    sub = iso_or_none(participant.get("subscription_until_date"))
    if kind == "channelparticipant":
        return ParticipantFacts(
            user_uri(participant["user_id"]), "member", iso_or_none(participant.get("date")),
            rank, sub, None,
        )
    if kind == "channelparticipantself":
        return ParticipantFacts(
            user_uri(participant["user_id"]), "member", iso_or_none(participant.get("date")),
            rank, sub, participant.get("inviter_id"),
        )
    if kind == "channelparticipantcreator":
        return ParticipantFacts(user_uri(participant["user_id"]), "creator", None, rank, None, None)
    if kind == "channelparticipantadmin":
        # `inviter_id` shares flag bit 1 with `self` — only ever set for us.
        inviter = participant.get("inviter_id") if participant.get("is_self") else None
        return ParticipantFacts(
            user_uri(participant["user_id"]), "admin", iso_or_none(participant.get("date")),
            rank, None, inviter,
        )
    if kind in ("channelparticipantbanned", "channelparticipantleft"):
        uri = peer_ref_uri(participant.get("peer"))
        if uri is None:
            return None
        if kind == "channelparticipantbanned":
            # `left` is the membership truth: left=true was kicked out; left
            # unset means RESTRICTED but still a member (schema
            # `channelParticipantBanned` `left:flags.0?true` — research
            # sources/mtproto-participants-users.md:238).
            status = "banned" if participant.get("left") else "member"
        else:
            status = "left"
        # `rank` rides on `channelParticipantBanned` for restricted-but-present
        # members too (it is on the wire either way); `Left` has no rank field.
        return ParticipantFacts(uri, status, None, rank, None, None)
    return None


def write_participant(
    store: Store, group_id: int, facts: ParticipantFacts, source_raw_id: int, observed_at: str
) -> str | None:
    if is_self(store, facts.uri):
        return None
    store.conn.execute(
        """
        INSERT INTO participants (
            group_id, uri, status, join_date, rank, subscription_until_date, inviter_id,
            source_raw_id, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, uri) DO UPDATE SET
            -- newest-observation-wins (ADR-0005 §6, recency only: no `min`
            -- concept here); a known join_date survives an observation
            -- without one (Creator, Banned, Left, USER_NOT_PARTICIPANT).
            status = CASE WHEN excluded.last_seen >= participants.last_seen
                          THEN excluded.status ELSE participants.status END,
            join_date = CASE WHEN excluded.last_seen >= participants.last_seen
                             THEN COALESCE(excluded.join_date, participants.join_date)
                             ELSE COALESCE(participants.join_date, excluded.join_date) END,
            rank = CASE WHEN excluded.last_seen >= participants.last_seen
                        THEN excluded.rank ELSE participants.rank END,
            subscription_until_date = CASE WHEN excluded.last_seen >= participants.last_seen
                                           THEN excluded.subscription_until_date
                                           ELSE participants.subscription_until_date END,
            inviter_id = CASE WHEN excluded.last_seen >= participants.last_seen
                              THEN COALESCE(excluded.inviter_id, participants.inviter_id)
                              ELSE COALESCE(participants.inviter_id, excluded.inviter_id) END,
            source_raw_id = CASE WHEN excluded.last_seen >= participants.last_seen
                                 THEN excluded.source_raw_id ELSE participants.source_raw_id END,
            first_seen = MIN(participants.first_seen, excluded.first_seen),
            last_seen = MAX(participants.last_seen, excluded.last_seen)
        """,
        (
            group_id, facts.uri, facts.status, facts.join_date, facts.rank,
            facts.subscription_until_date, facts.inviter_id, source_raw_id,
            observed_at, observed_at,
        ),
    )
    return facts.uri


def upsert_participant(
    store: Store, group_id: int, participant: dict, source_raw_id: int, observed_at: str
) -> ParticipantFacts | None:
    facts = participant_row(participant)
    if facts is None:
        return None
    return facts if write_participant(store, group_id, facts, source_raw_id, observed_at) else None


def add_participant_snapshot(
    store: Store,
    group_id: int,
    facts: ParticipantFacts,
    observed_at: str,
    source_raw_id: int | None,
    *,
    once: bool = False,
) -> bool:
    """Append one membership observation. `once=True` is for producers that
    re-scan stored rows every run (service messages): the same observation
    (same fact, same stamp) is never appended twice.

    The dedupe key is the observation's identity — `(group_id, uri,
    observed_at, status)` — deliberately NOT `source_raw_id`: that id comes
    from `messages.source_raw_id`, which `upsert_message`'s ON CONFLICT
    overwrites with the newest raw id on every re-observation of the same
    message (e.g. `HistoryCollector` re-paging the tail every run), so a
    service message's `source_raw_id` is not stable across runs even though
    the fact it recorded is unchanged.
    """
    if once:
        dup = store.conn.execute(
            "SELECT 1 FROM participant_snapshots WHERE group_id=? AND uri=? AND observed_at=? "
            "AND status IS ? LIMIT 1",
            (group_id, facts.uri, observed_at, facts.status),
        ).fetchone()
        if dup is not None:
            return False
    store.conn.execute(
        "INSERT INTO participant_snapshots (group_id, observed_at, uri, status, join_date, rank, "
        "subscription_until_date, source_raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id, observed_at, facts.uri, facts.status, facts.join_date, facts.rank,
            facts.subscription_until_date, source_raw_id,
        ),
    )
    return True


def add_roster_snapshot(
    store: Store,
    group_id: int,
    observed_at: str,
    *,
    enumerated: int,
    true_count: int | None,
    reason: str | None,
    source_raw_id: int | None,
) -> None:
    """The roster-level accounting row (spec §6.3): `enumerated / true_count`
    for this run, and `reason` when the roster was walled. A shortfall is
    never presented as completeness — it is stored as exactly that."""
    store.conn.execute(
        "INSERT INTO participant_snapshots (group_id, observed_at, uri, enumerated, true_count, "
        "reason, source_raw_id) VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (group_id, observed_at, enumerated, true_count, reason, source_raw_id),
    )


def membership_edges(
    store: Store,
    group_id: int,
    facts: ParticipantFacts,
    observed_at: str,
    tier: str,
    source_raw_id: int | None,
    evidence: dict,
) -> int:
    if facts.status not in MEMBER_STATUSES:
        return 0
    group = channel_uri(group_id)
    written = 0
    if add_edge_once(
        store, facts.uri, MEMBER_OF, group, observed_at, tier, source_raw_id, evidence
    ):
        written += 1
    if facts.status in _ADMIN_STATUSES and add_edge_once(
        store, facts.uri, ADMIN_OF, group, observed_at, tier, source_raw_id, evidence
    ):
        written += 1
    return written


def project_join_service_messages(store: Store, group_id: int, tier: str) -> dict[str, int]:
    """Membership + invite facts from join/leave service messages already in
    the captured history — zero RPC (spec §8). Partial by nature (silent
    joins leave no trace; channel subscriptions never emit one), so every
    edge is evidenced `source: service_message` and the roster remains the
    authority: each fact is stamped with the MESSAGE date (when it was true),
    so a later roster observation of the same member correctly wins.
    Idempotent — re-scans every run, and the returned `joins`/`leaves` counts
    (like `edges`) count only NEW facts recorded THIS call: `write_participant`
    always upserts (it has no "was this new" signal of its own — every row is
    re-observed on every scan), so `joins`/`leaves` are gated on
    `add_participant_snapshot`'s `once=True` return, which is the one signal
    that actually distinguishes a fact seen for the first time from a
    re-scan of history already reflected in the store — this is what keeps a
    stable channel's `run_events` (Task 8's `service_joins`/`service_leaves`)
    from reporting the same N joins as "new" on every single run forever."""
    counts = {"joins": 0, "leaves": 0, "edges": 0}
    rows = store.conn.execute(
        "SELECT uri, msg_id, from_uri, date, action_json, source_raw_id, first_seen FROM messages "
        "WHERE channel_id=? AND is_service=1 AND action_json IS NOT NULL ORDER BY msg_id",
        (group_id,),
    ).fetchall()
    for row in rows:
        action = json.loads(row["action_json"])
        kind = (action.get("_") or "").lower()
        observed_at = row["date"] or row["first_seen"]
        evidence = {"source": "service_message", "msg_uri": row["uri"]}
        joined: list[tuple[str, int | None]] = []  # (uri, inviter_id)
        if kind == "messageactionchatadduser":
            for user_id in action.get("users") or []:
                joined.append((user_uri(user_id), None))
        elif kind == "messageactionchatjoinedbylink" and row["from_uri"]:
            joined.append((row["from_uri"], action.get("inviter_id")))
        elif kind == "messageactionchatjoinedbyrequest" and row["from_uri"]:
            joined.append((row["from_uri"], None))
        elif kind == "messageactionchatdeleteuser" and action.get("user_id") is not None:
            facts = ParticipantFacts(user_uri(action["user_id"]), "left", None, None, None, None)
            if write_participant(store, group_id, facts, row["source_raw_id"], observed_at) is None:
                continue
            if add_participant_snapshot(
                store, group_id, facts, observed_at, row["source_raw_id"], once=True
            ):
                counts["leaves"] += 1
            continue
        else:
            continue
        for uri, inviter_id in joined:
            facts = ParticipantFacts(uri, "member", observed_at, None, None, inviter_id)
            if write_participant(store, group_id, facts, row["source_raw_id"], observed_at) is None:
                continue
            if add_participant_snapshot(
                store, group_id, facts, observed_at, row["source_raw_id"], once=True
            ):
                counts["joins"] += 1
            counts["edges"] += membership_edges(
                store, group_id, facts, observed_at, tier, row["source_raw_id"], evidence
            )
            # elif (not `or`): ADDED_BY is only ever attempted for
            # messageactionchatadduser, whose `joined` entries always carry
            # inviter_id=None, so INVITED_BY never fires there — but keep the
            # explicit elif so that invariant isn't required to read this correctly.
            if inviter_id is not None and add_edge_once(  # noqa: SIM114
                store, uri, INVITED_BY, user_uri(inviter_id), observed_at, tier,
                row["source_raw_id"], evidence,
            ):
                counts["edges"] += 1
            elif (
                kind == "messageactionchatadduser"
                and row["from_uri"] and row["from_uri"] != uri
                and add_edge_once(
                    store, uri, ADDED_BY, row["from_uri"], observed_at, tier,
                    row["source_raw_id"], evidence,
                )
            ):
                counts["edges"] += 1
    return counts
