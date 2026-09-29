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

from paperboy.collectors.channel import ChannelCollector
from paperboy.collectors.discussion import DiscussionCollector
from paperboy.collectors.history import HistoryCollector
from paperboy.collectors.media import MediaCollector
from paperboy.config import load_settings
from paperboy.recipes import collect_channel
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
