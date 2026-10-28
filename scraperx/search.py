"""
Search X/Twitter tweets: a search engine finds tweet URLs, FxTwitter resolves them to full JSON data.

Backends, tried in this order by default (``backend="auto"``); no X account is involved in any of them:

1. Desearch X Search API (Bittensor subnet 22, keyed: ``SCRAPERX_DESEARCH_API_KEY`` or ``DESEARCH_API_KEY``).
   Returns full posts, X advanced-search syntax (from:, #tag, "phrase"), billed per post.
2. Brave Search API (official, keyed: ``SCRAPERX_BRAVE_API_KEY`` or ``BRAVE_SEARCH_API_KEY``). Returns tweet URLs,
   FxTwitter fills them in.
3. Nitter search RSS (no key; public instances, rotated - scraperx.nitter). FxTwitter adds likes / views.
4. DuckDuckGo HTML (no key, Trawlx pattern). DDG now answers automated queries with a bot-check
   page after a few requests (measured 2026-10-28); that page is logged by name, not read as
   "no results".

Usage:
    from scraperx.search import search_tweets
    results = search_tweets("Meteora DLMM strategy", limit=10)
    for tweet in results:
        print(tweet.text, tweet.likes)

    # Search specific user's tweets
    results = search_tweets("from:qwerty_ytrevvq polymarket")

    # Force one backend
    results = search_tweets("pump.fun", backend="brave")

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
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

from scraperx.scraper import Tweet, XScraper

logger = logging.getLogger(__name__)

# Match x.com/user/status/ID in search result URLs
_TWEET_STATUS_RE = re.compile(r"https?://(?:twitter|x)\.com/([A-Za-z0-9_]+)/status/(\d+)")

# Cache directory
_CACHE_DIR = Path(os.environ.get("SCRAPERX_CACHE", "/tmp/scraperx_cache"))

# Brave Search API (https://api-dashboard.search.brave.com/app/documentation/web-search/get-started):
# GET with header X-Subscription-Token; count max 20, offset 0..9; results at web.results[].url.
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_KEY_ENV = ("SCRAPERX_BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY")
_BRAVE_FRESHNESS = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}
_BRAVE_MAX_PAGES = 2  # each page is one paid API call; 2 x 20 results covers the default limit*2 = 20 URLs
_FROM_RE = re.compile(r"\bfrom:@?([A-Za-z0-9_]{1,15})\b", re.IGNORECASE)

# DuckDuckGo's answer to an automated query: a bot-check page, not an empty result list.
_DDG_BLOCK_RE = re.compile(r"anomaly|bots use DuckDuckGo|challenge-form", re.IGNORECASE)

# Desearch, Bittensor subnet 22 (https://www.desearch.ai/docs/api-reference/get-twitter): GET /twitter with header
# "Authorization: <key>" (no "Bearer"); `query` is X advanced-search syntax (from:, #tag, "phrase" pass through as-is);
# the answer is full posts (text, user, counts, media), so no FxTwitter enrichment is needed. Billed per post
# ($0.15 / 1,000 posts, 10-post minimum per request, pricing page read 2026-10-28).
DESEARCH_TWITTER_URL = "https://api.desearch.ai/twitter"
_DESEARCH_KEY_ENV = ("SCRAPERX_DESEARCH_API_KEY", "DESEARCH_API_KEY")
_DESEARCH_DAYS = {"d": 1, "w": 7, "m": 31, "y": 365}
_DESEARCH_MIN_COUNT = 10

BACKENDS = ("auto", "desearch", "brave", "nitter", "ddg")
# time_filter → an X search operator, for backends that pass the query to X's own search (Nitter).
_SINCE_DAYS = {"d": 1, "w": 7, "m": 31, "y": 365}


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

    blocked = 0
    # Method 1: urllib
    try:
        html = _ddg_search_urllib(query, time_filter)
        blocked += _ddg_blocked(html)
        tweet_urls = _extract_tweet_urls(html)
        if tweet_urls:
            logger.info("DDG (urllib) '%s': %d tweet URLs", query, len(tweet_urls))
    except Exception as e:
        logger.warning("DDG urllib failed: %s", e)

    # Method 2: curl fallback (different TLS fingerprint)
    if not tweet_urls:
        try:
            html = _ddg_search_curl(query, time_filter)
            blocked += _ddg_blocked(html)
            tweet_urls = _extract_tweet_urls(html)
            if tweet_urls:
                logger.info("DDG (curl) '%s': %d tweet URLs", query, len(tweet_urls))
        except Exception as e:
            logger.warning("DDG curl also failed: %s", e)

    # Cache results (only non-empty to preserve good cache)
    if tweet_urls:
        _set_cache(query, time_filter, tweet_urls)
    elif blocked:
        logger.warning("DDG served its bot-check page for '%s' (%d of the tries): this is a block, not 'no tweets'. "
                       "Set SCRAPERX_BRAVE_API_KEY to search through the Brave Search API instead.", query, blocked)
    else:
        logger.warning("No tweet URLs found for '%s'. DDG may be rate-limiting.", query)

    return tweet_urls[:max_results]


# The keyed APIs sit behind Cloudflare, which refuses Python's default "Python-urllib/3.x" User-Agent with error 1010
# before the request reaches the API (issue #22, 2026-10-28: Desearch answered 403 "Error 1010: Access denied" to the
# default UA and 403 {"detail":"Forbidden"} - a real key check - to a named one). Every API call names itself.
API_USER_AGENT = "scraperx (+https://github.com/prezis/scraperx)"
_CF_ERROR_RE = re.compile(r"cloudflare-1xxx-errors/error-(1\d{3})|error code:?\s*(1\d{3})|Error (1\d{3})")


def _refusal_reason(e: HTTPError) -> str:
    """Why an API said no, from its status AND its body: a Cloudflare block is not a key problem."""
    try:
        body = (e.read() or b"")[:4000].decode("utf-8", errors="replace")
    except Exception:
        body = ""
    cf = _CF_ERROR_RE.search(body)
    if cf:
        code = next(g for g in cf.groups() if g)
        return f"Cloudflare refused the request before it reached the API (error {code}), not a key problem"
    if e.code in (401, 403):
        detail = ""
        try:
            parsed = json.loads(body)
            detail = str(parsed.get("detail") or parsed.get("message") or parsed.get("error") or "") if isinstance(
                parsed, dict) else ""
        except ValueError:
            pass
        return "key refused" + (f": {detail[:120]}" if detail else "")
    return {402: "no credit", 429: "rate limit"}.get(e.code, "error")


def _desearch_key() -> str | None:
    """The Desearch API key from SCRAPERX_DESEARCH_API_KEY or DESEARCH_API_KEY, or None."""
    for name in _DESEARCH_KEY_ENV:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return None


def _desearch_get(params: dict, key: str, timeout: int = 20) -> object:
    """One Desearch /twitter call. Raises HTTPError on 401/402/429 etc."""
    req = Request(
        f"{DESEARCH_TWITTER_URL}?{urlencode(params)}",
        headers={"Accept": "application/json", "Authorization": key, "User-Agent": API_USER_AGENT},
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _desearch_posts(data: object) -> list[dict]:
    """The post list of a Desearch answer: a bare list, or a list under data / tweets / results."""
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict):
        for k in ("data", "tweets", "results"):
            if isinstance(data.get(k), list):
                return [p for p in data[k] if isinstance(p, dict)]
    return []


def _int0(v: object) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _tweet_from_desearch(p: dict) -> Tweet | None:
    """A Desearch post as a Tweet (fields per the Desearch X Search reference); None without an id."""
    pid = str(p.get("id") or "").strip()
    if not pid:
        return None
    user = p.get("user") if isinstance(p.get("user"), dict) else {}
    media = []
    for m in p.get("media") or []:
        if isinstance(m, str):
            media.append(m)
        elif isinstance(m, dict):
            u = m.get("media_url_https") or m.get("media_url") or m.get("url")
            if u:
                media.append(str(u))
    return Tweet(
        id=pid,
        text=str(p.get("text") or ""),
        author=str(user.get("name") or ""),
        author_handle=str(user.get("username") or ""),
        likes=_int0(p.get("like_count")),
        retweets=_int0(p.get("retweet_count")),
        replies=_int0(p.get("reply_count")),
        views=_int0(p.get("view_count")),
        author_id=str(user.get("id") or ""),
        media_urls=media,
        is_quote=bool(p.get("is_quote_tweet")),
        conversation_id=str(p["conversation_id"]) if p.get("conversation_id") else None,
        created_at=p.get("created_at"),
        lang=p.get("lang"),
        author_verified=user.get("verified"),
        author_followers=user.get("followers_count"),
        source_method="desearch",
    )


def _desearch_search(query: str, limit: int = 10, time_filter: str | None = None, key: str | None = None) -> list[Tweet]:
    """Full posts for `query` from Desearch (one call). [] when no key is set or the API refuses (the reason is logged,
    search_tweets then falls back to Brave / DDG). "from:user" queries ask for the newest posts first."""
    key = key or _desearch_key()
    if not key:
        return []
    params = {"query": query, "count": max(_DESEARCH_MIN_COUNT, int(limit)), "sort": "Latest" if _FROM_RE.search(query) else "Top"}
    if time_filter in _DESEARCH_DAYS:
        start = time.gmtime(time.time() - _DESEARCH_DAYS[time_filter] * 86400)
        params["start_date"] = time.strftime("%Y-%m-%d", start)
    try:
        data = _desearch_get(params, key)
    except HTTPError as e:
        logger.warning("Desearch refused '%s': HTTP %s (%s)", query, e.code, _refusal_reason(e))
        return []
    except Exception as e:
        logger.warning("Desearch failed for '%s': %s", query, e)
        return []
    tweets = [t for t in (_tweet_from_desearch(p) for p in _desearch_posts(data)) if t is not None]
    logger.info("Desearch '%s': %d posts", query, len(tweets))
    return tweets


def _nitter_search(query: str, limit: int, time_filter: str | None) -> list[Tweet]:
    """Keyless X search through Nitter RSS (scraperx.nitter); time_filter becomes X's own since: operator."""
    from scraperx.nitter import search as nitter_search

    q = query
    if time_filter in _SINCE_DAYS and "since:" not in query:
        q = f"{query} since:{time.strftime('%Y-%m-%d', time.gmtime(time.time() - _SINCE_DAYS[time_filter] * 86400))}"
    return nitter_search(q, limit=limit)


