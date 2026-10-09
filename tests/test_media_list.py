"""`media_list`: parse a CSV / plain-line list of message URIs (#68).

All handles, ids and rows here are synthetic.
"""

import json
from pathlib import Path

import pytest

from paperboy.collectors.media import content_key, content_key_text
from paperboy.ids import utc_now_iso
from paperboy.media_list import (
    MediaListError,
    classify_rows,
    excluded_channel_ids,
    parse_media_list,
    plan_segments,
)
from paperboy.media_store import LocalMediaStore
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.edges import add_edge
from paperboy.store.messages import mark_deleted, upsert_message
from paperboy.store.peers import upsert_peer


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_csv_with_extra_columns_and_priority(tmp_path):
    path = _write(
        tmp_path, "l.csv",
        "uri,priority,kind,size_mb\n"
        "tg:msg:100/1,P1,video,3.5\n"
        "tg:msg:200/9,P2,photo,0.1\n"
        "tg:msg:100/2,,photo,0.1\n",
    )
    rows = parse_media_list(path)
    assert [(r.line_no, r.uri, r.channel_id, r.msg_id, r.priority) for r in rows] == [
        (2, "tg:msg:100/1", 100, 1, "P1"),
        (3, "tg:msg:200/9", 200, 9, "P2"),
        (4, "tg:msg:100/2", 100, 2, None),
    ]
    assert all(r.username is None for r in rows)


def test_csv_without_priority_column_and_with_bom_and_crlf(tmp_path):
    path = tmp_path / "l.csv"
    path.write_bytes(b"\xef\xbb\xbfuri,note\r\ntg:msg:1/2,a\r\n")
    (row,) = parse_media_list(path)
    assert (row.uri, row.priority) == ("tg:msg:1/2", None)


def test_plain_lines_accept_all_three_forms_and_comments(tmp_path):
    path = _write(
        tmp_path, "l.txt",
        "# a comment\n"
        "\n"
        "tg:msg:100/1\n"
        "https://t.me/c/200/7\n"
        "https://t.me/SomeChan/12\n"
        "t.me/other_chan/3\n",
    )
    rows = parse_media_list(path)
    assert [(r.line_no, r.uri, r.channel_id, r.username, r.msg_id) for r in rows] == [
        (3, "tg:msg:100/1", 100, None, 1),
        (4, "tg:msg:200/7", 200, None, 7),
        (5, "t.me/somechan/12", None, "somechan", 12),
        (6, "t.me/other_chan/3", None, "other_chan", 3),
    ]


def test_malformed_rows_fail_listing_every_line(tmp_path):
    path = _write(
        tmp_path, "l.csv",
        "uri,priority\n"
        "tg:msg:1/1,P1\n"
        "not-a-uri,P1\n"
        "tg:msg:1/2,P1\n"
        "tg:msg:1/3\n"
        ",P1\n"
        "https://example.com/x/1,P2\n",
    )
    with pytest.raises(MediaListError) as exc:
        parse_media_list(path)
    assert exc.value.line_nos == [3, 5, 6, 7]


def test_plain_malformed_lines_listed(tmp_path):
    path = _write(tmp_path, "l.txt", "tg:msg:1/1\nnope\ntg:msg:1/x\n")
    with pytest.raises(MediaListError) as exc:
        parse_media_list(path)
    assert exc.value.line_nos == [2, 3]


def test_duplicates_keep_first_and_flag_later(tmp_path):
    path = _write(
        tmp_path, "l.txt", "tg:msg:1/1\ntg:msg:1/2\nhttps://t.me/c/1/1\ntg:msg:1/2\n"
    )
    rows = parse_media_list(path)
    assert len(rows) == 4
    assert [r.duplicate for r in rows] == [False, False, True, True]


def test_empty_list_is_an_error(tmp_path):
    path = _write(tmp_path, "l.txt", "# nothing\n\n")
    with pytest.raises(MediaListError):
        parse_media_list(path)


# ── classification + segments ──────────────────────────────────────────────


