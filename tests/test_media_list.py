"""`media_list`: parse a CSV / plain-line list of message URIs (#68).

All handles, ids and rows here are synthetic.
"""

from pathlib import Path

import pytest

from paperboy.ids import utc_now_iso
from paperboy.media_list import (
    MediaListError,
    classify_rows,
    parse_media_list,
    plan_segments,
)
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.messages import mark_deleted, upsert_message


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


def seed_channel(st, channel_id, username):
    chan = {"_": "channel", "id": channel_id, "access_hash": 1, "title": "T", "broadcast": True}
    if username:
        chan["username"] = username
    full = {"_": "channelFull", "id": channel_id, "pts": 1}
    raw = st.add_raw("channelFull", full, "stranger", None)
    upsert_channel(st, full, chan, raw, utc_now_iso())


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


def record_media(st, message_uri, sha):
    st.conn.execute(
        "INSERT INTO media (sha256, message_uri, kind, size, path, downloaded_at) "
        "VALUES (?, ?, 'document', 1, ?, ?)",
        (sha, message_uri, f"media/{sha[:2]}/{sha}", utc_now_iso()),
    )


def test_classify_covers_every_offline_outcome(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        seed_channel(st, 10, "chan_a")
        seed_channel(st, 20, "chan_b")
        seed_channel(st, 30, None)  # linked group: no username
        seed_msg(st, 10, 1, doc_id=1001)                     # pending
        seed_msg(st, 10, 2, doc_id=1002)                     # already_stored
        record_media(st, "tg:msg:10/2", "a" * 64)
        seed_msg(st, 10, 3, text_only=True)                  # no_media (geo)
        seed_msg(st, 10, 4, doc_id=1004)                     # deleted
        mark_deleted(st, 10, 4, "update", utc_now_iso())
        seed_msg(st, 30, 5, doc_id=1005)                     # unresolvable
        seed_msg(st, 20, 6, doc_id=1002)                     # same file as 10/2, other channel
        seed_msg(st, 20, 7, photo_id=77)                     # pending via username row
        path = _write(
            tmp_path, "l.txt",
            "tg:msg:10/1\n"          # pending
            "tg:msg:10/2\n"          # already_stored
            "tg:msg:10/3\n"          # no_media
            "tg:msg:10/4\n"          # deleted
            "tg:msg:30/5\n"          # unresolvable
            "tg:msg:20/6\n"          # already_stored: key held by ANOTHER channel's file
            "tg:msg:10/999\n"        # not_in_store
            "https://t.me/Chan_B/7\n"  # username resolved offline -> pending
            "tg:msg:20/7\n"          # duplicate of the username row after resolution
            "t.me/nobody/1\n"        # username not in store
            "tg:msg:10/1\n",         # duplicate_row at parse time
        )
        out = classify_rows(st, parse_media_list(path))
        assert [c.outcome for c in out] == [
            "pending", "already_stored", "no_media", "deleted", "unresolvable",
            "already_stored", "not_in_store", "pending", "duplicate_row",
            "not_in_store", "duplicate_row",
        ]
        assert out[7].uri == "tg:msg:20/7" and out[7].channel_id == 20


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
        segs = plan_segments(st, classify_rows(st, parse_media_list(path)))
        assert [(s.priority, s.channel_id, s.username, s.msg_ids) for s in segs] == [
            ("P1", 10, "chan_a", [1, 2]),
            ("P1", 20, "chan_b", [1]),
            ("P2", 10, "chan_a", [3]),
            ("P2", 20, "chan_b", [2]),
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
