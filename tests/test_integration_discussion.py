"""Integration coverage for the `discussion` phase: full recipe composition,
CLI phase selection, and the read-only guardrail — the plan under-specifies
these (docs/superpowers/plans/2026-08-21-discussion-collector.md Task 0), so
they're written here rather than folded into the per-behaviour unit tests in
`test_collector_discussion.py`.

Follows the driving patterns already established in `tests/test_recipe.py`
(a full `collect_channel` run against `FakeGateway`, with an explicit
`collectors=` override so this suite does not have to wait for Task 4 to
register `DiscussionCollector` in `recipes._default_collectors`) and
`tests/test_cli.py` (`CliRunner` + a monkeypatched `composition.build_gateway`
for the two CLI-level phase-selection tests, which *do* exercise the real
default collector list and therefore do depend on Task 4).
"""

# See the matching comment in tests/test_collector_discussion.py: ruff's
# isort classifies `paperboy.collectors.discussion` third-party until Task 3
# creates it, then first-party — a moving target no static import order can
# satisfy for every intermediate task state, so I001 is suppressed here
# rather than chased.
from __future__ import annotations  # noqa: I001

import logging
from collections import Counter

import pytest
from paperboy.collectors.discussion import DiscussionCollector
from typer.testing import CliRunner

from paperboy import app as composition
from paperboy.cli import app as cli_app
from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

CHANNEL_ID = 5
GROUP_ID = 77

# The entire `Gateway` Protocol surface (src/paperboy/gateway.py) — every one
# of these is a read RPC; the Protocol declares no write method at all. Used
# by `test_discussion_run_issues_only_read_rpcs` below to assert the concrete
# `FakeGateway.calls` log (plan Task 2) never contains anything outside it.
_GATEWAY_READ_METHODS = frozenset({
    "resolve", "get_full_channel", "iter_history", "get_messages",
    "get_channel_difference", "get_self", "get_authorizations",
    "get_password_state", "get_privacy", "download_media",
    "get_channel_recommendations", "check_chat_invite", "get_sponsored_messages",
})


def _fixtures(linked_chat_id: int | None) -> dict:
    """A `channel` + `history` fixture set, optionally with `linked_chat_id`
    pointing at `GROUP_ID` and that group's `Channel` object present in
    `full_channel.chats` — exactly what the real `channel` collector needs to
    project an access-hash-bearing `peers` row for the group, which is what
    `DiscussionCollector._linked_group` looks up (no `resolve` of its own)."""
    full_chats = [
        {"_": "channel", "id": CHANNEL_ID, "access_hash": 99, "title": "C",
         "username": "c", "broadcast": True},
    ]
    if linked_chat_id:
        full_chats.append(
            {"_": "channel", "id": linked_chat_id, "access_hash": 4242,
             "title": "C Chat", "megagroup": True}
        )
    return {
        "resolve": {
            "peer": {"_": "PeerChannel", "channel_id": CHANNEL_ID},
            "chats": [
                {"_": "channel", "id": CHANNEL_ID, "access_hash": 99, "title": "C",
                 "username": "c", "broadcast": True},
            ],
            "users": [],
        },
        "full_channel": {
            "full_chat": {
                "_": "channelFull", "id": CHANNEL_ID, "participants_count": 10,
                "pts": 1, "linked_chat_id": linked_chat_id,
            },
            "chats": full_chats,
            "users": [],
        },
        "self": {"_": "user", "id": 1, "self": True},
        "history": [
            {"_": "message", "id": 2, "message": "m2", "date": 1767322445},
            {"_": "message", "id": 1, "message": "m1", "date": 1767322445},
        ],
        "get_messages": {},
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
    }


# --- full recipe composition ------------------------------------------------


@pytest.mark.asyncio
async def test_full_run_with_linked_group_composes_channel_history_discussion(tmp_path):
    gw = FakeGateway(_fixtures(GROUP_ID))
    collectors = [ChannelCollector(), HistoryCollector(), DiscussionCollector()]
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@c"),
            phases=["channel", "history", "discussion"], log=logging.getLogger("t"),
            collectors=collectors,
        )
        assert [r.name for r in results] == ["channel", "history", "discussion"]
        assert all(r.stopped is None for r in results)

        events = st.conn.execute("select phase, kind from run_events order by id").fetchall()
        assert [(e["phase"], e["kind"]) for e in events] == [
            ("channel", "complete"), ("history", "complete"), ("discussion", "complete"),
        ]

        channel_ids = {
            r["channel_id"]
            for r in st.conn.execute("select distinct channel_id from messages").fetchall()
        }
        assert channel_ids == {CHANNEL_ID, GROUP_ID}


