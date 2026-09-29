"""Media locations as profile-relative keys (ADR-0007, #62).

A media location is a POSIX-style string relative to the profile directory,
always `media/<sha256[:2]>/<sha256><ext>`. It never contains the data dir, the
profile name, a drive or a leading `/`, so a store stays valid when it is moved
between working directories, machines or storage backends. Extension case is
preserved (case-sensitive backends). A stored key is data, never trusted as a
path: it is validated before it is joined onto any root.
"""

from __future__ import annotations

import re
from pathlib import Path

from paperboy.config import Settings, profile_dir

MEDIA_PREFIX = "media"

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_EXT_RE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}")
_KEY_RE = re.compile(
    rf"{MEDIA_PREFIX}/(?P<shard>[0-9a-f]{{2}})/(?P<sha>[0-9a-f]{{64}})(?P<ext>\.[^/\\]*)?"
)


def _valid_ext(ext: str) -> bool:
    return ext == "" or (_EXT_RE.fullmatch(ext) is not None and ".." not in ext)


def media_key(sha256: str, ext: str) -> str:
    """The single constructor of media keys. Raises `ValueError` on bad input."""
    if _SHA_RE.fullmatch(sha256) is None:
        raise ValueError("media key: sha256 must be 64 lowercase hex characters")
    if not _valid_ext(ext):
        raise ValueError(f"media key: unusable extension {ext!r}")
    return f"{MEDIA_PREFIX}/{sha256[:2]}/{sha256}{ext}"


def is_media_key(value: object) -> bool:
    """True only for a canonical key (the shard must match the sha)."""
    if not isinstance(value, str):
        return False
    m = _KEY_RE.fullmatch(value)
    if m is None or m["shard"] != m["sha"][:2]:
        return False
    return _valid_ext(m["ext"] or "")


def media_dir(settings: Settings, profile: str) -> Path:
    """`<data_dir>/<profile>/media` — the directory keys live under."""
    return profile_dir(settings, profile) / MEDIA_PREFIX


def resolve_key_under(root: Path, key: str) -> Path:
    """Join a key onto `root` (a profile dir). Strict: only canonical keys."""
    if not is_media_key(key):
        raise ValueError("not a media key")
    return root / key


def resolve_media_key(settings: Settings, profile: str, key: str) -> Path:
    """Absolute path of `key` under the configured profile directory."""
    return resolve_key_under(profile_dir(settings, profile), key)


def normalize_legacy_location(value: str, sha256: str) -> str:
    """Map a legacy stored location (relative, absolute, any separator) to a key.

    Anchored on the sha, not on directory structure: the filename is the
    substring of `value` starting at `sha256`. Raises `ValueError` when the
    value does not contain its own sha; never guesses.
    """
    i = value.find(sha256)
    if i < 0:
        raise ValueError("legacy media location does not contain its sha256")
    return media_key(sha256, value[i + len(sha256) :])
