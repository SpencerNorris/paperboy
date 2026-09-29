"""Profile-relative media keys (ADR-0007, #62)."""

from __future__ import annotations

import pytest

from paperboy.config import load_settings
from paperboy.media_keys import (
    is_media_key,
    media_dir,
    media_key,
    normalize_legacy_location,
    resolve_key_under,
    resolve_media_key,
)

SHA = "ab" + "0" * 62


def test_media_key_builds_profile_relative_key():
    assert media_key(SHA, ".pdf") == f"media/ab/{SHA}.pdf"
    assert media_key(SHA, "") == f"media/ab/{SHA}"


def test_media_key_preserves_extension_case():
    assert media_key(SHA, ".MP4") == f"media/ab/{SHA}.MP4"


@pytest.mark.parametrize("bad", ["AB" + "0" * 62, "ab" + "0" * 61, "zz" + "0" * 62, ""])
def test_media_key_rejects_bad_sha(bad):
    with pytest.raises(ValueError):
        media_key(bad, ".pdf")


@pytest.mark.parametrize("bad", ["pdf", ".", "..", ".a/b", ".a\\b", ". x", "." + "a" * 17, ".a..b"])
def test_media_key_rejects_bad_ext(bad):
    with pytest.raises(ValueError):
        media_key(SHA, bad)


def test_is_media_key():
    assert is_media_key(f"media/ab/{SHA}.jpg")
    assert is_media_key(f"media/ab/{SHA}")
    assert not is_media_key(f"data/default/media/ab/{SHA}.jpg")
    assert not is_media_key(f"/abs/media/ab/{SHA}.jpg")
    assert not is_media_key(f"media/ab/{SHA}/x")
    assert not is_media_key(f"media/cd/{SHA}.jpg")  # shard must match the sha
    assert not is_media_key(None)


def test_resolve_media_key_joins_profile_dir(tmp_path):
    settings = load_settings("default", {"data_dir": tmp_path})
    assert resolve_media_key(settings, "p", f"media/ab/{SHA}.pdf") == (
        tmp_path / "p" / "media" / "ab" / f"{SHA}.pdf"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "media/../x",
        f"media/ab/../../{SHA}",
        "",
        "C:\\x",
        f"data/default/media/ab/{SHA}.jpg",
    ],
)
def test_resolve_rejects_non_keys(tmp_path, bad):
    with pytest.raises(ValueError):
        resolve_key_under(tmp_path, bad)


def test_normalize_legacy_both_forms_give_same_key():
    want = f"media/ab/{SHA}.jpg"
    for value in (
        f"data/default/media/ab/{SHA}.jpg",
        f"/mnt/x/data/default/media/ab/{SHA}.jpg",
        f"C:\\d\\media\\ab\\{SHA}.jpg",
        want,
    ):
        assert normalize_legacy_location(value, SHA) == want


def test_normalize_legacy_without_sha_raises():
    with pytest.raises(ValueError):
        normalize_legacy_location("elsewhere/nothing.bin", SHA)
    with pytest.raises(ValueError):
        normalize_legacy_location(f"data/media/ab/{SHA}.bad ext", SHA)


def test_media_dir(tmp_path):
    settings = load_settings("default", {"data_dir": tmp_path})
    assert media_dir(settings, "p") == tmp_path / "p" / "media"
