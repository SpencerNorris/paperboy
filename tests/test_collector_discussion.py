"""The `discussion` collector: linked-group sweep, comment→post mapping, edges."""

# `paperboy.collectors.discussion` does not exist yet (Task 3), so ruff's
# isort currently classifies it third-party and sorts it next to `pytest`
# below. Once Task 3 lands, that same module becomes first-party and isort
# will want it moved into the `paperboy.*` group instead — flipping the
# "correct" ordering out from under this file with no test-side edit
# possible, since import classification is filesystem-existence-driven and
# Task 2/Task 3 create their modules independently and in no fixed order.
# Suppressing I001 here keeps this file's own ordering stable (and importable
# either way) across every intermediate state instead of chasing a moving
# target; `ruff check --fix` still finds and fixes real ordering mistakes
# everywhere else.
from __future__ import annotations  # noqa: I001

import json
import logging
from pathlib import Path

import pytest
from paperboy.collectors.discussion import DiscussionCollector

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.collectors.history import HistoryCollector
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import get_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77
FOREIGN_CHANNEL_ID = 4242


def _ctx(st, gw, tier="stranger"):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        {"channel_id": CHANNEL_ID, "access_hash": 9}, CHANNEL_ID, tier, logging.getLogger("t"),
    )


def _seed_channel(st: Store, linked_chat_id: int | None, group_flags: dict | None = None) -> None:
    """Write the `channels` row and the linked group's `peers` row the way the
    `channel` collector would, so preflight has an access hash to find."""
    raw_id = st.add_raw("ChatFull", {"_": "ChatFull"}, "stranger", None)
    full_chat = {"_": "channelFull", "id": CHANNEL_ID, "pts": 1,
                 "linked_chat_id": linked_chat_id, "participants_count": 10}
    chan = {"_": "Channel", "id": CHANNEL_ID, "access_hash": 9, "title": "C",
            "username": "c", "broadcast": True}
    upsert_channel(st, full_chat, chan, raw_id, "2026-01-01T00:00:00+00:00")
    if linked_chat_id:
        group = {"_": "Channel", "id": linked_chat_id, "access_hash": 4242,
                 "title": "C Chat", "megagroup": True, **(group_flags or {})}
        upsert_peer(st, group, raw_id, "2026-01-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)


def _mirror(msg_id: int, channel_post: int, from_channel_id: int = CHANNEL_ID) -> dict:
    """The group's auto-forwarded copy of a channel post — or, when
    `from_channel_id` is some OTHER channel's id, a forward of a THIRD
    PARTY's post into the group (which must never be mistaken for a mirror
    of the collected channel; see spec §6 step 1)."""
    return {
        "_": "Message", "id": msg_id, "message": "", "date": 1767322445,
        "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
        "fwd_from": {"_": "MessageFwdHeader", "channel_post": channel_post,
                     "from_id": {"_": "PeerChannel", "channel_id": from_channel_id}},
    }


def _comment(msg_id: int, top_id: int, user_id: int | None, reply_to: int | None = None) -> dict:
    m = {
        "_": "Message", "id": msg_id, "message": f"c{msg_id}", "date": 1767322445,
        "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
        "reply_to": {"_": "MessageReplyHeader", "reply_to_msg_id": reply_to or top_id,
                     "reply_to_top_id": top_id},
    }
    if user_id is not None:
        m["from_id"] = {"_": "PeerUser", "user_id": user_id}
    return m


def _plain(msg_id: int, user_id: int) -> dict:
    """Ordinary group chatter with no `reply_to` at all — the majority of a
    real megagroup sweep, and the stated reason the group is swept wholesale
    rather than fetched per-post (spec §3). Must not crash and must not be
    counted as `unmapped` (it isn't reply-shaped, so it was never a mapping
    candidate in the first place)."""
    return {
        "_": "Message", "id": msg_id, "message": f"m{msg_id}", "date": 1767322445,
        "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
        "from_id": {"_": "PeerUser", "user_id": user_id},
    }


def _gw(history: list[dict]) -> FakeGateway:
    return FakeGateway({"history": history, "get_messages": {}})


@pytest.mark.asyncio
async def test_skips_cleanly_when_there_is_no_linked_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, None)
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is not None
        assert "linked" in res.stopped.lower()
        # Amendment 6: the two skip reasons must be lexically disjoint — the
        # old pair both contained "linked", which let a test asserting only
        # "linked" in stopped pass on the wrong branch, including on the
        # falsy-`0` access-hash bug that branch existed to catch.
        assert "access" not in res.stopped.lower()


@pytest.mark.asyncio
async def test_skips_cleanly_when_linked_chat_id_is_zero(tmp_path):
    """Spec §4.1: 'Absent, `0`, or `NULL`' — `0` is a distinct falsy value
    from `NULL`/absent and must be handled the same way, not treated as a
    truthy channel id."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.conn.execute("update channels set linked_chat_id=0 where id=?", (CHANNEL_ID,))
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is not None
        assert "linked" in res.stopped.lower()
        assert "access" not in res.stopped.lower()


@pytest.mark.asyncio
async def test_skips_when_the_group_requires_joining_to_read(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID, {"join_to_send": True})
        gw = _gw([])
        res = await DiscussionCollector().collect(_ctx(st, gw))
        assert res.stopped is not None
        assert "join" in res.stopped.lower()
        # Never an implicit join: the sweep must not run at all on this path.
        assert gw.calls == []


@pytest.mark.asyncio
async def test_skips_when_the_group_access_hash_is_unknown(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.conn.execute("update peers set access_hash=NULL where uri=?",
                        (f"tg:channel:{GROUP_ID}",))
        gw = _gw([])
        res = await DiscussionCollector().collect(_ctx(st, gw))
        assert res.stopped is not None
        assert "access" in res.stopped.lower()
        assert "linked" not in res.stopped.lower()
        assert gw.calls == []


@pytest.mark.asyncio
async def test_skips_when_the_group_access_hash_is_zero(tmp_path):
    """Amendment 6 / spec §4.2: a stored `0` is not a usable hash — it
    yields `CHANNEL_INVALID` against live Telegram, a phase error rather
    than the clean skip promised here. `upsert_peer` stores
    `obj.get("access_hash")` verbatim, so a `min` Channel with
    `access_hash: 0` puts a real `0` in the row; a guard written as
    `peer["access_hash"] is None` (the plan's own verbatim code) misses it
    entirely and the collector proceeds to sweep with an unusable hash."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.conn.execute("update peers set access_hash=0 where uri=?",
                        (f"tg:channel:{GROUP_ID}",))
        gw = _gw([])
        res = await DiscussionCollector().collect(_ctx(st, gw))
        assert res.stopped is not None
        assert "access" in res.stopped.lower()
        assert "linked" not in res.stopped.lower()
        assert gw.calls == []


