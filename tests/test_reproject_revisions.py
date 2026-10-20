"""Revision counting through a live collect AND a reproject (#96).

Each scenario observes one media message across several collect runs (a fresh
FakeGateway per run, the way a re-collection sees Telegram), then rebuilds the
store with `reproject` and asserts the rebuilt revisions equal the live ones.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from paperboy.cli import app
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway
from tests.test_reproject import assert_round_trip
from tests.test_reproject_parity import full_collect_fixtures

runner = CliRunner()


def _photo(photo_id: int, ref: str) -> dict:
    return {"_": "MessageMediaPhoto",
            "photo": {"_": "Photo", "id": photo_id, "file_reference": ref}}


def _document(doc_id: int, ref: str) -> dict:
    return {"_": "MessageMediaDocument",
            "document": {"_": "Document", "id": doc_id, "file_reference": ref,
                         "mime_type": "text/plain", "attributes": []}}


def _webpage(webpage: dict) -> dict:
    return {"_": "MessageMediaWebPage", "webpage": webpage}


def _collect_runs(data_dir: Path, medias: list[dict]) -> Path:
    """One collect run per entry; msg 7 carries that media in that run."""
    db = data_dir / "default" / "paperboy.sqlite"
    settings = load_settings("default", {"data_dir": data_dir})
    for media in medias:
        fx = full_collect_fixtures()
        fx["history"] = [{"_": "message", "id": 7, "message": "post",
                          "date": 1767322445, "media": media}]
        fx["channel_difference"] = {"_": "updates.channelDifferenceEmpty",
                                    "final": True, "pts": 1}
        with Store.open(db) as store:
            asyncio.run(collect_channel(
                FakeGateway(fx), store, settings, parse_target("@durov"),
                phases=["channel", "history"], log=logging.getLogger("t"),
            ))
    return db


def _revisions(db: Path) -> list[tuple]:
    """(text, media_json) per revision in observation order."""
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "select text, media_json from message_revisions "
            "where message_uri like '%/7' order by observed_at, id"
        ).fetchall()
    finally:
        conn.close()


def _reproject(data_dir: Path, monkeypatch) -> Path:
    monkeypatch.setenv("PAPERBOY_DATA_DIR", str(data_dir))
    result = runner.invoke(app, ["reproject", "--profile", "default"])
    assert result.exit_code == 0, result.output
    return data_dir / "default" / "paperboy.reprojected.sqlite"


def _check(tmp_path, monkeypatch, medias: list[dict], expected: int) -> tuple[Path, Path]:
    live = _collect_runs(tmp_path, medias)
    assert len(_revisions(live)) == expected
    rebuilt = _reproject(tmp_path, monkeypatch)
    assert _revisions(rebuilt) == _revisions(live)
    assert_round_trip(live, rebuilt)
    return live, rebuilt


def test_photo_edited_a_b_a_has_three_revisions(tmp_path, monkeypatch):
    # The photo id changes and changes back; file_reference rotates each time.
    _check(tmp_path, monkeypatch,
           [_photo(1, "r1"), _photo(2, "r2"), _photo(1, "r3")], expected=3)


def test_document_with_only_file_reference_changed_has_one_revision(tmp_path, monkeypatch):
    _check(tmp_path, monkeypatch,
           [_document(9, "r1"), _document(9, "r2"), _document(9, "r3")], expected=1)


def test_webpage_preview_filling_in_has_two_revisions(tmp_path, monkeypatch):
    empty = _webpage({"_": "WebPageEmpty", "id": 5})
    full = _webpage({"_": "WebPage", "id": 5, "url": "https://example.org/a",
                     "title": "A title",
                     "photo": {"_": "Photo", "id": 6, "file_reference": "r1"}})
    full_again = _webpage({"_": "WebPage", "id": 5, "url": "https://example.org/a",
                           "title": "A title",
                           "photo": {"_": "Photo", "id": 6, "file_reference": "r2"}})
    _check(tmp_path, monkeypatch, [empty, full, full_again], expected=2)


def test_old_scheme_latest_revision_is_not_duplicated_live_or_rebuilt(tmp_path, monkeypatch):
    import hashlib

    live = _collect_runs(tmp_path, [_photo(1, "r1")])
    conn = sqlite3.connect(live)
    text, media_json = conn.execute(
        "select text, media_json from message_revisions where message_uri like '%/7'"
    ).fetchone()
    old = hashlib.sha256(f"{text}\x00{media_json}".encode()).hexdigest()
    conn.execute("update message_revisions set content_hash=? where message_uri like '%/7'",
                 (old,))
    conn.commit()
    conn.close()
    # Re-observe unchanged (only file_reference rotated) on top of the old-scheme store.
    db = tmp_path / "default" / "paperboy.sqlite"
    fx = full_collect_fixtures()
    fx["history"] = [{"_": "message", "id": 7, "message": "post",
                      "date": 1767322445, "media": _photo(1, "r2")}]
    fx["channel_difference"] = {"_": "updates.channelDifferenceEmpty",
                                "final": True, "pts": 1}
    with Store.open(db) as store:
        asyncio.run(collect_channel(
            FakeGateway(fx), store, load_settings("default", {"data_dir": tmp_path}),
            parse_target("@durov"), phases=["channel", "history"],
            log=logging.getLogger("t"),
        ))
    assert len(_revisions(live)) == 1
    rebuilt = _reproject(tmp_path, monkeypatch)
    assert _revisions(rebuilt) == _revisions(live)
    # The rebuilt revision carries the new-scheme hash; the live one keeps its old stored hash.
    assert_round_trip(live, rebuilt, skip_tables=frozenset({"message_revisions"}))
