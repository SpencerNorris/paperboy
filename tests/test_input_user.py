"""Spec §5's load-bearing plumbing: the store-side ref builder (three cases)
and the gateway-side TL builders — verified against the installed Telethon."""

from __future__ import annotations

import base64

from telethon.tl.types import (
    ChannelParticipantsAdmins,
    ChannelParticipantsBots,
    ChannelParticipantsMentions,
    ChannelParticipantsRecent,
    InputPeerChannel,
    InputPeerUser,
    InputPeerUserFromMessage,
    InputUser,
    InputUserFromMessage,
)

from paperboy.gateway import (
    FILTER_ADMINS,
    FILTER_BOTS,
    FILTER_RECENT,
    _file_reference,
    _input_peer_user,
    _input_user,
    _largest_photo_size,
    _participants_filter,
)
from paperboy.store.db import Store
from paperboy.store.peers import input_user_ref, upsert_peer
from paperboy.store.users import upsert_user

T = "2026-01-01T00:00:00+00:00"
GROUP_ID = 77


def test_case_1_full_access_hash_builds_input_user():
    ref = {"user_id": 5, "access_hash": 99}
    built = _input_user(ref)
    assert isinstance(built, InputUser) and (built.user_id, built.access_hash) == (5, 99)
    peer = _input_peer_user(ref)
    assert isinstance(peer, InputPeerUser) and peer.access_hash == 99


def test_case_2_min_provenance_builds_from_message():
    ref = {"user_id": 5, "from_msg": {"channel_id": GROUP_ID, "access_hash": 4242, "msg_id": 200}}
    built = _input_user(ref)
    assert isinstance(built, InputUserFromMessage)
    assert isinstance(built.peer, InputPeerChannel)
    assert (built.peer.channel_id, built.peer.access_hash, built.msg_id, built.user_id) == (
        GROUP_ID, 4242, 200, 5,
    )
    peer = _input_peer_user(ref)
    assert isinstance(peer, InputPeerUserFromMessage) and peer.msg_id == 200


def test_store_ref_prefers_a_full_users_row_then_a_full_peer_then_provenance(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        group = {
            "_": "Channel", "id": GROUP_ID, "access_hash": 4242, "title": "G", "megagroup": True,
        }
        r = st.add_raw("Channel", group, "stranger", None)
        upsert_peer(st, group, r, T, seen_in_chat=None, seen_in_msg=None)

        # case 2: a min stub with provenance into a channel whose hash we know
        stub = {"_": "User", "id": 5, "min": True}
        r2 = st.add_raw("User", stub, "stranger", None)
        upsert_peer(st, stub, r2, T, seen_in_chat=GROUP_ID, seen_in_msg=200)
        assert input_user_ref(st, "tg:user:5") == {
            "user_id": 5, "from_msg": {"channel_id": GROUP_ID, "access_hash": 4242, "msg_id": 200},
        }

        # case 1 via peers: a full peer object
        full = {"_": "User", "id": 6, "access_hash": 66, "first_name": "F"}
        r3 = st.add_raw("User", full, "stranger", None)
        upsert_peer(st, full, r3, T, seen_in_chat=None, seen_in_msg=None)
        assert input_user_ref(st, "tg:user:6") == {"user_id": 6, "access_hash": 66}

        # case 1 via users: a triaged user outranks its own min peer stub
        triaged = {"_": "User", "id": 5, "access_hash": 55, "first_name": "T"}
        r4 = st.add_raw("User", triaged, "stranger", None)
        upsert_user(st, triaged, r4, T, "stranger")
        assert input_user_ref(st, "tg:user:5") == {"user_id": 5, "access_hash": 55}


def test_store_ref_case_3_unresolvable(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        stub = {"_": "User", "id": 7, "min": True}
        r = st.add_raw("User", stub, "stranger", None)
        # provenance into a channel we have NO hash for
        upsert_peer(st, stub, r, T, seen_in_chat=999, seen_in_msg=1)
        assert input_user_ref(st, "tg:user:7") is None
        # no provenance at all
        stub2 = {"_": "User", "id": 8, "min": True, "access_hash": 123}  # a min hash is not usable
        r2 = st.add_raw("User", stub2, "stranger", None)
        upsert_peer(st, stub2, r2, T, seen_in_chat=None, seen_in_msg=None)
        assert input_user_ref(st, "tg:user:8") is None
        assert input_user_ref(st, "tg:user:404") is None


def test_participants_filter_mapping():
    assert isinstance(_participants_filter(FILTER_RECENT), ChannelParticipantsRecent)
    assert isinstance(_participants_filter(FILTER_ADMINS), ChannelParticipantsAdmins)
    assert isinstance(_participants_filter(FILTER_BOTS), ChannelParticipantsBots)
    mentions = _participants_filter(
        {"_": "channelParticipantsMentions", "top_msg_id": 3, "q": None}
    )
    assert isinstance(mentions, ChannelParticipantsMentions) and mentions.top_msg_id == 3


def test_photo_download_helpers():
    sizes = [
        {"_": "PhotoStrippedSize", "type": "i", "bytes": b""},
        {"_": "PhotoSize", "type": "m", "w": 320, "h": 320, "size": 1},
        {"_": "PhotoSizeProgressive", "type": "x", "w": 800, "h": 800, "sizes": [1, 2]},
    ]
    assert _largest_photo_size(sizes) == "x"
    assert _largest_photo_size([]) == "x"
    assert _file_reference({"file_reference": b"\x01\x02"}) == b"\x01\x02"
    # a replayed/stored photo dict carries base64 text (store.db.dumps)
    encoded = base64.b64encode(b"\x01\x02").decode()
    assert _file_reference({"file_reference": encoded}) == b"\x01\x02"
