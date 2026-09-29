"""Profile split (#70): `reproject --include-target/--exclude-target/--out-profile`.

Every test runs offline against a two-target source built by
`seed_two_target_source`: run 1 collects `@alpha` (channel 5), run 2 collects
`@beta` (channel 6) with a linked discussion group (77). Nothing here touches
Telegram or a real archive.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
from paperboy.replay import ReplaySource, ResolveRecord
from paperboy.reproject import ReprojectError, TargetFilter, resolve_target_filter
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
