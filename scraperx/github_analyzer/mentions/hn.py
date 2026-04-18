"""Hacker News mentions via the Algolia HN Search API (free, unauthed)."""

from __future__ import annotations

import logging

from scraperx.github_analyzer.mentions._http import (
    cache_or_fetch,
    http_get_json,
    safe_int,
    safe_str,
)
from scraperx.github_analyzer.schemas import ExternalMention

logger = logging.getLogger(__name__)

SOURCE = "hn"
API_URL = "https://hn.algolia.com/api/v1/search"


def search(owner: str, repo: str, db=None) -> list[ExternalMention]:
    """Return HN story hits for a github repo. Cached via db if given."""
    query = f"{owner}/{repo}"

    def fetch() -> list[dict]:
        params = {"query": query, "tags": "story", "hitsPerPage": 30}
        try:
            response = http_get_json(API_URL, params=params)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        hits = response.get("hits", []) if isinstance(response, dict) else []
        out = []
        for hit in hits:
            object_id = hit.get("objectID")
            raw_url = hit.get("url")
            url_val = raw_url or f"https://news.ycombinator.com/item?id={object_id}"
            out.append(
                {
                    "source": SOURCE,
                    "title": hit.get("title") or "",
                    "url": url_val,
                    "score": safe_int(hit.get("points")),
                    "published_at": safe_str(hit.get("created_at")),
                    "author": safe_str(hit.get("author")),
                    "snippet": "",
                    "metadata": {"objectID": str(object_id) if object_id else ""},
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**d) for d in cached]
