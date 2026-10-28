"""scraperx.telegram — public channels via the t.me/s/ web preview. Network-free: `_http_get` is monkeypatched with a
synthetic page in the markup read 2026-10-28. Live smoke test behind SCRAPERX_LIVE=1."""

from __future__ import annotations

import os
import re

import pytest

from scraperx import telegram as tg
from scraperx.telegram import TelegramError, get_channel_info, get_channel_posts, search_channel

with open(os.path.join(os.path.dirname(__file__), "fixtures", "telegram", "channel.html"), encoding="utf-8") as _fh:
    _PAGE = _fh.read()


def _older_page(ids: list[int]) -> str:
    """The fixture page renumbered to the given ids (pages back with ?before=)."""
    page = _PAGE
    for old, new in zip((101, 102, 104), ids, strict=True):
        page = page.replace(f"demochan/{old}", f"demochan/{new}")
    return page


def _fake(pages: dict[str, str], seen: list[str]):
    def fake(url, *, timeout=30):
        seen.append(url)
        for key, body in pages.items():
            if url.endswith(key):
                return 200, url, body
        return 200, url, _PAGE
    return fake


def test_posts_are_parsed_with_every_field(monkeypatch):
    monkeypatch.setattr(tg, "_http_get", _fake({}, []))
    posts = get_channel_posts("demochan", limit=3)
    assert [p.id for p in posts] == [104, 102, 101]  # newest first
    video, forwarded, first = posts
    assert first.url == "https://t.me/demochan/101" and first.date == "2026-10-11T14:52:40+00:00"
    assert (first.views, first.views_text, first.author) == (1_610_000, "1.61M", "Alice Demo")
    assert first.text == "✨ Release notes\nPool drained in one transaction.\nPrice barely moved!\nRead the post now."
    assert first.links == ["https://example.com/post", "https://example.com/preview"]
    assert first.reactions == ["7.42K", "952"] and not first.forwarded
    assert forwarded.forwarded and forwarded.forwarded_from == "" and forwarded.views == 952
    assert forwarded.photos == ["https://cdn4.telesco.pe/file/demo-photo.jpg"]
    assert video.text == "" and video.videos == ["https://cdn1.telesco.pe/file/demo.mp4?token=abc"] and video.views == 5290


def test_paging_back_with_before(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(tg, "_http_get", _fake({"?before=101": _older_page([95, 97, 99])}, seen))
    posts = get_channel_posts("https://t.me/s/demochan", limit=5, delay_s=0)
    assert [p.id for p in posts] == [104, 102, 101, 99, 97]
    assert seen == ["https://t.me/s/demochan", "https://t.me/s/demochan?before=101"]


def test_paging_stops_when_a_page_adds_nothing(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(tg, "_http_get", _fake({}, seen))  # every page is the same three posts
    assert len(get_channel_posts("demochan", limit=50, delay_s=0)) == 3
    assert len(seen) == 2


def test_search_uses_the_preview_query(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(tg, "_http_get", _fake({}, seen))
    search_channel("@demochan", "pool drained", limit=2, delay_s=0)
    assert seen[0] == "https://t.me/s/demochan?q=pool+drained"
    with pytest.raises(TelegramError):
        search_channel("demochan", "  ")


def test_channel_info(monkeypatch):
    monkeypatch.setattr(tg, "_http_get", _fake({}, []))
    c = get_channel_info("demochan")
    assert (c.title, c.description, c.verified) == ("Demo Channel", "Demo channel & notes.", True)
    assert (c.subscribers, c.subscribers_text) == (10_200_000, "10.2M")


def test_channel_without_preview_is_named(monkeypatch):
    monkeypatch.setattr(tg, "_http_get", lambda url, timeout=30: (200, "https://t.me/someuser", "<html>user page</html>"))
    with pytest.raises(TelegramError, match="no public web preview"):
        get_channel_posts("someuser")
    with pytest.raises(TelegramError, match="not a Telegram channel"):
        get_channel_posts("bad name!")


def test_counts():
    assert [tg._count(x) for x in ("1.61M", "5.29K", "102", "1,204", "", "n/a")] == [1_610_000, 5290, 102, 1204, 0, 0]


def test_cli_telegram_json(monkeypatch, capsys):
    import json
    import sys

    from scraperx import __main__ as cli

    monkeypatch.setattr(tg, "_http_get", _fake({}, []))
    monkeypatch.setattr(sys, "argv", ["scraperx", "telegram", "posts", "demochan", "-n", "2", "--json"])
    cli.main()
    assert [p["id"] for p in json.loads(capsys.readouterr().out)] == [104, 102]


@pytest.mark.skipif(os.environ.get("SCRAPERX_LIVE") != "1", reason="live network; SCRAPERX_LIVE=1 to run")
def test_live_durov():
    posts = get_channel_posts("durov", limit=25)
    assert len(posts) == 25 and all(p.date and p.url for p in posts)
    assert re.match(r"^\d", posts[0].views_text)