def seed_channel(st, channel_id, username, access_hash=None, *, megagroup=False):
    """A `channels` row; with `access_hash`, also a full (non-`min`) `peers` row,
    i.e. a saved key (route 1 of Step A, #84)."""
    chan = {"_": "channel", "id": channel_id, "access_hash": 1, "title": "T"}
    chan["megagroup" if megagroup else "broadcast"] = True
    if username:
        chan["username"] = username
    full = {"_": "channelFull", "id": channel_id, "pts": 1}
    raw = st.add_raw("channelFull", full, "stranger", None)
    upsert_channel(st, full, chan, raw, utc_now_iso())
    if access_hash is not None:
        peer = {"_": "channel", "id": channel_id, "title": "T", "access_hash": access_hash}
        upsert_peer(st, peer, st.add_raw("Channel", peer, "stranger", None), utc_now_iso(),
                    seen_in_chat=None, seen_in_msg=None)


def seed_msg(st, channel_id, msg_id, *, doc_id=None, photo_id=None, text_only=False):
    msg = {"_": "message", "id": msg_id, "message": "", "date": 1767322445}
    if doc_id is not None:
        msg["media"] = {
            "_": "MessageMediaDocument",
            "document": {"_": "Document", "id": doc_id, "access_hash": 1, "size": 2_000_000,
                         "mime_type": "video/mp4", "attributes": []},
        }
    elif photo_id is not None:
        msg["media"] = {"_": "MessageMediaPhoto", "photo": {"_": "Photo", "id": photo_id}}
    elif not text_only:
        msg["media"] = {"_": "MessageMediaGeo", "geo": {}}
    raw = st.add_raw("message", msg, "stranger", {"channel_id": channel_id})
    return upsert_message(st, channel_id, msg, raw, utc_now_iso(), "stranger")


def record_media(st, message_uri, sha, store="local"):
    st.conn.execute(
        "INSERT INTO media (sha256, message_uri, kind, size, path, downloaded_at) "
        "VALUES (?, ?, 'document', 1, ?, ?)",
        (sha, message_uri, f"media/{sha[:2]}/{sha}", utc_now_iso()),
    )
    # A real download always leaves a custody row too (it names the store).
    record_media_custody(st, message_uri, sha, store)


def record_media_custody(st, message_uri, sha, store):
    """A download's custody sighting, with the content key of the message's
    media AS IT STANDS NOW (what the media phase records)."""
    row = st.conn.execute(
        "SELECT media_json FROM messages WHERE uri = ?", (message_uri,)
    ).fetchone()
    key = content_key(json.loads(row["media_json"])) if row and row["media_json"] else None
    st.conn.execute(
        "INSERT INTO custody_log "
        "(path, sha256, recorded_at, source_message_uri, store, content_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            f"media/{sha[:2]}/{sha}", sha, utc_now_iso(), message_uri, store,
            content_key_text(key) if key else None,
        ),
    )


def test_classify_covers_every_offline_outcome(tmp_path):
    """After #91 only `duplicate_row`, `excluded` and `pending` are offline
    outcomes; the rest of the old vocabulary became flags on pending rows."""
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 20, "chan_b")
        seed_channel(st, 30, None)  # linked group: no username
        seed_msg(st, 10, 1, doc_id=1001)                     # in store, media not held
        seed_msg(st, 10, 2, doc_id=1002)                     # in store, media held
        record_media(st, "tg:msg:10/2", "a" * 64)
        seed_msg(st, 10, 3, text_only=True)                  # in store, no media (geo)
        seed_msg(st, 10, 4, doc_id=1004)                     # tombstoned in store
        mark_deleted(st, 10, 4, "update", utc_now_iso())
        seed_msg(st, 30, 5, doc_id=1005)                     # reached by id
        seed_msg(st, 20, 6, doc_id=1002)                     # same file as 10/2, other channel
        seed_msg(st, 20, 7, photo_id=77)                     # via username row
        path = _write(
            tmp_path, "l.txt",
            "tg:msg:10/1\n"          # pending, in store
            "tg:msg:10/2\n"          # pending, media_held
            "tg:msg:10/3\n"          # pending, in store (a text post is still re-fetched)
            "tg:msg:10/4\n"          # pending: tombstoned rows are re-fetched too
            "tg:msg:30/5\n"          # pending: no username needed, the id is the address
            "tg:msg:20/6\n"          # pending, media_held: key held by ANOTHER channel's file
            "tg:msg:10/999\n"        # pending, not yet collected
            "https://t.me/Chan_B/7\n"  # username resolved offline -> pending
            "tg:msg:20/7\n"          # duplicate of the username row after resolution
            "t.me/nobody/1\n"        # username the store never saw: needs_resolve
            "tg:msg:10/1\n",         # duplicate_row at parse time
        )
        out = classify_rows(st, parse_media_list(path), media_store=LocalMediaStore(tmp_path))
        assert [c.outcome for c in out] == [
            "pending", "pending", "pending", "pending", "pending", "pending",
            "pending", "pending", "duplicate_row", "pending", "duplicate_row",
        ]
        assert [c.in_store for c in out[:8]] == [
            True, True, True, True, True, True, False, True,
        ]
        assert [c.media_held for c in out[:8]] == [
            False, True, False, False, False, True, False, False,
        ]
        assert [c.needs_resolve for c in out] == [False] * 9 + [True, False]
        assert out[7].uri == "tg:msg:20/7" and out[7].channel_id == 20
        assert out[9].uri == "t.me/nobody/1" and out[9].channel_id is None


