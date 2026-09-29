"""Profile split (#70): `reproject --include-target/--exclude-target/--out-profile`.

Every test runs offline against a two-target source built by
`seed_two_target_source`: run 1 collects `@alpha` (channel 5), run 2 collects
`@beta` (channel 6) with a linked discussion group (77). Nothing here touches
Telegram or a real archive.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource, ResolveRecord
from paperboy.reproject import ReprojectError, TargetFilter, reproject, resolve_target_filter
from paperboy.store.db import Store
from paperboy.targets import parse_target
from tests.fakes import FakeGateway

ALPHA_ID, BETA_ID, BETA_GROUP_ID = 5, 6, 77
ALPHA_BYTES = b"alpha file contents"
BETA_BYTES = b"beta file contents"


def _channel_fixtures(
    channel_id: int, username: str, *, linked: int | None, media: dict[int, bytes]
) -> dict:
    chan = {
        "_": "channel", "id": channel_id, "access_hash": 99, "title": username.upper(),
        "username": username, "broadcast": True,
    }
    full_chats = [chan]
    if linked:
        full_chats.append(
            {"_": "channel", "id": linked, "access_hash": 4242,
             "title": f"{username} chat", "megagroup": True}
        )
    history = [{"_": "message", "id": 1, "message": "m1", "date": 1767322445}]
    for msg_id in media:
        history.append({
            "_": "message", "id": msg_id, "message": "", "date": 1767322500,
            "media": {
                "_": "MessageMediaDocument",
                "document": {
                    "_": "Document", "id": channel_id * 1000 + msg_id, "access_hash": 1,
                    "mime_type": "text/plain", "size": len(media[msg_id]),
                    "attributes": [
                        {"_": "DocumentAttributeFilename", "file_name": f"{username}.txt"}
                    ],
                },
            },
        })
    return {
        "resolve": {
            "peer": {"_": "PeerChannel", "channel_id": channel_id},
            "chats": [chan], "users": [],
        },
        "full_channel": {
            "full_chat": {
                "_": "channelFull", "id": channel_id, "participants_count": 10,
                "pts": 1, "linked_chat_id": linked,
            },
            "chats": full_chats, "users": [],
        },
        "self": {"_": "user", "id": 1, "self": True},
        "history": history,
        "get_messages": {},
        "channel_difference": {"_": "updates.channelDifferenceEmpty", "final": True, "pts": 1},
        "media": media,
    }


async def _seed(data_dir: Path) -> Path:
    settings = load_settings("default", {"data_dir": data_dir})
    db = data_dir / "default" / "paperboy.sqlite"
    plans = [
        ("@alpha", _channel_fixtures(ALPHA_ID, "alpha", linked=None, media={2: ALPHA_BYTES}),
         ["channel", "history", "media"]),
        ("@beta", _channel_fixtures(BETA_ID, "beta", linked=BETA_GROUP_ID, media={2: BETA_BYTES}),
         ["channel", "history", "discussion", "media"]),
    ]
    with Store.open(db) as store:
        for target, fixtures, phases in plans:
            await collect_channel(
                FakeGateway(fixtures), store, settings, parse_target(target), phases,
                logging.getLogger("seed"),
                collectors=[
                    ChannelCollector(), HistoryCollector(), DiscussionCollector(),
                    MediaCollector(),
                ],
            )
    return db


def seed_two_target_source(data_dir: Path) -> Path:
    """Collect `@alpha` (run 1) and `@beta` + its linked group (run 2) into
    `<data_dir>/default/paperboy.sqlite`; returns the DB path."""
    return asyncio.run(_seed(data_dir))


def _add_stray_user_resolve(db: Path, target: str = "@stray") -> None:
    """A foreign `ResolvedPeer` that resolved to a USER, landing in the last
    run's rowid window (the ADR-0005 'stray intrusion')."""
    with Store.open(db) as st:
        st.add_raw(
            "ResolvedPeer",
            {"_": "contacts.ResolvedPeer", "peer": {"_": "PeerUser", "user_id": 9},
             "chats": [], "users": []},
            "stranger", {"target": target}, observed_at="2026-01-01T00:00:00+00:00",
        )


# --- filtered replay ----------------------------------------------------------

