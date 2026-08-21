"""Parse a `t.me/s/<name>` server-rendered feed page (spec §2.8, §6 `web`).

Deliberately stdlib-only (`html.parser.HTMLParser`) — no HTML/CSS-selector
dependency is added for this. `TmeParser` walks the known, verified-live
markup (`docs/research/sources/prior-art.md` §3.7):

    <div class="tgme_widget_message ..." data-post="name/523" ...>
      ...
      <div class="tgme_widget_message_text ...">post text</div>
      ...
      <a class="tgme_widget_message_date" ...>
        <time datetime="2026-08-20T12:00:00+00:00" class="time">...</time>
      </a>
      <span class="tgme_widget_message_views">1.42M</span>
      <span class="tgme_widget_message_author_signature">Jane</span>
      <div class="tgme_widget_message_forwarded_from">
        <span class="tgme_widget_message_forwarded_from_name">Other Channel</span>
      </div>
    </div>

Each post's own wrapping `div` (the one carrying `data-post`) is tracked by
counting nested `<div>` opens/closes from that point, so a post is only
finalized when *its own* div closes, not some inner one — matched purely
on HTML class *tokens* (space-separated), so `tgme_widget_message_bubble`
never matches the `tgme_widget_message` token check.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class TmePost:
    post_id: str  # raw `data-post` value, e.g. "durov/523"
    channel_username: str | None
    msg_id: int | None
    datetime: str | None = None
    views: str | None = None
    author_signature: str | None = None
    forwarded_from: str | None = None
    text: str = ""


def _split_post_id(post_id: str) -> tuple[str | None, int | None]:
    username, _, msg_id_s = post_id.rpartition("/")
    if not username or not msg_id_s.isdigit():
        return None, None
    return username, int(msg_id_s)


def _classes(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


class _TmeHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[TmePost] = []
        self._current: TmePost | None = None
        self._post_div_depth = 0
        self._collecting_text = False
        self._text_buf: list[str] = []
        self._awaiting_field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = _classes(attr_map)

        if tag == "div":
            if self._current is not None:
                self._post_div_depth += 1
            if "tgme_widget_message" in classes and attr_map.get("data-post"):
                post_id = attr_map["data-post"]
                assert post_id is not None
                username, msg_id = _split_post_id(post_id)
                self._current = TmePost(
                    post_id=post_id, channel_username=username, msg_id=msg_id
                )
                self._post_div_depth = 1
            elif "tgme_widget_message_text" in classes and self._current is not None:
                self._collecting_text = True
                self._text_buf = []
            return

        if self._current is None:
            return

        if tag == "time" and "datetime" in attr_map:
            self._current.datetime = attr_map["datetime"]
        elif tag == "br" and self._collecting_text:
            self._text_buf.append("\n")
        elif tag in ("span", "a"):
            if "tgme_widget_message_views" in classes:
                self._awaiting_field = "views"
            elif "tgme_widget_message_author_signature" in classes:
                self._awaiting_field = "author_signature"
            elif "tgme_widget_message_forwarded_from_name" in classes:
                self._awaiting_field = "forwarded_from"

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._current is not None:
            if self._collecting_text:
                self._current.text = "".join(self._text_buf).strip()
                self._collecting_text = False
            self._post_div_depth -= 1
            if self._post_div_depth <= 0:
                self.posts.append(self._current)
                self._current = None
        elif tag in ("span", "a"):
            self._awaiting_field = None

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._collecting_text:
            self._text_buf.append(data)
        elif self._awaiting_field is not None:
            field_name = self._awaiting_field
            current_value = getattr(self._current, field_name) or ""
            setattr(self._current, field_name, current_value + data)

    def close(self) -> None:
        super().close()
        for post in self.posts:
            if post.author_signature is not None:
                post.author_signature = post.author_signature.strip()
            if post.forwarded_from is not None:
                post.forwarded_from = post.forwarded_from.strip()
            if post.views is not None:
                post.views = post.views.strip()


def parse_tme_page(html: str) -> list[TmePost]:
    """Parse one `t.me/s/<name>` (or `?before=`/`?after=`-paged) HTML page
    into its posts, in document order (Telegram renders newest-first).
    """
    parser = _TmeHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.posts
