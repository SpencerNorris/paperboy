"""Reaction vectors on a group: the zero-RPC `recent_reactions` sample +
bookkeeping for the bounded RPC."""

from __future__ import annotations

from paperboy.store.db import Store
from paperboy.store.reactions import (
    backfill_recent_reactions,
    fetched_reaction_lists,
    reacted_message_ids,
)

GROUP_ID = 77


def _raw_message(st, msg_id: int, reactions: dict | None):
    payload = {"_": "Message", "id": msg_id, "message": "m", "date": 1767322445,
               "peer_id": {"_": "PeerChannel", "channel_id": GROUP_ID}}
    if reactions is not None:
        payload["reactions"] = reactions
    return st.add_raw("Message", payload, "stranger", {"channel_id": GROUP_ID},
                      observed_at="2026-01-01T00:00:00+00:00")


def test_recent_reactions_project_min_peers_and_reacted_to_edges(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        thumbs = {"_": "ReactionEmoji", "emoticon": "👍"}
        _raw_message(st, 10, {
            "_": "MessageReactions",
            "results": [
                {"_": "ReactionCount", "reaction": thumbs, "count": 2},
            ],
            "recent_reactions": [
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 5},
                 "date": 1767322500, "reaction": thumbs},
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 6},
                 "date": 1767322501, "reaction": thumbs},
            ],
        })
        _raw_message(st, 11, None)
        assert backfill_recent_reactions(st, GROUP_ID, "stranger") == 2
        assert backfill_recent_reactions(st, GROUP_ID, "stranger") == 0  # idempotent: nothing NEW
        peer = st.conn.execute(
            "select is_min, seen_in_chat, seen_in_msg from peers where uri='tg:user:5'"
        ).fetchone()
        assert (peer["is_min"], peer["seen_in_chat"], peer["seen_in_msg"]) == (1, GROUP_ID, 10)
        edges = st.conn.execute(
            "select subject_uri, predicate, object_uri, evidence_json from edges "
            "order by subject_uri"
        ).fetchall()
        assert [(e["subject_uri"], e["predicate"], e["object_uri"]) for e in edges] == [
            ("tg:user:5", "reacted_to", "tg:msg:77/10"),
            ("tg:user:6", "reacted_to", "tg:msg:77/10"),
        ]
        assert '"source": "recent_reactions"' in edges[0]["evidence_json"]
        assert '"emoticon": "👍"' in edges[0]["evidence_json"]


def test_reacted_message_ids_newest_first_and_fetched_set(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _raw_message(st, 10, {
            "_": "MessageReactions",
            "results": [{"_": "ReactionCount", "count": 1, "reaction": {}}],
        })
        _raw_message(st, 12, {
            "_": "MessageReactions",
            "results": [{"_": "ReactionCount", "count": 3, "reaction": {}}],
        })
        _raw_message(st, 11, {"_": "MessageReactions", "results": []})
        _raw_message(st, 13, None)
        assert reacted_message_ids(st, GROUP_ID) == [12, 10]
        assert fetched_reaction_lists(st, GROUP_ID) == set()
        st.add_raw(
            "messages.MessageReactionsList",
            {"_": "MessageReactionsList", "count": 3, "reactions": []},
            "stranger", {"channel_id": GROUP_ID, "msg_id": 12, "offset": ""},
        )
        assert fetched_reaction_lists(st, GROUP_ID) == {12}


def test_fetched_reaction_lists_excludes_a_message_truncated_mid_list(tmp_path):
    """A message interrupted mid-list (a `HardStop`, or the participants
    collector's hard per-message page cap) must not be reported as done just
    because SOME page was recorded — only the MOST RECENTLY recorded page's
    `next_offset` decides, so a later run resumes it instead of silently
    treating a partial reactor list as complete forever (round-3 review)."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _raw_message(st, 20, {
            "_": "MessageReactions",
            "results": [{"_": "ReactionCount", "count": 5, "reaction": {}}],
        })
        # page 1: more to come.
        st.add_raw(
            "messages.MessageReactionsList",
            {"_": "MessageReactionsList", "count": 2, "reactions": [], "next_offset": "p2"},
            "stranger", {"channel_id": GROUP_ID, "msg_id": 20, "offset": ""},
        )
        assert fetched_reaction_lists(st, GROUP_ID) == set()  # not done yet
        # page 2: still truncated (e.g. the hard page cap fired here).
        st.add_raw(
            "messages.MessageReactionsList",
            {"_": "MessageReactionsList", "count": 2, "reactions": [], "next_offset": "p3"},
            "stranger", {"channel_id": GROUP_ID, "msg_id": 20, "offset": "p2"},
        )
        assert fetched_reaction_lists(st, GROUP_ID) == set()  # latest page STILL truncated
        # page 3: the list actually ends.
        st.add_raw(
            "messages.MessageReactionsList",
            {"_": "MessageReactionsList", "count": 2, "reactions": [], "next_offset": None},
            "stranger", {"channel_id": GROUP_ID, "msg_id": 20, "offset": "p3"},
        )
        assert fetched_reaction_lists(st, GROUP_ID) == {20}


def test_recent_reactions_never_replace_an_existing_provenance(tmp_path):
    # A reaction is not a documented inputPeerFromMessage context; the edge is
    # still recorded, but a provenance the store already holds is kept.
    from paperboy.store.peers import upsert_peer

    with Store.open(tmp_path / "p.sqlite") as st:
        stub = {"_": "User", "id": 5, "min": True}
        rid = st.add_raw("User", stub, "stranger", None)
        upsert_peer(
            st, stub, rid, "2025-12-31T00:00:00+00:00", seen_in_chat=GROUP_ID, seen_in_msg=9
        )
        _raw_message(st, 10, {
            "_": "MessageReactions",
            "results": [
                {"_": "ReactionCount", "count": 1,
                 "reaction": {"_": "ReactionEmoji", "emoticon": "x"}},
            ],
            "recent_reactions": [
                {"_": "MessagePeerReaction", "peer_id": {"_": "PeerUser", "user_id": 5},
                 "date": 1767322500, "reaction": {"_": "ReactionEmoji", "emoticon": "x"}},
            ],
        })
        assert backfill_recent_reactions(st, GROUP_ID, "stranger") == 1  # the edge is new
        row = st.conn.execute("select seen_in_msg from peers where uri='tg:user:5'").fetchone()
        assert row["seen_in_msg"] == 9
        edges = st.conn.execute("select count(*) from edges where predicate='reacted_to'")
        assert edges.fetchone()[0] == 1
