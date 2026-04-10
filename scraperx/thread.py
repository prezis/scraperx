"""
Thread scraping for X/Twitter via FxTwitter API + syndication timeline.

Best-effort approach: walks up (via in_reply_to fields) and down (via
Twitter syndication timeline conversation grouping + DDG fallback)
to reconstruct self-reply threads by the same author.

Walk-down strategy (in priority order):
  1. **Syndication timeline** — fetch ``syndication.twitter.com`` profile
     timeline for the author, extract ``conversation_id_str`` grouping.
     All tweets sharing the root tweet's conversation_id are part of the
     thread. Fast, no per-tweet API calls needed.
  2. **DDG search fallback** — search DuckDuckGo for the author's tweets,
     fetch each via FxTwitter, keep those whose ``replying_to_status``
     points at a tweet already in the thread.

Usage:
    from scraperx import get_thread
    thread = get_thread("https://x.com/user/status/123456")
    for tweet in thread.all_tweets:
        print(tweet.text)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import Request, urlopen

from scraperx.scraper import Tweet, _http_get_json, parse_tweet_url

logger = logging.getLogger(__name__)

FXTWITTER_API = "https://api.fxtwitter.com"
_SYNDICATION_TIMELINE = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}"


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


# ---------------------------------------------------------------------------
# FxTwitter helpers
# ---------------------------------------------------------------------------

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
        author_avatar=t.get("author", {}).get("avatar_url", ""),
        author_id=str(t.get("author", {}).get("id", "")),
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
    for key in ("replying_to", "in_reply_to_status_id", "in_reply_to_status_id_str"):
        val = raw_tweet.get(key)
        if val:
            return str(val)
    rts = raw_tweet.get("replying_to_status")
    if isinstance(rts, dict) and rts.get("id"):
        return str(rts["id"])
    return None


def _get_author_handle(raw_tweet: dict) -> str:
    """Extract author screen_name from raw tweet data."""
    author = raw_tweet.get("author", {})
    return author.get("screen_name", "")


# ---------------------------------------------------------------------------
# Syndication timeline helpers
# ---------------------------------------------------------------------------

@dataclass
class _SyndicationTweet:
    """Minimal tweet info extracted from syndication __NEXT_DATA__."""
    id: str
    conversation_id: Optional[str]
    in_reply_to_id: Optional[str]
    in_reply_to_user: Optional[str]
    screen_name: str
    text: str


def _fetch_syndication_timeline(user: str, timeout: int = 15) -> list[_SyndicationTweet]:
    """Fetch the author's profile timeline from Twitter syndication.

    Returns a list of lightweight tweet objects with conversation_id.
    The syndication endpoint returns an HTML page with ``__NEXT_DATA__``
    containing JSON tweet objects that include ``conversation_id_str``.
    """
    url = _SYNDICATION_TIMELINE.format(user=user)
    req = Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Syndication timeline fetch failed for @%s: %s", user, exc)
        return []

    # Extract __NEXT_DATA__ JSON blob
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        logger.debug("No __NEXT_DATA__ in syndication response for @%s", user)
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        logger.debug("Failed to parse syndication JSON for @%s", user)
        return []

    # Walk the JSON tree to find tweet-like objects with id_str
    tweets: list[_SyndicationTweet] = []
    seen: set[str] = set()

    def _walk(obj: object, depth: int = 0) -> None:
        if depth > 15:
            return
        if isinstance(obj, dict):
            id_str = obj.get("id_str")
            if id_str and isinstance(id_str, str) and len(id_str) > 15:
                if id_str not in seen:
                    seen.add(id_str)
                    user_obj = obj.get("user", {})
                    tweets.append(_SyndicationTweet(
                        id=id_str,
                        conversation_id=obj.get("conversation_id_str"),
                        in_reply_to_id=obj.get("in_reply_to_status_id_str"),
                        in_reply_to_user=obj.get("in_reply_to_screen_name"),
                        screen_name=user_obj.get("screen_name", ""),
                        text=(obj.get("full_text") or obj.get("text") or "")[:500],
                    ))
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(data)
    return tweets


def _walk_down_syndication(
    root_id: str,
    author_handle: str,
    seen_ids: set[str],
    timeout: int,
    max_new: int,
) -> list[str]:
    """Find thread continuation tweet IDs via syndication timeline.

    Fetches the author's syndication timeline, groups by conversation_id,
    and returns IDs that belong to the same conversation as *root_id*
    AND are by the same author (self-replies only).

    Returns tweet IDs (sorted ascending) that are NOT in *seen_ids*.
    """
    timeline = _fetch_syndication_timeline(author_handle, timeout=timeout)
    if not timeline:
        return []

    author_lower = author_handle.lower()

    # Find tweets in the same conversation as root
    # For self-threads, conversation_id == root tweet ID
    new_ids: list[str] = []
    for tw in timeline:
        if tw.id in seen_ids:
            continue
        if tw.screen_name.lower() != author_lower:
            continue
        # Match by conversation_id
        if tw.conversation_id == root_id:
            new_ids.append(tw.id)
            if len(new_ids) >= max_new:
                break
        # Also match if this tweet replies to a known thread member
        elif tw.in_reply_to_id and tw.in_reply_to_id in seen_ids:
            if tw.in_reply_to_user and tw.in_reply_to_user.lower() == author_lower:
                new_ids.append(tw.id)
                if len(new_ids) >= max_new:
                    break

    new_ids.sort(key=int)
    if new_ids:
        logger.debug(
            "Syndication walk-down found %d new tweets for conversation %s",
            len(new_ids), root_id,
        )
    return new_ids


# ---------------------------------------------------------------------------
# DDG search fallback for walk-down
# ---------------------------------------------------------------------------

def _walk_down_ddg(
    author_handle: str,
    root_text: str,
    seen_ids: set[str],
    timeout: int,
    max_new: int,
    delay: float = 0.35,
) -> list[tuple[Tweet, dict]]:
    """Find thread continuations via DuckDuckGo search + FxTwitter verification.

    Searches DDG for the author's tweets, fetches each via FxTwitter, and
    keeps those whose ``replying_to_status`` points at a tweet already in
    the thread AND are by the same author.

    Returns ``(Tweet, raw_dict)`` pairs sorted by ID ascending.
    """
    from scraperx.search import _ddg_search  # noqa: WPS433 (lazy to avoid circular)

    # Build search query with text hint from root
    keywords = root_text[:60].split() if root_text else []
    keywords = [w for w in keywords if w.isalnum() and len(w) > 2][:4]
    extra = " ".join(keywords)
    query = f"site:x.com from:{author_handle} {extra}".strip()

    try:
        candidate_urls = _ddg_search(query, max_results=40)
    except Exception as exc:
        logger.debug("DDG search for walk-down failed: %s", exc)
        return []

    if not candidate_urls:
        logger.debug("walk-down DDG: no candidate URLs for @%s", author_handle)
        return []

    author_lower = author_handle.lower()
    new_tweets: list[tuple[Tweet, dict]] = []
    fetch_errors = 0
    # Copy so we can extend as we discover
    known_ids = set(seen_ids)

    for curl in candidate_urls:
        if len(new_tweets) >= max_new or fetch_errors >= 5:
            break
        try:
            _user, _tid = parse_tweet_url(curl)
        except ValueError:
            continue
        if _tid in known_ids:
            continue

        try:
            tweet, raw = _fetch_tweet_fxtwitter(author_handle, _tid, timeout)
        except Exception:
            fetch_errors += 1
            continue

        if tweet.author_handle.lower() != author_lower:
            continue

        parent_id = _get_parent_id(raw)
        if parent_id and parent_id in known_ids:
            known_ids.add(_tid)
            new_tweets.append((tweet, raw))
            logger.debug("walk-down DDG: found continuation %s -> %s", parent_id, _tid)

        if delay > 0:
            time.sleep(delay)

    new_tweets.sort(key=lambda tr: int(tr[0].id))
    return new_tweets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_thread(
    url: str,
    timeout: int = 15,
    max_depth: int = 20,
    walk_down: bool = True,
) -> Thread:
    """
    Fetch a tweet and try to reconstruct its thread.

    Walks UP to the root tweet (following in_reply_to by same author),
    then optionally walks DOWN from the root to find self-reply continuations
    via Twitter syndication timeline (conversation_id grouping) with a
    DuckDuckGo fallback.

    If thread detection fails or the tweet is standalone, returns a Thread
    with just the single tweet as root.

    Args:
        url: Any X/Twitter URL.
        timeout: HTTP timeout per request.
        max_depth: Maximum number of tweets to walk in each direction.
        walk_down: If True, search for self-reply continuations below the
            last known tweet.  Syndication is tried first (fast, no
            per-tweet API calls); DDG search is the fallback.

    Returns:
        Thread with all tweets in chronological order.
    """
    user, tweet_id = parse_tweet_url(url)

    try:
        initial_tweet, raw = _fetch_tweet_fxtwitter(user, tweet_id, timeout)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch tweet {tweet_id}: {e}") from e

    # --- walk UP to find root -------------------------------------------------
    chain: list[tuple[Tweet, dict]] = [(initial_tweet, raw)]
    current_raw = raw
    seen_ids: set[str] = {tweet_id}
    author_handle = initial_tweet.author_handle  # original casing
    author_lower = author_handle.lower()

    up_depth = 0
    while up_depth < max_depth:
        parent_id = _get_parent_id(current_raw)
        if not parent_id or parent_id in seen_ids:
            break

        try:
            parent_tweet, parent_raw = _fetch_tweet_fxtwitter(user, parent_id, timeout)
        except Exception:
            logger.debug("Could not fetch parent tweet %s, stopping walk-up", parent_id)
            break

        if parent_tweet.author_handle.lower() != author_lower:
            logger.debug(
                "Parent tweet %s by different author (%s), stopping",
                parent_id, parent_tweet.author_handle,
            )
            break

        seen_ids.add(parent_id)
        chain.append((parent_tweet, parent_raw))
        current_raw = parent_raw
        up_depth += 1

    # Reverse so root is first (we walked up from leaf)
    chain.reverse()

    root_id = chain[0][0].id

    # --- walk DOWN to find continuations --------------------------------------
    if walk_down:
        remaining = max(0, max_depth - len(chain) + 1)
        if remaining <= 0:
            logger.debug("walk-down: depth budget exhausted after walk-up")
        else:
            # Method 1: syndication timeline (conversation_id grouping)
            new_ids = _walk_down_syndication(
                root_id=root_id,
                author_handle=author_handle,
                seen_ids=seen_ids,
                timeout=timeout,
                max_new=remaining,
            )

            if new_ids:
                # Fetch each new tweet via FxTwitter for full data
                for tid in new_ids:
                    if len(chain) - 1 >= max_depth:
                        break
                    try:
                        tw, tw_raw = _fetch_tweet_fxtwitter(author_handle, tid, timeout)
                    except Exception:
                        logger.debug("Could not fetch syndication-discovered tweet %s", tid)
                        continue
                    if tw.author_handle.lower() != author_lower:
                        continue
                    seen_ids.add(tid)
                    chain.append((tw, tw_raw))

            # Method 2: DDG fallback (only if syndication found nothing)
            if not new_ids:
                remaining = max(0, max_depth - len(chain) + 1)
                if remaining > 0:
                    ddg_tweets = _walk_down_ddg(
                        author_handle=author_handle,
                        root_text=chain[0][0].text,
                        seen_ids=seen_ids,
                        timeout=timeout,
                        max_new=remaining,
                    )
                    for tw, tw_raw in ddg_tweets:
                        seen_ids.add(tw.id)
                        chain.append((tw, tw_raw))

            # Re-sort entire chain by tweet ID to guarantee chronological order
            chain.sort(key=lambda tr: int(tr[0].id))

    root_tweet = chain[0][0]
    replies = [t for t, _ in chain[1:]]

    return Thread(
        root_tweet=root_tweet,
        replies=replies,
        total_tweets=len(chain),
    )
