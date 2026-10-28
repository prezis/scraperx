"""reddit_archive — Reddit comment search and user history through the Arctic Shift archive API, no account, no browser.

Arctic Shift (github.com/ArthurHeitmann/arctic_shift, API ``https://arctic-shift.photon-reddit.com/api``) archives
Reddit as it is posted and serves it over plain HTTP. It answers what the live tiers in scraperx.reddit cannot:
full-text search inside COMMENTS, and an author's posts and comments across subreddits.

Rules from its API README (read 2026-10-28): full-text (``query`` / ``title`` / ``selftext`` on posts, ``body`` on
comments) only together with ``subreddit`` or ``author``, "not supported with very active users or subreddits"; items
are archived when posted and re-read about 36 h later, so before that ``score`` / ``num_comments`` are 0 or 1
(``score_as_of`` on every result says when the numbers were read). Rate limits are dynamic; 429 carries
``X-RateLimit-Reset``.

Measured from this host 2026-10-28: subreddit listing 200 in 1.2 s (newest item 1.1 h old); comment full-text in
r/ledgerwallet 200 in 3.0 s; author history 200 in 1.0 s; post ``query`` in r/ledgerwallet 200 in 9.0 s; ``title`` in
r/ledgerwallet and ``query`` in r/solana answered 422 "Timeout. Maybe slow down a bit"; a thread archived 24 s after
posting held score 1 (live: 5) and 1 of its 7 comments.

    from scraperx.reddit_archive import search_comments, user_history
    for c in search_comments("drained", subreddit="ledgerwallet", limit=50):
        print(c.created_utc, c.author, c.score, c.link_title, c.body[:80])
    posts, comments = user_history("some_user", limit=100)
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scraperx.reddit import RedditComment, RedditError, RedditPost, _post_from_t3

logger = logging.getLogger(__name__)

__all__ = ["ARCTIC_BASE", "search_comments", "search_posts", "user_history"]

ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "scraperx (+https://github.com/prezis/scraperx)"
_PAGE_MAX = 100
_MAX_PAGES = 20
_RATE_WAIT_CAP_S = 30.0


def _get(path: str, params: dict, timeout: int = 40) -> list[dict]:
    """GET → the ``data`` list. One retry after a 422 server timeout or a 429 (waiting for its reset, capped)."""
    url = f"{ARCTIC_BASE}{path}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    for attempt in (0, 1):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8", errors="replace"))
            if body.get("error"):
                raise RedditError(f"Arctic Shift error for {path}: {body['error']}")
            return [x for x in (body.get("data") or []) if isinstance(x, dict)]
        except HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = json.loads(e.read().decode("utf-8", errors="replace")).get("error") or ""
            if attempt == 0 and e.code == 422 and "timeout" in detail.lower():
                time.sleep(3.0)
                continue
            if attempt == 0 and e.code == 429:
                reset = e.headers.get("X-RateLimit-Reset") if e.headers else None
                try:
                    wait = min(max(float(reset or 5), 1.0), _RATE_WAIT_CAP_S)
                except ValueError:
                    wait = 5.0
                time.sleep(wait)
                continue
            raise RedditError(f"Arctic Shift HTTP {e.code} for {path}: {detail or e.reason}") from e
    raise RedditError(f"Arctic Shift gave up on {path}")  # pragma: no cover — loop always returns or raises


def _as_of(d: dict) -> float:
    """When the archive last read this item's numbers."""
    meta = d.get("_meta") if isinstance(d.get("_meta"), dict) else {}
    stamps = [d.get("retrieved_on"), d.get("updated_on"), meta.get("retrieved_2nd_on")]
    return float(max((s for s in stamps if isinstance(s, (int, float))), default=0))


def _post(d: dict) -> RedditPost:
    p = _post_from_t3(d, tier="arctic_shift")
    p.score_as_of = _as_of(d)
    return p


def _comment(d: dict) -> RedditComment:
    return RedditComment(
        id=str(d.get("id") or ""),
        author=str(d.get("author") or ""),
        body=str(d.get("body") or ""),
        score=int(d.get("score") or 0),
        parent_id=str(d.get("parent_id") or ""),
        created_utc=float(d.get("created_utc") or 0),
        permalink=str(d.get("permalink") or ""),
        subreddit=str(d.get("subreddit") or ""),
        link_id=str(d.get("link_id") or ""),
        link_title=str(d.get("link_title") or ""),
        score_as_of=_as_of(d),
    )


