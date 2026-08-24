# `discussion` Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect a broadcast channel's linked discussion group — the comment threads and the people in them — passively, without joining anything.

**Architecture:** `HistoryCollector.collect()` gains keyword-only `channel_id` / `input_channel` / `probe_gaps` parameters whose `None`/`True` defaults reproduce today's behaviour exactly. A new `DiscussionCollector` reuses it against the linked group with gap-probing off, then maps comments back to the channel posts they hang off and emits `commented_on` / `replied_to` edges. A zero-RPC pass over already-stored payloads harvests `recent_repliers` first.

**Tech Stack:** Python ≥3.12, `uv`, stdlib `sqlite3`, pytest + pytest-asyncio, ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-08-21-discussion-collector-design.md`

## Global Constraints

- Read-only. Never join, react, vote, mark read, or call `users.suggestBirthday`.
- Every RPC goes through the gateway; no collector calls Telethon directly.
- Raw first: every TL object hits `raw_records` before any projection.
- `min` peers are stored with `(seen_in_chat, seen_in_msg)` provenance.
- Timestamps are ISO-8601 UTC text via `utc_now_iso()`.
- Telethon's `to_dict()` emits **PascalCase** `_` discriminators (`"PeerUser"`, `"Message"`) and `is_self` (not `self`). Match case-insensitively.
- `_HISTORY_PAGE_SIZE` is **100**.
- Line length 100 (ruff). `uv run ruff check` and `uv run pyright` must be clean.
- **Regression contract:** `tests/test_collector_history.py` and `tests/test_history_catchup.py` must pass **unmodified**. Editing either is a design violation, not a test fix.

---

## Amendments (2026-08-24) — these supersede any conflicting code below

The Opus test gate rejected the first attempt three times. Every surviving
finding was a defect in the **spec**, not in the tests, so the spec was
corrected and this block carries the deltas. Where the verbatim code further
down disagrees with anything here, **this block wins**.

**1. The sweep was one-shot — CORRECTED, AND TASK 1 IS ALREADY DONE.**
The first version of this amendment was *unsatisfiable*: it wrote a three-key
dict into `sync_state('history', <id>)` and reset `offset_id` to 0 on
exhaustion, while the protected `test_backfill_persists_resume_cursor` asserts
exact equality with `{"offset_id": 3}` and `set_state` replaces `value_json`
wholesale. The spec contradicted the regression contract it also stated. The
Opus gate caught it by applying the patch and running the file.

The satisfiable design, now implemented and committed in `66f5e93`:

- `sync_state('history', <id>)` **keeps its shipped `{"offset_id": int}` shape.**
- Sweep progress moves to a new scope:
  `sync_state('history_sweep', <id>) = {"max_id_seen": int, "backfill_complete": bool}`.
- The cursor is **never reset**. Incremental mode ignores the stored cursor and
  starts from 0 (newest), stopping once `cursor <= max_id_seen-as-of-run-start`.
  Comparing against the run-start value, not the live one, is essential — the
  live value would stop the loop on its own first page.

Verified: `uv run pytest tests/test_collector_history.py tests/test_history_catchup.py -q`
→ **21 passed**, no test edited. Full suite 220 passed. ruff and pyright clean.

**Task 1 needs no implementer.** `src/paperboy/collectors/history.py` and
`src/paperboy/store/peers.py` are done. Amendments 2 and 3 below are also
already implemented there. Do not re-do or re-open that file.

**2. `add_range` only when `probe_gaps` is true.** *(done in `66f5e93`)* `store/sync.py` defines a
range as "verified-complete — every id in the span was either stored or
probed". With probing off that is false, and writing it makes `missing_ids()`
report zero gaps for the group forever. Move the call inside the
`if probe_gaps:` branch. The group gets no `sync_ranges` rows.

**3. `PeerChannel` authors are projected.** *(done in `66f5e93`)*
`_observe_message` upserts a peer only for `PeerUser` today. Anonymous and
channel-authored comments carry `PeerChannel` and are people-discovery data, so
widen it to both. This belongs in `history.py`; Task 3 must not grow a second
peer pass.

**4. Both thread edges are idempotent on `(subject_uri, predicate, object_uri)`.**
`_write_thread_edges` re-scans every stored group row on every run — it must, so
a comment paged in before its mirror still maps — so an unguarded insert appends
a fresh row with a fresh `observed_at` and the *previous* run's `source_raw_id`,
for evidence this run never gathered: ~70k phantom rows per re-run on the live
target. Skip the insert when an identical triple already exists.

**5. `unmapped` counts mapping failures, not non-candidates.** Only a message
carrying a `reply_to_top_id` is a candidate. Ordinary in-group replies
(`reply_to_top_id` NULL) get their `replied_to` edge and are **not** unmapped.
Counting them would swamp the counter on the live target and destroy the only
signal risk §13.3 relies on.

**6. The two skip reasons must be lexically disjoint.** Use
`"no linked discussion group"` and `"discussion group {id}: no access hash
known"`. The old pair both contained "linked", so a test asserting
`"linked" in stopped` passed on the wrong branch — including on the falsy-`0`
bug it existed to catch. Also guard a **falsy** `access_hash`, not just `None`:
a stored `0` yields `CHANNEL_INVALID` against live Telegram.


