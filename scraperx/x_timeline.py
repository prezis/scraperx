"""x_timeline — X/Twitter user timelines, reply threads and trends WITHOUT an X account, through the FxTwitter v2 API.

FxTwitter (github.com/FxEmbed/FxEmbed) is a public read-only relay: no key, no login, nothing of ours that X could ban.
Its v2 API (``https://api.fxtwitter.com/2``, OpenAPI at ``/2/openapi.json``; 1,000 requests per minute per IP per its
docs) adds what v1 never had. Measured from this host 2026-10-28:

  /2/profile/{handle}/statuses   200, 19 posts per page (count=20), cursor.bottom pages back; 9 of 19 were reposts
  /2/conversation/{id}           200, 35 replies on the first page, cursor.bottom for more
  /2/trends                      200
  /2/search, /2/search/users     404 for every query (FxEmbed issue #2598, since 2026-10-09): keyword search lives in
                                 scraperx.search (Desearch / Brave keys, Nitter RSS without a key)

    from scraperx.x_timeline import get_user_tweets, get_replies

    for t in get_user_tweets("solana", limit=40, include_reposts=False):
        print(t.created_at, t.likes, t.views, t.text[:80])
    replies = get_replies("https://x.com/solana/status/2115432312343318628", limit=100)
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scraperx.scraper import Tweet, _best_media_url, _parse_quoted_tweet

logger = logging.getLogger(__name__)

__all__ = ["FxV2Error", "get_replies", "get_trends", "get_user_tweets", "tweet_from_fx_v2"]

FX_V2 = "https://api.fxtwitter.com/2"
USER_AGENT = "scraperx (+https://github.com/prezis/scraperx)"
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
_STATUS_ID_RE = re.compile(r"(?:status(?:es)?/|^)(\d{5,25})(?:\D|$)")
_MAX_PAGES = 50


class FxV2Error(RuntimeError):
    """FxTwitter v2 answered with an error, or the input is not a handle / post."""


# FxTwitter v2 fails intermittently: the same timeline request answered 200 in 4 of 10 tries spaced 3 s apart
# (2026-10-28), the failures as HTTP 404 with {"code": 404} and no results, interleaved with successes, so not a rate
# limit on our side. A failed request is retried; five tries at that rate all fail about 8 % of the time.
_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
_ATTEMPTS = 5


def _get_json(path: str, params: dict, timeout: int = 30) -> dict:
    """GET {FX_V2}{path}?params → JSON dict, retrying transient failures. Raises FxV2Error (seam for tests)."""
    url = f"{FX_V2}{path}" + (f"?{urlencode(params)}" if params else "")
    last = 0
    for attempt in range(_ATTEMPTS):
        if attempt:
            time.sleep(1.5 + random.random() * 1.5)
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except HTTPError as e:
            last = e.code
            if e.code not in _RETRY_STATUSES:
                break
    raise FxV2Error(f"FxTwitter v2 HTTP {last} for {path} ({_ATTEMPTS} tries)")


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def tweet_from_fx_v2(s: dict, *, source: str = "fxtwitter_v2") -> Tweet:
    """One FxTwitter v2 status object → Tweet (same field meanings as the v1 path in scraperx.scraper)."""
    author = s.get("author") or {}
    verification = author.get("verification") or {}
    media = [_best_media_url(m) for m in ((s.get("media") or {}).get("all") or []) if isinstance(m, dict)]
    replying_to = s.get("replying_to") or {}
    reposted_by = s.get("reposted_by") or {}
    text = s.get("text") or ((s.get("raw_text") or {}).get("text") or "")
    quoted = None
    if isinstance(s.get("quote"), dict):
        try:
            quoted = _parse_quoted_tweet(s["quote"], depth=0)
        except Exception as e:  # a malformed quote never loses the post itself
            logger.debug("quote parse failed for %s: %s", s.get("id"), e)
    ts = s.get("created_timestamp")
    return Tweet(
        id=str(s.get("id") or ""),
        text=text,
        author=str(author.get("name") or ""),
        author_handle=str(author.get("screen_name") or ""),
        likes=_int(s.get("likes")),
        retweets=_int(s.get("reposts", s.get("retweets"))),
        replies=_int(s.get("replies")),
        views=_int(s.get("views")),
        author_avatar=str(author.get("avatar_url") or ""),
        author_id=str(author.get("id") or ""),
        media_urls=media,
        quoted_tweet=quoted,
        is_reply=bool(replying_to),
        in_reply_to_tweet_id=str(replying_to.get("status")) if replying_to.get("status") else None,
        in_reply_to_handle=replying_to.get("screen_name") if isinstance(replying_to, dict) else None,
        is_quote=quoted is not None,
        created_at=s.get("created_at"),
        created_timestamp=ts if isinstance(ts, int) else None,
        lang=s.get("lang"),
        possibly_sensitive=s.get("possibly_sensitive"),
        is_note_tweet=s.get("is_note_tweet"),
        is_community_note_marked=bool(s.get("community_note")),
        author_verified=verification.get("verified"),
        author_verified_type=verification.get("type"),
        author_followers=author.get("followers"),
        author_following=author.get("following"),
        author_joined=author.get("joined"),
        author_protected=author.get("protected"),
        reposted_by=reposted_by.get("screen_name") if isinstance(reposted_by, dict) else None,
        source_method=source,
        raw=s,
    )


def _handle(handle: str) -> str:
    h = (handle or "").strip()
    for prefix in ("https://x.com/", "https://twitter.com/", "x.com/", "twitter.com/"):
        if h.startswith(prefix):
            h = h[len(prefix):].split("/")[0].split("?")[0]
    m = _HANDLE_RE.match(h)
    if not m:
        raise FxV2Error(f"not an X handle: {handle!r}")
    return m.group(1)


def _status_id(post: str) -> str:
    m = _STATUS_ID_RE.search((post or "").strip())
    if not m:
        raise FxV2Error(f"not an X post id or URL: {post!r}")
    return m.group(1)


def _pages(path: str, params: dict, key: str, limit: int, timeout: int, delay_s: float) -> list[dict]:
    """Collect `key` items across cursor.bottom pages until `limit`, a page adds nothing, or there is no cursor."""
    items: list[dict] = []
    seen: set[str] = set()
    params = dict(params)
    for page in range(_MAX_PAGES):
        if page and delay_s:
            time.sleep(delay_s + random.random() * delay_s)
        try:
            data = _get_json(path, params, timeout)
        except FxV2Error as e:
            if not page:
                raise
            logger.warning("FxTwitter v2 page %d of %s failed, returning the %d items collected: %s",
                           page + 1, path, len(items), e)
            break
        batch = [x for x in (data.get(key) or []) if isinstance(x, dict)]
        new = [x for x in batch if str(x.get("id")) not in seen]
        for x in new:
            seen.add(str(x.get("id")))
        items.extend(new)
        cursor = (data.get("cursor") or {}).get("bottom")
        if not new or not cursor or len(items) >= limit:
            break
        params["cursor"] = cursor
    return items


def get_user_tweets(
    handle: str,
    limit: int = 20,
    *,
    with_replies: bool = False,
    include_reposts: bool = True,
    nitter_fallback: bool = True,
    timeout: int = 30,
    delay_s: float = 1.0,
) -> list[Tweet]:
    """Newest posts of an account (no X login), paged back as needed. FxTwitter v2 first (counts included); when it
    fails, Nitter user RSS (no counts, Tweet.source_method "nitter") unless nitter_fallback=False.

    Args:
        handle:          "solana", "@solana" or a profile URL.
        limit:           Max posts returned (after the repost filter).
        with_replies:    Include the account's replies.
        include_reposts: Keep reposts (posts by others shown on this timeline; Tweet.reposted_by names the
                         reposting account). False keeps only the account's own posts.
    """
    h = _handle(handle)
    want = max(1, int(limit))
    params: dict = {"count": 20}
    if with_replies:
        params["with_replies"] = "true"
    # Reposts are dropped AFTER paging, so ask for more pages than `limit` when they are excluded.
    target = want if include_reposts else want * 3
    try:
        tweets = [tweet_from_fx_v2(s) for s in _pages(f"/profile/{h}/statuses", params, "results", target, timeout, delay_s)]
    except FxV2Error as e:
        if not nitter_fallback:
            raise
        # FxTwitter's timeline answered 0 of 5 tries at 16:3x 2026-10-28 after 4 of 10 an hour earlier; Nitter user
        # RSS (no counts) keeps the timeline readable meanwhile.
        from scraperx.nitter import NitterError, user_tweets

        logger.warning("FxTwitter v2 timeline failed (%s); reading @%s through Nitter RSS", e, h)
        try:
            tweets = user_tweets(h, target, with_replies=with_replies, delay_s=delay_s)
        except NitterError as ne:
            raise FxV2Error(f"{e}; Nitter fallback failed too: {ne}") from ne
    if not include_reposts:
        tweets = [t for t in tweets if not t.reposted_by]
    return tweets[:want]


def get_replies(post: str, limit: int = 100, *, timeout: int = 30, delay_s: float = 1.0) -> list[Tweet]:
    """Replies under a post (no X login), in FxTwitter's ranking order, paged back as needed."""
    return [
        tweet_from_fx_v2(s)
        for s in _pages(f"/conversation/{_status_id(post)}", {}, "replies", max(1, int(limit)), timeout, delay_s)
    ][: max(1, int(limit))]


def get_trends(count: int = 20, *, timeout: int = 30) -> list[dict]:
    """Trending topics as FxTwitter returns them (list of dicts)."""
    data = _get_json("/trends", {"count": int(count)}, timeout)
    return [t for t in (data.get("trends") or []) if isinstance(t, dict)]