def test_edited_post_is_not_held_by_its_old_file(tmp_path):
    """`media_held` follows the content key of the post's CURRENT media: a file
    downloaded for an older version of the post does not make the new one held
    (#91), and the same photo on another message still does."""
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_msg(st, 10, 1, photo_id=100)
        record_media(st, "tg:msg:10/1", "a" * 64)  # file A, for photo 100
        path = _write(tmp_path, "l.txt", "tg:msg:10/1\n")
        before = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path)
        )
        assert before[0].media_held and before[0].stored == ("a" * 64, f"media/aa/{'a' * 64}")
        seed_msg(st, 10, 1, photo_id=200)  # edited to a different photo
        after = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path)
        )
        assert not after[0].media_held and after[0].stored is None
        record_media(st, "tg:msg:10/1", "b" * 64)  # file B, for photo 200
        again = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path)
        )
        assert again[0].media_held and again[0].stored == ("b" * 64, f"media/bb/{'b' * 64}")


def test_unknown_username_makes_a_handle_segment(tmp_path):
    """A handle row whose channel the store never saw is addressed by handle."""
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        path = _write(tmp_path, "l.txt", "t.me/Nobody/3\nt.me/nobody/1\ntg:msg:10/1\n")
        segs = plan_segments(
            classify_rows(st, parse_media_list(path), media_store=LocalMediaStore(tmp_path))
        )
    assert [(s.channel_id, s.username, s.msg_ids) for s in segs] == [
        (None, "nobody", [1, 3]),
        (10, None, [1]),
    ]


def test_segments_group_by_priority_then_channel_in_first_appearance_order(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 20, "chan_b")
        for ch, mid, doc in [(10, 1, 1), (20, 1, 2), (10, 2, 3), (10, 3, 4), (20, 2, 5)]:
            seed_msg(st, ch, mid, doc_id=doc)
        path = _write(
            tmp_path, "l.csv",
            "uri,priority\n"
            "tg:msg:10/2,P1\n"   # P1 chan_a
            "tg:msg:20/1,P1\n"   # P1 chan_b
            "tg:msg:10/3,P2\n"   # P2 chan_a
            "tg:msg:10/1,P1\n"   # P1 chan_a again -> same segment as the first
            "tg:msg:20/2,P2\n",  # P2 chan_b
        )
        store = LocalMediaStore(tmp_path)
        segs = plan_segments(classify_rows(st, parse_media_list(path), media_store=store))
        assert [(s.priority, s.channel_id, s.msg_ids) for s in segs] == [
            ("P1", 10, [1, 2]),
            ("P1", 20, [1]),
            ("P2", 10, [3]),
            ("P2", 20, [2]),
        ]


def test_quoted_csv_header_is_detected(tmp_path):
    path = _write(tmp_path, "l.csv", '"uri","priority"\ntg:msg:1/1,P1\n')
    rows = parse_media_list(path)
    assert [(r.uri, r.priority) for r in rows] == [("tg:msg:1/1", "P1")]


