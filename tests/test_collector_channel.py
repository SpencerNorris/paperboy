import json
import logging
from pathlib import Path

import pytest

from paperboy.budget import SkipAndRecord
from paperboy.collectors.base import CollectContext
from paperboy.collectors.channel import ChannelCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.peers import upsert_peer
from paperboy.store.sync import get_state
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

FX = Path("tests/fixtures/tl")


def _fixtures():
    return {
        "resolve": json.loads((FX / "resolve_durov.json").read_text()),
        "full_channel": json.loads((FX / "full_channel.json").read_text()),
        "self": {"_": "user", "id": 1, "self": True},
    }


@pytest.mark.asyncio
async def test_channel_collector(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        res = await ChannelCollector().collect(ctx)
        assert ctx.channel_id is not None
        row = st.conn.execute("select title, participants_count from channels").fetchone()
        assert row["participants_count"] >= 0
        assert res.counts["channels"] == 1


@pytest.mark.asyncio
async def test_channel_collector_sets_context_for_history(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert ctx.channel_id == 5
        assert ctx.input_channel == {"channel_id": 5, "access_hash": 99}


@pytest.mark.asyncio
async def test_channel_collector_seeds_pts(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert get_state(st, "channel", "5") == {"pts": 42}


@pytest.mark.asyncio
async def test_channel_collector_records_raw_and_peers(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        raw_kinds = {r["kind"] for r in st.conn.execute("select kind from raw_records")}
        assert "channelFull" in raw_kinds or "messages.chatFull" in raw_kinds
        peer_row = st.conn.execute("select uri from peers where uri='tg:channel:5'").fetchone()
        assert peer_row is not None


def test_applies_to_channel_like_targets():
    assert ChannelCollector().applies_to(parse_target("@durov"))
    assert not ChannelCollector().applies_to(parse_target("#osint"))


def _linked_group_first_fixtures():
    """Both chats vectors list the linked megagroup BEFORE the target.

    Telegram does not promise a vector ordering, and a linked discussion
    megagroup serialises as `Channel` too — the live ChatFull capture already
    carries both (issue #23). Identity must come from `peer` / `full_chat.id`,
    never from position.
    """
    group = {
        "_": "channel", "id": 777, "access_hash": 11,
        "title": "linked group", "megagroup": True,
    }
    target = {
        "_": "channel", "id": 5, "access_hash": 99,
        "title": "Durov", "username": "durov", "broadcast": True,
    }
    return {
        "resolve": {
            "_": "contacts.resolvedPeer",
            "peer": {"_": "PeerChannel", "channel_id": 5},
            "chats": [group, target], "users": [],
        },
        "full_channel": {
            "_": "messages.chatFull",
            "full_chat": {
                "_": "channelFull", "id": 5, "participants_count": 100,
                "about": "x", "pts": 42, "linked_chat_id": 777,
            },
            "chats": [group, target], "users": [],
        },
        "self": {"_": "user", "id": 1, "self": True},
    }


@pytest.mark.asyncio
async def test_channel_collector_picks_the_target_not_the_first_channel(tmp_path):
    gw = FakeGateway(_linked_group_first_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        await ChannelCollector().collect(ctx)
        assert ctx.channel_id == 5
        assert ctx.input_channel == {"channel_id": 5, "access_hash": 99}
        rows = [r["id"] for r in st.conn.execute("select id from channels")]
        assert rows == [5]  # the target's row — never the group's
        assert get_state(st, "channel", "5") == {"pts": 42}


@pytest.mark.asyncio
async def test_channel_collector_trusts_peer_over_the_chats_vector(tmp_path):
    # A resolution whose `peer` is not a channel must not be misattributed
    # to whatever channel happens to sit in `chats` — position is never
    # identity. A username can legitimately resolve to a user or basic group
    # (issue #34), so this is a clean skip, not a crash (SkipAndRecord).
    fx = _fixtures()
    fx["resolve"] = {
        "_": "contacts.resolvedPeer",
        "peer": {"_": "PeerUser", "user_id": 42},
        "chats": fx["resolve"]["chats"], "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(SkipAndRecord):
            await ChannelCollector().collect(ctx)


@pytest.mark.asyncio
async def test_non_channel_resolution_skips_cleanly_through_collect_channel(tmp_path):
    # #34, run end-to-end: the whole run must not crash — the channel phase
    # is recorded as a clean skip and later phases (none applicable here,
    # since `history` needs `channel_id`) do not raise either.
    fx = _fixtures()
    fx["resolve"] = {
        "_": "contacts.resolvedPeer",
        "peer": {"_": "PeerUser", "user_id": 42},
        "chats": fx["resolve"]["chats"], "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel", "history"], log=logging.getLogger("t"),
        )
    channel_result = next(r for r in results if r.name == "channel")
    assert channel_result.stopped == "skip"


@pytest.mark.asyncio
async def test_channel_collector_rejects_resolve_full_identity_mismatch(tmp_path):
    # ctx.input_channel (access_hash) comes from the resolve-side pick;
    # ctx.channel_id / pts come from full_chat.id. If the two ever disagreed,
    # history would key state to one channel while addressing another — fail
    # loudly rather than split identity across the run.
    fx = _fixtures()
    fx["full_channel"] = {
        "_": "messages.chatFull",
        "full_chat": {
            "_": "channelFull", "id": 6, "participants_count": 1, "about": "x",
            "pts": 1, "linked_chat_id": 0,
        },
        "chats": [{"_": "channel", "id": 6, "access_hash": 77, "title": "other"}],
        "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(ValueError):
            await ChannelCollector().collect(ctx)


@pytest.mark.asyncio
async def test_channel_collector_rejects_a_resolution_without_peer(tmp_path):
    # `contacts.ResolvedPeer` always carries `peer` in the wild; a response
    # without it gives us no authoritative identity, so guessing is refused —
    # a clean skip (#34), not a crash.
    fx = _fixtures()
    del fx["resolve"]["peer"]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        ctx = CollectContext(
            gw, st, load_settings("default", {}), parse_target("@durov"),
            None, None, "stranger", logging.getLogger("t"),
        )
        with pytest.raises(SkipAndRecord):
            await ChannelCollector().collect(ctx)


# --- #84: the id-first channel phase (Step A routes + the ChannelAccess receipt) ---

T0 = "2026-01-01T00:00:00+00:00"


def _seed_peer(st, cid, *, min_=False, hash_=99, seen=(None, None)):
    obj = {"_": "channel", "id": cid, "title": f"t{cid}"}
    if hash_ is not None:
        obj["access_hash"] = hash_
    if min_:
        obj["min"] = True
    raw = st.add_raw("Channel", obj, "stranger", None)
    upsert_peer(st, obj, raw, T0, seen_in_chat=seen[0], seen_in_msg=seen[1])
    return raw


def _ctx(gw, st, target):
    return CollectContext(
        gw, st, load_settings("default", {}), parse_target(target),
        None, None, "stranger", logging.getLogger("t"),
    )


def _raw_rows(st, kind):
    return [
        (r["id"], json.loads(r["payload_json"]), json.loads(r["context_json"] or "null"))
        for r in st.conn.execute(
            "select id, payload_json, context_json from raw_records where kind=? order by id",
            (kind,),
        )
    ]


@pytest.mark.asyncio
async def test_id_target_saved_key_takes_route_1(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_peer(st, 5)
        await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert "resolve" not in gw.calls
        assert gw.full_channel_inputs[0] == {"channel_id": 5, "access_hash": 99}
        ((access_id, receipt, context),) = _raw_rows(st, "ChannelAccess")
        assert receipt["via"] == "saved_key" and receipt["granted"] is True
        assert receipt["channel_id"] == 5 and receipt["requested"] == "5"
        assert context == {"target": "5", "channel_id": 5}
        chat_full = st.conn.execute(
            "select id from raw_records where kind='messages.chatFull'"
        ).fetchone()["id"]
        assert access_id < chat_full


@pytest.mark.asyncio
async def test_id_target_from_message_takes_route_2_then_route_1(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_peer(st, 7, hash_=11)
        _seed_peer(st, 5, min_=True, hash_=555, seen=(7, 3))
        ctx = _ctx(gw, st, "5")
        await ChannelCollector().collect(ctx)
        ((_, receipt, _),) = _raw_rows(st, "ChannelAccess")
        assert receipt["via"] == "from_message"
        assert receipt["input_channel"]["from_msg"] == {
            "channel_id": 7, "access_hash": 11, "msg_id": 3,
        }
        assert gw.full_channel_inputs[0]["from_msg"]["msg_id"] == 3
        assert "resolve" not in gw.calls
        assert ctx.input_channel == {"channel_id": 5, "access_hash": 99}
        row = st.conn.execute(
            "select is_min, access_hash from peers where uri='tg:channel:5'"
        ).fetchone()
        assert (row["is_min"], row["access_hash"]) == (0, 99)
        # A second collect now takes the saved key.
        await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert _raw_rows(st, "ChannelAccess")[-1][1]["via"] == "saved_key"
        assert gw.full_channel_inputs[1] == {"channel_id": 5, "access_hash": 99}


@pytest.mark.asyncio
async def test_id_target_min_hash_is_never_a_key(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_peer(st, 5, min_=True, hash_=555)
        with pytest.raises(SkipAndRecord) as exc:
            await ChannelCollector().collect(_ctx(gw, st, "5"))
        msg = str(exc.value)
        assert "saved full key" in msg and "message" in msg and "handle" in msg
        assert _raw_rows(st, "ChannelAccess") == []
        assert "get_full_channel" not in gw.calls


@pytest.mark.asyncio
async def test_id_target_stored_handle_verified(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        raw = st.add_raw("ChatFull", {}, "stranger", None)
        upsert_channel(
            st, {"id": 5, "pts": 1}, {"id": 5, "title": "t", "username": "durov"}, raw, T0
        )
        await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert gw.calls == ["get_self", "resolve", "get_full_channel"]
        ((resolve_id, _, resolve_ctx),) = _raw_rows(st, "contacts.resolvedPeer")
        assert resolve_ctx == {"target": "5", "handle": "durov"}
        ((access_id, receipt, _),) = _raw_rows(st, "ChannelAccess")
        assert receipt["via"] == "handle" and receipt["handle"] == "durov"
        assert receipt["granted"] is True
        assert receipt["key_source_raw_id"] == resolve_id
        assert resolve_id < access_id


@pytest.mark.asyncio
async def test_id_target_stored_handle_now_another_channel(tmp_path):
    fx = _fixtures()
    fx["resolve"] = {
        "_": "contacts.resolvedPeer",
        "peer": {"_": "PeerChannel", "channel_id": 6},
        "chats": [{"_": "channel", "id": 6, "access_hash": 77, "title": "other"}],
        "users": [],
    }
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        raw = st.add_raw("ChatFull", {}, "stranger", None)
        upsert_channel(
            st, {"id": 5, "pts": 1}, {"id": 5, "title": "t", "username": "durov"}, raw, T0
        )
        with pytest.raises(SkipAndRecord) as exc:
            await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert "6" in str(exc.value) and "5" in str(exc.value)
        ((_, receipt, _),) = _raw_rows(st, "ChannelAccess")
        assert receipt["granted"] is False and receipt["resolved_channel_id"] == 6
        assert receipt["input_channel"] is None and receipt["channel_id"] == 5
        assert "get_full_channel" not in gw.calls


@pytest.mark.asyncio
async def test_handle_target_unchanged_plus_receipt(tmp_path):
    gw = FakeGateway(_fixtures())
    with Store.open(tmp_path / "p.sqlite") as st:
        await ChannelCollector().collect(_ctx(gw, st, "@durov"))
        assert gw.calls == ["get_self", "resolve", "get_full_channel"]
        ((resolve_id, _, resolve_ctx),) = _raw_rows(st, "contacts.resolvedPeer")
        assert resolve_ctx == {"target": "@durov"}
        ((access_id, receipt, _),) = _raw_rows(st, "ChannelAccess")
        assert receipt["via"] == "handle" and receipt["granted"] is True
        assert receipt["input_channel"] == {"channel_id": 5, "access_hash": 99}
        assert access_id > resolve_id


@pytest.mark.asyncio
async def test_replay_receipt_beats_the_store(tmp_path):
    receipt = {
        "_": "ChannelAccess", "channel_id": 5, "requested": "5", "via": "saved_key",
        "granted": True, "input_channel": {"channel_id": 5, "access_hash": 99},
        "key_source_raw_id": 1,
    }
    fx = _fixtures()
    fx["channel_access"] = receipt
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:  # the store has NO row for 5
        await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert "resolve" not in gw.calls
        ((_, recorded, _),) = _raw_rows(st, "ChannelAccess")
        assert recorded == receipt


# --- #84 spec §2.2: "stop at the first that works" (route fallback) ---

def _rejected(error_cls):
    """What `Budget.call` raises for an access-type RPC error: a SkipAndRecord
    whose `__cause__` is the Telethon error."""
    exc = SkipAndRecord(f"{error_cls.__name__} (caused by GetFullChannelRequest)")
    exc.__cause__ = error_cls(None)
    return exc


def _seed_route2_and_handle(st):
    """Channel 5: a `min` peer reachable from message 3 of full-key chat 7 (route 2),
    and a stored handle (route 3)."""
    _seed_peer(st, 7, hash_=11)
    _seed_peer(st, 5, min_=True, hash_=555, seen=(7, 3))
    raw = st.add_raw("ChatFull", {}, "stranger", None)
    upsert_channel(st, {"id": 5, "pts": 1}, {"id": 5, "title": "t", "username": "durov"}, raw, T0)


@pytest.mark.asyncio
async def test_rejected_route_2_falls_through_to_route_3(tmp_path):
    from telethon.errors import MsgIdInvalidError

    fx = _fixtures()
    fx["full_channel_sequence"] = [_rejected(MsgIdInvalidError)]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_route2_and_handle(st)
        ctx = _ctx(gw, st, "5")
        await ChannelCollector().collect(ctx)
        assert [i.get("from_msg", {}).get("msg_id") for i in gw.full_channel_inputs] == [3, None]
        assert gw.full_channel_inputs[1] == {"channel_id": 5, "access_hash": 99}
        assert gw.calls == ["get_self", "get_full_channel", "resolve", "get_full_channel"]
        rows = _raw_rows(st, "ChannelAccess")
        assert [(r["via"], r["granted"]) for _, r, _ in rows] == [
            ("from_message", False), ("handle", True),
        ]
        refused = rows[0][1]
        assert refused["error"] == "MsgIdInvalidError"
        assert refused["channel_id"] == 5 and refused["requested"] == "5"
        resolve_id = _raw_rows(st, "contacts.resolvedPeer")[0][0]
        assert rows[0][0] < resolve_id < rows[1][0]
        assert ctx.channel_id == 5


@pytest.mark.asyncio
async def test_rejected_saved_key_falls_through_to_route_2(tmp_path):
    from telethon.errors import ChannelPrivateError

    fx = _fixtures()
    fx["full_channel_sequence"] = [_rejected(ChannelPrivateError)]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_peer(st, 7, hash_=11)
        _seed_peer(st, 5, hash_=555, seen=(7, 3))  # a full key AND provenance
        await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert "resolve" not in gw.calls
        rows = _raw_rows(st, "ChannelAccess")
        assert [(r["via"], r["granted"]) for _, r, _ in rows] == [
            ("saved_key", False), ("from_message", True),
        ]


@pytest.mark.asyncio
async def test_every_route_rejected_ends_in_the_route_4_failure(tmp_path):
    from telethon.errors import ChannelPrivateError, MsgIdInvalidError

    fx = _fixtures()
    fx["full_channel_sequence"] = [
        _rejected(MsgIdInvalidError), _rejected(ChannelPrivateError),
    ]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_route2_and_handle(st)
        with pytest.raises(SkipAndRecord) as exc:
            await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert "from_message" in str(exc.value) and "handle" in str(exc.value)
        assert [(r["via"], r["granted"]) for _, r, _ in _raw_rows(st, "ChannelAccess")] == [
            ("from_message", False), ("handle", False),
        ]


@pytest.mark.asyncio
async def test_a_flood_on_route_1_does_not_fall_back(tmp_path):
    from paperboy.budget import PhaseStop

    fx = _fixtures()
    fx["full_channel_sequence"] = [PhaseStop("flood wait 900s")]
    gw = FakeGateway(fx)
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed_peer(st, 7, hash_=11)
        _seed_peer(st, 5, hash_=555, seen=(7, 3))
        with pytest.raises(PhaseStop):
            await ChannelCollector().collect(_ctx(gw, st, "5"))
        assert len(gw.full_channel_inputs) == 1 and "resolve" not in gw.calls
        ((_, receipt, _),) = _raw_rows(st, "ChannelAccess")
        assert receipt["via"] == "saved_key" and receipt["granted"] is True
