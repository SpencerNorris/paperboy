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
    rf"{MEDIA_PREFIX}/(?P<shard>[0-9a-f]{{2}})/(?P<sha>[0-9a-f]{{64}})(?P<ext>\.[^/\\\x00]*)?"
)


def is_valid_ext(ext: str) -> bool:
    return ext == "" or (_EXT_RE.fullmatch(ext) is not None and ".." not in ext)


def media_key(sha256: str, ext: str) -> str:
    """The single constructor of media keys. Raises `ValueError` on bad input."""
    if _SHA_RE.fullmatch(sha256) is None:
        raise ValueError("media key: sha256 must be 64 lowercase hex characters")
    if not is_valid_ext(ext):
        raise ValueError(f"media key: unusable extension {ext!r}")
    return f"{MEDIA_PREFIX}/{sha256[:2]}/{sha256}{ext}"


def key_sha256(value: object) -> str | None:
    """The sha256 a well-formed key names, else `None`. Traversal-safe grammar
    (see module docstring): shard matches the sha, the extension is empty or a
    `.`-led run with no `/`, backslash or NUL."""
    if not isinstance(value, str):
        return None
    m = _KEY_RE.fullmatch(value)
    if m is None or m["shard"] != m["sha"][:2]:
        return None
    return m["sha"]


def is_media_key(value: object) -> bool:
    """True for a well-formed, traversal-safe key (new or legacy-suffixed)."""
    return key_sha256(value) is not None


def find_existing_key(root: Path, sha256: str) -> str | None:
    """The key of a file already on disk for `sha256` under profile dir `root`,
    whatever its extension, else `None`. Content-addressed, so any such file
    already holds the right bytes; reusing it stops a re-derived extension from
    writing a second copy (reproject of a legacy archive, #62)."""
    if _SHA_RE.fullmatch(sha256) is None:
        return None
    shard_dir = root / MEDIA_PREFIX / sha256[:2]
    if not shard_dir.is_dir():
        return None
    candidates = sorted(
        e.name for e in shard_dir.iterdir()
        if e.name.startswith(sha256) and is_media_key(f"{MEDIA_PREFIX}/{sha256[:2]}/{e.name}")
    )
    if not candidates:
        return None
    return f"{MEDIA_PREFIX}/{sha256[:2]}/{candidates[0]}"


def media_dir(settings: Settings, profile: str) -> Path:
    """`<data_dir>/<profile>/media` — the directory keys live under."""
    return profile_dir(settings, profile) / MEDIA_PREFIX


def resolve_key_under(root: Path, key: str) -> Path:
    """Join a key onto `root` (a profile dir). Only well-formed keys."""
    if not is_media_key(key):
        raise ValueError("not a media key")
    return root / key


def resolve_media_key(settings: Settings, profile: str, key: str) -> Path:
    """Absolute path of `key` under the configured profile directory."""
    return resolve_key_under(profile_dir(settings, profile), key)


def normalize_legacy_location(value: str, sha256: str) -> str:
    """Map a legacy stored location (relative, absolute, any separator) to a key.

    Anchored on the sha, not on directory structure: the filename is the
    substring of `value` starting at `sha256`. The legacy extension is kept
    verbatim (it names the file on disk) provided it is traversal-safe.
    Raises `ValueError` when the value does not contain its own sha or the
    result is not a well-formed key; never guesses.
    """
    if _SHA_RE.fullmatch(sha256) is None:
        raise ValueError("media key: sha256 must be 64 lowercase hex characters")
    i = value.find(sha256)
    if i < 0:
        raise ValueError("legacy media location does not contain its sha256")
    key = f"{MEDIA_PREFIX}/{sha256[:2]}/{sha256}{value[i + len(sha256) :]}"
    if not is_media_key(key):
        raise ValueError("legacy media location has an unsafe extension")
    return key
