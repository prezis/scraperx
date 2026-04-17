"""
Search X/Twitter tweets via DuckDuckGo + FxTwitter (Trawlx pattern).

Zero credentials, zero API key. DuckDuckGo finds tweet URLs,
FxTwitter resolves them to full JSON data.

Usage:
    from scraperx.search import search_tweets
    results = search_tweets("Meteora DLMM strategy", limit=10)
    for tweet in results:
        print(tweet.text, tweet.likes)

    # Search specific user's tweets
    results = search_tweets("from:qwerty_ytrevvq polymarket")

Note: DDG rate-limits aggressively. Use sparingly (~1 search per minute).
If DDG fails, falls back to curl subprocess with different TLS fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from scraperx.scraper import Tweet, XScraper

logger = logging.getLogger(__name__)

# Match x.com/user/status/ID in search result URLs
_TWEET_STATUS_RE = re.compile(r"https?://(?:twitter|x)\.com/([A-Za-z0-9_]+)/status/(\d+)")

# Cache directory
_CACHE_DIR = Path(os.environ.get("SCRAPERX_CACHE", "/tmp/scraperx_cache"))


def _cache_key(query: str, time_filter: str | None) -> str:
    """Generate cache filename from query."""
    raw = f"{query}|{time_filter or ''}"
    h = hashlib.md5(raw.encode()).hexdigest()[:12]
    return h


def _get_cached(query: str, time_filter: str | None, max_age: int = 3600) -> list[str] | None:
    """Return cached URLs if fresh enough."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"search_{_cache_key(query, time_filter)}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _set_cache(query: str, time_filter: str | None, urls: list[str]) -> None:
    """Cache search result URLs."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"search_{_cache_key(query, time_filter)}.json"
    path.write_text(json.dumps(urls))


def _extract_tweet_urls(html: str) -> list[str]:
    """Extract unique tweet URLs from search result HTML."""
    # Method 1: DDG uddg redirect params
    raw_urls = re.findall(r"uddg=([^&\"]+)", html)
    decoded = [unquote(u) for u in raw_urls]

    # Method 2: Direct href links
    href_urls = re.findall(r'href="(https?://(?:twitter|x)\.com/[^"]+)"', html)
    decoded.extend(href_urls)

    # Method 3: Bing-style cite URLs
    cite_urls = re.findall(r"(https?://(?:twitter|x)\.com/\w+/status/\d+)", html)
    decoded.extend(cite_urls)

    # Deduplicate by tweet ID
    seen_ids: set[str] = set()
    tweet_urls: list[str] = []

    for u in decoded:
        m = _TWEET_STATUS_RE.search(u)
        if m:
            tweet_id = m.group(2)
            if tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                tweet_urls.append(f"https://x.com/{m.group(1)}/status/{tweet_id}")

    return tweet_urls


def _ddg_search_urllib(query: str, time_filter: str | None = None) -> str:
    """Fetch DDG HTML via urllib. Expects query already has site: prefix."""
    params = f"q={quote(query)}"
    if time_filter and time_filter in ("d", "w", "m", "y"):
        params += f"&df={time_filter}"

    url = f"https://html.duckduckgo.com/html/?{params}"
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _ddg_search_curl(query: str, time_filter: str | None = None) -> str:
    """Fetch DDG HTML via curl subprocess (different TLS fingerprint).
    Expects query already has site: prefix."""
    params = f"q={quote(query)}"
    if time_filter and time_filter in ("d", "w", "m", "y"):
        params += f"&df={time_filter}"

    url = f"https://html.duckduckgo.com/html/?{params}"
    result = subprocess.run(
        [
            "curl",
            "-s",
            "-L",
            "--max-time",
            "15",
            "-H",
            "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
            "-H",
            "Accept: text/html",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr[:200]}")
    return result.stdout


def _ddg_search(query: str, max_results: int = 30, time_filter: str | None = None) -> list[str]:
    """Search DuckDuckGo for tweet URLs with fallback methods.

    Returns list of unique x.com/user/status/ID URLs.
    """
    # Normalize query for consistent caching
    if "site:" not in query.lower():
        query = f"site:x.com {query}"

    # Check cache first
    cached = _get_cached(query, time_filter)
    if cached:
        logger.info("Cache hit for '%s': %d URLs", query, len(cached))
        return cached[:max_results]

    tweet_urls: list[str] = []

    # Method 1: urllib
    try:
        html = _ddg_search_urllib(query, time_filter)
        tweet_urls = _extract_tweet_urls(html)
        if tweet_urls:
            logger.info("DDG (urllib) '%s': %d tweet URLs", query, len(tweet_urls))
    except Exception as e:
        logger.warning("DDG urllib failed: %s", e)

    # Method 2: curl fallback (different TLS fingerprint)
    if not tweet_urls:
        try:
            html = _ddg_search_curl(query, time_filter)
            tweet_urls = _extract_tweet_urls(html)
            if tweet_urls:
                logger.info("DDG (curl) '%s': %d tweet URLs", query, len(tweet_urls))
        except Exception as e:
            logger.warning("DDG curl also failed: %s", e)

    # Cache results (only non-empty to preserve good cache)
    if tweet_urls:
        _set_cache(query, time_filter, tweet_urls)
    else:
        logger.warning("No tweet URLs found for '%s'. DDG may be rate-limiting.", query)

    return tweet_urls[:max_results]


def search_tweets(
    query: str,
    limit: int = 10,
    time_filter: str | None = None,
    delay: float = 0.3,
    enrich: bool = True,
    cache_hours: float = 1.0,
) -> list[Tweet]:
    """Search tweets using DuckDuckGo discovery + FxTwitter enrichment.

    Args:
        query: Search query. Supports DDG operators:
            - "from:username" — tweets from specific user
            - "Meteora DLMM" — keyword search
            - Quotes for exact match
        limit: Maximum number of tweets to return.
        time_filter: Time range — 'd' (day), 'w' (week), 'm' (month), None (any).
        delay: Seconds between FxTwitter API calls (be nice).
        enrich: If True, fetch full tweet data via FxTwitter. If False, return
                stub Tweets with only ID and URL (fast, no API calls).
        cache_hours: How long to cache search results.

    Returns:
        List of Tweet objects sorted by discovery order (most relevant first).
    """
    urls = _ddg_search(query, max_results=limit * 2, time_filter=time_filter)

    if not urls:
        return []

    if not enrich:
        tweets = []
        for url in urls[:limit]:
            m = _TWEET_STATUS_RE.search(url)
            if m:
                tweets.append(
                    Tweet(
                        id=m.group(2),
                        text="",
                        author="",
                        author_handle=m.group(1),
                        source_method="ddg_stub",
                    )
                )
        return tweets

    scraper = XScraper()
    tweets: list[Tweet] = []
    errors = 0

    for url in urls:
        if len(tweets) >= limit:
            break
        if errors >= 5:
            logger.warning("Too many errors (%d), stopping enrichment", errors)
            break

        try:
            tweet = scraper.get_tweet(url)
            tweet.source_method = f"ddg+{tweet.source_method}"
            tweets.append(tweet)
        except Exception as e:
            logger.warning("Failed to enrich %s: %s", url, e)
            errors += 1

        if delay > 0 and len(tweets) < limit:
            time.sleep(delay)

    logger.info("Search '%s': %d tweets enriched out of %d URLs", query, len(tweets), len(urls))
    return tweets
