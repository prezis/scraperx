"""Reddit BMW scraper — generalized from github_analyzer/mentions/reddit.py.

Pulls posts from BMW-relevant subreddits via the public unauthed
/r/<sub>/new.json + /search.json endpoints. No PRAW, no auth.

Limits:
  - 60 req/min unauthed (Reddit API budget) → throttle 1 req/s.
  - /search.json caps at 100 results per call (use after=).
  - /new.json gives ~25 newest per call.

Strategy:
  - For each curated subreddit: fetch /new.json (newest), paginate via after=.
  - For r/MechanicAdvice + r/justrolledintotheshop: search with q=BMW.
  - Stop pagination after MAX_PAGES (default 10) per source.

Each post becomes one row in external_repair_corpus
(content_type='reddit_post'). Comments are NOT pulled (separate pass if
ever needed — they're cheaper in volume but noisier in quality).

Usage:
  python -m scraperx.bmw_corpus.reddit.core
  python -m scraperx.bmw_corpus.reddit.core --max-pages 3 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from scraperx.bmw_corpus._output import write_batch

log = logging.getLogger(__name__)

USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair corpus for ML training; "
    "contact: przemyslaw.palyska@gmail.com)"
)
TIMEOUT_S = 15
THROTTLE_S = 1.5  # 1 req per 1.5s = ~40 req/min, well under 60 limit

# Curated subreddits — order = priority for time budget
# 2026-04-25: removed BMWi, X3, X5 (404), E36 (403 with this UA — drop until OAuth)
# Verified live via HTTP HEAD with USER_AGENT before this edit.
# Future-add candidates (verified 200 OK): E39, E92, BmwTech.
BMW_SUBS = [
    "BMW",
    "E46",
    "E30",
    "E90",
    "E60",
    "F30",
    "G80",
    "BMWmotorrad",  # motorcycles, but mechanical content
]

# General mechanic subreddits — search-filtered to BMW
MECHANIC_SUBS_QUERIED = [
    ("MechanicAdvice", "BMW"),
    ("justrolledintotheshop", "BMW"),
    ("AskMechanics", "BMW"),
    ("Cartalk", "BMW"),
]


def _get_json(url: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_listing(url: str, after: str | None = None, limit: int = 100) -> dict:
    params: dict = {"limit": limit, "raw_json": 1}
    if after:
        params["after"] = after
    return _get_json(url, params=params)


def _fetch_search(subreddit: str, query: str, after: str | None = None) -> dict:
    params: dict = {
        "q": query,
        "restrict_sr": "on",
        "limit": 100,
        "sort": "new",
        "raw_json": 1,
    }
    if after:
        params["after"] = after
    return _get_json(
        f"https://www.reddit.com/r/{subreddit}/search.json", params=params
    )


def _normalize(post: dict, source_subreddit: str) -> dict | None:
    """Convert Reddit post dict to corpus record. Returns None if filtered out."""
    if not isinstance(post, dict):
        return None
    pid = post.get("id")
    if not pid:
        return None

    title = (post.get("title") or "").strip()
    selftext = (post.get("selftext") or "").strip()
    permalink = post.get("permalink") or ""
    url = post.get("url") or ""
    body = selftext or title  # if body empty, use title as body
    if len(body) < 5:
        return None

    created_utc = post.get("created_utc")
    if isinstance(created_utc, (int, float)):
        published_at = datetime.fromtimestamp(
            float(created_utc), tz=timezone.utc
        ).isoformat()
    else:
        published_at = None

    score = post.get("score")
    upvote_ratio = post.get("upvote_ratio")
    num_comments = post.get("num_comments")
    subreddit = post.get("subreddit") or source_subreddit

    return {
        "source": "reddit",
        "source_id": f"r_{pid}",
        "source_url": f"https://www.reddit.com{permalink}" if permalink else url,
        "source_lang": "en",
        "content_type": "reddit_post",
        "title": title or None,
        "body_text": body,
        "raw_payload": {
            "id": pid,
            "subreddit": subreddit,
            "permalink": permalink,
            "score": score,
            "upvote_ratio": upvote_ratio,
            "num_comments": num_comments,
            "author": post.get("author"),
            "is_self": post.get("is_self"),
            "url": url,
        },
        "metadata_json": {
            "subreddit": subreddit,
            "score": score,
            "upvote_ratio": upvote_ratio,
            "num_comments": num_comments,
            "author": post.get("author"),
            "subreddit_subscribers": post.get("subreddit_subscribers"),
            "link_flair_text": post.get("link_flair_text"),
            "filter_keyword": "BMW" if source_subreddit in {s for s, _ in MECHANIC_SUBS_QUERIED} else None,
            "from_subreddit": source_subreddit,
        },
        "bmw_models": None,  # post-process pass could extract from title/flair
        "year_from": None,
        "year_to": None,
        "published_at": published_at,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def crawl_subreddit_listing(subreddit: str, max_pages: int = 10) -> list[dict]:
    """Pull /r/<sub>/new.json paginated."""
    log.info("r/%s — pulling new (max_pages=%d)", subreddit, max_pages)
    out = []
    after: str | None = None
    for page in range(max_pages):
        try:
            data = _fetch_listing(
                f"https://www.reddit.com/r/{subreddit}/new.json",
                after=after,
            )
        except urllib.error.URLError as e:
            log.warning("r/%s page %d failed: %s", subreddit, page + 1, e)
            break
        children = ((data.get("data") or {}).get("children") or [])
        if not children:
            break
        for child in children:
            rec = _normalize(child.get("data") or {}, subreddit)
            if rec:
                out.append(rec)
        after = (data.get("data") or {}).get("after")
        log.info(
            "  r/%s page %d/%d — %d new", subreddit, page + 1, max_pages, len(children)
        )
        if not after:
            break
        time.sleep(THROTTLE_S)
    return out


def crawl_subreddit_search(subreddit: str, query: str, max_pages: int = 5) -> list[dict]:
    """Pull /r/<sub>/search.json?q=<query> paginated."""
    log.info("r/%s — search %r (max_pages=%d)", subreddit, query, max_pages)
    out = []
    after: str | None = None
    for page in range(max_pages):
        try:
            data = _fetch_search(subreddit, query, after=after)
        except urllib.error.URLError as e:
            log.warning("r/%s search page %d failed: %s", subreddit, page + 1, e)
            break
        children = ((data.get("data") or {}).get("children") or [])
        if not children:
            break
        for child in children:
            rec = _normalize(child.get("data") or {}, subreddit)
            if rec:
                out.append(rec)
        after = (data.get("data") or {}).get("after")
        log.info(
            "  r/%s search page %d — %d hits (BMW-filtered)",
            subreddit, page + 1, len(children),
        )
        if not after:
            break
        time.sleep(THROTTLE_S)
    return out


def crawl_all(max_pages_per_sub: int = 10, max_pages_search: int = 5) -> list[dict]:
    all_records: list[dict] = []
    for sub in BMW_SUBS:
        recs = crawl_subreddit_listing(sub, max_pages=max_pages_per_sub)
        all_records.extend(recs)
        log.info("r/%s yielded %d posts (running total: %d)", sub, len(recs), len(all_records))
    for sub, query in MECHANIC_SUBS_QUERIED:
        recs = crawl_subreddit_search(sub, query, max_pages=max_pages_search)
        all_records.extend(recs)
        log.info(
            "r/%s search %r yielded %d posts (running total: %d)",
            sub, query, len(recs), len(all_records),
        )
    # Dedupe by source_id (in case same post surfaces from search + listing)
    by_id: dict[str, dict] = {}
    for r in all_records:
        by_id.setdefault(r["source_id"], r)
    return list(by_id.values())


def main() -> int:
    p = argparse.ArgumentParser(description="Reddit BMW corpus scraper")
    p.add_argument("--max-pages", type=int, default=10, help="Per-sub listing pages")
    p.add_argument("--max-pages-search", type=int, default=5, help="Mechanic-sub search pages")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    log.info(
        "Reddit BMW crawl: %d subs × max %d pages, %d mechanic subs × max %d search pages",
        len(BMW_SUBS), args.max_pages, len(MECHANIC_SUBS_QUERIED), args.max_pages_search,
    )

    records = crawl_all(args.max_pages, args.max_pages_search)
    log.info("Total unique posts: %d", len(records))

    if args.dry_run:
        log.info("DRY RUN — no write")
        for r in records[:3]:
            log.info(
                "  %s — r/%s — %s",
                r["source_id"],
                r["metadata_json"]["subreddit"],
                (r["title"] or "")[:60],
            )
        return 0

    if records:
        path, n = write_batch(records)
        log.info("Wrote %d records to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
