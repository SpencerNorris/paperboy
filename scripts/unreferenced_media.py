"""List media files a profile's store no longer references. Read-only: it
prints `key  size` lines and a total, and deletes nothing.

Use it after a profile split (docs/features/reproject.md, "Splitting a mixed
profile"): the files it lists are candidates for deletion, and the decision is
yours.

    uv run python scripts/unreferenced_media.py [--profile default]
"""

from __future__ import annotations

import argparse
import sys

from paperboy.config import load_settings, profile_dir
from paperboy.media_audit import find_unreferenced


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()

    profile = profile_dir(load_settings(args.profile, {}), args.profile)
    try:
        files = find_unreferenced(profile)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    for f in files:
        print(f"{f.key}  {f.size}")
    print(f"total: {len(files)} file(s), {sum(f.size for f in files)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
