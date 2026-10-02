"""`collect_channel`'s pre-resolved channel context and stop-exception capture (#68)."""

import json
import logging

import pytest

from paperboy.budget import HardStop, SkipAndRecord
from paperboy.collectors.base import ChannelContext, CollectContext, CollectResult
from paperboy.collectors.media import DiskFloorStop
from paperboy.config import load_settings
from paperboy.recipes import collect_channel, collect_channel_with_context
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway


class _Stub:
    name: str

    def __init__(self, name: str, exc: BaseException | None = None):
        self.name = name
        self._exc = exc

    def applies_to(self, target):
        return True

    async def collect(self, ctx: CollectContext) -> CollectResult:
        if self._exc:
            raise self._exc
        return CollectResult(name=self.name, counts={"ok": 1})


class _ChannelStub(_Stub):
    """Stands in for `channel`: primes the context the way the real one does."""

    def __init__(self):
        super().__init__("channel")

    async def collect(self, ctx: CollectContext) -> CollectResult:
        ctx.input_channel = {"channel_id": 5, "access_hash": 99}
        ctx.channel_id = 5
        ctx.tier = "admin"
        return CollectResult(name=self.name, counts={"channels": 1})


class _ForbiddenChannelStub(_Stub):
    def __init__(self):
        super().__init__("channel", exc=AssertionError("channel must not run with a context"))


class _SeenCtxStub(_Stub):
    def __init__(self):
        super().__init__("media")
        self.seen: tuple | None = None

    async def collect(self, ctx: CollectContext) -> CollectResult:
        self.seen = (ctx.input_channel, ctx.channel_id, ctx.tier)
        return CollectResult(name=self.name, counts={})


_LOG = logging.getLogger("t")


@pytest.mark.asyncio
async def test_collect_channel_with_context_returns_established_context(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        results, cc = await collect_channel_with_context(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_ChannelStub(), _SeenCtxStub()],
        )
        assert [r.name for r in results] == ["channel", "media"]
        assert cc == ChannelContext(
            input_channel={"channel_id": 5, "access_hash": 99}, channel_id=5,
            tier="admin", source_run_id=st.run_id or "",
        )


@pytest.mark.asyncio
async def test_no_context_returned_when_channel_phase_did_not_complete(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        _, cc = await collect_channel_with_context(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel"], log=_LOG,
            collectors=[_Stub("channel", exc=SkipAndRecord("private"))],
        )
        assert cc is None


@pytest.mark.asyncio
async def test_channel_context_skips_channel_phase_and_writes_marker(tmp_path):
    media = _SeenCtxStub()
    given = ChannelContext(
        input_channel={"channel_id": 5, "access_hash": 99}, channel_id=5,
        tier="admin", source_run_id="src-run",
    )
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_ForbiddenChannelStub(), media], channel_context=given,
        )
        assert [r.name for r in results] == ["media"]
        assert media.seen == ({"channel_id": 5, "access_hash": 99}, 5, "admin")
        markers = st.conn.execute(
            "SELECT run_id, tier, payload_json, context_json FROM raw_records "
            "WHERE kind='ChannelContextReused'"
        ).fetchall()
        assert len(markers) == 1
        m = markers[0]
        assert m["run_id"] == st.run_id and m["tier"] == "admin"
        assert json.loads(m["payload_json"]) == {"channel_id": 5, "source_run_id": "src-run"}
        assert "access_hash" not in m["payload_json"] + (m["context_json"] or "")


@pytest.mark.asyncio
async def test_stop_exc_carries_the_exception_type(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["a", "b", "c"], log=_LOG,
            collectors=[
                _Stub("a", exc=DiskFloorStop("full")),
                _Stub("b", exc=SkipAndRecord("gone")),
                _Stub("c", exc=HardStop("boom")),
            ],
        )
        assert isinstance(results[0].stop_exc, DiskFloorStop)
        assert isinstance(results[1].stop_exc, SkipAndRecord)
        assert isinstance(results[2].stop_exc, HardStop)


@pytest.mark.asyncio
async def test_media_selection_is_recorded_once_when_media_runs_with_msgs(tmp_path):
    # A `media_msgs`-scoped media phase walks only those rows; the run records
    # the ids so a reproject walks exactly the same rows (#68). Without it a
    # replay would re-derive dedup custody rows for messages the live run
    # never considered.
    settings = load_settings("default", {"media_msgs": [7, 3, 5]})
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            FakeGateway({}), st, settings, parse_target("@durov"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_ChannelStub(), _SeenCtxStub()],
        )
        rows = st.conn.execute(
            "SELECT run_id, payload_json FROM raw_records WHERE kind='MediaSelection'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["run_id"] == st.run_id
        assert json.loads(rows[0]["payload_json"]) == {"channel_id": 5, "msg_ids": [3, 5, 7]}


@pytest.mark.asyncio
async def test_media_selection_names_the_established_channel_and_follows_channel_raws(tmp_path):
    # The selection is written just before the media phase, once the channel is
    # known (#68 spec 9.2): after the channel phase's raws, before media's.
    class _ChannelWithRaw(_ChannelStub):
        async def collect(self, ctx):
            ctx.store.add_raw("ChatFull", {"x": 1}, "stranger", {"channel_id": 5})
            return await super().collect(ctx)

    class _MediaWithRaw(_SeenCtxStub):
        async def collect(self, ctx):
            ctx.store.add_raw("MediaDownload", {"y": 1}, "stranger", None)
            return await super().collect(ctx)

    settings = load_settings("default", {"media_msgs": [2, 1]})
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            FakeGateway({}), st, settings, parse_target("5"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_ChannelWithRaw(), _MediaWithRaw()],
        )
        kinds = [r["kind"] for r in st.conn.execute("SELECT kind FROM raw_records ORDER BY id")]
        assert kinds == ["ChatFull", "MediaSelection", "MediaDownload"]


@pytest.mark.asyncio
async def test_media_selection_absent_when_channel_not_established(tmp_path):
    settings = load_settings("default", {"media_msgs": [1]})
    with Store.open(tmp_path / "p.sqlite") as st:
        results = await collect_channel(
            FakeGateway({}), st, settings, parse_target("5"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_Stub("channel", exc=SkipAndRecord("no route")), _SeenCtxStub()],
        )
        assert st.conn.execute(
            "SELECT count(*) FROM raw_records WHERE kind='MediaSelection'"
        ).fetchone()[0] == 0
        assert [r.stopped for r in results][0] == "skip"


@pytest.mark.asyncio
async def test_no_media_selection_without_msgs_or_without_media_phase(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        await collect_channel(
            FakeGateway({}), st, load_settings("default", {}), parse_target("@durov"),
            phases=["channel", "media"], log=_LOG,
            collectors=[_ChannelStub(), _SeenCtxStub()],
        )
        await collect_channel(
            FakeGateway({}), st, load_settings("default", {"media_msgs": [1]}),
            parse_target("@durov"), phases=["channel"], log=_LOG,
            collectors=[_ChannelStub(), _SeenCtxStub()],
        )
        assert st.conn.execute(
            "SELECT count(*) FROM raw_records WHERE kind='MediaSelection'"
        ).fetchone()[0] == 0
