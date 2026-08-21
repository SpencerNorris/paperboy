import json

from paperboy.export.jsonl import export_jsonl
from paperboy.store.channels import upsert_channel
from paperboy.store.db import Store
from paperboy.store.edges import add_edge
from paperboy.store.messages import upsert_message
from paperboy.store.sync import set_state


def _seed(st, self_uri="tg:user:1"):
    chan = {"_": "channel", "id": 5, "title": "Durov", "broadcast": True}
    full = {"_": "channelFull", "id": 5, "participants_count": 10}
    r = st.add_raw("channelFull", full, "stranger", None)
    upsert_channel(st, full, chan, r, "2026-01-01T00:00:00+00:00")
    set_state(st, "account", "self", {"uri": self_uri, "id": 1})


def test_export_writes_three_files_with_revisions(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st)
        m = {
            "_": "message", "id": 1, "message": "hello", "date": 1767322445,
            "peer_id": {"channel_id": 5},
        }
        r1 = st.add_raw("message", m, "stranger", None)
        upsert_message(st, 5, m, r1, "2026-01-01T00:00:00+00:00", "stranger")
        m2 = {**m, "message": "hello EDITED", "edit_date": 1767322900}
        r2 = st.add_raw("message", m2, "stranger", None)
        upsert_message(st, 5, m2, r2, "2026-01-02T00:00:00+00:00", "stranger")

        out = tmp_path / "out"
        counts = export_jsonl(st, "tg:channel:5", out)

        assert counts == {"channel": 1, "messages": 1, "edges": 0}
        assert (out / "channel.jsonl").exists()
        lines = (out / "messages.jsonl").read_text().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["text"] == "hello EDITED"
        assert row["deleted_at"] is None
        assert [rev["text"] for rev in row["revisions"]] == ["hello", "hello EDITED"]
        assert (out / "edges.jsonl").read_text() == ""


def test_export_scrubs_self_authored_messages(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st, self_uri="tg:user:1")
        m = {
            "_": "message", "id": 1, "message": "not mine", "date": 1767322445,
            "peer_id": {"channel_id": 5}, "from_id": {"_": "peerUser", "user_id": 2},
        }
        r1 = st.add_raw("message", m, "stranger", None)
        upsert_message(st, 5, m, r1, "2026-01-01T00:00:00+00:00", "stranger")
        self_msg = {
            "_": "message", "id": 2, "message": "mine", "date": 1767322445,
            "peer_id": {"channel_id": 5}, "from_id": {"_": "peerUser", "user_id": 1},
        }
        r2 = st.add_raw("message", self_msg, "self", None)
        upsert_message(st, 5, self_msg, r2, "2026-01-01T00:00:00+00:00", "self")

        out = tmp_path / "out"
        counts = export_jsonl(st, "tg:channel:5", out)
        assert counts["messages"] == 1
        lines = (out / "messages.jsonl").read_text().splitlines()
        assert json.loads(lines[0])["text"] == "not mine"


def test_export_scopes_edges_to_channel(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _seed(st)
        add_edge(
            st, "tg:channel:5", "linked_group", "tg:channel:77",
            "2026-01-01T00:00:00+00:00", "stranger", None, None,
        )
        add_edge(
            st, "tg:channel:999", "linked_group", "tg:channel:888",
            "2026-01-01T00:00:00+00:00", "stranger", None, None,
        )
        out = tmp_path / "out"
        counts = export_jsonl(st, "tg:channel:5", out)
        assert counts["edges"] == 1
        lines = (out / "edges.jsonl").read_text().splitlines()
        assert json.loads(lines[0])["object_uri"] == "tg:channel:77"


def test_export_rejects_non_channel_uri(tmp_path):
    import pytest

    with Store.open(tmp_path / "p.sqlite") as st, pytest.raises(ValueError, match="channel URI"):
        export_jsonl(st, "tg:user:1", tmp_path / "out")
