"""
Optional twscrape backend for ScraperX.

twscrape is an async Twitter scraper that requires real Twitter accounts
(not guest tokens). This module wraps it as a sync-compatible backend.

Install: pip install twscrape
Setup:   Add accounts to the pool before use.

Usage:
    from scraperx.twscrape_backend import has_twscrape, TwscrapeBackend
    if has_twscrape():
        backend = TwscrapeBackend()
        if backend.is_configured():
            tweet = backend.get_tweet("1234567890")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from scraperx.scraper import Tweet

logger = logging.getLogger(__name__)

try:
    from twscrape import API, gather  # type: ignore[import-untyped]

    TWSCRAPE_AVAILABLE = True
except ImportError:
    TWSCRAPE_AVAILABLE = False


def has_twscrape() -> bool:
    """Check if twscrape is installed. Never raises."""
    return TWSCRAPE_AVAILABLE


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop — create a new one in a thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _tw_to_tweet(tw: Any) -> Tweet:
    """Convert a twscrape Tweet object to our Tweet dataclass.

    twscrape Tweet attributes:
        id, rawContent, user.displayname, user.username,
        likeCount, retweetCount, replyCount, viewCount,
        media (list of dicts with photos/videos)
    """
    media_urls: list[str] = []
    if hasattr(tw, "media") and tw.media:
        if isinstance(tw.media, dict):
            # photos
            for photo in tw.media.get("photos", []):
                url = photo.get("url", "")
                if url:
                    media_urls.append(url)
            # videos
            for video in tw.media.get("videos", []):
                variants = video.get("variants", [])
                if variants:
                    best = max(
                        (v for v in variants if v.get("contentType") == "video/mp4"),
                        key=lambda v: v.get("bitrate", 0),
                        default=None,
                    )
                    if best and best.get("url"):
                        media_urls.append(best["url"])
        elif isinstance(tw.media, list):
            for item in tw.media:
                if hasattr(item, "photos") and item.photos:
                    for photo in item.photos:
                        url = getattr(photo, "url", "") or ""
                        if url:
                            media_urls.append(url)
                elif hasattr(item, "url"):
                    media_urls.append(item.url)

    raw_dict: dict = {}
    _SENSITIVE = {"auth_token", "cookies", "session", "password", "token", "secret"}
    if hasattr(tw, "dict"):
        try:
            raw_dict = tw.dict()
        except Exception:
            pass
    elif hasattr(tw, "__dict__"):
        try:
            raw_dict = {
                k: str(v) for k, v in tw.__dict__.items() if not k.startswith("_") and k.lower() not in _SENSITIVE
            }
        except Exception:
            pass

    user = getattr(tw, "user", None)
    author = getattr(user, "displayname", "") if user else ""
    handle = getattr(user, "username", "") if user else ""

    return Tweet(
        id=str(getattr(tw, "id", "")),
        text=getattr(tw, "rawContent", "") or getattr(tw, "text", ""),
        author=author,
        author_handle=handle,
        likes=getattr(tw, "likeCount", 0) or 0,
        retweets=getattr(tw, "retweetCount", 0) or 0,
        replies=getattr(tw, "replyCount", 0) or 0,
        views=getattr(tw, "viewCount", 0) or 0,
        media_urls=media_urls,
        source_method="twscrape",
        raw=raw_dict,
    )


class TwscrapeBackend:
    """Sync wrapper around twscrape async API.

    Requires twscrape to be installed and at least one Twitter account
    added to the account pool.
    """

    def __init__(self, db_path: str = "data/twscrape_accounts.db"):
        if not TWSCRAPE_AVAILABLE:
            raise ImportError(
                "twscrape is not installed. Install it with: pip install twscrape\n"
                "Then add accounts: "
                "await api.pool.add_account('user', 'pass', 'email', 'email_pass')"
            )
        self._api = API(db_path)

    def is_configured(self) -> bool:
        """Check if any accounts exist in the pool."""
        try:
            accounts = _run_async(self._api.pool.accounts_info())
            return len(accounts) > 0
        except Exception as e:
            logger.warning("Failed to check twscrape accounts: %s", e)
            return False

    def get_tweet(self, tweet_id: str) -> Tweet:
        """Fetch a single tweet by ID."""
        tw = _run_async(self._api.tweet_details(int(tweet_id)))
        if tw is None:
            raise ValueError(f"Tweet {tweet_id} not found or deleted")
        return _tw_to_tweet(tw)

    def get_profile(self, handle: str) -> dict:
        """Fetch a user profile. Returns raw user dict."""
        user = _run_async(self._api.user_by_login(handle))
        if user is None:
            raise ValueError(f"User @{handle} not found")
        if hasattr(user, "dict"):
            return user.dict()
        return {k: str(v) for k, v in user.__dict__.items() if not k.startswith("_")}

    def search(self, query: str, limit: int = 20) -> list[Tweet]:
        """Search tweets. Returns list of our Tweet dataclass."""
        results = _run_async(gather(self._api.search(query, limit=limit)))
        return [_tw_to_tweet(tw) for tw in results]

    def get_user_tweets(self, handle: str, limit: int = 20) -> list[Tweet]:
        """Get tweets from a user's timeline."""
        user = _run_async(self._api.user_by_login(handle))
        if user is None:
            raise ValueError(f"User @{handle} not found")
        results = _run_async(gather(self._api.user_tweets(user.id, limit=limit)))
        return [_tw_to_tweet(tw) for tw in results]