_PAIR_LOG = re.compile(
    r"reproject: run=(?P<run>\S+) target=(?P<target>\S+) channel_id=(?P<cid>\S+) "
    r"decision=(?P<decision>included|excluded)"
)


def run_filtered(
    data_dir: Path, out: Path, *, include=(), exclude=(), caplog=None
) -> dict:
    """Call `reproject()` directly (no CLI) and return `{pair: decision}`
    parsed from the per-pair INFO lines when `caplog` is given."""
    db = data_dir / "default" / "paperboy.sqlite"
    settings = load_settings("default", {"data_dir": data_dir})
    log = logging.getLogger("paperboy.test.split")
    if caplog is not None:
        caplog.set_level(logging.INFO, logger=log.name)
    with ReplaySource.open(db, data_dir / "default") as src, Store.open(out) as store:
        flt = resolve_target_filter(src, list(include), list(exclude))
        asyncio.run(reproject(src, store, settings, "default", None, log, target_filter=flt))
    if caplog is None:
        return {}
    return {
        (m["run"], m["target"]): m["decision"]
        for rec in caplog.records
        if (m := _PAIR_LOG.search(rec.getMessage()))
    }


def _count(db: Path, sql: str, *params) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, params).fetchone()[0]


def _leaks(db: Path, ids: tuple[int, ...]) -> dict[str, int]:
    marks = ",".join("?" * len(ids))
    q = {
        "messages": f"SELECT count(*) FROM messages WHERE channel_id IN ({marks})",
        "channels": f"SELECT count(*) FROM channels WHERE id IN ({marks})",
        "raw_ctx": "SELECT count(*) FROM raw_records WHERE "
                   f"json_extract(context_json,'$.channel_id') IN ({marks})",
        "raw_resolve": "SELECT count(*) FROM raw_records WHERE lower(kind) LIKE '%resolvedpeer' "
                       "AND json_extract(context_json,'$.target') = '@beta'",
        "media": "SELECT count(*) FROM media m JOIN messages s ON m.message_uri=s.uri "
                 f"WHERE s.channel_id IN ({marks})",
        "custody": "SELECT count(*) FROM custody_log c JOIN messages s "
                   f"ON c.source_message_uri=s.uri WHERE s.channel_id IN ({marks})",
    }
    out = {}
    for name, sql in q.items():
        out[name] = _count(db, sql, *(() if name == "raw_resolve" else ids))
    return out


def test_exclude_target_output_has_no_rows_for_the_channel_or_its_linked_group(tmp_path):
    seed_two_target_source(tmp_path)
    full, clean = tmp_path / "full.sqlite", tmp_path / "clean.sqlite"
    run_filtered(tmp_path, full)
    run_filtered(tmp_path, clean, exclude=["@beta"])
    assert _leaks(clean, (BETA_ID, BETA_GROUP_ID)) == dict.fromkeys(
        ("messages", "channels", "raw_ctx", "raw_resolve", "media", "custody"), 0
    )
    # the unfiltered reprojection does carry them (the leak query can fail)
    assert _leaks(full, (BETA_ID, BETA_GROUP_ID))["messages"] > 0
    # alpha's projections are identical to an unfiltered reproject's
    for table, where in (
        ("messages", "channel_id = 5"), ("channels", "id = 5"),
        ("media", "message_uri LIKE 'tg:msg:5/%'"),
        ("custody_log", "source_message_uri LIKE 'tg:msg:5/%'"),
    ):
        assert _rows(clean, table, where) == _rows(full, table, where), table


def test_include_target_output_has_only_that_channel(tmp_path):
    seed_two_target_source(tmp_path)
    out = tmp_path / "beta.sqlite"
    run_filtered(tmp_path, out, include=["@beta"])
    assert _count(out, "SELECT count(*) FROM messages WHERE channel_id NOT IN (6, 77)") == 0
    assert _count(out, "SELECT count(*) FROM messages WHERE channel_id = 77") > 0
    assert _count(out, "SELECT count(*) FROM messages WHERE channel_id = 6") > 0
    assert _count(out, "SELECT count(*) FROM channels WHERE id = 5") == 0


