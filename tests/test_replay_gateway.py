"""Unit tests for `RawReplayGateway`/`ReplaySource` (spec §2–§3): each Gateway
method is tested in isolation against a hand-built raw log — no collector
involved."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.clock import ReplayClock
from paperboy.replay import RawReplayGateway, ReplaySource, ReprojectSourceError
from paperboy.store.db import Store

CID = 100
IC = {"channel_id": CID, "access_hash": 7}


def _seed(tmp_path):
    """A minimal raw log: self, resolve, full, three messages (one edited),
    a probe MessageEmpty, one diff, one recommendation set, one MediaDownload."""
    db = tmp_path / "src.sqlite"
    media_root = tmp_path / "media"
    with Store.open(db) as st:
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None,
                   observed_at="2026-01-01T00:00:00+00:00")
        st.add_raw("ResolvedPeer",
                   {"_": "contacts.ResolvedPeer",
                    "peer": {"_": "PeerChannel", "channel_id": CID},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"target": "@durov"},
                   observed_at="2026-01-01T00:00:01+00:00")
        st.add_raw("ChatFull",
                   {"_": "messages.ChatFull",
                    "full_chat": {"_": "ChannelFull", "id": CID, "pts": 40,
                                  "linked_chat_id": 555},
                    "chats": [{"_": "Channel", "id": CID, "access_hash": 7}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:00:02+00:00")
        for mid, text, t in [
            (3, "m3", "2026-01-01T00:01:03+00:00"),
            (2, "m2", "2026-01-01T00:01:02+00:00"),
            (1, "m1", "2026-01-01T00:01:01+00:00"),
            (1, "m1 edited", "2026-01-01T00:02:00+00:00"),  # later revision
        ]:
            st.add_raw("Message", {"_": "message", "id": mid, "message": text},
                       "stranger", {"channel_id": CID}, observed_at=t)
        st.add_raw("MessageEmpty", {"_": "MessageEmpty", "id": 4}, "stranger",
                   {"channel_id": CID}, observed_at="2026-01-01T00:03:00+00:00")
        st.add_raw("ChannelDifference",
                   {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:04:00+00:00")
        st.add_raw("Chats", {"_": "messages.chats",
                             "chats": [{"_": "Channel", "id": 200, "access_hash": 9}]},
                   "stranger", {"channel_id": CID},
                   observed_at="2026-01-01T00:05:00+00:00")
        sha = "ab" + "0" * 62
        path = media_root / sha[:2] / f"{sha}.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"file contents")
        st.add_raw("MediaDownload",
                   {"sha256": sha, "kind": "document", "size": 13,
                    "mime_type": "text/plain", "file_name": "a.txt",
                    "path": str(path), "message_uri": f"tg:msg:{CID}/2"},
                   "stranger", {"channel_id": CID, "msg_id": 2},
                   observed_at="2026-01-01T00:06:00+00:00")
    return db, media_root


def _gateway(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    clock = ReplayClock()
    # The seeded fixtures are single-run (no begin_run/run_id involved), so
    # this is the source's one (legacy-labeled) run — behavior is unchanged
    # from before per-run scoping.
    run = src.runs()[0]
    return RawReplayGateway(src, clock, run), clock


@pytest.mark.asyncio
async def test_resolve_matches_target_and_stamps_clock(tmp_path):
    gw, clock = _gateway(tmp_path)
    resolved = await gw.resolve("durov")
    assert resolved["peer"]["channel_id"] == CID
    assert clock.for_payload(resolved) == "2026-01-01T00:00:01+00:00"


@pytest.mark.asyncio
async def test_resolve_unknown_target_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    with pytest.raises(SkipAndRecord):
        await gw.resolve("someone_else")


@pytest.mark.asyncio
async def test_get_self_serves_self_tier_record(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_self())["id"] == 1


@pytest.mark.asyncio
async def test_iter_history_pages_newest_first_excluding_empties(tmp_path):
    gw, clock = _gateway(tmp_path)
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=100)]
    # id DESC; both revisions of msg 1 in capture order; MessageEmpty excluded.
    assert [(m["id"], m["message"]) for m in page] == [
        (3, "m3"), (2, "m2"), (1, "m1"), (1, "m1 edited"),
    ]
    assert clock.for_payload(page[3]) == "2026-01-01T00:02:00+00:00"
    assert clock.for_payload(page[1]) == "2026-01-01T00:01:02+00:00"


@pytest.mark.asyncio
async def test_iter_history_never_splits_an_id_group_across_pages(tmp_path):
    gw, _ = _gateway(tmp_path)
    # limit=3 would cut between msg 1's two revisions; the page extends.
    page = [m async for m in gw.iter_history(IC, offset_id=0, limit=3)]
    assert [m["id"] for m in page] == [3, 2, 1, 1]
    next_page = [m async for m in gw.iter_history(IC, offset_id=1, limit=3)]
    assert next_page == []


@pytest.mark.asyncio
async def test_get_messages_serves_stored_and_placeholder(tmp_path):
    gw, _ = _gateway(tmp_path)
    out = await gw.get_messages(IC, [4, 99])
    assert out[0]["_"] == "MessageEmpty"          # stored probe result
    assert out[1] == {"_": "ReplayUnknownMessage", "id": 99}  # D4.1: no fabricated evidence


@pytest.mark.asyncio
async def test_channel_difference_serves_stored_then_synthetic_final(tmp_path):
    gw, _ = _gateway(tmp_path)
    first = await gw.get_channel_difference(IC, 40, 100)
    assert first["pts"] == 41 and first["final"]
    again = await gw.get_channel_difference(IC, 41, 100)
    assert again == {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 41}


@pytest.mark.asyncio
async def test_recommendations_served_and_missing_raw_skips(tmp_path):
    gw, _ = _gateway(tmp_path)
    recs = await gw.get_channel_recommendations(IC)
    assert recs["chats"][0]["id"] == 200
    with pytest.raises(SkipAndRecord):
        await gw.get_channel_recommendations({"channel_id": 999, "access_hash": 0})


@pytest.mark.asyncio
async def test_sponsored_reconstructs_envelope_or_empty(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.get_sponsored_messages(IC))["_"] == "sponsoredMessagesEmpty"


@pytest.mark.asyncio
async def test_download_media_reads_content_addressed_file(tmp_path):
    gw, clock = _gateway(tmp_path)
    del clock
    data = await gw.download_media(IC, {"id": 2})
    assert data == b"file contents"
    assert await gw.download_media(IC, {"id": 3}) is None  # no record -> unavailable


@pytest.mark.asyncio
async def test_join_channel_is_synthetic_and_offline(tmp_path):
    gw, _ = _gateway(tmp_path)
    assert (await gw.join_channel(IC))["_"] == "Updates"


@pytest.mark.asyncio
async def test_doctor_methods_are_not_replayable(tmp_path):
    gw, _ = _gateway(tmp_path)
    for coro in (gw.get_authorizations(), gw.get_password_state(), gw.get_privacy("phone")):
        with pytest.raises(SkipAndRecord):
            await coro


def test_source_helpers(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    run = src.runs()[0]
    assert src.resolve_targets(run) == ["@durov"]
    assert src.linked_group_ids(run) == {555}
    assert src.has_kind(run, "mediadownload") and not src.has_kind(run, "tme_page")


def test_source_is_read_only(tmp_path):
    db, media_root = _seed(tmp_path)
    src = ReplaySource.open(db, media_root)
    with pytest.raises(sqlite3.OperationalError):
        src.conn.execute("DELETE FROM raw_records")


# ---------------------------------------------------------------------------
# Revision R (ADR-0005): per-run replay — ReplaySource.runs()
# ---------------------------------------------------------------------------


def test_runs_groups_by_run_id_in_capture_order(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.begin_run("aaa")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        st.begin_run("bbb")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
    src = ReplaySource.open(db, tmp_path / "media")
    runs = src.runs()
    assert [(r.run_id, r.lo, r.hi) for r in runs] == [("aaa", 1, 2), ("bbb", 3, 3)]


def test_runs_segments_legacy_rows_at_self_markers(tmp_path):
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:  # no begin_run: run_id stays NULL (legacy)
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 2), ("legacy-0002", 3, 4),
    ]


def test_runs_handles_a_source_predating_the_run_id_column(tmp_path):
    # A real archive captured before this feature existed has only
    # 0001_init/0002_web applied — raw_records has NO run_id column at all
    # (found running the R6 real-archive smoke, ADR-0005). Every migration up
    # through 0002 is applied by hand (not via Store.open, which would also
    # apply 0003 and add the column) to reproduce that exact pre-migration
    # shape; runs() must treat the whole log as legacy, not crash.
    db = tmp_path / "pre_migration.sqlite"
    conn = sqlite3.connect(db)
    migrations_dir = Path("src/paperboy/store/migrations")
    conn.executescript((migrations_dir / "0001_init.sql").read_text())
    conn.executescript((migrations_dir / "0002_web.sql").read_text())
    conn.execute(
        "INSERT INTO raw_records(kind, observed_at, tier, context_json, payload_json) "
        "VALUES ('user', '2026-01-01T00:00:00+00:00', 'self', NULL, '{}')"
    )
    conn.execute(
        "INSERT INTO raw_records(kind, observed_at, tier, context_json, payload_json) "
        "VALUES ('message', '2026-01-01T00:00:01+00:00', 'stranger', "
        "'{\"channel_id\": 5}', '{\"id\": 1}')"
    )
    conn.commit()
    conn.close()

    src = ReplaySource.open(db, tmp_path / "media")
    runs = src.runs()
    assert [(r.run_id, r.lo, r.hi) for r in runs] == [("legacy-0001", 1, 2)]


def test_runs_absorbs_leading_rows_written_before_the_first_self_marker(tmp_path):
    # Found running the R6 real-archive smoke (ADR-0005): a real archive's
    # earliest collect pass(es) predate the "self written first" invariant —
    # its first raw writes are resolve/full, THEN self. Only the SECOND and
    # later self markers may cut a new segment; the first one just confirms
    # the segment already open, so these leading rows stay attached to the
    # run they belong to rather than becoming an orphan segment with no self
    # record (which would have no target to resolve on replay, silently
    # dropping that whole run).
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:  # no begin_run: run_id stays NULL (legacy)
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                   {"target": "@x"})
        st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        # A genuinely new pass: its own self marker cuts a real boundary.
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 4), ("legacy-0002", 5, 6),
    ]


def test_runs_absorbs_resolve_before_self_at_every_boundary(tmp_path):
    # The real archive's resolve/full-before-self ordering (see the test
    # above) turned out to recur at EVERY historical pass boundary
    # throughout its whole history, not just the first — a MID-log
    # transition must attach its own opening cluster to the run it starts,
    # not leave it behind in the run that precedes it (which silently
    # dropped the entire final run — 3154 of 6258 raw records — in the
    # first cut of this fix, caught before commit by re-running the smoke).
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger", {"target": "@x"})
        st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        # Pass 2's own opening cluster, same old ordering — must land in
        # pass 2, not stay attached to pass 1's tail.
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger", {"target": "@x"})
        st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 4), ("legacy-0002", 5, 8),
    ]
    assert src.resolve_targets(src.runs()[1]) == ["@x"]


def test_runs_does_not_split_a_run_on_a_foreign_single_row_intrusion(tmp_path):
    # Found running the R6 real-archive smoke a second time (ADR-0005, post
    # revision R): the archive's `default` profile has no file lock stopping
    # two `collect` invocations from writing to the same profile
    # concurrently, and a lone `ResolvedPeer`/`ChatFull` from some unrelated,
    # short-lived process can land mid-run, between two rows of the SAME
    # legitimate run's own substantive activity (there: a `MediaDownload`
    # loop, interrupted by a stray resolve of `@atom8388`). The OPENING-
    # CLUSTER rule (see the absorption tests above) treated that lone row as
    # an unconditional new-segment boundary on its own -- with no self
    # marker anywhere near it, the synthetic segment then fails replay at
    # `get_self()` ('no self User recorded'), silently discarding every row
    # from the intrusion up to the NEXT genuine opening cluster (there: 157
    # rows, 156 of them real historical `MediaDownload` observations).
    # A cluster only cuts a new boundary once it's confirmed genuine -- i.e.
    # it contains its own self marker before the run of opening-kind rows
    # ends -- so a lone foreign row with no self anywhere near it folds into
    # whichever run is already open instead of orphaning everything after it.
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:  # no begin_run: run_id stays NULL (legacy)
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                   {"target": "@target"})
        st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
        st.add_raw("MediaDownload", {"sha256": "a" * 64}, "stranger",
                   {"channel_id": 5, "msg_id": 1})
        # A foreign, short-lived process's resolve lands mid-run -- no self
        # anywhere near it, so it is NOT a genuine new pass.
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                   {"target": "@atom8388"})
        st.add_raw("MediaDownload", {"sha256": "b" * 64}, "stranger",
                   {"channel_id": 5, "msg_id": 2})
        st.add_raw("MediaDownload", {"sha256": "c" * 64}, "stranger",
                   {"channel_id": 5, "msg_id": 3})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [("legacy-0001", 1, 7)]


def test_runs_still_cuts_a_genuine_boundary_after_a_foreign_intrusion(tmp_path):
    # The fix above must not swallow a REAL run boundary -- a cluster that
    # DOES contain its own self marker still cuts, even if a foreign
    # intrusion happened earlier in the same log.
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        # Foreign intrusion, no self nearby -- absorbed into run 1.
        st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                   {"target": "@atom8388"})
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
        # A genuine second pass: its own self marker cuts a real boundary.
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 3}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 4), ("legacy-0002", 5, 6),
    ]


def test_runs_raises_on_a_genuinely_interleaved_stamped_run_id(tmp_path):
    # ADR-0005 point 3: "ReplaySource asserts contiguity and fails loudly if
    # ever violated" -- previously unexercised by any test. A source whose
    # `run_id` sequence is aaa, bbb, aaa (bbb's rows are not a contiguous
    # rowid range) is corrupt/hand-edited; refuse to replay it silently.
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.begin_run("aaa")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.begin_run("bbb")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.begin_run("aaa")
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    with pytest.raises(ReprojectSourceError, match="aaa"):
        src.runs()


def test_runs_splits_consecutive_all_opening_passes(tmp_path):
    # `paperboy collect @x --phases channel` writes exactly self/Resolved-
    # Peer/ChatFull — all three opening-kind — and nothing substantive. Two
    # or more such passes in a row leave NO non-opening row between them to
    # end the pending cluster, so the whole run of opening rows must be cut
    # at each REPEATED role rather than folded into one giant cluster
    # (correctness-reviewer, round 2): three back-to-back channel-only
    # passes must yield three runs, not one.
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:  # no begin_run: run_id stays NULL (legacy)
        for _ in range(3):
            st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
            st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                       {"target": "@x"})
            st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 3), ("legacy-0002", 4, 6), ("legacy-0003", 7, 9),
    ]


def test_runs_splits_consecutive_resolve_full_self_passes(tmp_path):
    # Same failure class as above, with the pre-invariant resolve/full/self
    # ordering (see `test_runs_absorbs_resolve_before_self_at_every_
    # boundary`): two back-to-back passes, each opening-only, must still cut
    # at the second pass's `ResolvedPeer` (the first repeated role).
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        for _ in range(2):
            st.add_raw("ResolvedPeer", {"_": "contacts.resolvedPeer"}, "stranger",
                       {"target": "@x"})
            st.add_raw("ChatFull", {"_": "messages.chatFull"}, "stranger", {"channel_id": 5})
            st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 3), ("legacy-0002", 4, 6),
    ]


def test_runs_mixed_legacy_then_stamped(tmp_path):
    # Legacy segment(s) precede stamped runs — the migration boundary shape.
    db = tmp_path / "src.sqlite"
    with Store.open(db) as st:
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 1}, "stranger", {"channel_id": 5})
        st.begin_run("ccc")
        st.add_raw("User", {"_": "user", "id": 1, "self": True}, "self", None)
        st.add_raw("Message", {"_": "message", "id": 2}, "stranger", {"channel_id": 5})
    src = ReplaySource.open(db, tmp_path / "media")
    assert [(r.run_id, r.lo, r.hi) for r in src.runs()] == [
        ("legacy-0001", 1, 2), ("ccc", 3, 4),
    ]
