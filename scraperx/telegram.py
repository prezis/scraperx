"""telegram — public Telegram channels through the web preview ``https://t.me/s/<channel>``. No account, no API id,
no phone number: plain HTTP, the same route open-source scrapers use (axmaier/tme-s, Rel8ble/telegram-channel-scraper-python).

Measured 2026-10-28 from this host: ``t.me/s/durov`` and ``t.me/s/telegram`` answered 200 with 20 messages per page in
0.2-0.3 s (one earlier response took 30 s); ``?before=<id>`` pages back (19-20 older messages), ``?q=<text>`` searches
inside the channel. A channel without a public preview redirects away from ``/s/`` and gives no messages.

    from scraperx.telegram import get_channel_posts, search_channel, get_channel_info

    for p in get_channel_posts("durov", limit=40):
        print(p.date, p.views, p.url, p.text[:80])

Counts: ``views`` and ``subscribers`` are Telegram's ROUNDED display values ("1.61M" -> 1_610_000); the raw text stays
in ``views_text`` / ``subscribers_text``. Reactions are kept as display text.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = [
    "TelegramChannel",
    "TelegramError",
    "TelegramPost",
    "get_channel_info",
    "get_channel_posts",
    "search_channel",
]

PREVIEW_BASE = "https://t.me/s/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_MAX_PAGES = 50


class TelegramError(RuntimeError):
    """The channel has no public preview, or the page could not be read."""


@dataclass
class TelegramPost:
    """One channel message from the web preview.

    Attributes:
        channel:        Channel username (as in the URL).
        id:             Message id inside the channel.
        url:            https://t.me/<channel>/<id>
        text:           Message text, one line per paragraph ("" for media-only posts).
        date:           ISO-8601 time from the page (UTC offset included), "" when absent.
        views:          Rounded display count as an int (1.61M -> 1610000), 0 when absent.
        views_text:     The display text itself ("1.61M").
        author:         Signature of the author on signed channels, "".
        forwarded:      True for a forwarded message (also when the source is hidden).
        forwarded_from: Name of the forwarded source, "" when not forwarded or hidden.
        forwarded_from_url: Link to the source when Telegram shows one, "".
        photos:         Image URLs (Telegram CDN, short-lived tokens).
        videos:         Video URLs (Telegram CDN, short-lived tokens).
        links:          Links inside the text, plus the link-preview target.
        reactions:      Reaction counts as display text, paid stars first when present.
    """

    channel: str
    id: int
    url: str
    text: str = ""
    date: str = ""
    views: int = 0
    views_text: str = ""
    author: str = ""
    forwarded: bool = False
    forwarded_from: str = ""
    forwarded_from_url: str = ""
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)


@dataclass
class TelegramChannel:
    """The channel header of the web preview."""

    username: str
    title: str = ""
    description: str = ""
    verified: bool = False
    subscribers: int = 0
    subscribers_text: str = ""


# ---------------------------------------------------------------------------
# HTTP (module-level seam so tests monkeypatch one function)
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int = 30) -> tuple[int, str, str]:
    """GET → (status, final_url, body)."""
    req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.geturl(), resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return int(e.code), url, ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|blockquote|pre|li)>", re.IGNORECASE)
_WRAP_RE = re.compile(r'<div class="tgme_widget_message_wrap')
_POST_ATTR_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_VIEWS_RE = re.compile(r'<span class="tgme_widget_message_views">([^<]+)</span>')
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_AUTHOR_RE = re.compile(r'<span class="tgme_widget_message_from_author"[^>]*>([^<]+)</span>')
# The source name sits in a <span> (hidden source: empty) or, when Telegram links it, presumably an <a> with the same
# class; only the <span> form was on the 2026-10-28 pages. Matched on the class name, the element name left open.
_FORWARD_RE = re.compile(
    r'class="tgme_widget_message_forwarded_from_name"([^>]*)>(.*?)</(?:a|span)>\s*</div>', re.DOTALL
)
_PHOTO_RE = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)")
_VIDEO_RE = re.compile(r'<video[^>]*\bsrc="([^"]+)"')
_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')
_PREVIEW_LINK_RE = re.compile(r'<a class="tgme_widget_message_link_preview" href="([^"]+)"')
_REACTION_RE = re.compile(r'<span class="tgme_reaction[^"]*">(.*?)</span>', re.DOTALL)
_COUNT_RE = re.compile(r"^\s*([\d.,]+)\s*([KMB]?)\s*$", re.IGNORECASE)


def _text(fragment: str) -> str:
    text = unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", fragment))).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _first_href(attrs: str) -> str:
    m = re.search(r'\bhref="([^"]+)"', attrs or "")
    return m.group(1) if m else ""


def _count(display: str) -> int:
    """Telegram's rounded display count → int ("1.61M" → 1610000, "5.29K" → 5290, "102" → 102); 0 if unreadable."""
    m = _COUNT_RE.match(display or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", ""))
    return round(num * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def _parse_posts(html: str) -> list[TelegramPost]:
    """All messages on one preview page, in page order (oldest first)."""
    starts = [m.start() for m in _WRAP_RE.finditer(html)]
    posts: list[TelegramPost] = []
    for k, start in enumerate(starts):
        block = html[start : starts[k + 1] if k + 1 < len(starts) else len(html)]
        pm = _POST_ATTR_RE.search(block)
        if not pm:
            continue
        channel, msg_id = pm.group(1), int(pm.group(2))
        tm = _TEXT_RE.search(block)
        text_html = tm.group(1) if tm else ""
        views_text = (_VIEWS_RE.search(block) or [None, ""])[1].strip()
        fwd = _FORWARD_RE.search(block)
        links = [unescape(u) for u in _HREF_RE.findall(text_html)]
        preview = _PREVIEW_LINK_RE.search(block)
        if preview:
            links.append(unescape(preview.group(1)))
        posts.append(
            TelegramPost(
                channel=channel,
                id=msg_id,
                url=f"https://t.me/{channel}/{msg_id}",
                text=_text(text_html),
                date=(_TIME_RE.search(block) or [None, ""])[1],
                views=_count(views_text),
                views_text=views_text,
                author=unescape((_AUTHOR_RE.search(block) or [None, ""])[1]).strip(),
                forwarded='class="tgme_widget_message_forwarded_from' in block,
                forwarded_from=_text(fwd.group(2)) if fwd else "",
                forwarded_from_url=unescape(_first_href(fwd.group(1))) if fwd else "",
                photos=[unescape(u) for u in _PHOTO_RE.findall(block)],
                videos=[unescape(u) for u in _VIDEO_RE.findall(block)],
                links=links,
                reactions=[_text(r) for r in _REACTION_RE.findall(block) if _text(r)],
            )
        )
    return posts


def _first_text(rx: re.Pattern, html: str) -> str:
    m = rx.search(html)
    return _text(m.group(1)) if m else ""


_CH_TITLE_RE = re.compile(r'<div class="tgme_channel_info_header_title">(.*?)</div>', re.DOTALL)
_CH_DESC_RE = re.compile(r'<div class="tgme_channel_info_description">(.*?)</div>', re.DOTALL)
_CH_COUNTER_RE = re.compile(
    r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>'
)


def _parse_channel(html: str, username: str) -> TelegramChannel:
    subs = ""
    for value, kind in _CH_COUNTER_RE.findall(html):
        if kind.strip().startswith("subscriber"):
            subs = value.strip()
    header = html[html.find('class="tgme_channel_info_header"') :][:3000] if "tgme_channel_info_header" in html else ""
    return TelegramChannel(
        username=username,
        title=_first_text(_CH_TITLE_RE, html),
        description=_first_text(_CH_DESC_RE, html),
        verified="verified-icon" in header,
        subscribers=_count(subs),
        subscribers_text=subs,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _check_channel(channel: str) -> str:
    ch = (channel or "").strip().removeprefix("@")
    for prefix in ("https://t.me/s/", "https://t.me/", "t.me/s/", "t.me/"):
        if ch.startswith(prefix):
            ch = ch[len(prefix) :]
    ch = ch.split("/")[0].split("?")[0]
    if not _CHANNEL_RE.match(ch):
        raise TelegramError(f"not a Telegram channel username: {channel!r}")
    return ch


def _page(channel: str, params: dict[str, str | int], timeout: int) -> str:
    url = f"{PREVIEW_BASE}{channel}" + (f"?{urlencode(params)}" if params else "")
    status, final_url, body = _http_get(url, timeout=timeout)
    if status != 200:
        raise TelegramError(f"t.me answered HTTP {status} for {url}")
    if "/s/" not in final_url or "tgme_channel_info" not in body:
        raise TelegramError(f"@{channel} has no public web preview (private channel, a user, or a bot)")
    return body


def _collect(channel: str, params: dict[str, str | int], limit: int, timeout: int, delay_s: float) -> list[TelegramPost]:
    """Newest-first posts, paging back with ?before=<oldest id seen> until `limit` or the start of the channel."""
    seen: dict[int, TelegramPost] = {}
    params = dict(params)
    for page in range(_MAX_PAGES):
        if page and delay_s:
            time.sleep(delay_s + random.random() * delay_s)
        posts = _parse_posts(_page(channel, params, timeout))
        new = [p for p in posts if p.id not in seen]
        for p in new:
            seen[p.id] = p
        if not new or len(seen) >= limit:
            break
        params["before"] = min(p.id for p in posts)
    return sorted(seen.values(), key=lambda p: p.id, reverse=True)[:limit]


def get_channel_posts(
    channel: str, limit: int = 20, *, before: int | None = None, timeout: int = 30, delay_s: float = 1.0
) -> list[TelegramPost]:
    """Newest-first messages of a public channel (20 per preview page, paged back as needed).

    Args:
        channel: Username, @username, or a t.me link.
        limit:   How many messages at most.
        before:  Start below this message id (older messages only).
        delay_s: Pause between pages (plus up to the same again at random).
    """
    ch = _check_channel(channel)
    return _collect(ch, {"before": before} if before else {}, max(1, int(limit)), timeout, delay_s)


def search_channel(
    channel: str, query: str, limit: int = 20, *, timeout: int = 30, delay_s: float = 1.0
) -> list[TelegramPost]:
    """Messages of one public channel that match `query` (the preview's own ``?q=`` search), newest first."""
    ch = _check_channel(channel)
    if not (query or "").strip():
        raise TelegramError("query must be non-empty")
    return _collect(ch, {"q": query.strip()}, max(1, int(limit)), timeout, delay_s)


def get_channel_info(channel: str, *, timeout: int = 30) -> TelegramChannel:
    """Title, description, verified flag and (rounded) subscriber count of a public channel."""
    ch = _check_channel(channel)
    return _parse_channel(_page(ch, {}, timeout), ch)
