"""Store api_id / api_hash in the macOS Keychain — run this in YOUR OWN terminal.

Values are read with getpass (no echo) and never printed. Claude never reads the
Keychain, so once they're here they stay out of the assistant's context.

    uv run python scripts/store_api.py [--profile default]
"""

from __future__ import annotations

import argparse
import getpass

import keyring

SERVICE = "paperboy"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()

    api_id = getpass.getpass("api_id (hidden): ").strip()
    if not api_id.isdigit():
        raise SystemExit("api_id must be an integer")
    api_hash = getpass.getpass("api_hash (hidden): ").strip()
    if len(api_hash) != 32:
        raise SystemExit("api_hash should be 32 hex characters")

    keyring.set_password(SERVICE, f"{args.profile}:api_id", api_id)
    keyring.set_password(SERVICE, f"{args.profile}:api_hash", api_hash)
    print(f"Stored api_id + api_hash for profile '{args.profile}' in the Keychain.")


if __name__ == "__main__":
    main()
