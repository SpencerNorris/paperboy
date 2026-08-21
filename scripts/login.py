"""Interactive Telegram login — run this in YOUR OWN terminal.

Reads api_id/api_hash from the Keychain, prompts for the phone number and the
login code (which arrives in your existing Telegram app), and saves the
resulting session string back to the Keychain. Prints no secrets. Do the login
yourself so the phone number and code never enter the assistant's context.

    uv run python scripts/login.py [--profile default]
"""

from __future__ import annotations

import argparse
import asyncio

import keyring
from telethon import TelegramClient
from telethon.sessions import StringSession

SERVICE = "paperboy"


async def run(profile: str) -> None:
    api_id = keyring.get_password(SERVICE, f"{profile}:api_id")
    api_hash = keyring.get_password(SERVICE, f"{profile}:api_hash")
    if not api_id or not api_hash:
        raise SystemExit("Run scripts/store_api.py first.")

    # NOTE: no proxy here yet — for a throwaway spike only. Real collection
    # goes through the configured proxy (require_proxy). Do the spike over a
    # VPN/proxy at the OS level if you want the spike itself masked.
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()  # prompts for phone + code (+ 2FA) on stdin
    me = await client.get_me()
    session = client.session.save()
    keyring.set_password(SERVICE, f"{profile}:session", session)
    await client.disconnect()
    # print only non-sensitive confirmation
    print(f"Logged in as id={me.id} (username={me.username!r}); session saved to Keychain.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()
    asyncio.run(run(args.profile))


if __name__ == "__main__":
    main()
