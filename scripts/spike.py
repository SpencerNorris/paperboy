"""Throwaway read-only spike — settles the spec §13 API unknowns.

Reads the session from the Keychain (never prints it), connects, and runs a
handful of READ-ONLY probes against a public channel you name. Prints pass/fail
and object shapes only — no participant PII is dumped. Safe for the assistant to
run: it authenticates from the stored session and never emits secrets.

    uv run python scripts/spike.py <public_channel_username> [--profile default]

This is throwaway (spec §11 Phase 0): its findings update docs/research and its
recorded TL shapes seed test fixtures. Not part of the shipped tool.
"""

from __future__ import annotations

import argparse
import asyncio

import keyring
from telethon import TelegramClient, functions
from telethon.errors import ChatAdminRequiredError, RPCError
from telethon.sessions import StringSession
from telethon.tl.alltlobjects import LAYER
from telethon.tl.types import ChannelParticipantsAdmins

SERVICE = "paperboy"


async def run(username: str, profile: str) -> None:
    api_id = keyring.get_password(SERVICE, f"{profile}:api_id")
    api_hash = keyring.get_password(SERVICE, f"{profile}:api_hash")
    session = keyring.get_password(SERVICE, f"{profile}:session")
    if not (api_id and api_hash and session):
        raise SystemExit("Run scripts/store_api.py then scripts/login.py first.")

    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Session not authorized — re-run scripts/login.py.")

    print(f"[env] telethon LAYER={LAYER}")

    # Resolve WITHOUT joining.
    entity = await client.get_entity(username)
    ch = await client(functions.channels.GetFullChannelRequest(channel=entity))
    full = ch.full_chat
    print(f"[#13.2] resolve+getFullChannel un-joined OK: "
          f"participants={getattr(full, 'participants_count', None)} "
          f"hidden={getattr(full, 'participants_hidden', None)} "
          f"linked={getattr(full, 'linked_chat_id', None)} pts={getattr(full, 'pts', None)}")

    # History WITHOUT joining.
    n = 0
    async for _ in client.iter_messages(entity, limit=5):
        n += 1
    print(f"[#13.7] getHistory un-joined returned {n} messages (expect >0 for a public channel)")

    # Admin list on a broadcast channel as non-admin (expect CHAT_ADMIN_REQUIRED
    # for a broadcast; supergroups may allow it).
    try:
        parts = await client(functions.channels.GetParticipantsRequest(
            channel=entity,
            filter=ChannelParticipantsAdmins(),
            offset=0, limit=1, hash=0,
        ))
        print(f"[#13.3] getParticipants(Admins) returned (count={getattr(parts, 'count', '?')}) "
              f"— channel likely a supergroup or you're admin")
    except ChatAdminRequiredError:
        print("[#13.3] getParticipants → CHAT_ADMIN_REQUIRED (expected for a broadcast channel)")
    except RPCError as e:
        print(f"[#13.3] getParticipants → {type(e).__name__}: {e}")

    # Similar channels (public, no premium needed for the call).
    try:
        rec = await client(functions.channels.GetChannelRecommendationsRequest(channel=entity))
        print(f"[#graph] getChannelRecommendations OK: "
              f"count={getattr(rec, 'count', len(rec.chats))} returned={len(rec.chats)}")
    except RPCError as e:
        print(f"[#graph] getChannelRecommendations → {type(e).__name__}: {e}")

    await client.disconnect()
    print("[done] spike complete — no secrets printed.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()
    asyncio.run(run(args.username, args.profile))


if __name__ == "__main__":
    main()
