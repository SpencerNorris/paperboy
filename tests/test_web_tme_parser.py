from pathlib import Path

from paperboy.web.tme_parser import parse_tme_page

FX = Path("tests/fixtures/web/tme_durov_page1.html")


def test_parses_expected_post_count_and_order():
    posts = parse_tme_page(FX.read_text())
    assert [p.post_id for p in posts] == ["durov/523", "durov/524"]


def test_parses_data_post_into_channel_username_and_msg_id():
    posts = parse_tme_page(FX.read_text())
    first = posts[0]
    assert first.channel_username == "durov"
    assert first.msg_id == 523


def test_parses_datetime_attribute_exactly():
    posts = parse_tme_page(FX.read_text())
    assert posts[0].datetime == "2026-08-20T12:00:00+00:00"
    assert posts[1].datetime == "2026-08-19T10:15:00+00:00"


def test_parses_abbreviated_views():
    posts = parse_tme_page(FX.read_text())
    assert posts[0].views == "1.42M"
    assert posts[1].views == "982K"


def test_parses_author_signature():
    posts = parse_tme_page(FX.read_text())
    assert posts[0].author_signature == "Pavel"
    assert posts[1].author_signature is None


def test_parses_forwarded_from():
    posts = parse_tme_page(FX.read_text())
    assert posts[0].forwarded_from is None
    assert posts[1].forwarded_from == "Telegram News"


def test_parses_text_with_inline_link_and_br_as_newline():
    posts = parse_tme_page(FX.read_text())
    assert posts[0].text == "First post text with a link."
    assert posts[1].text == "Second post text\nwith a line break."


def test_empty_page_parses_to_no_posts():
    assert parse_tme_page("<html><body>no feed here</body></html>") == []
