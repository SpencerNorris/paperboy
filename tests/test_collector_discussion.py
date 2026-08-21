"""The `discussion` collector: linked-group sweep, comment→post mapping, edges."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from paperboy.collectors.discussion import DiscussionCollector

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer
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
    `iter_history` call was actually given, not just that a call happened."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        gw = _gw([_comment(200, 100, 111), _mirror(100, 42)])
        await DiscussionCollector().collect(_ctx(st, gw))
        assert gw.history_targets == [{"channel_id": GROUP_ID, "access_hash": 4242}]


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
        comments = [_comment(1000 + i, 100, 111) for i in range(149, -1, -1)]
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
        comments = [_comment(1000 + i, 100, 111) for i in range(149, -1, -1)]
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


@pytest.mark.asyncio
async def test_rerunning_collect_does_not_duplicate_thread_edges(tmp_path):
    """`edges` carries no unique index and `add_edge` is a bare `INSERT`
    (`store/edges.py`); `_write_thread_edges` re-scans ALL stored group rows
    on every run. Left unaddressed, every re-run — routine after a
    page-budget `PhaseStop`, per spec §9 — re-emits every `commented_on`/
    `replied_to` edge, so commenter degree in any graph export becomes
    'how many times paperboy ran' rather than a real count. This pins the
    decision that a second identical run must not change edge counts."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        history = [_comment(201, 100, 112, reply_to=200), _comment(200, 100, 111),
                   _mirror(100, 42)]
        await DiscussionCollector().collect(_ctx(st, _gw(history)))
        await DiscussionCollector().collect(_ctx(st, _gw(history)))
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 2
        assert st.conn.execute(
            "select count(*) c from edges where predicate='replied_to'"
        ).fetchone()["c"] == 1


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
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 100, None), _mirror(100, 42)]))
        )
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0
        assert res.counts["unmapped"] == 1


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
    """`tests/fixtures/tl/discussion_group_history.json` was captured from
    the live target and previously drove no test at all — spec §11's
    'fixtures derive from real captured payloads where possible, not
    hand-authored shapes' (the `is_self` vs `self` bug found during P0 is
    the argument for this) was unmet in practice. Its five messages are: a
    mirror (id 4102, channel_post 801); a direct reply to it (4105, a
    user); a nested reply to that (4106, another user); a direct reply to
    the mirror from an anonymous/channel-authored account (4107); and an
    unmappable, authorless reply to a nonexistent thread root (4110, top_id
    9999)."""
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
