"""`recent_repliers` arrives free inside every stored Message payload."""

# See the matching comment in tests/test_collector_discussion.py: ruff's
# isort classifies `paperboy.store.repliers` third-party until Task 2 creates
# it, then first-party — a moving target no static import order can satisfy
# for every intermediate task state, so I001 is suppressed here rather than
# chased.
from __future__ import annotations  # noqa: I001

import json

from paperboy.store.repliers import backfill_recent_repliers

from paperboy.store.db import Store
from paperboy.store.sync import set_state


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


def test_self_replier_is_not_projected_or_counted(tmp_path):
    # If the collecting account appears among a post's recent_repliers, it is
    # excluded from peers (issue #12) — and must not be counted in the returned
    # backfilled_peers total either (upsert_peer returns None for self).
    with Store.open(tmp_path / "p.sqlite") as st:
        set_state(st, "account", "self", {"uri": "tg:user:900", "id": 900})
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 900},
                          {"_": "PeerUser", "user_id": 111}])
        n = backfill_recent_repliers(st, 5, "stranger")
        assert n == 1, "self must not be counted among backfilled peers"
        assert st.conn.execute(
            "select 1 from peers where uri='tg:user:900'"
        ).fetchone() is None
        assert st.conn.execute(
            "select 1 from peers where uri='tg:user:111'"
        ).fetchone() is not None


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


def test_commented_on_evidence_marks_the_recent_repliers_source(tmp_path):
    """`commented_on` is emitted by two producers (this backfill and the
    `discussion` sweep's `_write_thread_edges`) — the evidence field is the
    only way downstream consumers can tell them apart, so pin it here rather
    than let it silently drift to `None`."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        backfill_recent_repliers(st, 5, "stranger")
        row = st.conn.execute(
            "select evidence_json from edges where predicate='commented_on'"
        ).fetchone()
        assert row["evidence_json"] is not None
        assert json.loads(row["evidence_json"])["source"] == "recent_repliers"


def test_stores_the_given_tier_on_the_edge(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        backfill_recent_repliers(st, 5, "member")
        row = st.conn.execute(
            "select tier from edges where predicate='commented_on'"
        ).fetchone()
        assert row["tier"] == "member"


def test_counts_distinct_peers_not_occurrences(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        _post(st, 5, 11, [{"_": "PeerUser", "user_id": 111}])
        assert backfill_recent_repliers(st, 5, "stranger") == 1
        # One peer, but TWO posts, so TWO distinct triples. Amendment 4's
        # dedup is on the whole (subject, predicate, object) — a guard that
        # forgot `object_uri` would collapse these to one and still return 1.
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 2


def test_two_repliers_on_one_post_each_get_an_edge(tmp_path):
    """The other axis: same post, different people.

    A dedup guard that forgot `subject_uri` would keep only the first
    commenter and still look correct everywhere else.
    """
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111},
                          {"_": "PeerUser", "user_id": 222}])
        assert backfill_recent_repliers(st, 5, "stranger") == 2
        subjects = [r["subject_uri"] for r in st.conn.execute(
            "select subject_uri from edges where predicate='commented_on' order by subject_uri"
        )]
        assert subjects == ["tg:user:111", "tg:user:222"]


def test_posts_without_repliers_are_ignored(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        st.add_raw("Message", {"_": "Message", "id": 10}, "stranger", {"channel_id": 5})
        assert backfill_recent_repliers(st, 5, "stranger") == 0
        assert st.conn.execute("select count(*) c from peers").fetchone()["c"] == 0


def test_unrecognized_replier_kind_is_skipped_not_crashed(tmp_path):
    """`_peer_stub` only understands `PeerUser`/`PeerChannel`. A `PeerChat`
    (a legacy basic-group replier, or any future discriminator) must be
    silently skipped — not raise, and not counted as a projected peer."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerChat", "chat_id": 333}])
        n = backfill_recent_repliers(st, 5, "stranger")
        assert n == 0
        assert st.conn.execute("select count(*) c from peers").fetchone()["c"] == 0


def test_scans_only_the_target_channels_raw_messages(tmp_path):
    """The store is one SQLite file per PROFILE, not per channel/target
    (`profile_dir(settings, profile)/paperboy.sqlite`) — a second channel's
    `Message` payloads sitting in the same `raw_records` table must never
    leak into another channel's backfill. `add_raw` already tags every raw
    record with `context_json = {"channel_id": ...}`; the scan must filter
    on it rather than trusting the `channel_id` argument alone to describe
    what's in the table."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        _post(st, 999, 10, [{"_": "PeerUser", "user_id": 222}])
        assert backfill_recent_repliers(st, 5, "stranger") == 1
        assert st.conn.execute(
            "select count(*) c from peers where uri='tg:user:222'"
        ).fetchone()["c"] == 0
        assert st.conn.execute(
            "select count(*) c from edges where object_uri='tg:msg:5/10' "
            "and subject_uri='tg:user:222'"
        ).fetchone()["c"] == 0


def test_repeated_backfill_does_not_duplicate_the_peer_row(tmp_path):
    """Amendment 4 (authoritative; supersedes the ADR-0002-citing docstring
    this test previously carried): both `commented_on` producers —
    `_write_thread_edges` and this backfill — are idempotent on
    `(subject_uri, predicate, object_uri)`. `backfill_recent_repliers`
    re-scans every stored `Message` payload on every run (spec §8: it runs
    unconditionally, before preflight, on every `discussion` invocation), so
    an unguarded insert would append a fresh `commented_on` edge for every
    recent replier on every re-run — a fresh `observed_at` and the previous
    run's `source_raw_id` attached to evidence this run never re-gathered,
    inflating exactly the degree counts amendment 4 protects for the
    sibling thread-edge producer. Left unpinned here, the suite would
    disagree with itself about one predicate: dedup enforced for
    `_write_thread_edges`, forbidden-by-omission for the backfill. The skip-
    if-triple-exists guard belongs inside `repliers.py`, not inside
    `store/edges.py::add_edge` — `channel`, `history`, and `graph` all still
    depend on `add_edge`'s append-only semantics for their own edges."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _post(st, 5, 10, [{"_": "PeerUser", "user_id": 111}])
        first = backfill_recent_repliers(st, 5, "stranger")
        second = backfill_recent_repliers(st, 5, "stranger")
        assert first == second == 1
        assert st.conn.execute(
            "select count(*) c from peers where uri='tg:user:111'"
        ).fetchone()["c"] == 1
        assert st.conn.execute(
            "select count(*) c from edges where predicate='commented_on'"
        ).fetchone()["c"] == 1
