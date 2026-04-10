"""
X/Twitter Scraper - reusable module with multi-method fallback.

Methods (in priority order):
1. FxTwitter API  - free, no auth, rich JSON (primary)
2. vxTwitter API  - free, no auth, simpler JSON (fallback)
3. yt-dlp          - requires cookies/login, extracts metadata
4. oembed API      - official Twitter endpoint, text-only, ultra-reliable

Usage:
    from scraperx import XScraper
    scraper = XScraper()
    tweet = scraper.get_tweet("https://x.com/user/status/123456")
    print(tweet.text, tweet.author, tweet.media_urls)
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:twitter|x)\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)"
    r"/(?P<user>[^/]+)/status/(?P<id>\d+)"
)


class TweetNotFoundError(RuntimeError):
    """Raised when a tweet does not exist (deleted, private, or suspended)."""
    pass


@dataclass
class Tweet:
    """Parsed tweet data."""
    id: str
    text: str
    author: str
    author_handle: str
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    views: int = 0
    author_avatar: str = ""
    author_id: str = ""
    media_urls: list[str] = field(default_factory=list)
    article_title: Optional[str] = None
    article_text: Optional[str] = None
    quoted_tweet: Optional['Tweet'] = None
    source_method: str = ""
    raw: dict = field(default_factory=dict, repr=False)


def parse_tweet_url(url: str) -> tuple[str, str]:
    """Extract (username, tweet_id) from any X/Twitter URL variant."""
    m = TWEET_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid tweet URL: {url}")
    return m.group("user"), m.group("id")


_ALLOWED_DOMAINS = frozenset({
    "api.fxtwitter.com",
    "api.vxtwitter.com",
    "publish.twitter.com",
})


def _http_get_json(url: str, timeout: int = 15) -> dict:
    """Simple HTTP GET returning parsed JSON. Only allows known API domains."""
    host = urlparse(url).hostname or ""
    if host not in _ALLOWED_DOMAINS:
        raise ValueError(f"Domain not allowed: {host}")
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; ScraperX/1.0)",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type and "json" not in content_type:
            raise ValueError(
                f"Expected JSON but got HTML from {host} "
                f"(tweet may not exist or API returned an error page)"
            )
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON from {host}: {e} (first 200 chars: {body[:200]})"
            ) from e


def _best_media_url(media_item: dict) -> str:
    """Pick the highest quality URL from a media item.

    For videos: selects variant with highest bitrate.
    For photos: appends :large for full resolution.
    """
    # Video: check for variants array with bitrate
    variants = media_item.get("variants", [])
    if variants:
        best = max(
            (v for v in variants if isinstance(v, dict) and v.get("url")),
            key=lambda v: v.get("bitrate", 0),
            default=None,
        )
        if best:
            return best["url"]

    url = media_item.get("url", media_item.get("thumbnail_url", ""))

    # Photo: append :large for full resolution
    if url and "pbs.twimg.com" in url and not url.endswith(":large"):
        media_type = media_item.get("type", "")
        if media_type in ("photo", "") and not any(
            ext in url for ext in (".mp4", ".m3u8")
        ):
            url = url + ":large"

    return url


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities to plain text."""
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str):
            self.parts.append(data)

    s = _Stripper()
    s.feed(html)
    return " ".join(s.parts).strip()


_MAX_QUOTE_DEPTH = 3