@pytest.mark.asyncio
async def test_full_run_without_linked_group_discussion_skips_and_run_completes(tmp_path):
    gw = FakeGateway(_fixtures(None))
    collectors = [ChannelCollector(), HistoryCollector(), DiscussionCollector()]
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@c"),
            phases=["channel", "history", "discussion"], log=logging.getLogger("t"),
            collectors=collectors,
        )
        assert [r.name for r in results] == ["channel", "history", "discussion"]
        discussion_result = results[-1]
        assert discussion_result.stopped is not None
        assert "linked" in discussion_result.stopped.lower()

        # A clean "no linked group" skip is a `CollectResult(stopped=...)`
        # returned normally, never an exception — so the recipe layer records
        # it as a *complete* phase (spec §4), not a "skip"/"phase_stop"
        # run_events kind (those are reserved for SkipAndRecord/PhaseStop).
        kind = st.conn.execute(
            "select kind from run_events where phase='discussion'"
        ).fetchone()["kind"]
        assert kind == "complete"

        channel_ids = {
            r["channel_id"]
            for r in st.conn.execute("select distinct channel_id from messages").fetchall()
        }
        assert channel_ids == {CHANNEL_ID}


# --- read-only guardrail ----------------------------------------------------


@pytest.mark.asyncio
async def test_discussion_run_issues_only_read_rpcs(tmp_path):
    """paperboy is read-only end to end (CLAUDE.md non-negotiable guardrail):
    the `Gateway` Protocol exposes no write method at all, so this pins the
    concrete evidence — `FakeGateway.calls` (plan Task 2) — to never record
    anything outside that read-only surface across a full
    channel+history+discussion run. Fails today with `AttributeError` since
    `FakeGateway` has no `calls` list yet; that is the expected failure until
    Task 2 lands.

    The subset check alone is tautological: `_GATEWAY_READ_METHODS` is
    transcribed straight from the `Gateway` Protocol, i.e. it IS the full
    alphabet `FakeGateway` can ever append to `calls` — no production
    change to `DiscussionCollector` could ever make `set(gw.calls) <=
    _GATEWAY_READ_METHODS` false. Pinning the actual RPC spend below makes
    the test capable of failing on a stray `resolve` during discussion
    preflight, a duplicated sweep, or an unexpected extra call:
    `channel` issues `resolve`/`get_full_channel`/`get_self` once each;
    `history`'s backfill pages the 2-message fixture to exhaustion (one
    non-empty page, then the empty page that ends the loop = 2
    `iter_history` calls) with no id gap to probe (0 `get_messages`), and
    its folded-in `catch_up` issues exactly one `get_channel_difference`;
    `discussion`'s sweep of the linked group reuses
    `HistoryCollector.collect(probe_gaps=False)` against the SAME flat
    `history` fixture (`FakeGateway` has no per-target fixture story), so
    it pages that same 2-message list to exhaustion again (another 2
    `iter_history` calls) and never calls `get_messages` (gap-probing is
    off) or `get_channel_difference` (only `HistoryCollector.catch_up` —
    which `discussion` never calls — issues that).
    """
    gw = FakeGateway(_fixtures(GROUP_ID))
    collectors = [ChannelCollector(), HistoryCollector(), DiscussionCollector()]
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@c"),
            phases=["channel", "history", "discussion"], log=logging.getLogger("t"),
            collectors=collectors,
        )
        assert gw.calls, "expected at least one recorded gateway call"
        assert set(gw.calls) <= _GATEWAY_READ_METHODS
        counts = Counter(gw.calls)
        assert counts["resolve"] == 1
        assert counts["get_full_channel"] == 1
        assert counts["get_self"] == 1
        assert counts["get_channel_difference"] == 1
        assert counts["iter_history"] == 4
        assert counts["get_messages"] == 0


# --- CLI phase selection -----------------------------------------------------
#
# Unlike the tests above, these two exercise the *real* default collector
# list (`recipes._default_collectors`) and `cli.py`'s `_dependent_phases`
# guard, via `paperboy.cli.app` directly — so they only pass once Task 4
# registers `DiscussionCollector` after `history` and adds `"discussion"` to
# `_dependent_phases`. Today, `--phases discussion` alone is silently
# accepted (exit 0, nothing collected) rather than rejected, and
# `--phases channel,discussion` silently runs only `channel` — both are the
# expected failures until Task 4 lands.


def _patch_gateway(monkeypatch, linked_chat_id: int | None) -> None:
    async def fake_build_gateway(settings, secrets, profile, store):
        del settings, secrets, profile, store
        return FakeGateway(_fixtures(linked_chat_id))

    monkeypatch.setattr(composition, "build_gateway", fake_build_gateway)


def test_cli_phases_channel_discussion_works(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, GROUP_ID)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["collect", "@c", "--profile", "disctest1", "--phases", "channel,discussion",
         "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.stdout
    db_path = tmp_path / "disctest1" / "paperboy.sqlite"
    assert db_path.exists()
    with Store.open(db_path) as st:
        channel_ids = {
            r["channel_id"]
            for r in st.conn.execute("select distinct channel_id from messages").fetchall()
        }
        # `discussion` swept the linked group's own history even though the
        # `history` phase itself was never selected for the channel.
        assert GROUP_ID in channel_ids


def test_cli_phases_discussion_alone_is_rejected(tmp_path, monkeypatch):
    _patch_gateway(monkeypatch, GROUP_ID)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["collect", "@c", "--profile", "disctest2", "--phases", "discussion", "--unsafe"],
        env={"PAPERBOY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert "channel" in result.stdout.lower()
