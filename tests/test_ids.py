from datetime import UTC, datetime

from paperboy.ids import channel_uri, chat_uri, msg_uri, parse_uri, to_iso, user_uri, utc_now_iso


def test_uris():
    assert channel_uri(123) == "tg:channel:123"
    assert chat_uri(456) == "tg:chat:456"
    assert user_uri(789) == "tg:user:789"
    assert msg_uri(123, 45) == "tg:msg:123/45"
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
