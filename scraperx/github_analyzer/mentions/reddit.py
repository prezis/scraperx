"""Reddit mentions via the public /search.json endpoint (unauthed)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from scraperx.github_analyzer.mentions._http import (
    cache_or_fetch,
    http_get_json,
    safe_int,
    safe_str,
)
from scraperx.github_analyzer.schemas import ExternalMention

logger = logging.getLogger(__name__)

SOURCE = "reddit"
API_URL = "https://www.reddit.com/search.json"


def _epoch_to_iso(epoch) -> str:
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return ""


def search(owner: str, repo: str, db=None) -> list[ExternalMention]:
    query = f"{owner}/{repo}"

    def fetch() -> list[dict]:
        params = {"q": query, "limit": 25, "sort": "relevance"}
        try:
            response = http_get_json(API_URL, params=params)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        children = (response.get("data", {}) or {}).get("children", []) if isinstance(response, dict) else []
        out = []
        for child in children:
            d = child.get("data", {}) if isinstance(child, dict) else {}
            # Authority signals (v1.4.1 — already in /search.json, was being discarded):
            # subreddit_subscribers = platform authority (r/programming 4M ≠ r/rustjerk 50k).
            # upvote_ratio = consensus quality (0.97 = broad agreement, 0.51 = contentious).
            # num_comments = engagement depth (77 score / 3 comments is weaker than 77 / 150).
            out.append(
                {
                    "source": SOURCE,
                    "title": d.get("title") or "",
                    "url": f"https://reddit.com{d.get('permalink', '')}",
                    "score": safe_int(d.get("score")),
                    "published_at": _epoch_to_iso(d.get("created_utc")),
                    "author": safe_str(d.get("author")),
                    "snippet": (d.get("selftext") or "")[:280],
                    "metadata": {
                        "subreddit": d.get("subreddit"),
                        "id": d.get("id"),
                        "subreddit_subscribers": safe_int(d.get("subreddit_subscribers")),
                        "num_comments": safe_int(d.get("num_comments")),
                        "upvote_ratio": d.get("upvote_ratio"),  # float 0.0-1.0 or None
                    },
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