def _enrich_nitter(found: list[Tweet], limit: int, enrich: bool, delay: float) -> list[Tweet]:
    """Nitter RSS posts carry no counts; with enrich=True each is re-read through FxTwitter (likes, views, author
    details). A post FxTwitter cannot read keeps its RSS fields (source_method "nitter")."""
    if not enrich:
        return found[:limit]
    scraper = XScraper()
    out: list[Tweet] = []
    for t in found[:limit]:
        try:
            full = scraper.get_tweet(f"https://x.com/{t.author_handle}/status/{t.id}")
            full.source_method = f"nitter+{full.source_method}"
            full.reposted_by = full.reposted_by or t.reposted_by
            out.append(full)
        except Exception as e:
            logger.warning("FxTwitter could not enrich nitter hit %s: %s", t.id, e)
            out.append(t)
        if delay > 0 and len(out) < min(limit, len(found)):
            time.sleep(delay)
    return out


def _ddg_blocked(html: str) -> bool:
    """True when DDG answered with its bot-check page instead of results."""
    return bool(_DDG_BLOCK_RE.search(html or ""))


def _brave_key() -> str | None:
    """The Brave Search API key from SCRAPERX_BRAVE_API_KEY or BRAVE_SEARCH_API_KEY, or None."""
    for name in _BRAVE_KEY_ENV:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return None


