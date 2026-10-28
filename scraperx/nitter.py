"""nitter — keyless X/Twitter SEARCH (keyword, #hashtag, "phrase", from:user) and user timelines through public Nitter
instances' RSS. No X account of ours is involved: an instance talks to X with its own accounts, the way a redlib mirror
talks to Reddit.

Measured 2026-10-28 (instances listed healthy at status.d420.de 15:49 UTC), plain HTTP, one request each:

  instance                 search RSS                     user RSS
  nitter.mie.foundation    200, 13 items, 1.6 s           200, 20 items
  nitter.mizuki.st         200, 13 items, 3.0 s           200, 20 items      (HTML pages behind Anubis)
  nitter.kareem.one        200, 13 items, 8.2 s           503
  nitter.netbub.com        Gandalf challenge              200, 20 items
  nitter.jaydenha.uk       429                            200, 20 items
  nitter.click             200, 0 items                   200, 0 items
  nitter.pp.ua, shitter.thepixora.com: Cloudflare challenge; nitter.tiekoetter.com, nitter.xitter.cc: Anubis (not listed)

Search responses carry a ``Min-Id`` header: the next page is the same URL with ``&cursor=<Min-Id>``. Instances churn
(X sent the Nitter project a cease-and-desist 2026-08, per TechCrunch): ``SCRAPERX_NITTER_INSTANCES`` (comma-separated
base URLs) replaces the default list without a code change. A failed instance waits 10 min at the back of the order.

    from scraperx.nitter import search, user_tweets
    for t in search('#solana "wallet drained"', limit=30):
        print(t.created_at, t.author_handle, t.text[:80], t.id)
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

from scraperx.fetch import detect_block_page
from scraperx.scraper import Tweet

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_NITTER_INSTANCES", "NitterError", "search", "user_tweets"]

DEFAULT_NITTER_INSTANCES: tuple[str, ...] = (
    "https://nitter.mie.foundation",
    "https://nitter.mizuki.st",
    "https://nitter.kareem.one",
    "https://nitter.netbub.com",
    "https://nitter.jaydenha.uk",
)
_INSTANCES_ENV = "SCRAPERX_NITTER_INSTANCES"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_COOLDOWN_S = 600.0
_MAX_PAGES = 20
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")

_down_until: dict[str, float] = {}
_next_start = 0


class NitterError(RuntimeError):
    """No instance answered with a readable feed (each instance's reason is in the message)."""


# ---------------------------------------------------------------------------
# HTTP + instance rotation
# ---------------------------------------------------------------------------


def _http_get(url: str, *, timeout: int = 25) -> tuple[int, dict, str]:
    """GET → (status, headers, body). Seam for tests."""
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as r:
            return int(r.status), dict(r.headers), r.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return int(e.code), dict(e.headers or {}), ""


def _instances(instances: tuple[str, ...] | None) -> list[str]:
    if instances:
        pool = list(instances)
    else:
        env = [u.strip().rstrip("/") for u in (os.environ.get(_INSTANCES_ENV) or "").split(",") if u.strip()]
        pool = env or list(DEFAULT_NITTER_INSTANCES)
    n = len(pool)
    rot = [pool[(_next_start + i) % n] for i in range(n)] if n else []
    now = time.monotonic()
    up = [u for u in rot if _down_until.get(u, 0.0) <= now]
    return up + [u for u in rot if u not in up]


def _served(instance: str, pool_size: int, pool: list[str]) -> None:
    global _next_start
    _down_until.pop(instance, None)
    _next_start = (pool.index(instance) + 1) % pool_size if instance in pool else 0


def _feed(path_and_query: str, timeout: int, instances: tuple[str, ...] | None) -> tuple[str, dict, str]:
    """The first instance that answers `path_and_query` with an RSS feed holding items → (instance, headers, xml).

    A valid feed with no items from every responding instance → (instance, headers, xml) of the last one (a genuine
    empty result). Nothing readable anywhere → NitterError naming each instance's reason."""
    pool = _instances(instances)
    errors: list[str] = []
    empty: tuple[str, dict, str] | None = None
    for inst in pool:
        try:
            status, headers, body = _http_get(f"{inst}{path_and_query}", timeout=timeout)
        except Exception as e:
            _down_until[inst] = time.monotonic() + _COOLDOWN_S
            errors.append(f"{inst}: {type(e).__name__}: {e}")
            continue
        blocked = detect_block_page(body)
        if status != 200 or blocked or "<rss" not in body:
            _down_until[inst] = time.monotonic() + _COOLDOWN_S
            errors.append(f"{inst}: HTTP {status}" + (f" block page: {blocked}" if blocked else ""))
            continue
        if "<item>" in body:
            _served(inst, len(pool), pool)
            logger.info("nitter: served by %s", inst)
            return inst, headers, body
        empty = (inst, headers, body)
        errors.append(f"{inst}: feed with 0 items")
    if empty:
        return empty
    raise NitterError(f"no Nitter instance answered {path_and_query}: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_TAG = {
    name: re.compile(rf"<{name}>(.*?)</{name}>", re.DOTALL)
    for name in ("title", "dc:creator", "description", "pubDate", "guid", "link")
}
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", re.DOTALL)
_IMG_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"')
_BREAK_RE = re.compile(r"<br\s*/?>|</p>", re.IGNORECASE)
_TAGS_RE = re.compile(r"<[^>]+>")
_RT_RE = re.compile(r"^RT by @([A-Za-z0-9_]{1,15}):\s*")
_STATUS_LINK_RE = re.compile(r"/([A-Za-z0-9_]{1,15})/status/(\d+)")


def _field(item: str, name: str) -> str:
    m = _TAG[name].search(item)
    if not m:
        return ""
    raw = m.group(1)
    cdata = _CDATA_RE.match(raw)
    return cdata.group(1) if cdata else unescape(raw)


def _html_text(fragment: str) -> str:
    text = unescape(_TAGS_RE.sub("", _BREAK_RE.sub("\n", fragment)))
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _twimg(src: str) -> str:
    """A Nitter /pic/ proxy URL → the pbs.twimg.com original ("/pic/media%2FX.jpg" → https://pbs.twimg.com/media/X.jpg)."""
    i = src.find("/pic/")
    if i < 0:
        return src
    path = unquote(src[i + len("/pic/"):]).removeprefix("orig/")
    return f"https://pbs.twimg.com/{path}"


def _parse_rss(xml: str) -> list[Tweet]:
    tweets: list[Tweet] = []
    for item in _ITEM_RE.findall(xml):
        link = _field(item, "link")
        lm = _STATUS_LINK_RE.search(link)
        tid = _field(item, "guid").strip() or (lm.group(2) if lm else "")
        if not tid.isdigit():
            continue
        creator = _field(item, "dc:creator").strip().removeprefix("@")
        handle = creator or (lm.group(1) if lm else "")
        title = _field(item, "title")
        rt = _RT_RE.match(title)
        desc = _field(item, "description")
        text = _html_text(desc) or _RT_RE.sub("", title)
        created = _field(item, "pubDate").strip()
        try:
            ts = int(parsedate_to_datetime(created).timestamp()) if created else None
        except (TypeError, ValueError):
            ts = None
        tweets.append(
            Tweet(
                id=tid,
                text=text,
                author="",
                author_handle=handle,
                media_urls=[_twimg(unescape(s)) for s in _IMG_RE.findall(desc)],
                created_at=created or None,
                created_timestamp=ts,
                reposted_by=rt.group(1) if rt else None,
                source_method="nitter",
            )
        )
    return tweets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _collect(path: str, params: dict, limit: int, timeout: int, delay_s: float, instances) -> list[Tweet]:
    out: list[Tweet] = []
    seen: set[str] = set()
    params = dict(params)
    for page in range(_MAX_PAGES):
        if page and delay_s:
            time.sleep(delay_s + random.random() * delay_s)
        _inst, headers, xml = _feed(f"{path}?{urlencode(params)}" if params else path, timeout, instances)
        new = [t for t in _parse_rss(xml) if t.id not in seen]
        for t in new:
            seen.add(t.id)
        out.extend(new)
        cursor = next((v for k, v in headers.items() if k.lower() == "min-id"), "")
        if not new or not cursor or len(out) >= limit:
            break
        params["cursor"] = cursor
    return out[:limit]


def search(
    query: str,
    limit: int = 20,
    *,
    timeout: int = 25,
    delay_s: float = 1.0,
    instances: tuple[str, ...] | None = None,
) -> list[Tweet]:
    """Latest posts matching an X search query (keywords, #tag, "exact phrase", from:user, since:YYYY-MM-DD, ...),
    newest first, through Nitter search RSS. Tweet fields from RSS: id, author_handle, text, media_urls, created_at;
    counts (likes, views) are not in RSS: enrich through scraperx.search or XScraper when needed."""
    if not (query or "").strip():
        raise NitterError("query must be non-empty")
    return _collect("/search/rss", {"f": "tweets", "q": query.strip()}, max(1, int(limit)), timeout, delay_s, instances)


def user_tweets(
    handle: str,
    limit: int = 20,
    *,
    with_replies: bool = False,
    timeout: int = 25,
    delay_s: float = 1.0,
    instances: tuple[str, ...] | None = None,
) -> list[Tweet]:
    """An account's timeline through Nitter user RSS (reposts carry Tweet.reposted_by). The FxTwitter v2 timeline in
    scraperx.x_timeline has counts and is the first choice; this is the fallback."""
    m = _HANDLE_RE.match((handle or "").strip())
    if not m:
        raise NitterError(f"not an X handle: {handle!r}")
    path = f"/{m.group(1)}/with_replies/rss" if with_replies else f"/{m.group(1)}/rss"
    return _collect(path, {}, max(1, int(limit)), timeout, delay_s, instances)
