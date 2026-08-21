import json
import logging
from pathlib import Path

import pytest

from paperboy.budget import PhaseStop, SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.collectors.graph import GraphCollector
from paperboy.config import load_settings
from paperboy.store.db import Store
from paperboy.store.messages import upsert_message
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/tl")


def _load(name: str) -> dict:
    return json.loads((FX / name).read_text())


def _ctx(st, gw, channel_id=5, tier="stranger"):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target("@x"),
        {"channel_id": channel_id, "access_hash": 9}, channel_id, tier, logging.getLogger("t"),
    )


def _seed_message(st: Store, channel_id: int, msg_id: int, text: str, entities: list[dict]) -> None:
    raw_id = st.add_raw("message", {"id": msg_id}, "stranger", None)
    msg = {
        "_": "message", "id": msg_id, "message": text, "entities": entities,
        "date": 1767322445, "peer_id": {"channel_id": channel_id},
    }
    upsert_message(st, channel_id, msg, raw_id, "2026-01-01T00:00:00+00:00", "stranger")


def _base_fixtures() -> dict:
    return {
        "channel_recommendations": {"_": "Chats", "chats": []},
        "sponsored_messages": {"_": "SponsoredMessagesEmpty"},
    }


def test_applies_to_channel_like_targets():
    assert GraphCollector().applies_to(parse_target("@durov"))
    assert not GraphCollector().applies_to(parse_target("#osint"))


@pytest.mark.asyncio
async def test_graph_requires_channel_context(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            FakeGateway(_base_fixtures()), st, load_settings("default", {}), parse_target("@x"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(PhaseStop):
            await GraphCollector().collect(ctx)


@pytest.mark.asyncio
async def test_recommendations_produce_edges_peers_and_true_count(tmp_path):
    fx = _base_fixtures()
    fx["channel_recommendations"] = _load("recommendations.json")
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["edges"] >= 2
        assert res.counts["peers"] == 2

        edges = st.conn.execute(
            "select subject_uri, object_uri, evidence_json from edges "
            "where predicate='recommended_with' order by object_uri"
        ).fetchall()
        assert [e["subject_uri"] for e in edges] == ["tg:channel:5", "tg:channel:5"]
        assert [e["object_uri"] for e in edges] == ["tg:channel:501", "tg:channel:502"]
        for e in edges:
            assert json.loads(e["evidence_json"])["total_count"] == 37

        peer = st.conn.execute("select username from peers where uri='tg:channel:501'").fetchone()
        assert peer["username"] == "similarone"


@pytest.mark.asyncio
async def test_recommendations_chat_not_modified_is_skipped_without_crashing(tmp_path):
    fx = _base_fixtures()
    fx["channel_recommendations"] = SkipAndRecord("CHAT_NOT_MODIFIED")
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["skipped"] >= 1
        assert res.counts["edges"] == 0
        assert st.conn.execute("select count(*) as n from edges").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_mention_name_entity_produces_mentions_edge(tmp_path):
    fx = _base_fixtures()
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_message(
            st, 5, 10, "hi @friend",
            [{"_": "MessageEntityMentionName", "offset": 3, "length": 7, "user_id": 99}],
        )
        await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        row = st.conn.execute(
            "select subject_uri, predicate, object_uri from edges where predicate='mentions'"
        ).fetchone()
        assert row["subject_uri"] == "tg:msg:5/10"
        assert row["object_uri"] == "tg:user:99"


@pytest.mark.asyncio
async def test_text_url_tme_username_link_produces_mentions_edge(tmp_path):
    fx = _base_fixtures()
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_message(
            st, 5, 11, "check this out",
            [{
                "_": "MessageEntityTextUrl", "offset": 0, "length": 15,
                "url": "https://t.me/coolchannel",
            }],
        )
        await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        row = st.conn.execute(
            "select subject_uri, predicate, object_uri from edges where predicate='mentions'"
        ).fetchone()
        assert row["subject_uri"] == "tg:msg:5/11"
        assert row["object_uri"] == "tg:username:coolchannel"


@pytest.mark.asyncio
async def test_bare_url_entity_sliced_from_text_produces_mentions_edge(tmp_path):
    fx = _base_fixtures()
    with Store.open(tmp_path / "p.sqlite") as st:
        text = "join https://t.me/anotherone now"
        offset = text.index("https://")
        length = len("https://t.me/anotherone")
        _seed_message(
            st, 5, 12, text,
            [{"_": "MessageEntityUrl", "offset": offset, "length": length}],
        )
        await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        row = st.conn.execute(
            "select object_uri from edges where predicate='mentions'"
        ).fetchone()
        assert row["object_uri"] == "tg:username:anotherone"


@pytest.mark.asyncio
async def test_bare_mention_entity_is_not_resolved(tmp_path):
    # MessageEntityMention (@name, no id) is a deliberate non-goal for this
    # pass — it must not crash and must not produce an edge.
    fx = _base_fixtures()
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_message(
            st, 5, 13, "hi @friend",
            [{"_": "MessageEntityMention", "offset": 3, "length": 7}],
        )
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["edges"] == 0


@pytest.mark.asyncio
async def test_invite_link_produces_mentions_edge_and_unjoined_preview(tmp_path):
    fx = _base_fixtures()
    fx["chat_invite"] = {"AbCdEf123": _load("chat_invite_preview.json")}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_message(
            st, 5, 14, "join us",
            [{
                "_": "MessageEntityTextUrl", "offset": 0, "length": 7,
                "url": "https://t.me/+AbCdEf123",
            }],
        )
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))

        mention = st.conn.execute(
            "select object_uri from edges where predicate='mentions'"
        ).fetchone()
        assert mention["object_uri"] == "tg:invite:AbCdEf123"

        invited = st.conn.execute(
            "select subject_uri, object_uri, evidence_json from edges "
            "where predicate='invited_via'"
        ).fetchone()
        assert invited["subject_uri"] == "tg:msg:5/14"
        assert invited["object_uri"] == "tg:invite:AbCdEf123"
        evidence = json.loads(invited["evidence_json"])
        assert evidence["resolved"] is False
        assert evidence["title"] == "Cool Group"
        assert evidence["participants_count"] == 250

        raw_kinds = {r["kind"] for r in st.conn.execute("select kind from raw_records")}
        assert "ChatInvite" in raw_kinds
        assert res.counts["raw"] >= 1