def _brave_query(query: str) -> str:
    """Brave has site: but no from: operator. "from:user ..." becomes "site:x.com/user ..." (a tweet URL is
    x.com/<user>/status/<id>); any other query is restricted with "site:x.com". UNVERIFIED until the first live
    key test: whether Brave honours a path inside site: (its docs list site: but say nothing about paths)."""
    m = _FROM_RE.search(query)
    rest = _FROM_RE.sub(" ", query)
    rest = re.sub(r"\bsite:\S+", " ", rest, flags=re.IGNORECASE)
    rest = " ".join(rest.split())
    site = f"site:x.com/{m.group(1)}" if m else "site:x.com"
    return f"{site} {rest}".strip()


def _brave_get(params: dict, key: str, timeout: int = 15) -> dict:
    """One Brave Search API call. Raises HTTPError on 401/403/429 etc."""
    req = Request(
        f"{BRAVE_API_URL}?{urlencode(params)}",
        headers={"Accept": "application/json", "X-Subscription-Token": key, "User-Agent": API_USER_AGENT},
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _brave_search(query: str, max_results: int = 20, time_filter: str | None = None, key: str | None = None) -> list[str]:
    """Tweet status URLs for `query` from the Brave Search API, at most _BRAVE_MAX_PAGES calls. [] when no key is set
    or the API refuses (the reason is logged, the caller falls back to DDG)."""
    key = key or _brave_key()
    if not key:
        return []
    q = _brave_query(query)
    cache_q = f"brave|{q}"
    cached = _get_cached(cache_q, time_filter)
    if cached:
        logger.info("Cache hit for '%s': %d URLs", cache_q, len(cached))
        return cached[:max_results]
    urls: list[str] = []
    for page in range(_BRAVE_MAX_PAGES):
        params = {"q": q, "count": 20, "offset": page}
        if time_filter in _BRAVE_FRESHNESS:
            params["freshness"] = _BRAVE_FRESHNESS[time_filter]
        try:
            data = _brave_get(params, key)
        except HTTPError as e:
            logger.warning("Brave Search API refused '%s': HTTP %s (%s)", q, e.code, _refusal_reason(e))
            break
        except Exception as e:
            logger.warning("Brave Search API failed for '%s': %s", q, e)
            break
        results = ((data.get("web") or {}).get("results")) or []
        page_urls = _extract_tweet_urls(" ".join(str(r.get("url") or "") for r in results if isinstance(r, dict)))
        urls.extend(u for u in page_urls if u not in urls)
        more = bool((data.get("query") or {}).get("more_results_available"))
        if len(urls) >= max_results or not more or not results:
            break
    logger.info("Brave '%s': %d tweet URLs", q, len(urls))
    if urls:
        _set_cache(cache_q, time_filter, urls)
    return urls[:max_results]


def search_tweets(
    query: str,
    limit: int = 10,
    time_filter: str | None = None,
    delay: float = 0.3,
    enrich: bool = True,
    cache_hours: float = 1.0,
    backend: str = "auto",
) -> list[Tweet]:
    """Search tweets: discovery (Brave Search API when a key is set, else / then DuckDuckGo) + FxTwitter enrichment.

    Args:
        query: Search query. Supports:
            - "from:username" — tweets from specific user
            - "Meteora DLMM" — keyword search
            - Quotes for exact match
        limit: Maximum number of tweets to return.
        time_filter: Time range — 'd' (day), 'w' (week), 'm' (month), 'y' (year), None (any).
        delay: Seconds between FxTwitter API calls (be nice).
        enrich: If True, fetch full tweet data via FxTwitter. If False, return
                stub Tweets with only ID and URL (fast, no API calls).
        cache_hours: How long to cache search results.
        backend: "auto" (Desearch if its key is set, then Brave if its key is set, then DuckDuckGo, each only when
                 the one before found nothing), "desearch" / "brave" (that one only; raises when its key is not set),
                 or "ddg" (DuckDuckGo only).

    Returns:
        List of Tweet objects sorted by discovery order (most relevant first).
        source_method says where each URL came from: "brave+…" / "ddg+…" (or "brave_stub" / "ddg_stub").
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}, not {backend!r}")
    if backend in ("auto", "desearch"):
        if _desearch_key():
            found = _desearch_search(query, limit=limit, time_filter=time_filter)
            if found or backend == "desearch":
                return found[:limit]  # full posts already: no FxTwitter enrichment
        elif backend == "desearch":
            raise RuntimeError(f"backend='desearch' needs a Desearch API key in {' or '.join(_DESEARCH_KEY_ENV)}")
    urls: list[str] = []
    source = "ddg"
    if backend in ("auto", "brave"):
        if _brave_key():
            urls = _brave_search(query, max_results=limit * 2, time_filter=time_filter)
            source = "brave"
        elif backend == "brave":
            raise RuntimeError(f"backend='brave' needs a Brave Search API key in {' or '.join(_BRAVE_KEY_ENV)}")
    if not urls and backend in ("auto", "nitter"):
        try:
            found = _nitter_search(query, limit, time_filter)
        except Exception as e:  # NitterError: no instance gave a readable feed
            if backend == "nitter":
                raise
            logger.warning("Nitter search failed, trying DuckDuckGo: %s", e)
            found = []
        if found or backend == "nitter":
            return _enrich_nitter(found, limit, enrich, delay)
    if not urls and backend in ("auto", "ddg"):
        urls = _ddg_search(query, max_results=limit * 2, time_filter=time_filter)
        source = "ddg"

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
                        source_method=f"{source}_stub",
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
            tweet.source_method = f"{source}+{tweet.source_method}"
            tweets.append(tweet)
        except Exception as e:
            logger.warning("Failed to enrich %s: %s", url, e)
            errors += 1

        if delay > 0 and len(tweets) < limit:
            time.sleep(delay)

    logger.info("Search '%s': %d tweets enriched out of %d URLs", query, len(tweets), len(urls))
    return tweets
