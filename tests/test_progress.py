import asyncio
import logging

import pytest

from paperboy.progress import Progress, human_bytes, phase_status
from paperboy.store.db import Store


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(140 * 1024 * 1024) == "140.0 MB"


def test_phase_status_reads_store(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        st.conn.execute(
            "INSERT INTO media (sha256, kind, size, downloaded_at) VALUES ('a', 'photo', 1048576, 't')"
        )
        st.conn.execute(
            "INSERT INTO media (sha256, kind, size, downloaded_at) VALUES ('b', 'document', 2097152, 't')"
        )
        assert phase_status(st, "media") == "2 files · 3.0 MB"
        assert phase_status(st, "channel") == "resolving…"
        assert phase_status(st, "history") == "0 messages"


@pytest.mark.asyncio
async def test_progress_logs_phase_start_and_end(tmp_path, caplog):
    with Store.open(tmp_path / "p.sqlite") as st:
        log = logging.getLogger("test.progress.startend")
        prog = Progress(st, log)
        with caplog.at_level(logging.INFO, logger="test.progress.startend"):
            prog.start_phase("media")
            prog.end_phase("media", {"downloaded": 5})
        msgs = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("▶ media") for m in msgs)
        assert any(m.startswith("✓ media") and "downloaded=5" in m for m in msgs)


@pytest.mark.asyncio
async def test_progress_logs_stopped_phase(tmp_path, caplog):
    with Store.open(tmp_path / "p.sqlite") as st:
        log = logging.getLogger("test.progress.stopped")
        prog = Progress(st, log)
        with caplog.at_level(logging.INFO, logger="test.progress.stopped"):
            prog.start_phase("graph")
            prog.end_phase("graph", None, stopped="skip")
        assert any("⏹ graph" in r.getMessage() and "skip" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_fires_during_a_phase(tmp_path, caplog):
    with Store.open(tmp_path / "p.sqlite") as st:
        log = logging.getLogger("test.progress.hb")
        prog = Progress(st, log, interval=0.01)
        with caplog.at_level(logging.INFO, logger="test.progress.hb"):
            prog.begin()
            prog.start_phase("history")
            await asyncio.sleep(0.05)  # let the heartbeat tick a few times
            await prog.close()
        beats = [r.getMessage() for r in caplog.records if "…" in r.getMessage()]
        assert beats, "heartbeat should have logged at least one status line"
        assert any("history" in b for b in beats)


@pytest.mark.asyncio
async def test_close_is_safe_without_begin(tmp_path):
    with Store.open(tmp_path / "p.sqlite") as st:
        prog = Progress(st, logging.getLogger("test.progress.noop"))
        await prog.close()  # never started — must not raise