@pytest.mark.asyncio
async def test_invite_link_resolves_to_real_peer_when_already_known(tmp_path):
    fx = _base_fixtures()
    fx["chat_invite"] = {"knownhash": _load("chat_invite_already.json")}
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_message(
            st, 5, 15, "x",
            [{
                "_": "MessageEntityTextUrl", "offset": 0, "length": 1,
                "url": "https://t.me/+knownhash",
            }],
        )
        await GraphCollector().collect(_ctx(st, FakeGateway(fx)))

        invited = st.conn.execute(
            "select object_uri, evidence_json from edges where predicate='invited_via'"
        ).fetchone()
        assert invited["object_uri"] == "tg:channel:777"
        assert json.loads(invited["evidence_json"])["resolved"] is True

        peer = st.conn.execute("select username from peers where uri='tg:channel:777'").fetchone()
        assert peer["username"] == "knowngroup"


@pytest.mark.asyncio
async def test_sponsored_message_raw_logged_and_edge_added_for_tme_url(tmp_path):
    fx = _base_fixtures()
    fx["sponsored_messages"] = _load("sponsored_messages.json")
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["raw"] >= 1

        raw = st.conn.execute(
            "select payload_json from raw_records where kind='SponsoredMessage'"
        ).fetchone()
        payload = json.loads(raw["payload_json"])
        assert payload["sponsor_info"] == "Acme Corp"

        edge = st.conn.execute(
            "select subject_uri, object_uri, evidence_json from edges where predicate='mentions' "
            "and subject_uri='tg:channel:5'"
        ).fetchone()
        assert edge["object_uri"] == "tg:username:sponsorchannel"
        evidence = json.loads(edge["evidence_json"])
        assert evidence["sponsor_info"] == "Acme Corp"


@pytest.mark.asyncio
async def test_sponsored_messages_empty_is_a_noop(tmp_path):
    fx = _base_fixtures()
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        # `channel_recommendations` (also empty in the base fixture) still
        # raw-logs its own envelope, so `raw` isn't 0 overall — assert
        # specifically that `SponsoredMessagesEmpty` produced no per-message
        # raw record or edge of its own.
        assert "SponsoredMessage" not in {
            r["kind"] for r in st.conn.execute("select kind from raw_records")
        }
        assert res.counts["edges"] == 0


@pytest.mark.asyncio
async def test_sponsored_messages_admin_only_error_is_skipped_without_crashing(tmp_path):
    fx = _base_fixtures()
    fx["sponsored_messages"] = SkipAndRecord("CHAT_ADMIN_REQUIRED")
    with Store.open(tmp_path / "p.sqlite") as st:
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["skipped"] >= 1
        assert res.counts["edges"] == 0