def test_private_link_without_message_id_is_malformed(tmp_path):
    path = _write(tmp_path, "l.txt", "tg:msg:1/1\nhttps://t.me/c/12345\n")
    with pytest.raises(MediaListError) as exc:
        parse_media_list(path)
    assert exc.value.line_nos == [2]


def test_exclude_target_marks_rows_excluded_offline(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 20, "chan_b")
        seed_msg(st, 10, 1, doc_id=1001)   # would be pending
        seed_msg(st, 10, 2, doc_id=1002)   # would be already_stored
        record_media(st, "tg:msg:10/2", "a" * 64)
        seed_msg(st, 20, 7, photo_id=77)
        path = _write(
            tmp_path, "l.txt",
            "tg:msg:10/1\ntg:msg:10/2\ntg:msg:10/999\n"  # not yet collected, but excluded first
            "https://t.me/chan_a/1\n"                       # username row: resolved, then excluded
            "tg:msg:20/7\n",
        )
        out = classify_rows(
            st, parse_media_list(path), media_store=LocalMediaStore(tmp_path),
            excluded_ids=frozenset({10}),
        )
    assert [c.outcome for c in out] == [
        "excluded", "excluded", "excluded", "excluded", "pending",
    ]
    assert out[3].uri == "tg:msg:10/1" and out[3].channel_id == 10


def test_excluded_channel_ids_follows_the_linked_group(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 77, None, megagroup=True)
        seed_channel(st, 20, "chan_b")
        add_edge(st, "tg:channel:10", "linked_group", "tg:channel:77",
                 utc_now_iso(), "stranger", None, None)
        for spec in ("@chan_a", "chan_a", "10", "-10010", "t.me/c/10"):
            assert excluded_channel_ids(st, [spec]) == frozenset({10, 77}), spec
        assert excluded_channel_ids(st, ["@chan_b", "@chan_a"]) == frozenset({10, 20, 77})
        assert excluded_channel_ids(st, []) == frozenset()


@pytest.mark.parametrize("spec", ["@nobody", "999", "t.me/+AbCdEf123", "+15551234567", "#tag"])
def test_excluded_channel_ids_rejects_unknown_or_non_channel_specs(tmp_path, spec):
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        with pytest.raises(MediaListError) as exc:
            excluded_channel_ids(st, [spec])
    assert spec in str(exc.value)


def test_already_stored_is_per_store(tmp_path):
    """A file the DB holds from a LOCAL run is `pending` for a bucket run until a custody
    row names the bucket (offline); an unrecorded bucket object does not count."""
    from paperboy.media_store import GcsMediaStore
    from tests.fake_gcs import FakeGcsClient

    client = FakeGcsClient()
    bucket_store = GcsMediaStore("bkt", "p/x", client_factory=lambda: client)
    sha = "a" * 64
    key = f"media/{sha[:2]}/{sha}"
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_msg(st, 10, 1, doc_id=1001)
        record_media(st, "tg:msg:10/1", sha)  # a local run's row: custody store 'local'
        path = _write(tmp_path, "l.txt", "tg:msg:10/1\n")
        rows = parse_media_list(path)

        local = classify_rows(st, rows, media_store=LocalMediaStore(tmp_path))
        assert [c.media_held for c in local] == [True]  # custody says local: no stat

        pending = classify_rows(st, rows, media_store=bucket_store)
        assert [c.media_held for c in pending] == [False]
        assert client.bucket("bkt").calls["exists"] == 0  # no custody row, no GCS call

        client.bucket("bkt").objects[f"p/x/{key}"] = b"x"  # an orphan, put there by hand
        orphan = classify_rows(st, rows, media_store=bucket_store)
        # The media phase would re-fetch and CRC-verify it; it is not "held".
        assert [c.media_held for c in orphan] == [False]

        calls_before = client.bucket("bkt").calls["exists"]
        record_media_custody(st, "tg:msg:10/1", sha, "gs://bkt/p/x")
        client.bucket("bkt").objects.clear()
        fast = classify_rows(st, rows, media_store=bucket_store)
        assert [c.media_held for c in fast] == [True]  # a custody row names the bucket
        assert client.bucket("bkt").calls["exists"] == calls_before  # zero HEADs