**7. `_write_thread_edges`'s verbatim code in Task 3 is wrong in two ways.**
The plan's version guards the reply edge with
`if row["reply_to_msg_id"] and row["reply_to_msg_id"] != row["reply_to_top_id"]`
and selects `WHERE channel_id=? AND reply_to_top_id IS NOT NULL`. Four committed
tests contradict both. **Drop the `!=` clause** (keep the truthiness check) — a
direct reply to a thread root still earns a `replied_to` edge. **Widen the
query** to `WHERE channel_id=? AND (reply_to_msg_id IS NOT NULL OR
reply_to_top_id IS NOT NULL)` — an ordinary in-group reply carries
`reply_to_msg_id` with a NULL `reply_to_top_id`, and amendment 5 already says it
gets its edge. `unmapped` still increments only for rows that carry a
`reply_to_top_id`.

**8. `FakeGateway` needs `history_targets`, and Task 2 owns it.**
`test_sweeps_the_linked_groups_own_peer_not_the_broadcast_channels` asserts on
`gw.history_targets` — the only test able to catch a sweep silently pointed at
the broadcast channel or built with the wrong access hash. Add
`self.history_targets: list[dict] = []` in `FakeGateway.__init__` and
`self.history_targets.append(dict(input_channel))` at the top of
`iter_history`, *before* the existing `del input_channel`, alongside the `calls`
entry. Note the test expects **two** entries for a completed sweep: a page loop
always makes a final empty call to terminate.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/paperboy/collectors/history.py` | *Modify.* Page loop gains an explicit target + `probe_gaps` switch. |
| `src/paperboy/collectors/discussion.py` | *Create.* Preflight, sweep delegation, mirror mapping, edge emission. |
| `src/paperboy/store/repliers.py` | *Create.* Zero-RPC `recent_repliers` harvest. Own file: it reads `raw_records` and writes `peers`/`edges` with no gateway involvement, so it is independently testable and belongs beside the other store projections. |
| `src/paperboy/config.py` | *Modify.* `discussion_page_budget`. |
| `src/paperboy/recipes.py` | *Modify.* Register `DiscussionCollector` after `HistoryCollector`. |
| `src/paperboy/cli.py` | *Modify.* `--phases` help text + dependent-phase list. |
| `src/paperboy/gateway.py` | *Modify.* `FakeGateway.calls` counter (test 12 needs it). |
| `docs/data-model.md` | *Modify.* Document `commented_on` / `replied_to` shapes. |

**Task dependency graph.** Task 0 gates everything. Tasks 1, 2 and 3 are then independent — their interfaces are pinned below so three implementers can work simultaneously without waiting on each other. Task 4 needs Task 3's class to exist.

```
Task 0 (tests) ──► [ Task 1 │ Task 2 │ Task 3 ] ──► Task 4
```

---

### Task 0: The test suite (written first, by a dedicated author)

**Files:**
- Create: `tests/test_collector_discussion.py`
- Create: `tests/test_store_repliers.py`
- Create: `tests/fixtures/tl/discussion_group_history.json`
- Modify: `tests/test_collector_history.py` — **APPEND ONLY.** Existing tests must not change.

**Interfaces:**
- Consumes: nothing.
- Produces: the executable specification every later task is measured against.

**This task writes tests only. No production code. Tests are expected to fail — that is the deliverable.** Every later task's Step 1 is "run these and watch them fail for the right reason."

- [ ] **Step 1: Write the `HistoryCollector` generalization tests (append to `tests/test_collector_history.py`)**

```python
# --- generalization: explicit target + probe_gaps switch -------------------


