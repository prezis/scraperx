"""Optional Xquik search backend for ScraperX."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from scraperx.scraper import Tweet

XQUIK_BASE_URL = "https://xquik.com"
XQUIK_SEARCH_PATH = "/api/v1/x/tweets/search"
_ALLOWED_HOSTS = frozenset({"xquik.com"})
_QUERY_TYPES = frozenset({"Latest", "Top"})


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _parse_timestamp(value: Any) -> int | None:
    if not isinstance(value, str) or value == "":
        return None
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _media_urls(raw: dict[str, Any]) -> list[str]:
    media = raw.get("media")
    if not isinstance(media, list):
        return []

    urls: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        url = item.get("mediaUrl") or item.get("url")
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def _xquik_to_tweet(raw: dict[str, Any]) -> Tweet:
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    assert isinstance(author, dict)
    created_at = raw.get("createdAt")

    return Tweet(
        id=str(raw.get("id", "")),
        text=str(raw.get("text", "")),
        author=str(author.get("name", "")),
        author_handle=str(author.get("username", "")),
        author_avatar=str(author.get("profilePicture", "")),
        author_id=str(author.get("id", "")),
        likes=_int_or_zero(raw.get("likeCount")),
        retweets=_int_or_zero(raw.get("retweetCount")),
        replies=_int_or_zero(raw.get("replyCount")),
        views=_int_or_zero(raw.get("viewCount")),
        media_urls=_media_urls(raw),
        is_reply=bool(raw.get("isReply", False)),
        in_reply_to_tweet_id=raw.get("inReplyToId") if isinstance(raw.get("inReplyToId"), str) else None,
        in_reply_to_handle=raw.get("inReplyToUsername") if isinstance(raw.get("inReplyToUsername"), str) else None,
        in_reply_to_author_id=raw.get("inReplyToUserId") if isinstance(raw.get("inReplyToUserId"), str) else None,
        is_quote=bool(raw.get("isQuoteStatus", False)),
        conversation_id=raw.get("conversationId") if isinstance(raw.get("conversationId"), str) else None,
        created_at=created_at if isinstance(created_at, str) else None,
        created_timestamp=_parse_timestamp(created_at),
        lang=raw.get("lang") if isinstance(raw.get("lang"), str) else None,
        source_client=raw.get("source") if isinstance(raw.get("source"), str) else None,
        is_note_tweet=raw.get("isNoteTweet") if isinstance(raw.get("isNoteTweet"), bool) else None,
        author_verified=author.get("verified") if isinstance(author.get("verified"), bool) else None,
        author_verified_type=author.get("verifiedType") if isinstance(author.get("verifiedType"), str) else None,
        author_followers=_int_or_zero(author.get("followers")),
        author_following=_int_or_zero(author.get("following")),
        author_joined=author.get("createdAt") if isinstance(author.get("createdAt"), str) else None,
        source_method="xquik",
        raw=raw,
    )


class XquikBackend:
    """Sync wrapper around the Xquik tweet search API."""

    def __init__(self, api_key: str | None = None, *, base_url: str = XQUIK_BASE_URL, timeout: int = 15):
        key = api_key or os.environ.get("XQUIK_API_KEY")
        if key is None or key == "":
            raise ValueError("Xquik API key required. Pass api_key or set XQUIK_API_KEY.")

        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError("Xquik base URL must use https://xquik.com.")

        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, limit: int = 20, query_type: str = "Latest") -> list[Tweet]:
        """Search X and return ScraperX Tweet objects."""
        if query_type not in _QUERY_TYPES:
            raise ValueError("query_type must be Latest or Top.")
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        params = urlencode(
            {
                "q": query,
                "queryType": query_type,
                "limit": min(limit, 200),
            }
        )
        request = Request(
            f"{self.base_url}{XQUIK_SEARCH_PATH}?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": "ScraperX Xquik backend",
                "x-api-key": self.api_key,
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(f"Xquik search failed with HTTP {error.code}.") from error

        tweets = payload.get("tweets") if isinstance(payload, dict) else None
        if not isinstance(tweets, list):
            raise RuntimeError("Xquik search returned an invalid response.")

        return [_xquik_to_tweet(tweet) for tweet in tweets if isinstance(tweet, dict)]