def test_include_and_exclude_partition_the_source(tmp_path, caplog):
    seed_two_target_source(tmp_path)
    inc = run_filtered(tmp_path, tmp_path / "inc.sqlite", include=["@beta"], caplog=caplog)
    caplog.clear()
    exc = run_filtered(tmp_path, tmp_path / "exc.sqlite", exclude=["@beta"], caplog=caplog)
    with ReplaySource.open(tmp_path / "default" / "paperboy.sqlite", tmp_path / "default") as src:
        catalogue = {(r.run_id, r.raw_target) for r in src.resolve_catalogue()}
    assert set(inc) == set(exc) == catalogue
    assert all({inc[p], exc[p]} == {"included", "excluded"} for p in catalogue)
    # Replay re-records exactly what the source held, run by run, so the two
    # outputs' raw rows add up to an unfiltered reprojection's.
    run_filtered(tmp_path, tmp_path / "full.sqlite")
    raw = "SELECT count(*) FROM raw_records"
    assert _count(tmp_path / "inc.sqlite", raw) + _count(tmp_path / "exc.sqlite", raw) == _count(
        tmp_path / "full.sqlite", raw
    )


def test_reproject_summary_line_counts_replayed_and_skipped(tmp_path, caplog):
    seed_two_target_source(tmp_path)
    run_filtered(tmp_path, tmp_path / "o.sqlite", exclude=["@beta"], caplog=caplog)
    summary = [r.getMessage() for r in caplog.records if "targets replayed=" in r.getMessage()]
    assert summary == [
        "reproject: targets replayed=1 skipped=1 runs_touched=1 filter=exclude ids=[6]"
    ]


def test_reproject_that_filters_everything_out_is_an_error(tmp_path):
    seed_two_target_source(tmp_path)
    with pytest.raises(ReprojectError, match="nothing to replay"):
        run_filtered(tmp_path, tmp_path / "o.sqlite", exclude=["@alpha", "@beta"])


def test_stray_user_resolve_goes_with_the_channel_of_its_run(tmp_path, caplog):
    db = seed_two_target_source(tmp_path)
    _add_stray_user_resolve(db)  # lands in @beta's run
    inc = run_filtered(tmp_path, tmp_path / "inc.sqlite", include=["@beta"], caplog=caplog)
    caplog.clear()
    exc = run_filtered(tmp_path, tmp_path / "exc.sqlite", exclude=["@beta"], caplog=caplog)
    stray = [p for p in inc if p[1] == "@stray"]
    assert len(stray) == 1
    assert inc[stray[0]] == "included" and exc[stray[0]] == "excluded"
    # the stray resolve's raw row is out of the clean output, in the split-out one
    assert _count(tmp_path / "exc.sqlite",
                  "SELECT count(*) FROM raw_records WHERE json_extract(context_json,"
                  "'$.target') = '@stray'") == 0
    assert _count(tmp_path / "inc.sqlite",
                  "SELECT count(*) FROM raw_records WHERE json_extract(context_json,"
                  "'$.target') = '@stray'") == 1


def test_stray_in_a_mixed_run_is_replayed_in_both_outputs_with_a_warning(tmp_path, caplog):
    seed_two_target_source(tmp_path)
    # Two channel targets in ONE run: rewrite @alpha's raw rows into @beta's run.
    db = tmp_path / "default" / "paperboy.sqlite"
    with closing(sqlite3.connect(db)) as conn:
        beta_run = conn.execute(
            "SELECT run_id FROM raw_records WHERE json_extract(context_json,'$.target')='@beta'"
        ).fetchone()[0]
        conn.execute("UPDATE raw_records SET run_id = ?", (beta_run,))
        conn.commit()
    _add_stray_user_resolve(db)
    for mode in ("include", "exclude"):
        caplog.clear()
        decisions = run_filtered(
            tmp_path, tmp_path / f"{mode}.sqlite", caplog=caplog, **{mode: ["@beta"]}
        )
        assert decisions[(beta_run, "@stray")] == "included"
        assert any(
            r.levelno == logging.WARNING and beta_run in r.getMessage()
            and "stray" in r.getMessage().lower()
            for r in caplog.records
        ), mode


def _rows(db: Path, table: str, where: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        cols = [
            c[1] for c in conn.execute(f"PRAGMA table_info({table})")
            if c[1] not in ("id", "run_id", "source_raw_id", "raw_id")
        ]
        return sorted(
            conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}").fetchall(),
            key=repr,
        )


# --- selector -----------------------------------------------------------------