def _when(v: int | float | str | None) -> int | str | None:
    """Accept an epoch or a date string ("2026-10-01"); the API takes both."""
    if v is None or isinstance(v, (int, str)):
        return v
    return int(v)


def _paged(path: str, params: dict, limit: int, timeout: int, delay_s: float) -> list[dict]:
    """Newest first, paging back with before=<oldest created_utc seen> until `limit` or the archive runs out."""
    out: list[dict] = []
    seen: set[str] = set()
    params = {**params, "sort": "desc"}
    for page in range(_MAX_PAGES):
        if page and delay_s:
            time.sleep(delay_s)
        params["limit"] = min(_PAGE_MAX, max(1, limit - len(out)))
        batch = _get(path, params, timeout)
        new = [x for x in batch if str(x.get("id")) not in seen]
        seen.update(str(x.get("id")) for x in new)
        out.extend(new)
        if not new or len(batch) < params["limit"] or len(out) >= limit:
            break
        oldest = min(int(x.get("created_utc") or 0) for x in new)
        params["before"] = oldest
    return out[:limit]


def _need_scope(text: str | None, subreddit: str | None, author: str | None) -> None:
    if text and not (subreddit or author):
        raise RedditError("Arctic Shift full-text search needs subreddit= or author= as well")


def search_posts(
    query: str | None = None,
    *,
    subreddit: str | None = None,
    author: str | None = None,
    title: str | None = None,
    after: int | float | str | None = None,
    before: int | float | str | None = None,
    limit: int = 100,
    timeout: int = 40,
    delay_s: float = 1.0,
) -> list[RedditPost]:
    """Archived posts, newest first. `query` / `title` need `subreddit` or `author`."""
    _need_scope(query or title, subreddit, author)
    if not (subreddit or author):
        raise RedditError("search_posts needs subreddit= or author=")
    params = {"subreddit": subreddit, "author": author, "query": query, "title": title,
              "after": _when(after), "before": _when(before)}
    return [_post(d) for d in _paged("/posts/search", params, max(1, int(limit)), timeout, delay_s)]


def search_comments(
    body: str | None = None,
    *,
    subreddit: str | None = None,
    author: str | None = None,
    link_id: str | None = None,
    after: int | float | str | None = None,
    before: int | float | str | None = None,
    limit: int = 100,
    timeout: int = 40,
    delay_s: float = 1.0,
) -> list[RedditComment]:
    """Archived comments, newest first. Full-text `body` (Postgres syntax: words, "phrase", a OR b, -word) needs
    `subreddit` or `author`; `link_id` ("t3_<post id>" or the bare id) lists one thread's comments."""
    _need_scope(body, subreddit, author)
    if not (subreddit or author or link_id):
        raise RedditError("search_comments needs subreddit=, author= or link_id=")
    if link_id and not link_id.startswith("t3_"):
        link_id = f"t3_{link_id}"
    params = {"subreddit": subreddit, "author": author, "body": body, "link_id": link_id,
              "after": _when(after), "before": _when(before)}
    return [_comment(d) for d in _paged("/comments/search", params, max(1, int(limit)), timeout, delay_s)]


def user_history(author: str, limit: int = 100, *, timeout: int = 40, delay_s: float = 1.0
                 ) -> tuple[list[RedditPost], list[RedditComment]]:
    """An author's newest posts and comments (each up to `limit`), across subreddits."""
    name = (author or "").strip().removeprefix("u/").removeprefix("/u/")
    if not name:
        raise RedditError("author must be non-empty")
    posts = search_posts(author=name, limit=limit, timeout=timeout, delay_s=delay_s)
    comments = search_comments(author=name, limit=limit, timeout=timeout, delay_s=delay_s)
    return posts, comments


def iso(ts: float) -> str:
    """Epoch → ISO-8601 UTC (helper for printing created_utc / score_as_of)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z") if ts else ""