# --- invite-preview participant sample -------------------------------------
#
# `messages.checkChatInvite` returns a *sample* of members alongside the title
# and count — the only roster data Telegram will hand an account that has not
# joined (see docs/research/sources/mtproto-participants-users.md: "Member
# sample from an invite link, without joining ... access: anyone with the
# t.me/+hash"). The sample rotates between calls, so projecting it on every run
# accumulates real membership over time without ever joining.


def _invite_fx(hash_: str = "AbCdEf123", fixture: str = "chat_invite_with_participants.json"):
    fx = _base_fixtures()
    fx["chat_invite"] = {hash_: _load(fixture)}
    return fx


def _seed_invite_message(st: Store, hash_: str = "AbCdEf123") -> None:
    _seed_message(
        st, 5, 14, "join us",
        [{
            "_": "MessageEntityTextUrl", "offset": 0, "length": 7,
            "url": f"https://t.me/+{hash_}",
        }],
    )


@pytest.mark.asyncio
async def test_invite_preview_participants_are_projected_into_peers(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))

        rows = {
            r["uri"]: r
            for r in st.conn.execute(
                "select uri, kind, id, first_name, last_name, username, flags_json from peers"
            )
        }
        assert "tg:user:6674021615" in rows
        assert "tg:user:7931433362" in rows
        assert "tg:user:5631259670" in rows

        eck = rows["tg:user:6674021615"]
        assert eck["kind"] == "user"
        assert eck["first_name"] == "EckArt"

        rockwell = rows["tg:user:7931433362"]
        assert rockwell["last_name"] == "Rockwell"
        assert json.loads(rockwell["flags_json"])["premium"] is True


@pytest.mark.asyncio
async def test_invite_participant_bots_are_projected_too(tmp_path):
    """Bots identify the tooling a group runs — keep them, flagged as bots."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))

        bot = st.conn.execute(
            "select username, flags_json from peers where uri='tg:user:5631259670'"
        ).fetchone()
        assert bot is not None
        assert bot["username"] == "uasaverbot"
        assert json.loads(bot["flags_json"])["bot"] is True


@pytest.mark.asyncio
async def test_invite_participants_carry_the_invite_raw_record_as_provenance(tmp_path):
    """An unjoined invite has no numeric chat id, so the ChatInvite raw row —
    whose `context_json` holds the hash — is the only provenance available."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))

        peer = st.conn.execute(
            "select source_raw_id, seen_in_chat, seen_in_msg from peers "
            "where uri='tg:user:6674021615'"
        ).fetchone()
        raw = st.conn.execute(
            "select kind, context_json from raw_records where id=?", (peer["source_raw_id"],)
        ).fetchone()
        assert raw["kind"] == "ChatInvite"
        assert json.loads(raw["context_json"])["hash"] == "AbCdEf123"
        # No chat id exists for an unjoined invite — must not be faked.
        assert peer["seen_in_chat"] is None
        assert peer["seen_in_msg"] is None


@pytest.mark.asyncio
async def test_invite_participants_are_counted_in_the_result(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        res = await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))
        assert res.counts["peers"] == 3


@pytest.mark.asyncio
async def test_invite_preview_without_participants_projects_no_peers(tmp_path):
    """Regression guard: the sample is optional and often absent."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        fx = _invite_fx(fixture="chat_invite_preview.json")
        res = await GraphCollector().collect(_ctx(st, FakeGateway(fx)))
        assert res.counts["peers"] == 0
        assert st.conn.execute("select count(*) c from peers").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_invite_participants_produce_member_of_edges(tmp_path):
    """A roster sample is only useful if the membership itself is queryable.

    `member_of` is in the spec §2 edge vocabulary; without it the association
    between a sampled person and the group they were sampled from survives
    only as a `source_raw_id` back-reference, which the graph export cannot
    traverse.
    """
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))

        edges = {
            (r["subject_uri"], r["object_uri"])
            for r in st.conn.execute(
                "select subject_uri, object_uri from edges where predicate='member_of'"
            )
        }
        assert edges == {
            ("tg:user:6674021615", "tg:invite:AbCdEf123"),
            ("tg:user:7931433362", "tg:invite:AbCdEf123"),
            ("tg:user:5631259670", "tg:invite:AbCdEf123"),
        }


@pytest.mark.asyncio
async def test_invite_member_of_edges_record_that_it_was_a_sample(tmp_path):
    """The sample is a handful of a much larger group — a reader must not
    mistake three rows for a three-person group."""
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_invite_message(st)
        await GraphCollector().collect(_ctx(st, FakeGateway(_invite_fx())))

        edge = st.conn.execute(
            "select evidence_json from edges where predicate='member_of' limit 1"
        ).fetchone()
        evidence = json.loads(edge["evidence_json"])
        assert evidence["sampled"] is True
        assert evidence["participants_count"] == 307