@pytest.mark.asyncio
async def test_skips_when_there_is_no_peers_row_for_the_group_at_all(tmp_path):
    """Spec §4.2 lists this case first ('If no `peers` row or no
    `access_hash`') but it was previously only exercised with the row
    present and the column NULLed. `ctx.store.conn.execute(...).fetchone()`
    returns `None` when the row is absent entirely, and a lazy
    `peer["access_hash"]` on that would raise `TypeError` — which
    `recipes.collect_channel` does not catch (only `SkipAndRecord`,
    `PhaseStop`, `HardStop`), killing the whole run rather than skipping
    just this phase."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.conn.execute("delete from peers where uri=?", (f"tg:channel:{GROUP_ID}",))
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is not None
        assert "access" in res.stopped.lower()
        assert "linked" not in res.stopped.lower()


@pytest.mark.asyncio
async def test_raises_phase_stop_when_channel_context_is_not_established(tmp_path):
    """Mirrors `HistoryCollector`'s own PhaseStop guard (`tests/test_collector_history.py`)
    for the case where the `channel` phase never completed. Dropping this
    guard turns a missing channel context into a raw sqlite3 query against
    `id=None`, which silently returns no row and reports the wrong
    diagnosis ('no linked discussion group') for a channel that may well
    have had one."""
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            _gw([]), st, load_settings("default", {}), parse_target("@x"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(PhaseStop):
            await DiscussionCollector().collect(ctx)


@pytest.mark.asyncio
async def test_comments_are_stored_under_the_group_channel_id(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        rows = st.conn.execute("select distinct channel_id from messages").fetchall()
        assert {r["channel_id"] for r in rows} == {GROUP_ID}


@pytest.mark.asyncio
async def test_sweeps_the_linked_groups_own_peer_not_the_broadcast_channels(tmp_path):
    """Spec §4.2: 'Build `input_channel` for the group from the `peers`
    row's `access_hash`. No `resolve` RPC.' `FakeGateway.iter_history`
    discards `input_channel` entirely (`del input_channel`), so nothing
    previously distinguished a correctly-targeted sweep from one silently
    pointed at the broadcast channel, at `access_hash=0`, or at a
    hardcoded stand-in — all of which would pass every other test in this
    file. Against live Telegram the wrong access_hash is `CHANNEL_INVALID`
    at best; the wrong *peer* is a silent, unrecoverable cross-channel
    sweep. `FakeGateway` must therefore record the `input_channel` each
    `iter_history` call was actually given (a new `history_targets: list[dict]`
    attribute, owned by Task 2/gateway.py — not yet added, so this test
    fails today with `AttributeError` until it lands), not just that a
    call happened.

    A correct page loop over this 2-message page always issues TWO
    `iter_history` calls, not one: the page with data, then the empty page
    that terminates `while True:` (`history.py`'s `if not page: break`).
    `tests/test_integration_discussion.py::test_discussion_run_issues_only_read_rpcs`
    pins the same fact end to end (`counts["iter_history"] == 4` for two
    sweeps). Pinning `history_targets` to a one-element list would therefore
    fail against a *correct* implementation — assert every recorded call
    targeted the group instead, plus the call count as its own explicit
    fact (not a hidden side effect of the list's length)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        gw = _gw([_comment(200, 100, 111), _mirror(100, 42)])
        await DiscussionCollector().collect(_ctx(st, gw))
        assert gw.history_targets, "sweep issued no getHistory call"
        assert all(
            t == {"channel_id": GROUP_ID, "access_hash": 4242} for t in gw.history_targets
        )
        # Exactly 2: the one data page, then the empty page that ends the loop.
        assert len(gw.history_targets) == 2


@pytest.mark.asyncio
async def test_linked_group_sweep_issues_no_resolve(tmp_path):
    """Spec §11 test 4, previously asserted nowhere: the only zero-RPC test
    (`test_the_backfill_issues_no_gateway_calls`) exercises the no-linked-
    group path, where preflight never builds an `input_channel` at all —
    it cannot catch a stray `resolve` on the *with-group* path, which is
    where spec §4.2's 'no resolve RPC' actually applies."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        gw = _gw([_comment(200, 100, 111), _mirror(100, 42)])
        await DiscussionCollector().collect(_ctx(st, gw))
        assert "resolve" not in gw.calls


@pytest.mark.asyncio
async def test_no_gap_tombstones_are_written_for_the_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(203, 100, 111), _comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert st.conn.execute("select count(*) c from message_tombstones").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_linked_group_with_no_messages_completes_cleanly(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is None
        assert res.counts["unmapped"] == 0
        assert res.counts["messages"] == 0


@pytest.mark.asyncio
async def test_plain_group_chatter_with_no_reply_is_stored_and_produces_no_edges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_plain(300, 111), _comment(200, 100, 112), _mirror(100, 42)]))
        )
        assert st.conn.execute(
            "select count(*) c from messages where msg_id=300"
        ).fetchone()["c"] == 1
        # Not reply-shaped at all: never a mapping candidate, so it must not
        # inflate `unmapped` either.
        assert res.counts["unmapped"] == 0
        assert st.conn.execute(
            "select count(*) c from edges where predicate in ('commented_on','replied_to') "
            "and (subject_uri=? or object_uri=?)",
            (f"tg:msg:{GROUP_ID}/300", f"tg:msg:{GROUP_ID}/300"),
        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_maps_a_comment_through_the_mirror_to_the_channel_post(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        row = st.conn.execute(
            "select subject_uri, object_uri from edges where predicate='commented_on'"
        ).fetchone()
        assert row["subject_uri"] == "tg:user:111"
        assert row["object_uri"] == f"tg:msg:{CHANNEL_ID}/42"


@pytest.mark.asyncio
async def test_commented_on_edge_carries_tier_source_and_evidence(tmp_path):
    """Spec §7: both edges 'carry `observed_at`, `tier`, and `source_raw_id`
    like every other edge', and `commented_on`'s evidence 'records the
    comment URI it was derived from' — none of this was previously
    asserted, so an implementation emitting `tier=None`/`evidence=None`
    passed every test."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]), tier="member")
        )
        row = st.conn.execute(
            "select tier, source_raw_id, observed_at, evidence_json from edges "
            "where predicate='commented_on'"
        ).fetchone()
        assert row["tier"] == "member"
        assert row["source_raw_id"] is not None
        assert row["observed_at"] is not None
        assert json.loads(row["evidence_json"])["comment_uri"] == f"tg:msg:{GROUP_ID}/200"


@pytest.mark.asyncio
async def test_a_forward_from_a_different_channel_is_never_treated_as_a_mirror(tmp_path):
    """Spec §6 step 1: a group message is a mirror only when
    `fwd_from.channel_post` is set AND `fwd_from.from_id.channel_id` equals
    the COLLECTED channel's id. Members forwarding some other channel's
    post into the discussion group is routine, and such a forward also
    carries `channel_post`. Without the origin check, this comment would be
    silently attributed to a foreign channel's post id — the review panel's
    stated priority-3 risk, and previously untested: every mirror fixture
    in this file forwarded from `CHANNEL_ID`."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111),
                          _mirror(100, 42, from_channel_id=FOREIGN_CHANNEL_ID)]))
        )
        assert res.counts["unmapped"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_a_real_mirror_still_maps_when_a_foreign_forward_is_also_present(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([
                _comment(201, 100, 111),
                _mirror(100, 42),
                _comment(202, 150, 112),
                _mirror(150, 43, from_channel_id=FOREIGN_CHANNEL_ID),
            ]))
        )
        rows = {
            r["subject_uri"]: r["object_uri"]
            for r in st.conn.execute(
                "select subject_uri, object_uri from edges where predicate='commented_on'"
            ).fetchall()
        }
        assert rows == {"tg:user:111": f"tg:msg:{CHANNEL_ID}/42"}


@pytest.mark.asyncio
async def test_a_mirror_paged_in_after_its_comments_still_maps_within_one_run(tmp_path):
    """`getHistory` pages newest-first and `_HISTORY_PAGE_SIZE` is 100, so a
    comment (high id) is always paged before its low-id mirror once more
    than a page separates them. The mirror map must be rebuilt from stored
    `messages` rows after the whole sweep, not accumulated only from
    whatever page is currently being walked — otherwise every comment more
    than one page from its mirror silently loses its `commented_on` edge,
    which on the live ~35k-message/~350-page target is nearly all of them."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        # Distinct author per comment (amendment 4: thread edges are
        # idempotent on the (subject, predicate, object) triple, so 150
        # comments from the SAME author would collapse into 1 stored edge —
        # that would make this test assert the dedup guard's absence, not
        # the mirror-rebuild behaviour it's actually pinning).
        comments = [_comment(1000 + i, 100, 111 + i) for i in range(149, -1, -1)]
        history = comments + [_mirror(100, 42)]
        res = await DiscussionCollector().collect(_ctx(st, _gw(history)))
        assert res.counts["unmapped"] == 0
        n = st.conn.execute(
            "select count(*) c from edges where predicate='commented_on' and object_uri=?",
            (f"tg:msg:{CHANNEL_ID}/42",),
        ).fetchone()["c"]
        assert n == 150


@pytest.mark.asyncio
async def test_resumed_run_maps_comments_stored_before_their_mirror_arrived(tmp_path):
    """Spec §6: 'A resumed run rebuilds it from `messages` rows already
    stored for the group, so a comment paged in after its mirror still
    maps.' The page budget (spec §9) makes multi-run collection routine on
    a large group — this pins the cross-*run* case specifically (as
    opposed to the cross-*page*-within-one-run case above): a first run
    stops on its page budget having seen only comments, a second run
    reaches the mirror, and the comments stored in the FIRST run must still
    get their `commented_on` edge once the second run's
    `_write_thread_edges` rebuilds the map from stored rows."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        # Distinct author per comment — see the comment in
        # test_a_mirror_paged_in_after_its_comments_still_maps_within_one_run
        # for why a shared author would make this test vacuous under
        # amendment 4's dedup guard.
        comments = [_comment(1000 + i, 100, 111 + i) for i in range(149, -1, -1)]
        history = comments + [_mirror(100, 42)]
        gw = _gw(history)

        ctx1 = _ctx(st, gw)
        ctx1.settings.discussion_page_budget = 1
        with pytest.raises(PhaseStop):
            await DiscussionCollector().collect(ctx1)
        # Run 1 never saw the mirror (it's in page 2): nothing mappable yet.
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0

        ctx2 = _ctx(st, gw)
        res2 = await DiscussionCollector().collect(ctx2)
        assert res2.counts["unmapped"] == 0
        n = st.conn.execute(
            "select count(*) c from edges where predicate='commented_on' and object_uri=?",
            (f"tg:msg:{CHANNEL_ID}/42",),
        ).fetchone()["c"]
        assert n == 150
        # Distinguishes an actual RESUME from a silent restart-from-scratch:
        # run 1's page budget (100/page) consumed ids 1149..1050, so run 2
        # must page only the remaining 50 comments (1049..1000) plus the
        # mirror — 51 messages this run, not all 151 again.
        assert res2.counts["messages"] == 51


@pytest.mark.asyncio
async def test_rerunning_collect_does_not_duplicate_thread_edges(tmp_path):
    """Amendment 4 (authoritative, supersedes the ADR-0002-citing stance this
    test previously took): `_write_thread_edges` re-scans every stored group
    row on every run — it must, so a comment paged in before its mirror
    still maps on a later run — so an unguarded `add_edge` call would append
    a fresh `commented_on`/`replied_to` row, with a fresh `observed_at` and
    the *previous* run's `source_raw_id`, for evidence this run never
    gathered: ~70k phantom rows per re-run on the live target, inflating
    every degree count an analyst reads off `edges`. Both edges are
    therefore idempotent on `(subject_uri, predicate, object_uri)` — skip
    the insert when an identical triple already exists (`idx_edges_subject`
    already covers that lookup). `tests/test_store_repliers.py::
    test_repeated_backfill_does_not_duplicate_the_peer_row` pins the same
    stance for the `recent_repliers` backfill's own `commented_on` edges.

    Repeated re-running also must not duplicate `messages` rows —
    `(channel_id, msg_id)` is a unique index (`migrations/0001_init.sql`),
    so that side is a genuine idempotence guarantee and is asserted here
    too."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        # 201 is a nested reply to 200; 200 is a DIRECT reply to the thread
        # root (its `reply_to_msg_id` defaults to `top_id`=100) — per spec
        # §6/§7 (no exclusion for `reply_to_msg_id == reply_to_top_id`) that
        # still gets its own `replied_to` edge, so one run emits 2
        # `commented_on` + 2 `replied_to`, not 2 + 1.
        history = [_comment(201, 100, 112, reply_to=200), _comment(200, 100, 111),
                   _mirror(100, 42)]
        await DiscussionCollector().collect(_ctx(st, _gw(history)))
        first_commented = st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"]
        first_replied = st.conn.execute(
            "select count(*) c from edges where predicate='replied_to'"
        ).fetchone()["c"]
        assert (first_commented, first_replied) == (2, 2)

        await DiscussionCollector().collect(_ctx(st, _gw(history)))
        second_commented = st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"]
        second_replied = st.conn.execute(
            "select count(*) c from edges where predicate='replied_to'"
        ).fetchone()["c"]
        assert second_commented == first_commented
        assert second_replied == first_replied

        assert st.conn.execute(
            "select count(*) c from messages where channel_id=?", (GROUP_ID,)
        ).fetchone()["c"] == 3


@pytest.mark.asyncio
async def test_direct_reply_to_the_thread_root_still_gets_a_replied_to_edge(tmp_path):
    """Spec §6/§7 draw no exclusion for a comment whose `reply_to_msg_id`
    equals its `reply_to_top_id` — i.e. a comment that directly replies to
    the group's mirror of the channel post, rather than to another comment.
    That shape is the DOMINANT one in a real discussion group (3 of 4
    replies in `discussion_group_history.json`'s real capture — 4105, 4107,
    4110 — have `reply_to_msg_id == reply_to_top_id`), so silently
    suppressing its `replied_to` edge would leave the `replied_to`
    projection near-empty on a live sweep. `_comment`'s default
    (`reply_to=None`) already produces this exact shape."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        row = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{GROUP_ID}/200",),
        ).fetchone()
        assert row is not None
        assert row["object_uri"] == f"tg:msg:{GROUP_ID}/100"


@pytest.mark.asyncio
async def test_discussion_sweep_does_not_disturb_the_channels_own_history_cursor(tmp_path):
    """`sync_state` scope stays `"history"`, keyed by the *target* channel
    id (spec §5), so the channel's own `history` cursor and the group's
    must resume independently. A regression that let the group sweep write
    under the channel's key would silently truncate future channel
    backfills."""
    from paperboy.store.sync import get_state, set_state

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        set_state(st, "history", str(CHANNEL_ID), {"offset_id": 555})
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert get_state(st, "history", str(CHANNEL_ID)) == {"offset_id": 555}
        assert get_state(st, "history", str(GROUP_ID)) is not None


@pytest.mark.asyncio
async def test_emits_replied_to_edges_within_the_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(201, 100, 112, reply_to=200),
                          _comment(200, 100, 111), _mirror(100, 42)]))
        )
        row = st.conn.execute(
            "select subject_uri, object_uri from edges where predicate='replied_to' "
            "and subject_uri=?", (f"tg:msg:{GROUP_ID}/201",)
        ).fetchone()
        assert row["object_uri"] == f"tg:msg:{GROUP_ID}/200"


@pytest.mark.asyncio
async def test_replied_to_edge_carries_tier_and_source(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(201, 100, 112, reply_to=200),
                          _comment(200, 100, 111), _mirror(100, 42)]), tier="member")
        )
        row = st.conn.execute(
            "select tier, source_raw_id from edges where predicate='replied_to' "
            "and subject_uri=?", (f"tg:msg:{GROUP_ID}/201",)
        ).fetchone()
        assert row["tier"] == "member"
        assert row["source_raw_id"] is not None


@pytest.mark.asyncio
async def test_unmappable_comment_is_stored_and_counted_never_guessed(tmp_path):
    """top_id 999 has no mirror: store it, give it its `replied_to` edge to
    the thread root it DOES know about, never a `commented_on` guess.
    Nested (`reply_to_msg_id != reply_to_top_id`) so the `replied_to` edge
    is actually reachable under the plan's own guard — the previous fixture
    had both equal to 999, which suppressed the edge entirely despite the
    docstring claiming it was covered."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 999, 111, reply_to=198)]))
        )
        assert res.counts["unmapped"] == 1
        assert st.conn.execute(
            "select count(*) c from messages where msg_id=200"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0
        row = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{GROUP_ID}/200",),
        ).fetchone()
        assert row is not None
        assert row["object_uri"] == f"tg:msg:{GROUP_ID}/198"