def test_target_spellings_resolve_to_one_id(tmp_path):
    db = seed_two_target_source(tmp_path)
    with ReplaySource.open(db, tmp_path / "default") as src:
        for spelling in ("@beta", "beta", "BETA", "t.me/beta", "6"):
            flt = resolve_target_filter(src, [], [spelling])
            assert flt is not None and flt.ids == frozenset({BETA_ID}), spelling
            assert flt.mode == "exclude"
        union = resolve_target_filter(src, ["@alpha", "6"], [])
        assert union is not None and union.ids == frozenset({ALPHA_ID, BETA_ID})
        assert union.mode == "include"


def test_no_filter_flags_means_no_filter(tmp_path):
    db = seed_two_target_source(tmp_path)
    with ReplaySource.open(db, tmp_path / "default") as src:
        assert resolve_target_filter(src, [], []) is None


def test_include_and_exclude_together_is_an_error(tmp_path):
    db = seed_two_target_source(tmp_path)
    with ReplaySource.open(db, tmp_path / "default") as src, pytest.raises(ReprojectError):
        resolve_target_filter(src, ["@alpha"], ["@beta"])


def test_unknown_target_lists_available_targets(tmp_path):
    db = seed_two_target_source(tmp_path)
    _add_stray_user_resolve(db)
    with ReplaySource.open(db, tmp_path / "default") as src, pytest.raises(
        ReprojectError
    ) as exc:
        resolve_target_filter(src, ["@nope"], [])
    msg = str(exc.value)
    assert "@nope" in msg
    assert "@alpha (5)" in msg and "@beta (6)" in msg
    assert "linked group 77" in msg
    assert "non-channel" in msg


def test_linked_group_id_is_not_a_target(tmp_path):
    db = seed_two_target_source(tmp_path)
    with ReplaySource.open(db, tmp_path / "default") as src, pytest.raises(
        ReprojectError, match="unknown target"
    ):
        # The group follows its parent (discussion runs inside the parent's
        # collect); naming it on its own is a mistake, not a second target.
        resolve_target_filter(src, [], [str(BETA_GROUP_ID)])


def test_bot_api_style_and_non_channel_targets_are_rejected(tmp_path):
    db = seed_two_target_source(tmp_path)
    with ReplaySource.open(db, tmp_path / "default") as src:
        for bad in ("-1006", "+15551234567", "t.me/+abcdef", "@stray"):
            with pytest.raises(ReprojectError):
                resolve_target_filter(src, [bad], [])


# --- per-run decisions (orchestrator decision 1) --------------------------------


def _rec(run: str, target: str, cid: int | None) -> ResolveRecord:
    return ResolveRecord(run, target, cid, None)


def test_stray_pair_follows_the_channel_targets_of_its_run():
    stray, chan = _rec("r1", "@stray", None), _rec("r1", "@x", 6)
    exclude = TargetFilter("exclude", frozenset({6}))
    include = TargetFilter("include", frozenset({6}))
    # channel excluded -> the stray goes with it; included -> it goes with it.
    assert exclude.decide_run([chan, stray]) == {"@x": False, "@stray": False}
    assert include.decide_run([chan, stray]) == {"@x": True, "@stray": True}
    # a run whose channel is NOT the target: the stray stays where its run stays
    other = TargetFilter("exclude", frozenset({99}))
    assert other.decide_run([chan, stray]) == {"@x": True, "@stray": True}
    assert TargetFilter("include", frozenset({99})).decide_run([chan, stray]) == {
        "@x": False, "@stray": False,
    }


def test_stray_pair_in_a_mixed_run_is_kept_in_both_outputs():
    recs = [_rec("r1", "@a", 5), _rec("r1", "@b", 6), _rec("r1", "@stray", None)]
    for mode in ("include", "exclude"):
        decisions = TargetFilter(mode, frozenset({6})).decide_run(recs)
        assert decisions["@stray"] is True
        assert decisions["@a"] != decisions["@b"]


def test_stray_pair_alone_in_a_run_is_kept_only_under_exclude():
    recs = [_rec("r1", "@stray", None)]
    assert TargetFilter("exclude", frozenset({6})).decide_run(recs) == {"@stray": True}
    assert TargetFilter("include", frozenset({6})).decide_run(recs) == {"@stray": False}