@pytest.mark.asyncio
async def test_collect_defaults_to_the_context_channel(tmp_path):
    """No kwargs => today's behaviour, bit for bit."""
    gw = FakeGateway({"history": [_msg(5, 2), _msg(5, 1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        rows = st.conn.execute("select channel_id from messages").fetchall()
        assert {r["channel_id"] for r in rows} == {5}
        assert res.counts["messages"] == 2


@pytest.mark.asyncio
async def test_collect_targets_an_explicit_channel(tmp_path):
    gw = FakeGateway({"history": [_msg(77, 2), _msg(77, 1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        await HistoryCollector().collect(
            _ctx(st, gw), channel_id=77, input_channel={"channel_id": 77, "access_hash": 3},
        )
        rows = st.conn.execute("select channel_id from messages").fetchall()
        assert {r["channel_id"] for r in rows} == {77}


@pytest.mark.asyncio
async def test_probe_gaps_false_writes_no_tombstones(tmp_path):
    # ids 1 and 3 present, 2 missing: probing ON would tombstone 2.
    gw = FakeGateway({
        "history": [_msg(5, 3), _msg(5, 1)],
        "get_messages": {2: {"_": "MessageEmpty", "id": 2}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw), probe_gaps=False)
        assert res.counts["tombstones"] == 0
        assert st.conn.execute("select count(*) c from message_tombstones").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_probe_gaps_true_still_tombstones(tmp_path):
    gw = FakeGateway({
        "history": [_msg(5, 3), _msg(5, 1)],
        "get_messages": {2: {"_": "MessageEmpty", "id": 2}},
    })
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await HistoryCollector().collect(_ctx(st, gw))
        assert res.counts["tombstones"] == 1


@pytest.mark.asyncio
async def test_explicit_target_resumes_on_its_own_cursor(tmp_path):
    gw = FakeGateway({"history": [_msg(77, 2), _msg(77, 1)], "get_messages": {}})
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "history", "5", {"offset_id": 999})
        await HistoryCollector().collect(
            _ctx(st, gw), channel_id=77, input_channel={"channel_id": 77, "access_hash": 3},
        )
        assert get_state(st, "history", "5") == {"offset_id": 999}
        assert get_state(st, "history", "77") == {"offset_id": 1}
```

Add at the top of the file if not already present:
`from paperboy.store.sync import get_state, set_state`. Reuse the file's existing `_ctx` and message-builder helpers; if the file names them differently, use its names rather than introducing `_msg`.

- [ ] **Step 2: Write `tests/test_store_repliers.py`**

```python
"""`recent_repliers` arrives free inside every stored Message payload."""

from __future__ import annotations

import json

from paperboy.store.db import Store
from paperboy.store.repliers import backfill_recent_repliers


def _post(store: Store, channel_id: int, msg_id: int, repliers: list[dict]) -> None:
    store.add_raw(
        "Message",
        {
            "_": "Message", "id": msg_id, "peer_id": {"_": "PeerChannel", "channel_id": channel_id},
            "replies": {
                "_": "MessageReplies", "comments": True, "channel_id": 2918715880,
                "replies": len(repliers), "recent_repliers": repliers,
            },
        },
        "stranger",
        {"channel_id": channel_id},
    )


def test_projects_peeruser_repliers_into_peers(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        n = backfill_recent_repliers(st, 5, "stranger")
        assert n == 1
        row = st.conn.execute("select kind, is_min from peers where uri='tg:user:111'").fetchone()
        assert row["kind"] == "user"
        assert row["is_min"] == 1


def test_projects_peerchannel_repliers_too(tmp_path):
    """The live capture contains a PeerChannel replier — do not assume users."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerChannel", "channel_id": 2207320787}])
        backfill_recent_repliers(st, 5, "stranger")
        row = st.conn.execute(
            "select kind from peers where uri='tg:channel:2207320787'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "channel"


def test_records_min_provenance_pointing_at_the_post(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        backfill_recent_repliers(st, 5, "stranger")
        row = st.conn.execute(
            "select seen_in_chat, seen_in_msg from peers where uri='tg:user:111'"
        ).fetchone()
        assert row["seen_in_chat"] == 5
        assert row["seen_in_msg"] == 10


def test_emits_commented_on_from_person_to_post(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        backfill_recent_repliers(st, 5, "stranger")
        row = st.conn.execute(
            "select subject_uri, object_uri from edges where predicate='commented_on'"
        ).fetchone()
        assert row["subject_uri"] == "tg:user:111"
        assert row["object_uri"] == "tg:msg:5/10"


def test_counts_distinct_peers_not_occurrences(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        _post(st, 5, 11, [{"_": "PeerUser", "user_id": 111}])
        assert backfill_recent_repliers(st, 5, "stranger") == 1


def test_posts_without_repliers_are_ignored(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        st.add_raw("Message", {"_": "Message", "id": 10}, "stranger", {"channel_id": 5})
        assert backfill_recent_repliers(st, 5, "stranger") == 0
        assert st.conn.execute("select count(*) c from peers").fetchone()["c"] == 0


def test_is_idempotent_across_runs(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        backfill_recent_repliers(st, 5, "stranger")
        backfill_recent_repliers(st, 5, "stranger")
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 1
```

- [ ] **Step 3: Write `tests/test_collector_discussion.py`**

```python
"""The `discussion` collector: linked-group sweep, comment→post mapping, edges."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from paperboy.collectors.base import CollectContext
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.config import load_settings
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77


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


def _mirror(msg_id: int, channel_post: int) -> dict:
    """The group's auto-forwarded copy of a channel post."""
    return {
        "_": "Message", "id": msg_id, "message": "", "date": 1767322445,
        "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID},
        "fwd_from": {"_": "MessageFwdHeader", "channel_post": channel_post,
                     "from_id": {"_": "PeerChannel", "channel_id": CHANNEL_ID}},
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
async def test_skips_when_the_group_requires_joining_to_read(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID, {"join_to_send": True})
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is not None
        assert "join" in res.stopped.lower()


@pytest.mark.asyncio
async def test_skips_when_the_group_access_hash_is_unknown(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        st.conn.execute("update peers set access_hash=NULL where uri=?",
                        (f"tg:channel:{GROUP_ID}",))
        res = await DiscussionCollector().collect(_ctx(st, _gw([])))
        assert res.stopped is not None
        assert "access" in res.stopped.lower()


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
async def test_no_gap_tombstones_are_written_for_the_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(203, 100, 111), _comment(200, 100, 111), _mirror(100, 42)]))
        )
        assert st.conn.execute("select count(*) c from message_tombstones").fetchone()["c"] == 0


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
async def test_unmappable_comment_is_stored_and_counted_never_guessed(tmp_path):
    """top_id 999 has no mirror: store it, give it replied_to, no commented_on."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        res = await DiscussionCollector().collect(
            _ctx(st, _gw([_comment(200, 999, 111)]))
        )
        assert res.counts["unmapped"] == 1
        assert st.conn.execute(
            "select count(*) c from messages where msg_id=200"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 0


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
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, None)
        gw = _gw([])
        await DiscussionCollector().collect(_ctx(st, gw))
        assert gw.calls == []


@pytest.mark.asyncio
async def test_page_budget_exhaustion_stops_the_phase_and_keeps_the_cursor(tmp_path):
    from paperboy.budget import PhaseStop
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_channel(st, GROUP_ID)
        ctx = _ctx(st, _gw([_comment(i, 100, 111) for i in range(300, 0, -1)]))
        ctx.settings.discussion_page_budget = 1
        with pytest.raises(PhaseStop):
            await DiscussionCollector().collect(ctx)
        from paperboy.store.sync import get_state
        assert get_state(st, "history", str(GROUP_ID)) is not None


def test_applies_to_channel_like_targets():
    assert DiscussionCollector().applies_to(parse_target("@durov"))
    assert not DiscussionCollector().applies_to(parse_target("#osint"))
```

- [ ] **Step 4: Run the whole suite — new tests fail, old tests pass**

Run: `uv run pytest -q`
Expected: the ~25 new tests fail with `ModuleNotFoundError: paperboy.collectors.discussion`, `ModuleNotFoundError: paperboy.store.repliers`, and `TypeError: collect() got an unexpected keyword argument`. **All 212 pre-existing tests still pass.** If any pre-existing test fails, stop — the test author has broken the regression contract.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(discussion): executable spec for the discussion collector

Tests only, all failing by design. Written before implementation so the
suite is the contract three implementers are measured against rather than
a description of whatever they happened to build."
```

> **OPUS REVIEW GATE 1 — the strictest gate in this plan.**
> A wrong test here is worse than a wrong implementation: it will be faithfully satisfied, look green, and corrupt every downstream review. Reviewers must check specifically for (a) assertions on implementation details rather than on the spec's behaviour, (b) tests that pass vacuously, (c) **missing negative cases** — the unmapped/authorless/no-group paths are where this feature will actually break, and (d) any edit to the two protected history test files. Do not start Tasks 1–3 until this gate clears.

---

### Task 1: Generalize `HistoryCollector`

**Files:**
- Modify: `src/paperboy/collectors/history.py:45-89` — **this task is the sole
  owner of this file.** No other task may edit it.

**Interfaces:**
- Consumes: nothing.
- Produces — Task 3 codes against this exact signature:
  ```python
  async def collect(
      self,
      ctx: CollectContext,
      *,
      channel_id: int | None = None,
      input_channel: dict | None = None,
      probe_gaps: bool = True,
  ) -> CollectResult
  ```
  `None` means "fall back to `ctx.*`". Returns `CollectResult(name="history", counts={"messages","revisions","tombstones","edges"})`.

- [ ] **Step 1: Run the Task 0 tests and watch them fail**

Run: `uv run pytest tests/test_collector_history.py -q`
Expected: the five appended tests fail with `TypeError: collect() got an unexpected keyword argument 'channel_id'`. The pre-existing tests in the same file pass.

- [ ] **Step 2: Change the signature and the two guarded lines**

Replace lines 45-56 of `src/paperboy/collectors/history.py`:

```python
    async def collect(
        self,
        ctx: CollectContext,
        *,
        channel_id: int | None = None,
        input_channel: dict | None = None,
        probe_gaps: bool = True,
    ) -> CollectResult:
        """Backfill one channel's history newest→oldest.

        By default the target is the context's channel — the Phase 1 behaviour.
        `channel_id`/`input_channel` retarget the same page loop at another
        channel (the `discussion` collector points it at a linked group), and
        `probe_gaps=False` suppresses the deletion-probing second pass, which
        is prohibitively expensive on a large, churn-heavy group and yields
        only the weak `evidence='gap'` tier.
        """
        channel_id = channel_id if channel_id is not None else ctx.channel_id
        input_channel = input_channel if input_channel is not None else ctx.input_channel
        if input_channel is None or channel_id is None:
            # The `channel` phase didn't complete (e.g. it raised `PhaseStop`
            # on a FLOOD_WAIT during resolve()/getFullChannel before setting
            # these) — a handled disposition the recipe layer records and
            # continues past, never a bare `AssertionError` crash.
            raise PhaseStop(
                "history skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {"messages": 0, "revisions": 0, "tombstones": 0, "edges": 0}
```

- [ ] **Step 3: Use the local `input_channel` in the page loop**

In the same method, replace `ctx.input_channel` with `input_channel` in the `iter_history` call (was line 69):

```python
                async for m in ctx.gateway.iter_history(
                    input_channel, offset_id=cursor, limit=_HISTORY_PAGE_SIZE
                )
```

- [ ] **Step 4: Gate the gap probe and pass the target through**

Replace the tail of `collect` (was lines 85-89):

```python
        if min_id is not None and max_id is not None:
            if probe_gaps:
                await self._probe_gaps(
                    ctx, channel_id, input_channel, min_id, max_id, ids_seen, counts
                )
            add_range(ctx.store, channel_id, min_id, max_id)

        return CollectResult(name=self.name, counts=counts)
```

- [ ] **Step 5: Take the target as a parameter in `_probe_gaps`**

In `_probe_gaps`, add `input_channel: dict,` after `channel_id: int,` in the signature, delete the `assert ctx.input_channel is not None` line, and change the RPC call to use it:

```python
            results = await ctx.gateway.get_messages(input_channel, chunk)
```

- [ ] **Step 6: Add the page-budget stop**

The sweep must not run unbounded. Add a `page_budget: int | None = None`
keyword to `collect()` (documented as "max pages; `None` = unbounded, the
Phase 1 default"), and count pages inside the `while True:` loop:

```python
        pages = 0
        while True:
            ...
            cursor = min(m["id"] for m in page)
            set_state(ctx.store, "history", str(channel_id), {"offset_id": cursor})
            pages += 1
            if page_budget is not None and pages >= page_budget:
                raise PhaseStop(
                    f"page budget ({page_budget}) reached at offset_id={cursor}; "
                    "re-run to continue from the saved cursor"
                )
```

The check sits **after** `set_state`, so the cursor is always persisted before
the stop and a re-run resumes rather than restarts.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_collector_history.py tests/test_history_catchup.py -q`
Expected: all pass, including the five new ones. **If you needed to edit any pre-existing test, revert and rethink — that is the regression contract failing.**

- [ ] **Step 8: Lint, type-check, commit**

```bash
uv run ruff check && uv run pyright
git add src/paperboy/collectors/history.py
git commit -m "refactor(history): let collect() target an explicit channel

Adds keyword-only channel_id/input_channel/probe_gaps whose defaults
reproduce current behaviour exactly, so `discussion` can reuse the page
loop against a linked group instead of duplicating cursor, resumability
and FLOOD_WAIT handling. Existing history tests pass unmodified."
```

---

### Task 2: `recent_repliers` backfill

**Files:**
- Create: `src/paperboy/store/repliers.py`
- Modify: `src/paperboy/gateway.py` — add a `calls` list to `FakeGateway`

**Interfaces:**
- Consumes: `upsert_peer`, `add_edge`, `peer_ref_uri`, `msg_uri`, `utc_now_iso` — all existing.
- Produces — Task 3 calls exactly this:
  ```python
  def backfill_recent_repliers(store: Store, channel_id: int, tier: str) -> int
  ```
  Returns the count of **distinct** peers projected.

- [ ] **Step 1: Run the Task 0 tests and watch them fail**

Run: `uv run pytest tests/test_store_repliers.py -q`
Expected: `ModuleNotFoundError: No module named 'paperboy.store.repliers'`.

- [ ] **Step 2: Create `src/paperboy/store/repliers.py`**

```python
"""Harvest `messageReplies.recent_repliers` from already-stored payloads.

Telegram attaches a handful of recent commenters to every post that has a
comment thread, and it costs nothing: the field arrives inside the `Message`
object the `history` collector already wrote to `raw_records`. Projecting it
is therefore pure store work — no gateway, no RPC, no join — which is why it
lives beside the other projections rather than inside a collector.

The sample only survives on recent posts, so this complements the full
discussion sweep and never replaces it.
"""

from __future__ import annotations

import json

from paperboy.ids import msg_uri, peer_ref_uri, utc_now_iso
from paperboy.store.db import Store
from paperboy.store.edges import add_edge
from paperboy.store.peers import upsert_peer

_COMMENTED_ON = "commented_on"

# `PeerUser`/`PeerChannel` -> the `_` a peer projection expects. The live
# capture contains both: an anonymous admin commenting as the channel shows
# up as a PeerChannel, not a PeerUser.
_PEER_STUB_KIND = {"peeruser": ("User", "user_id"), "peerchannel": ("Channel", "channel_id")}


def backfill_recent_repliers(store: Store, channel_id: int, tier: str) -> int:
    """Project every `recent_repliers` peer found in this channel's stored
    `Message` payloads into `peers`, with a `commented_on` edge to the post.

    Returns the number of distinct peers projected. Idempotent: `upsert_peer`
    and `add_edge` both no-op on a repeat observation of the same fact.
    """
    rows = store.conn.execute(
        "SELECT id, payload_json FROM raw_records WHERE kind='Message'"
    ).fetchall()

    seen: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload_json"])
        repliers = ((payload.get("replies") or {}).get("recent_repliers")) or []
        if not repliers:
            continue
        post_id = payload.get("id")
        if post_id is None:
            continue
        observed_at = utc_now_iso()
        post_uri = msg_uri(channel_id, post_id)
        for peer in repliers:
            uri = peer_ref_uri(peer)
            if uri is None:
                continue
            stub = _peer_stub(peer)
            if stub is None:
                continue
            # `min`: a bare peer reference carries no name or username, so say
            # so honestly rather than writing a hollow authoritative row.
            upsert_peer(
                store, stub, row["id"], observed_at,
                seen_in_chat=channel_id, seen_in_msg=post_id,
            )
            add_edge(
                store, uri, _COMMENTED_ON, post_uri, observed_at, tier, row["id"],
                {"source": "recent_repliers"},
            )
            seen.add(uri)
    return len(seen)


def _peer_stub(peer: dict) -> dict | None:
    """A minimal projectable object for a bare `Peer*` reference."""
    mapped = _PEER_STUB_KIND.get((peer.get("_") or "").lower())
    if mapped is None:
        return None
    tag, id_field = mapped
    peer_id = peer.get(id_field)
    return None if peer_id is None else {"_": tag, "id": peer_id, "min": True}
```

- [ ] **Step 3: Add a call counter to `FakeGateway`**

In `src/paperboy/gateway.py`, in `FakeGateway.__init__` beside `self.download_media_calls`:

```python
        self.calls: list[str] = []
```

Then record the call at the top of each async method on `FakeGateway` — `resolve`, `get_full_channel`, `get_self`, `iter_history`, `get_messages`, `get_channel_difference`, `download_media`, `get_channel_recommendations`, `check_chat_invite`, `get_sponsored_messages`, `get_authorizations`, `get_password_state`, `get_privacy` — e.g.:

```python
    async def resolve(self, target_value: str) -> dict:
        self.calls.append("resolve")
        del target_value
        return self._fx["resolve"]
```

A test asserting "no RPCs happened" must be able to fail. Without this it can only assume.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_store_repliers.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, full suite, commit**

```bash
uv run ruff check && uv run pyright && uv run pytest -q
git add src/paperboy/store/repliers.py src/paperboy/gateway.py
git commit -m "feat(store): project recent_repliers into peers and commented_on edges

Telegram attaches recent commenters to every post with a comment thread and
we were discarding it. Pure store work, zero RPCs. Handles PeerChannel as
well as PeerUser — the live capture contains both.

Also gives FakeGateway a call counter so a zero-RPC assertion can actually
fail rather than being assumed."
```

---

### Task 3: `DiscussionCollector`

**Files:**
- Create: `src/paperboy/collectors/discussion.py`
- Modify: `src/paperboy/config.py:49` — add `discussion_page_budget: int = 500`
- **Must not touch** `src/paperboy/collectors/history.py` (Task 1 owns it).

**Interfaces:**
- Consumes: `HistoryCollector.collect(ctx, *, channel_id, input_channel, probe_gaps)` from Task 1; `backfill_recent_repliers(store, channel_id, tier) -> int` from Task 2. Code against these signatures — do not wait for those tasks to land.
- Produces: `DiscussionCollector` with `name = "discussion"`, satisfying the `Collector` Protocol.

- [ ] **Step 1: Run the Task 0 tests and watch them fail**

Run: `uv run pytest tests/test_collector_discussion.py -q`
Expected: `ModuleNotFoundError: No module named 'paperboy.collectors.discussion'`.

- [ ] **Step 2: Add the budget setting**

In `src/paperboy/config.py`, after `profile_budget: int = 2000`:

```python
    discussion_page_budget: int = 500
```

- [ ] **Step 3: Create `src/paperboy/collectors/discussion.py`**

```python
"""The `discussion` collector: the linked group's comments, and the people in them.

For a broadcast channel a non-admin account can enumerate nothing about
subscribers — not the member list, not even the admin list. The linked
discussion group is therefore *the* person vector, and reading it never
requires joining unless the group sets `join_to_send`
(docs/research/telegram-extraction-surface.md).

The subtle part is mapping a comment back to the post it hangs off.
`reply_to_top_id` is **not** the channel post id: it is the id of the group's
auto-forwarded mirror of that post. The mirror carries the real post id in
`fwd_from.channel_post`, so the chain is
`comment.reply_to_top_id -> mirror.id -> mirror.fwd_from.channel_post -> post`.
A comment that does not resolve is stored and counted, never guessed at.
"""

from __future__ import annotations

import json

from paperboy.budget import PhaseStop
from paperboy.collectors.base import CollectContext, CollectResult
from paperboy.collectors.history import HistoryCollector
from paperboy.ids import msg_uri, peer_ref_uri, utc_now_iso
from paperboy.store.edges import add_edge
from paperboy.store.repliers import backfill_recent_repliers
from paperboy.targets import Target

_COMMENTED_ON = "commented_on"
_REPLIED_TO = "replied_to"


class DiscussionCollector:
    name = "discussion"

    def applies_to(self, target: Target) -> bool:
        return target.is_channel_like

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if ctx.channel_id is None:
            raise PhaseStop(
                "discussion skipped: channel context not established "
                "(channel phase did not complete)"
            )
        counts = {
            "messages": 0, "revisions": 0, "tombstones": 0, "edges": 0,
            "backfilled_peers": 0, "unmapped": 0,
        }

        # Zero-RPC harvest first: it works even when the group turns out to be
        # absent or unreadable, so it must not sit behind preflight.
        counts["backfilled_peers"] = backfill_recent_repliers(
            ctx.store, ctx.channel_id, ctx.tier
        )

        target = self._linked_group(ctx)
        if isinstance(target, str):
            return CollectResult(name=self.name, counts=counts, stopped=target)
        group_id, input_channel = target

        sweep = await HistoryCollector().collect(
            ctx, channel_id=group_id, input_channel=input_channel, probe_gaps=False
        )
        for key in ("messages", "revisions", "tombstones", "edges"):
            counts[key] += sweep.counts.get(key, 0)

        self._write_thread_edges(ctx, group_id, counts)
        return CollectResult(name=self.name, counts=counts)

    def _linked_group(self, ctx: CollectContext) -> tuple[int, dict] | str:
        """`(group_id, input_channel)`, or a `stopped` reason string.

        Every failure here is a clean skip, never an exception: a channel with
        no discussion group, or one that gates reading behind membership, is a
        normal thing to encounter, not an error.
        """
        row = ctx.store.conn.execute(
            "SELECT linked_chat_id FROM channels WHERE id=?", (ctx.channel_id,)
        ).fetchone()
        group_id = row["linked_chat_id"] if row else None
        if not group_id:
            return "no linked discussion group"

        peer = ctx.store.conn.execute(
            "SELECT access_hash, flags_json FROM peers WHERE uri=?",
            (f"tg:channel:{group_id}",),
        ).fetchone()
        if peer is None or peer["access_hash"] is None:
            return f"linked group {group_id}: no access hash known"

        flags = json.loads(peer["flags_json"]) if peer["flags_json"] else {}
        if flags.get("join_to_send"):
            # Reading is open to anyone *unless* this flag is set; honouring it
            # is what keeps collection passive.
            return f"linked group {group_id}: join_to_send set, reading requires membership"

        return group_id, {"channel_id": group_id, "access_hash": peer["access_hash"]}

    def _write_thread_edges(
        self, ctx: CollectContext, group_id: int, counts: dict[str, int]
    ) -> None:
        """Emit `commented_on` (person → channel post) and `replied_to`
        (comment → parent comment) from the rows the sweep just stored.

        Runs over stored rows rather than the live page stream so a resumed
        run maps comments whose mirror was paged in during an earlier run.
        """
        mirrors = self._mirror_map(ctx, group_id)
        rows = ctx.store.conn.execute(
            "SELECT uri, msg_id, from_uri, reply_to_msg_id, reply_to_top_id, source_raw_id "
            "FROM messages WHERE channel_id=? AND reply_to_top_id IS NOT NULL",
            (group_id,),
        ).fetchall()

        for row in rows:
            observed_at = utc_now_iso()
            if row["reply_to_msg_id"] and row["reply_to_msg_id"] != row["reply_to_top_id"]:
                add_edge(
                    ctx.store, row["uri"], _REPLIED_TO,
                    msg_uri(group_id, row["reply_to_msg_id"]),
                    observed_at, ctx.tier, row["source_raw_id"], None,
                )
                counts["edges"] += 1

            post_id = mirrors.get(row["reply_to_top_id"])
            if post_id is None or not row["from_uri"]:
                # No mirror for this thread root, or an authorless comment —
                # both are counted and reported rather than attributed wrongly.
                counts["unmapped"] += 1
                continue
            add_edge(
                ctx.store, row["from_uri"], _COMMENTED_ON,
                msg_uri(ctx.channel_id, post_id),
                observed_at, ctx.tier, row["source_raw_id"],
                {"comment_uri": row["uri"]},
            )
            counts["edges"] += 1

    def _mirror_map(self, ctx: CollectContext, group_id: int) -> dict[int, int]:
        """`group_msg_id -> channel_post_id` for the group's auto-forwarded
        copies of channel posts. Rebuilt from stored rows on every run, so it
        survives resumption."""
        rows = ctx.store.conn.execute(
            "SELECT msg_id, fwd_json FROM messages "
            "WHERE channel_id=? AND fwd_json IS NOT NULL",
            (group_id,),
        ).fetchall()
        mirrors: dict[int, int] = {}
        for row in rows:
            fwd = json.loads(row["fwd_json"])
            post = fwd.get("channel_post")
            origin = peer_ref_uri(fwd.get("from_id"))
            if post is not None and origin == f"tg:channel:{ctx.channel_id}":
                mirrors[row["msg_id"]] = post
        return mirrors
```

- [ ] **Step 4: Pass the page budget down**

`HistoryCollector` owns the page counter (Task 1, Step 6). `discussion` only
supplies the value:

```python
        sweep = await HistoryCollector().collect(
            ctx, channel_id=group_id, input_channel=input_channel, probe_gaps=False,
            page_budget=ctx.settings.discussion_page_budget,
        )
```

**Do not edit `src/paperboy/collectors/history.py` in this task.** Task 1 owns
that file; touching it here is what would make these two tasks collide.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_collector_discussion.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, full suite, commit**

```bash
uv run ruff check && uv run pyright && uv run pytest -q
git add src/paperboy/collectors/discussion.py src/paperboy/config.py src/paperboy/collectors/history.py
git commit -m "feat(discussion): collect the linked group's comments and commenters

Reuses the history page loop against the linked group with gap-probing off,
then maps each comment back to its channel post through the group's
auto-forwarded mirror and emits commented_on (person -> post) and
replied_to (comment -> parent). Unmappable and authorless comments are
counted and reported rather than attributed to the wrong post."
```

---

### Task 4: Wire it into the run

**Files:**
- Modify: `src/paperboy/recipes.py:45-57`
- Modify: `src/paperboy/cli.py:134-135`, `:168-170`
- Modify: `docs/data-model.md` — the `edges` predicate table

**Interfaces:**
- Consumes: `DiscussionCollector` from Task 3.
- Produces: `discussion` as a selectable phase.

- [ ] **Step 1: Write the failing test (append to `tests/test_recipe.py`)**

```python
def test_discussion_runs_by_default_after_history():
    from paperboy.recipes import _default_collectors
    names = [c.name for c in _default_collectors(include_media=False, include_web=False)]
    assert "discussion" in names
    assert names.index("discussion") == names.index("history") + 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_recipe.py::test_discussion_runs_by_default_after_history -v`
Expected: FAIL — `'discussion' not in [...]`.

- [ ] **Step 3: Register the collector**

In `src/paperboy/recipes.py`, add the import beside the others:

```python
from paperboy.collectors.discussion import DiscussionCollector
```

and change the default list:

```python
    collectors: list[Collector] = [
        ChannelCollector(), HistoryCollector(), DiscussionCollector(), GraphCollector(),
    ]
```

- [ ] **Step 4: Update the CLI**

In `src/paperboy/cli.py` line 135, change the help text to:

```python
        None, "--phases", help="Comma-separated: channel,history,discussion,graph,web,media"
```

and add `"discussion"` to the `_dependent_phases` list at line 168 — it needs `channel` for `linked_chat_id` and the group's access hash.

- [ ] **Step 5: Document the edge shapes**

In `docs/data-model.md`, under the `edges` table, add after the predicate row:

```markdown
Predicate shapes worth stating explicitly, because subject/object order is not
guessable:

| Predicate | Subject | Object |
|---|---|---|
| `commented_on` | `tg:user:<id>` or `tg:channel:<id>` (the commenter) | `tg:msg:<channel_id>/<post_id>` (the channel post) |
| `replied_to` | `tg:msg:<group_id>/<comment_id>` | `tg:msg:<group_id>/<parent_id>` |
| `member_of` | `tg:user:<id>` | `tg:invite:<hash>` or `tg:channel:<id>` |

`commented_on` is emitted both by the `discussion` sweep and by the zero-RPC
`recent_repliers` harvest; the latter sets `evidence_json.source =
"recent_repliers"`.
```

- [ ] **Step 6: Run the full suite, lint, type-check**

Run: `uv run pytest -q && uv run ruff check && uv run pyright`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/paperboy/recipes.py src/paperboy/cli.py docs/data-model.md tests/test_recipe.py
git commit -m "feat(discussion): register the phase and document the edge shapes"
```

---

### Task 5: Definition of Done report

**Files:**
- Create: `docs/features/discussion-collector.md`

- [ ] **Step 1: Smoke-test against the real capture**

Unit tests are necessary, not sufficient (global CLAUDE.md). Replay the live database the way the invite-roster fix was verified — read-only, on a copy, never mutating the evidence file:

```bash
cp data/default/paperboy.sqlite /tmp/smoke.sqlite
uv run python - <<'PY'
import sqlite3
from paperboy.store.db import Store
from paperboy.store.repliers import backfill_recent_repliers
with Store.open("/tmp/smoke.sqlite") as st:
    n = backfill_recent_repliers(st, 2541889325, "stranger")
    print("distinct commenter peers projected:", n)
    print("commented_on edges:", st.conn.execute(
        "select count(*) c from edges where predicate='commented_on'").fetchone()["c"])
PY
```

Expected: **31 distinct peers** — the acceptance figure from spec §8. Anything lower means `PeerChannel` handling or the distinct-count logic is wrong.

- [ ] **Step 2: Write the DoD report**

`docs/features/discussion-collector.md` must contain: what shipped, the smoke transcript verbatim, the test counts before and after, confirmation that the two protected history test files are unmodified (`git diff --stat main -- tests/test_collector_history.py tests/test_history_catchup.py` showing only additions), and any follow-ups filed as issues.

- [ ] **Step 3: Commit**

```bash
git add docs/features/discussion-collector.md
git commit -m "docs(discussion): DoD report with smoke transcript"
```

> **OPUS REVIEW PANEL — after Tasks 1–5 have all landed.**
> Review the accumulated diff, not the tasks individually. Priorities, in order:
> 1. **The regression contract.** `git diff main -- tests/test_collector_history.py tests/test_history_catchup.py` must show additions only. Any modification to an existing test is a blocking finding.
> 2. **Interface drift.** Task 3 coded against Task 1's signature without waiting for it. Confirm they actually match, including the `page_budget` widening added in Task 3 Step 4.
> 3. **The mirror mapping.** The highest-risk logic in the feature. A silently wrong map attributes comments to the wrong posts and nothing fails loudly. Check the `origin == f"tg:channel:{ctx.channel_id}"` guard specifically.
> 4. **Counter honesty.** `unmapped` must cover both no-mirror and no-author. A comment must never be counted twice or dropped silently.
> 5. **Passivity.** No join, no write RPC, no reactions anywhere in the diff.

---

## Self-Review

**Spec coverage.** §4 preflight → Task 3 `_linked_group` + tests 1-3. §5 generalization → Task 1. §6 mapping → Task 3 `_mirror_map`/`_write_thread_edges` + tests 7, 9. §7 edges → Task 3 + tests 7, 8, 10, 11. §8 backfill → Task 2 + `tests/test_store_repliers.py` + test 12. §9 guardrails → Task 3 Step 4 + test 14. §10 ordering → Task 4. §11 testing → Task 0. §12 files → Tasks 1-4. §13 risks → the two review gates.

**Deviation from spec, flagged deliberately.** Spec §9 implies the page budget is enforced inside `discussion`; the plan puts the counter in `HistoryCollector` (Task 3 Step 4) because that is where the page loop lives, and passes the budget down. Same behaviour, and the `None` default leaves `history` unbounded as today.

**Type consistency.** `backfill_recent_repliers(store, channel_id, tier) -> int` is defined in Task 2 and called with that exact signature in Task 3. `HistoryCollector.collect`'s keywords are defined in Task 1 and used identically in Task 3, plus `page_budget` added in Task 3 Step 4 and noted for the reviewer. `_COMMENTED_ON` is defined separately in both `repliers.py` and `discussion.py` — deliberate, so neither module imports the other for a three-word constant.