@pytest.mark.asyncio
async def test_anonymous_comment_yields_a_channel_subject(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        anon = _comment(200, 100, None)
        anon["from_id"] = {"_": "PeerChannel", "channel_id": 999}
        await DiscussionCollector().collect(_ctx(st, _gw([anon, _mirror(100, 42)])))
        row = st.conn.execute(
            "select subject_uri from edges where predicate='commented_on'"
        ).fetchone()
        assert row["subject_uri"] == "tg:channel:999"


@pytest.mark.asyncio
async def test_authorless_comment_gets_no_commented_on_edge(tmp_path):
    """`_comment(200, 100, None, reply_to=198)` is nested (`reply_to_msg_id`
    198 != `reply_to_top_id` 100) so its `replied_to` edge is reachable
    under any implementation that emits it at all — the previous fixture
    used the default `reply_to=None`, which collapses to
    `reply_to_msg_id == reply_to_top_id == 100` and made the `replied_to`
    edge unobservable by this test even though spec §7 promises it
    ('a comment with no resolvable author... is stored and gets its
    `replied_to` edge, but yields no `commented_on`'). A guard that checks
    the author before emitting `replied_to` (e.g. `if not from_uri:
    counts["unmapped"] += 1; continue`, placed ahead of the `replied_to`
    write) would drop the edge for every authorless comment and still pass
    the old fixture."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, None, reply_to=198), _mirror(100, 42)]))
        )
        assert st.conn.execute(
            "select count(*) c from messages where msg_id=200"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0
        assert res.counts["unmapped"] == 1
        row = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{GROUP_ID}/200",),
        ).fetchone()
        assert row is not None
        assert row["object_uri"] == f"tg:msg:{GROUP_ID}/198"


@pytest.mark.asyncio
async def test_runs_the_recent_repliers_backfill_before_preflight(tmp_path):
    """Even with no linked group, the zero-RPC harvest still yields peers."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, None)
        st.add_raw("Message", {
            "_": "Message", "id": 10,
            "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID},
            "replies": {"_": "MessageReplies", "recent_repliers": [
                {"_": "PeerUser", "user_id": 111}]},
        }, "stranger", {"channel_id": CHANNEL_ID})
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.counts["backfilled_peers"] == 1
        assert st.conn.execute(
            "select count(*) c from peers where uri='tg:user:111'"
        ).fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_the_backfill_issues_no_gateway_calls(tmp_path):
    """Previously vacuous: no `Message` rows were seeded, so the backfill
    had zero rows to scan and `gw.calls == []` held no matter what it did.
    Seeding a real replier (both peer kinds, to also exercise the count)
    makes the zero-RPC claim actually falsifiable."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, None)
        st.add_raw("Message", {
            "_": "Message", "id": 10,
            "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID},
            "replies": {"_": "MessageReplies", "recent_repliers": [
                {"_": "PeerUser", "user_id": 111},
                {"_": "PeerChannel", "channel_id": 222},
            ]},
        }, "stranger", {"channel_id": CHANNEL_ID})
        gw = _gw([])
        res = await DiscussionCollector().collect(_ctx(st, gw))
        assert res.counts["backfilled_peers"] == 2
        assert gw.calls == []


@pytest.mark.asyncio
async def test_page_budget_exhaustion_stops_the_phase_and_keeps_the_cursor(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        ctx = _ctx(st, _gw([_comment(i, 100, 111) for i in range(300, 0, -1)]))
        ctx.settings.discussion_page_budget = 1
        with pytest.raises(PhaseStop):
            await DiscussionCollector().collect(ctx)
        from paperboy.store.sync import get_state
        # The correct resume point is knowable: newest-first paging at
        # _HISTORY_PAGE_SIZE=100 over ids 300..1 with a 1-page budget
        # consumes ids 300..201, so the persisted cursor is 201 — not a
        # restart-from-scratch value like `{"offset_id": 0}`, which is
        # exactly the failure mode spec §9 names.
        assert get_state(st, "history", str(GROUP_ID)) == {"offset_id": 201}


@pytest.mark.asyncio
async def test_sweep_counts_are_merged_into_the_discussion_result(tmp_path):
    """The linked-group sweep's own `HistoryCollector.collect()` counts
    (`messages`, `revisions`, `tombstones`, `edges`) must surface on the
    `discussion` `CollectResult`, not be silently discarded — previously
    unasserted anywhere in this file."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert res.counts["messages"] == 2


@pytest.mark.asyncio
async def test_edges_count_reflects_both_edge_kinds(tmp_path):
    """`res.counts["edges"]` was previously asserted nowhere in this file —
    only `messages` and `unmapped` — so it could be zero, or double-count
    one branch, and every other test here would still pass. Broken down:
    the sweep's own reused `HistoryCollector._observe_message` emits 1
    `forwarded_from` edge for the mirror message itself (it carries
    `fwd_from`, same as any other forward — see
    `tests/test_collector_history.py::test_forwarded_from_edge_recorded`);
    `_write_thread_edges` then adds 2 `commented_on` (201 and 200 both map
    through the mirror to the channel post) + 2 `replied_to` (201 nested to
    200; 200 direct to the thread root — see the direct-reply test above) =
    1 + 2 + 2 = 5."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(201, 100, 112, reply_to=200), _comment(200, 100, 111),
                          _mirror(100, 42)]))
        )
        assert res.counts["edges"] == 5


@pytest.mark.asyncio
async def test_commenters_are_projected_into_peers_with_provenance(tmp_path):
    """Design spec §1: the collector's entire stated purpose is to 'turn
    [linked-group activity] into stored entities: comment messages, THEIR
    AUTHORS AS PEERS, and edges' — not edges alone. `HistoryCollector
    ._observe_message` (which the sweep reuses wholesale) only upserts a
    `peers` row for `PeerUser` `from_id`s; a channel-authored (anonymous)
    commenter still gets a `commented_on` edge with subject
    `tg:channel:<id>` (see `test_anonymous_comment_yields_a_channel_subject`)
    but, on an implementation that relies solely on that reuse for peer
    projection, no matching `peers` row at all — silently disagreeing with
    the `recent_repliers` backfill, which IS pinned to project `PeerChannel`
    repliers too (`tests/test_store_repliers.py::
    test_projects_peerchannel_repliers_too`). Both commenter kinds must
    land in `peers`, with `seen_in_chat`/`seen_in_msg` provenance pointing
    at the comment they were actually observed in — the same provenance
    shape `_observe_message` already uses for ordinary chatter."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        anon = _comment(201, 100, None)
        anon["from_id"] = {"_": "PeerChannel", "channel_id": 999}
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), anon, _mirror(100, 42)]))
        )
        user_row = st.conn.execute(
            "select kind, seen_in_chat, seen_in_msg from peers where uri='tg:user:111'"
        ).fetchone()
        assert user_row is not None
        assert (user_row["kind"], user_row["seen_in_chat"], user_row["seen_in_msg"]) == (
            "user", GROUP_ID, 200,
        )
        channel_row = st.conn.execute(
            "select kind, seen_in_chat, seen_in_msg from peers where uri='tg:channel:999'"
        ).fetchone()
        assert channel_row is not None
        assert (channel_row["kind"], channel_row["seen_in_chat"], channel_row["seen_in_msg"]) == (
            "channel", GROUP_ID, 201,
        )


@pytest.mark.asyncio
async def test_the_backfill_and_the_sweep_both_run_within_one_collect(tmp_path):
    """Both zero-RPC backfill tests
    (`test_runs_the_recent_repliers_backfill_before_preflight`,
    `test_the_backfill_issues_no_gateway_calls`) exercise only the
    no-linked-group path, where preflight — and so the sweep — never runs.
    Spec §8 says the backfill runs 'as its first step, before preflight',
    which implies it also runs, unconditionally, on the WITH-group path;
    nothing previously exercised both producers together in one
    `collect()`, so an implementation that (for example) only calls the
    backfill inside the no-linked-group branch would still pass every
    other test in this file."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.add_raw("Message", {
            "_": "Message", "id": 10,
            "peer_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID},
            "replies": {"_": "MessageReplies", "recent_repliers": [
                {"_": "PeerUser", "user_id": 555}]},
        }, "stranger", {"channel_id": CHANNEL_ID})
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert res.counts["backfilled_peers"] == 1
        assert res.counts["messages"] == 2
        assert st.conn.execute(
            "select count(*) c from peers where uri='tg:user:555'"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from peers where uri='tg:user:111'"
        ).fetchone()["c"] == 1


def test_discussion_page_budget_defaults_to_500():
    """Spec §9: 'New setting `discussion_page_budget: int = 500`.' Every
    test elsewhere in this file that cares about the budget sets it
    explicitly (`ctx.settings.discussion_page_budget = 1`); nothing pins
    the shipped default, so an implementation shipping a much smaller (or
    unbounded) default would pass every other test here."""
    settings = load_settings("default", {})
    assert settings.discussion_page_budget == 500


@pytest.mark.asyncio
async def test_service_message_in_group_is_stored_without_spurious_edges(tmp_path):
    """Spec §14 names discussion-group service messages
    (`messageActionChatAddUser`, `...JoinedByLink`) as a real payload class
    present in the live target, deliberately out of scope for edge
    projection (its own future feature). A `MessageService` row must not
    crash the sweep, must not inflate `unmapped` (it was never
    reply-shaped, exactly like ordinary chatter — see
    `test_plain_group_chatter_with_no_reply_is_stored_and_produces_no_edges`),
    and must not produce a `commented_on`/`replied_to` edge."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        service = {
            "_": "MessageService", "id": 250, "date": 1767322445,
            "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
            "from_id": {"_": "PeerUser", "user_id": 111},
            "action": {"_": "MessageActionChatAddUser", "users": [111]},
        }
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([service, _comment(200, 100, 112), _mirror(100, 42)]))
        )
        assert res.counts["unmapped"] == 0
        assert st.conn.execute(
            "select count(*) c from messages where msg_id=250"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate in ('commented_on','replied_to') "
            "and (subject_uri=? or object_uri=?)",
            (f"tg:msg:{GROUP_ID}/250", f"tg:msg:{GROUP_ID}/250"),
        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_a_plain_in_group_reply_with_no_thread_top_id_still_gets_a_replied_to_edge(
    tmp_path,
):
    """The live capture contains ordinary in-group replies with
    `reply_to_top_id: null` — a reply to another message that never started
    a comment thread at all (real shape:
    `{"_":"MessageReplyHeader", "reply_to_msg_id":720, "reply_to_top_id":null}`).
    Spec §7 defines `replied_to` as comment -> parent without restricting it
    to comment-THREAD messages, so an implementation whose thread-edge query
    filters on `WHERE reply_to_top_id IS NOT NULL` (a natural way to walk
    only comment threads, and the shape every other `replied_to` test in
    this file exercises via `_comment`, which always sets `reply_to_top_id`)
    would silently drop this shape — the dominant reply shape in ordinary
    group chatter, as opposed to channel-post comment threads.

    Amendment 5: a NULL `reply_to_top_id` was never a comment-thread
    candidate in the first place, so it must not inflate `unmapped` either —
    the widened query amendment 7 requires (`reply_to_msg_id IS NOT NULL OR
    reply_to_top_id IS NOT NULL`) makes this row reachable by the same loop
    that computes `unmapped`, so the guard has to be checked explicitly."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        reply = {
            "_": "Message", "id": 720, "message": "same here", "date": 1767322445,
            "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
            "from_id": {"_": "PeerUser", "user_id": 111},
            "reply_to": {"_": "MessageReplyHeader", "reply_to_msg_id": 700,
                         "reply_to_top_id": None},
        }
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([reply, _plain(700, 112)]))
        )
        assert res.counts["unmapped"] == 0
        row = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{GROUP_ID}/720",),
        ).fetchone()
        assert row is not None
        assert row["object_uri"] == f"tg:msg:{GROUP_ID}/700"


@pytest.mark.asyncio
async def test_cursor_after_a_completed_sweep_reflects_the_oldest_id_seen(tmp_path):
    """`test_page_budget_exhaustion_stops_the_phase_and_keeps_the_cursor`
    pins the cursor mid-sweep; nothing previously pinned what it holds once
    a sweep runs to full exhaustion, leaving a resumed run's behaviour on an
    already fully-collected group unspecified. `history`'s own paging
    semantics persist `offset_id` as the oldest id seen — mirrored here
    (`test_explicit_target_resumes_on_its_own_cursor` in
    `tests/test_collector_history.py` pins `{"offset_id": 1}` after a full
    2-message sweep) — the group's cursor after a COMPLETE run must do the
    same, not restart at 0 and not freeze at a mid-sweep value."""
    from paperboy.store.sync import get_state

    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert get_state(st, "history", str(GROUP_ID)) == {"offset_id": 100}


@pytest.mark.asyncio
async def test_a_second_run_collects_new_group_messages_via_the_high_water_mark(tmp_path):
    """Spec §3's 'Re-collection' row is the entire reason `history_sweep`/
    `max_id_seen` exists: 'the linked group has no `pts` seed, so it would
    freeze at day one' without it. The high-water-mark mechanism itself is
    already pinned at the `HistoryCollector` level
    (`tests/test_collector_history.py::
    test_incremental_run_stopped_by_budget_resumes_where_it_stopped`), but
    nothing previously drove a SECOND `DiscussionCollector.collect()` at
    all, so a regression that dropped the delegation (e.g. `discussion`
    accidentally passing `page_budget=None` and always starting a full
    backfill, or never reaching `history_sweep` state) would go unnoticed
    here."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        gw1 = _gw([_comment(200, 100, 111), _mirror(100, 42)])
        await DiscussionCollector().collect(_ctx(st, gw1))
        assert get_state(st, "history_sweep", str(GROUP_ID)) == {
            "max_id_seen": 200, "pending_high": 200,
            "backfill_complete": True, "incremental_in_progress": False,
        }

        # A new comment (id 300) arrives after the first sweep completed.
        gw2 = _gw([_comment(300, 100, 999), _comment(200, 100, 111), _mirror(100, 42)])
        res2 = await DiscussionCollector().collect(_ctx(st, gw2))
        # The stop condition must actually FIRE, not merely not-lose-anything.
        # Exactly one page: the data page returns cursor=100 <= stop_at=200, so
        # the loop breaks BEFORE the terminating empty-page call. Without the
        # high-water-mark stop this is a full re-sweep of the group on every
        # run — ~351 RPCs instead of 1 on the live target — and every other
        # assertion here still passes. Do not "fix" this to 2.
        assert gw2.calls.count("iter_history") == 1

        assert st.conn.execute(
            "select count(*) c from messages where channel_id=? and msg_id=300", (GROUP_ID,)
        ).fetchone()["c"] == 1
        assert res2.counts["unmapped"] == 0
        row = st.conn.execute(
            "select object_uri from edges where predicate='commented_on' and subject_uri=?",
            ("tg:user:999",),
        ).fetchone()
        assert row is not None
        assert row["object_uri"] == f"tg:msg:{CHANNEL_ID}/42"
        sweep = get_state(st, "history_sweep", str(GROUP_ID))
        assert sweep is not None
        assert sweep["max_id_seen"] == 300


@pytest.mark.asyncio
async def test_probing_off_writes_no_sync_ranges_for_the_group(tmp_path):
    """Amendment 2 / spec §5: `add_range` fires only when `probe_gaps` is
    true, and `discussion` sweeps the group with probing off (it is not the
    channel being backfilled). Writing a verified range for an unprobed
    span would make `missing_ids()` report zero gaps for the group forever
    — previously unpinned in this file; only the channel-scoped case was
    covered (`tests/test_collector_history.py::
    test_backfill_records_verified_range`)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert st.conn.execute(
            "select count(*) c from sync_ranges where channel_id=?", (GROUP_ID,)
        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_thread_edges_do_not_leak_across_channels(tmp_path):
    """`_mirror_map` and `_write_thread_edges` both filter `WHERE
    channel_id=?` (spec §6/§7). Every other test in this file seeds
    messages only for the swept group, so cross-channel bleed is
    structurally untested: without that predicate, a broadcast-channel
    message carrying `fwd_from.channel_post` would enter the mirror map,
    and the broadcast channel's own reply-shaped rows would earn
    `replied_to` edges misattributed to the group. Seed the broadcast
    channel (id `CHANNEL_ID`) with a mirror-shaped and a reply-shaped
    message the way `history` already would have, via a direct
    `HistoryCollector` run against `ctx.channel_id`, then confirm the
    group's own sweep produces only the edges its own comments earn.

    `_mirror(300, 999)` is seeded under `CHANNEL_ID`'s own message space —
    mirror-shaped and origin-valid, so a `_mirror_map` that forgets its
    `WHERE channel_id=?` predicate would happily fold it in. The group's own
    sweep then carries `_comment(201, 300, 112)`, a comment whose
    `reply_to_top_id` (300) matches that foreign row's id but which the
    group has no mirror for at all. A correct, channel-scoped map cannot
    resolve id 300 within the group, so the comment must land in
    `unmapped`, and 112 must never appear as a `commented_on` subject — that
    is the only way this test can tell the two implementations apart."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await HistoryCollector().collect(
            _ctx(st, _gw([_mirror(9001, 42), _comment(9002, 9001, 555), _mirror(300, 999)]))
        )
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, 111), _mirror(100, 42), _comment(201, 300, 112)]))
        )
        assert res.counts["unmapped"] == 1
        commented = {
            r["subject_uri"]: r["object_uri"]
            for r in st.conn.execute(
                "select subject_uri, object_uri from edges where predicate='commented_on'"
            ).fetchall()
        }
        assert commented == {"tg:user:111": f"tg:msg:{CHANNEL_ID}/42"}
        assert st.conn.execute(
            "select count(*) c from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{CHANNEL_ID}/9002",),
        ).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_member_forwards_do_not_crash_the_mirror_map(tmp_path):
    """Ordinary member forwards are the most common message shape in a real
    discussion group, and every `fwd_from`-bearing fixture elsewhere in this
    file is mirror-shaped (a `PeerChannel` origin plus `channel_post`). A
    member forward's `fwd_from.from_id` is a `PeerUser` with no
    `channel_post` at all, and a hidden-origin forward carries only
    `from_name` with `from_id` absent entirely. `_mirror_map` must treat
    both as "not a mirror" and move on rather than raising `TypeError`/
    `KeyError` — `recipes.collect_channel` only catches
    `SkipAndRecord`/`PhaseStop`/`HardStop` (src/paperboy/recipes.py), so
    anything else here would escape the `discussion` phase and crash the
    whole collect run against a live target."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        user_forward = {
            "_": "Message", "id": 301, "message": "fwd", "date": 1767322445,
            "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
            "from_id": {"_": "PeerUser", "user_id": 113},
            "fwd_from": {"_": "MessageFwdHeader", "from_id": {"_": "PeerUser", "user_id": 900}},
        }
        hidden_forward = {
            "_": "Message", "id": 302, "message": "fwd2", "date": 1767322445,
            "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
            "from_id": {"_": "PeerUser", "user_id": 114},
            "fwd_from": {"_": "MessageFwdHeader", "from_name": "Anon"},
        }
        res = await DiscussionCollector().collect(
            _ctx(
                st,
                _gw([user_forward, hidden_forward, _comment(200, 100, 111), _mirror(100, 42)]),
            )
        )
        # Neither forward is reply-shaped, so neither is an unmapped
        # candidate (amendment 5) -- only the plain comment's mapping counts.
        assert res.counts["unmapped"] == 0
        stored = {
            r["msg_id"]
            for r in st.conn.execute(
                "select msg_id from messages where channel_id=?", (GROUP_ID,)
            ).fetchall()
        }
        assert {301, 302}.issubset(stored)
        commented_subjects = {
            r["subject_uri"]
            for r in st.conn.execute(
                "select subject_uri from edges where predicate='commented_on'"
            ).fetchall()
        }
        assert "tg:user:113" not in commented_subjects
        assert "tg:user:114" not in commented_subjects


def test_applies_to_channel_like_targets():
    assert DiscussionCollector().applies_to(parse_target("@durov"))
    assert not DiscussionCollector().applies_to(parse_target("#osint"))


# --- real captured payload (spec §11: "derive fixtures from real captured
# payloads where possible") --------------------------------------------------

_REAL_CHANNEL_ID = 2541889325
_REAL_GROUP_ID = 2918715880


def _load_fixture(name: str) -> list[dict]:
    path = Path(__file__).parent / "fixtures" / "tl" / name
    return json.loads(path.read_text())


def _real_ctx(st, gw):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        {"channel_id": _REAL_CHANNEL_ID, "access_hash": 9}, _REAL_CHANNEL_ID, "stranger",
        logging.getLogger("t"),
    )


@pytest.mark.asyncio
async def test_real_capture_fixture_maps_and_counts_correctly(tmp_path):
    """`tests/fixtures/tl/discussion_group_history.json` drove no test at
    all before this suite. Its five messages are: a mirror (id 4102,
    channel_post 801); a direct reply to it (4105, a user); a nested reply
    to that (4106, another user); a direct reply to the mirror from an
    anonymous/channel-authored account (4107); and an unmappable, authorless
    reply to a nonexistent thread root (4110, top_id 9999).

    IMPORTANT — only PARTLY a real capture, corrected from a prior docstring
    that overclaimed it: the three peer ids (8867058919, 6877317589,
    2207320787) and channel_post 801 are lifted from a real
    `replies.recent_repliers` payload captured against the live target. The
    group-message ENVELOPE around them is hand-authored, not captured —
    confirmed against `data/default/paperboy.sqlite`: the linked discussion
    group (2918715880) has never actually been swept (`select distinct
    channel_id from messages` returns only the broadcast channel), so no
    real `Message` payload for it exists to capture. Concretely, this
    fixture's `date` is a hand-picked epoch int, where a real capture's
    `to_dict()` serializes `date` as ISO text; and its `MessageFwdHeader`/
    `MessageReplyHeader` objects carry only 2-4 keys, where a real capture
    carries the full null-filled field set (`saved_from_peer`,
    `quote_offset`, `reply_to_peer_id`, `forum_topic`, …). Spec §11's 'derive
    fixtures from real captured payloads where possible' is therefore only
    partially discharged here — capturing a real group-history page once the
    collector can run against the live target is tracked as a follow-up
    (github.com/SpencerNorris/paperboy/issues/19) rather than faked here."""
    with Store.open(tmp_path / "p.sqlite") as st:
        raw_id = st.add_raw("ChatFull", {"_": "ChatFull"}, "stranger", None)
        full_chat = {"_": "channelFull", "id": _REAL_CHANNEL_ID, "pts": 1,
                     "linked_chat_id": _REAL_GROUP_ID, "participants_count": 10}
        chan = {"_": "Channel", "id": _REAL_CHANNEL_ID, "access_hash": 9, "title": "C",
                "username": "c", "broadcast": True}
        upsert_channel(st, full_chat, chan, raw_id, "2026-01-01T00:00:00+00:00")
        group = {"_": "Channel", "id": _REAL_GROUP_ID, "access_hash": 4242,
                 "title": "C Chat", "megagroup": True}
        upsert_peer(st, group, raw_id, "2026-01-01T00:00:00+00:00",
                    seen_in_chat=None, seen_in_msg=None)

        history = _load_fixture("discussion_group_history.json")
        gw = FakeGateway({"history": history, "get_messages": {}})
        res = await DiscussionCollector().collect(_real_ctx(st, gw))

        assert res.counts["unmapped"] == 1  # 4110: no mirror for top_id 9999
        commented = {
            r["subject_uri"]: r["object_uri"]
            for r in st.conn.execute(
                "select subject_uri, object_uri from edges where predicate='commented_on'"
            ).fetchall()
        }
        assert commented == {
            "tg:user:8867058919": f"tg:msg:{_REAL_CHANNEL_ID}/801",
            "tg:user:6877317589": f"tg:msg:{_REAL_CHANNEL_ID}/801",
            "tg:channel:2207320787": f"tg:msg:{_REAL_CHANNEL_ID}/801",
        }
        nested = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{_REAL_GROUP_ID}/4106",),
        ).fetchone()
        assert nested["object_uri"] == f"tg:msg:{_REAL_GROUP_ID}/4105"

        # 4105 and 4107 are DIRECT replies to the mirror (reply_to_msg_id ==
        # reply_to_top_id == 4102) — spec §6/§7 draw no exclusion for that
        # shape, and it's the majority shape here (3 of 4 replies). Both
        # must still get a `replied_to` edge to the mirror.
        direct = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{_REAL_GROUP_ID}/4105",),
        ).fetchone()
        assert direct["object_uri"] == f"tg:msg:{_REAL_GROUP_ID}/4102"
        anon_direct = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{_REAL_GROUP_ID}/4107",),
        ).fetchone()
        assert anon_direct["object_uri"] == f"tg:msg:{_REAL_GROUP_ID}/4102"

        # 4110 is unmapped (no mirror for top_id 9999) but per spec §6 still
        # gets its own `replied_to` edge — it's not a `commented_on` guess,
        # it's a distinct edge kind with its own resolvable target.
        unmapped_reply = st.conn.execute(
            "select object_uri from edges where predicate='replied_to' and subject_uri=?",
            (f"tg:msg:{_REAL_GROUP_ID}/4110",),
        ).fetchone()
        assert unmapped_reply is not None
        assert unmapped_reply["object_uri"] == f"tg:msg:{_REAL_GROUP_ID}/9999"


@pytest.mark.asyncio
async def test_one_commenter_on_two_posts_keeps_both_edges(tmp_path):
    """Amendment 4's dedup is on the FULL triple, not on (subject, predicate).

    A guard written as `WHERE subject_uri=? AND predicate=?` — the natural
    misreading of "one commented_on per commenter" — passes every other test
    in this suite while silently keeping only the first post each regular ever
    commented on. On the live group that is the difference between a real
    person-to-post graph and a single edge per person.
    """
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        gw = _gw([_comment(201, 101, 111), _comment(200, 100, 111),
                  _mirror(101, 43), _mirror(100, 42)])
        await DiscussionCollector().collect(_ctx(st, gw))
        objects = [r["object_uri"] for r in st.conn.execute(
            "select object_uri from edges where predicate='commented_on' "
            "and subject_uri='tg:user:111' order by object_uri"
        )]
        assert objects == [f"tg:msg:{CHANNEL_ID}/42", f"tg:msg:{CHANNEL_ID}/43"]
