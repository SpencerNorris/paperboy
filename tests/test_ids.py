from datetime import UTC, datetime

from paperboy.ids import (
    channel_uri,
    chat_uri,
    invite_uri,
    msg_uri,
    parse_tme_link,
    parse_uri,
    to_iso,
    user_uri,
    username_uri,
    utc_now_iso,
    utf16_slice,
)


def test_uris():
    assert channel_uri(123) == "tg:channel:123"
    assert chat_uri(456) == "tg:chat:456"
    assert user_uri(789) == "tg:user:789"
    assert msg_uri(123, 45) == "tg:msg:123/45"
    assert username_uri("durov") == "tg:username:durov"
    assert invite_uri("AbCdEf") == "tg:invite:AbCdEf"
    assert parse_uri("tg:msg:123/45") == ("msg", (123, 45))
    assert parse_uri("tg:user:9") == ("user", (9,))
    assert parse_uri("tg:channel:5") == ("channel", (5,))
    assert parse_uri("tg:chat:5") == ("chat", (5,))


def test_parse_uri_malformed_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_uri("not-a-uri")
    with pytest.raises(ValueError):
        parse_uri("tg:msg:abc/def")


def test_to_iso_normalizes_utc():
    assert to_iso(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2026-01-02T03:04:05+00:00"
    # Epoch for 2026-01-02T03:04:05+00:00 (verified against datetime.fromtimestamp).
    assert to_iso(1767323045) == "2026-01-02T03:04:05+00:00"


def test_to_iso_requires_aware_datetime():
    import pytest

    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 2, 3, 4, 5))  # noqa: DTZ001 - deliberately naive


def test_utc_now_iso_is_iso_utc():
    s = utc_now_iso()
    assert s.endswith("+00:00")
    # round-trips through fromisoformat
    datetime.fromisoformat(s)


def test_utf16_slice_ascii():
    assert utf16_slice("hello world", 6, 5) == "world"


def test_utf16_slice_handles_astral_prefix():
    # U+1F600 (grinning face) is one Python codepoint but 2 UTF-16 code
    # units, so "world" starts at UTF-16 offset 8, not Python-index 7.
    text = "\U0001f600 world"
    assert utf16_slice(text, 3, 5) == "world"


def test_parse_tme_link_username():
    assert parse_tme_link("https://t.me/durov") == ("username", "durov")
    assert parse_tme_link("t.me/durov") == ("username", "durov")
    assert parse_tme_link("https://telegram.me/durov") == ("username", "durov")
    # message-link and preview-link variants reduce to the bare username
    assert parse_tme_link("https://t.me/durov/123") == ("username", "durov")
    assert parse_tme_link("https://t.me/s/durov") == ("username", "durov")


def test_parse_tme_link_invite():
    assert parse_tme_link("https://t.me/+AbCdEf123") == ("invite", "AbCdEf123")
    assert parse_tme_link("https://t.me/joinchat/AbCdEf123") == ("invite", "AbCdEf123")


def test_parse_tme_link_rejects_non_telegram_urls():
    assert parse_tme_link("https://example.com/durov") is None
    assert parse_tme_link("not a url at all") is None