def _extract_article(obj: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract article title and full text from a tweet or quote dict."""
    article = obj.get("article")
    if not article:
        return None, None
    title = article.get("title")
    text = article.get("preview_text")
    content = article.get("content", {})
    blocks = content.get("blocks", []) if isinstance(content, dict) else []
    if blocks:
        parts = []
        for block in blocks:
            txt = block.get("text", "").strip()
            if txt:
                parts.append(txt)
        if parts:
            text = "\n".join(parts)
    return title, text


def _parse_quoted_tweet(quote: dict, depth: int = 0) -> 'Tweet':
    """Parse a quoted tweet dict from FxTwitter into a Tweet, recursively."""
    media_urls: list[str] = []
    if quote.get("media") and quote["media"].get("all"):
        for m in quote["media"]["all"]:
            media_urls.append(_best_media_url(m))

    article_title, article_text = _extract_article(quote)

    text = quote.get("text", "")
    if not text and article_text:
        text = article_text
    elif not text:
        raw_text = quote.get("raw_text")
        if isinstance(raw_text, dict) and raw_text.get("text"):
            text = raw_text["text"]

    nested_quote = None
    if depth < _MAX_QUOTE_DEPTH and quote.get("quote"):
        nested_quote = _parse_quoted_tweet(quote["quote"], depth + 1)

    return Tweet(
        id=str(quote.get("id", "")),
        text=text,
        author=quote.get("author", {}).get("name", ""),
        author_handle=quote.get("author", {}).get("screen_name", ""),
        author_avatar=quote.get("author", {}).get("avatar_url", ""),
        author_id=str(quote.get("author", {}).get("id", "")),
        likes=quote.get("likes", 0),
        retweets=quote.get("retweets", 0),
        replies=quote.get("replies", 0),
        views=quote.get("views", 0),
        media_urls=media_urls,
        article_title=article_title,
        article_text=article_text,
        quoted_tweet=nested_quote,
    )


class XScraper:
    """Multi-method X/Twitter scraper with automatic fallback."""

    def __init__(self, *, timeout: int = 15, ytdlp_cookies: Optional[str] = None):
        """
        Args:
            timeout: HTTP request timeout in seconds.
            ytdlp_cookies: Path to cookies file for yt-dlp method.
        """
        self.timeout = timeout
        self.ytdlp_cookies = ytdlp_cookies

    def get_tweet(self, url: str) -> Tweet:
        """
        Fetch tweet data using fallback chain.
        Tries: fxtwitter -> vxtwitter -> yt-dlp -> oembed.
        Raises RuntimeError if all methods fail.
        """
        user, tweet_id = parse_tweet_url(url)
        errors = []

        for name, method in [
            ("fxtwitter", self._via_fxtwitter),
            ("vxtwitter", self._via_vxtwitter),
            ("yt-dlp", self._via_ytdlp),
            ("oembed", self._via_oembed),
        ]:
            try:
                tweet = method(user, tweet_id)
                tweet.source_method = name
                logger.info("Fetched tweet %s via %s", tweet_id, name)
                return tweet
            except Exception as e:
                logger.warning("Method %s failed: %s", name, e)
                errors.append(f"{name}: {e}")

        # Detect if this is likely a deleted/non-existent tweet (all 404s)
        # vs an infrastructure problem (mixed errors)
        not_found_indicators = ("404", "not found", "NOT_FOUND", "not exist")
        all_not_found = all(
            any(ind.lower() in err.lower() for ind in not_found_indicators)
            for err in errors
            if "yt-dlp" not in err  # skip yt-dlp since it may not be installed
        )
        non_ytdlp_errors = [e for e in errors if "yt-dlp" not in e]

        if all_not_found and non_ytdlp_errors:
            raise TweetNotFoundError(
                f"Tweet {tweet_id} not found (likely deleted, private, or "
                f"suspended account). All API methods returned 404.\n"
                f"Details: {'; '.join(errors)}"
            )

        raise RuntimeError(
            f"All scraping methods failed for tweet {tweet_id}:\n"
            + "\n".join(errors)
        )

    # --- Method 1: FxTwitter API ---

    def _via_fxtwitter(self, user: str, tweet_id: str) -> Tweet:
        url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
        data = _http_get_json(url, self.timeout)

        if data.get("code") != 200:
            raise ValueError(f"FxTwitter returned code {data.get('code')}: {data.get('message')}")

        t = data["tweet"]
        media_urls = []
        if t.get("media") and t["media"].get("all"):
            for m in t["media"]["all"]:
                media_urls.append(_best_media_url(m))

        article_title, article_text = _extract_article(t)

        # For article-only tweets, use article preview/text as tweet text
        text = t.get("text", "")
        if not text and article_text:
            text = article_text
        elif not text and t.get("raw_text", {}).get("text"):
            text = t["raw_text"]["text"]

        # Quoted tweet expansion
        quoted_tweet = None
        if t.get("quote"):
            quoted_tweet = _parse_quoted_tweet(t["quote"], depth=0)

        return Tweet(
            id=tweet_id,
            text=text,
            author=t.get("author", {}).get("name", user),
            author_handle=t.get("author", {}).get("screen_name", user),
            author_avatar=t.get("author", {}).get("avatar_url", ""),
            author_id=str(t.get("author", {}).get("id", "")),
            likes=t.get("likes", 0),
            retweets=t.get("retweets", 0),
            replies=t.get("replies", 0),
            views=t.get("views", 0),
            media_urls=media_urls,
            article_title=article_title,
            article_text=article_text,
            quoted_tweet=quoted_tweet,
            raw=data,
        )

    # --- Method 2: vxTwitter API ---

    def _via_vxtwitter(self, user: str, tweet_id: str) -> Tweet:
        url = f"https://api.vxtwitter.com/{user}/status/{tweet_id}"
        data = _http_get_json(url, self.timeout)

        media_urls = []
        for m in data.get("media_extended", data.get("mediaURLs", [])):
            if isinstance(m, str):
                media_urls.append(m)
            elif isinstance(m, dict):
                media_urls.append(_best_media_url(m))

        # vxTwitter may include quote data
        quoted_tweet = None
        if data.get("quote"):
            quoted_tweet = _parse_quoted_tweet(data["quote"], depth=0)

        return Tweet(
            id=tweet_id,
            text=data.get("text", ""),
            author=data.get("user_name", user),
            author_handle=data.get("user_screen_name", user),
            likes=data.get("likes", 0),
            retweets=data.get("retweets", 0),
            replies=data.get("replies", 0),
            views=data.get("views", 0),
            media_urls=media_urls,
            quoted_tweet=quoted_tweet,
            raw=data,
        )

    # --- Method 3: yt-dlp (requires cookies or login) ---

    def _via_ytdlp(self, user: str, tweet_id: str) -> Tweet:
        import shutil
        if not shutil.which("yt-dlp"):
            raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")

        cmd = ["yt-dlp", "--dump-json", "--no-download"]
        if self.ytdlp_cookies:
            cmd.extend(["--cookies", self.ytdlp_cookies])
        cmd.append(f"https://x.com/{user}/status/{tweet_id}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        except FileNotFoundError:
            raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp exit {result.returncode}: {result.stderr[:200]}")

        # yt-dlp outputs one JSON per line for playlists; take first
        first_line = result.stdout.split('\n', 1)[0]
        data = json.loads(first_line)
        return Tweet(
            id=tweet_id,
            text=data.get("description", ""),
            author=data.get("uploader", user),
            author_handle=data.get("uploader_id", user),
            likes=data.get("like_count", 0),
            retweets=data.get("repost_count", 0),
            views=data.get("view_count", 0),
            media_urls=[data["url"]] if data.get("url") else [],
            raw=data,
        )

    # --- Method 4: oembed (official Twitter endpoint, text-only) ---

    def _via_oembed(self, user: str, tweet_id: str) -> Tweet:
        """Use Twitter's official oembed endpoint. Ultra-reliable but text-only."""
        url = (
            f"https://publish.twitter.com/oembed"
            f"?url=https://twitter.com/{user}/status/{tweet_id}"
            f"&omit_script=true"
        )
        data = _http_get_json(url, self.timeout)

        text = _strip_html(data.get("html", ""))
        author = data.get("author_name", user)
        # Extract handle from author_url: https://twitter.com/handle
        author_url = data.get("author_url", "")
        handle = author_url.rstrip("/").rsplit("/", 1)[-1] if author_url else user

        return Tweet(
            id=tweet_id,
            text=text,
            author=author,
            author_handle=handle,
            raw=data,
        )
