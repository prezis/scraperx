"""
Thread scraping for X/Twitter via FxTwitter API.

Best-effort approach: walks up (via in_reply_to fields) and down (via replies)
to reconstruct self-reply threads by the same author.

Usage:
    from scraperx import get_thread
    thread = get_thread("https://x.com/user/status/123456")
    for tweet in thread.all_tweets:
        print(tweet.text)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scraperx.scraper import Tweet, _http_get_json, parse_tweet_url

logger = logging.getLogger(__name__)

FXTWITTER_API = "https://api.fxtwitter.com"


@dataclass
class Thread:
    """A thread of tweets by the same author."""
    root_tweet: Tweet
    replies: list[Tweet] = field(default_factory=list)
    total_tweets: int = 0

    def __post_init__(self):
        if self.total_tweets == 0:
            self.total_tweets = 1 + len(self.replies)

    @property
    def all_tweets(self) -> list[Tweet]:
        """All tweets in chronological order (root first)."""
        return [self.root_tweet] + self.replies


def _fetch_tweet_fxtwitter(user: str, tweet_id: str, timeout: int = 15) -> tuple[Tweet, dict]:
    """Fetch a single tweet via FxTwitter, return (Tweet, raw_tweet_dict)."""
    url = f"{FXTWITTER_API}/{user}/status/{tweet_id}"
    data = _http_get_json(url, timeout=timeout)

    if data.get("code") != 200:
        raise ValueError(
            f"FxTwitter returned code {data.get('code')}: {data.get('message')}"
        )

    t = data["tweet"]

    media_urls = []
    if t.get("media") and t["media"].get("all"):
        for m in t["media"]["all"]:
            media_urls.append(m.get("url", ""))

    tweet = Tweet(
        id=str(t.get("id", tweet_id)),
        text=t.get("text", ""),
        author=t.get("author", {}).get("name", user),
        author_handle=t.get("author", {}).get("screen_name", user),
        likes=t.get("likes", 0),
        retweets=t.get("retweets", 0),
        replies=t.get("replies", 0),
        views=t.get("views", 0),
        media_urls=media_urls,
        source_method="fxtwitter",
        raw=data,
    )
    return tweet, t


def _get_parent_id(raw_tweet: dict) -> str | None:
    """Extract parent tweet ID from FxTwitter raw tweet data."""
    # FxTwitter may use different field names
    for key in ("replying_to", "in_reply_to_status_id", "in_reply_to_status_id_str"):
        val = raw_tweet.get(key)
        if val:
            return str(val)
    # Nested under replying_to_status
    rts = raw_tweet.get("replying_to_status")
    if isinstance(rts, dict) and rts.get("id"):
        return str(rts["id"])
    return None


def _get_author_handle(raw_tweet: dict) -> str:
    """Extract author screen_name from raw tweet data."""
    author = raw_tweet.get("author", {})
    return author.get("screen_name", "")


def get_thread(url: str, timeout: int = 15, max_depth: int = 20) -> Thread:
    """
    Fetch a tweet and try to reconstruct its thread.

    Walks UP to the root tweet (following in_reply_to by same author),
    then returns the thread in chronological order.

    If thread detection fails or the tweet is standalone, returns a Thread
    with just the single tweet as root.

    Args:
        url: Any X/Twitter URL.
        timeout: HTTP timeout per request.
        max_depth: Maximum number of parent tweets to walk up.

    Returns:
        Thread with all tweets in chronological order.
    """
    user, tweet_id = parse_tweet_url(url)

    try:
        initial_tweet, raw = _fetch_tweet_fxtwitter(user, tweet_id, timeout)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch tweet {tweet_id}: {e}") from e

    # Collect tweets: walk up to find root
    chain: list[tuple[Tweet, dict]] = [(initial_tweet, raw)]
    current_raw = raw
    seen_ids: set[str] = {tweet_id}
    author_handle = initial_tweet.author_handle.lower()

    depth = 0
    while depth < max_depth:
        parent_id = _get_parent_id(current_raw)
        if not parent_id or parent_id in seen_ids:
            break

        try:
            parent_tweet, parent_raw = _fetch_tweet_fxtwitter(user, parent_id, timeout)
        except Exception:
            logger.debug("Could not fetch parent tweet %s, stopping walk-up", parent_id)
            break

        # Only follow if same author (self-reply thread)
        if parent_tweet.author_handle.lower() != author_handle:
            logger.debug(
                "Parent tweet %s by different author (%s), stopping",
                parent_id, parent_tweet.author_handle,
            )
            break

        seen_ids.add(parent_id)
        chain.append((parent_tweet, parent_raw))
        current_raw = parent_raw
        depth += 1

    # Reverse so root is first (we walked up from leaf)
    chain.reverse()

    root_tweet = chain[0][0]
    replies = [t for t, _ in chain[1:]]

    return Thread(
        root_tweet=root_tweet,
        replies=replies,
        total_tweets=len(chain),
    )
