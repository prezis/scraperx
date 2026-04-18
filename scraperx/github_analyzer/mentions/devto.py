"""DEV.to mentions — fetch top articles, client-side filter by repo slug.

dev.to's search API is limited; we pull the top 30 recent articles and keep
the ones that mention `owner/repo` in their title, description, or tags.
"""

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

SOURCE = "devto"
API_URL = "https://dev.to/api/articles"


def search(owner: str, repo: str, db=None) -> list[ExternalMention]:
    query = f"{owner}/{repo}"
    target = query.lower()

    def fetch() -> list[dict]:
        params = {"per_page": 30, "top": "7"}
        try:
            response = http_get_json(API_URL, params=params)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        if not isinstance(response, list):
            return []

        out = []
        for hit in response:
            if not isinstance(hit, dict):
                continue
            title = hit.get("title") or ""
            description = hit.get("description") or ""
            tag_list = hit.get("tag_list") or []
            search_text = f"{title} {description} {' '.join(tag_list)}".lower()

            if target not in search_text:
                continue

            user_info = hit.get("user") or {}
            out.append(
                {
                    "source": SOURCE,
                    "title": title,
                    "url": hit.get("url") or "",
                    "score": safe_int(hit.get("positive_reactions_count")),
                    "published_at": safe_str(hit.get("published_at")),
                    "author": safe_str(user_info.get("username")),
                    "snippet": description[:280],
                    "metadata": {"tags": tag_list},
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
