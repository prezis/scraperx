"""Papers With Code mentions via the v1 papers search API."""

from __future__ import annotations

import logging

from scraperx.github_analyzer.mentions._http import (
    cache_or_fetch,
    http_get_json,
    safe_str,
)
from scraperx.github_analyzer.schemas import ExternalMention

logger = logging.getLogger(__name__)

SOURCE = "pwc"
API_URL = "https://paperswithcode.com/api/v1/papers/"


def search(owner: str, repo: str, db=None) -> list[ExternalMention]:
    query = f"{owner}/{repo}"

    def fetch() -> list[dict]:
        params = {"q": query, "items_per_page": 20}
        try:
            response = http_get_json(API_URL, params=params)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        results_data = response.get("results", []) if isinstance(response, dict) else []
        out = []
        for hit in results_data:
            if not isinstance(hit, dict):
                continue
            authors = hit.get("authors", []) or []
            paper_id = hit.get("id")
            url_abs = hit.get("url_abs")
            url_val = url_abs or (f"https://paperswithcode.com/paper/{paper_id}" if paper_id else "")

            out.append(
                {
                    "source": SOURCE,
                    "title": hit.get("title") or "",
                    "url": url_val,
                    "score": 0,
                    "published_at": safe_str(hit.get("published")),
                    "author": ", ".join(a for a in authors[:3] if isinstance(a, str)),
                    "snippet": (hit.get("abstract") or "")[:280],
                    "metadata": {"paper_id": paper_id},
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
