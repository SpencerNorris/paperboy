import pytest

from paperboy.targets import TargetKind, UnsupportedTarget, parse_target


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ("@durov", TargetKind.USERNAME, "durov"),
        ("https://t.me/durov", TargetKind.USERNAME, "durov"),
        ("t.me/+AbCdEf", TargetKind.INVITE, "AbCdEf"),
        ("t.me/joinchat/AbCdEf", TargetKind.INVITE, "AbCdEf"),
        ("https://t.me/durov/1234", TargetKind.MSG_LINK, "durov"),
        ("-1001234567890", TargetKind.PEER_ID, "-1001234567890"),
        ("+15551234567", TargetKind.PHONE, "+15551234567"),
        ("#osint", TargetKind.HASHTAG, "osint"),
    ],
)
def test_parse(text, kind, value):
    t = parse_target(text)
    assert t.kind == kind
    assert t.value == value
    assert t.raw == text


def test_msg_link_captures_id():
    assert parse_target("t.me/durov/1234").msg_id == 1234


def test_non_msg_link_has_no_msg_id():
    assert parse_target("@durov").msg_id is None


def test_channel_like():
    assert parse_target("@durov").is_channel_like
    assert parse_target("t.me/+AbCdEf").is_channel_like
    assert parse_target("https://t.me/durov/1234").is_channel_like
    assert parse_target("-1001234567890").is_channel_like
    assert not parse_target("#osint").is_channel_like
    assert not parse_target("+15551234567").is_channel_like


def test_bare_username_without_at():
    bare, at = parse_target("durov"), parse_target("@durov")
    assert (bare.kind, bare.value) == (at.kind, at.value) == (TargetKind.USERNAME, "durov")


def test_unsupported_target_raises():
    with pytest.raises(UnsupportedTarget):
        parse_target("")


def test_target_is_frozen():
    t = parse_target("@durov")
    with pytest.raises(Exception):  # noqa: B017, PT011 - frozen dataclass raises FrozenInstanceError
        t.value = "x"
