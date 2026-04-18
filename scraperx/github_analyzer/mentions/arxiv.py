"""arXiv mentions via the Atom export API — stdlib XML parsing.

Signal for algorithmic/research-adjacent repos. Low noise, high precision —
when arXiv returns a hit on a repo, the paper almost always references it.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from scraperx.github_analyzer.mentions._http import (
    cache_or_fetch,
    http_get_text,
)
from scraperx.github_analyzer.schemas import ExternalMention

logger = logging.getLogger(__name__)

SOURCE = "arxiv"
API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def search(owner: str, repo: str, db=None) -> list[ExternalMention]:
    query = f"{owner}/{repo}"

    def fetch() -> list[dict]:
        params = {
            "search_query": f"all:{query}",
            "max_results": 20,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            xml_text = http_get_text(API_URL, params=params)
            root = ET.fromstring(xml_text)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        out = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            title = (entry.findtext(f"{ATOM_NS}title", default="") or "").strip()
            url_val = (entry.findtext(f"{ATOM_NS}id", default="") or "").strip()
            published_at = entry.findtext(f"{ATOM_NS}published", default="") or ""
            updated = entry.findtext(f"{ATOM_NS}updated", default="") or ""

            authors = [
                a.text.strip()
                for a in entry.findall(f"{ATOM_NS}author/{ATOM_NS}name")
                if a.text
            ]
            summary = (entry.findtext(f"{ATOM_NS}summary", default="") or "").strip()

            out.append(
                {
                    "source": SOURCE,
                    "title": title,
                    "url": url_val,
                    "score": 0,
                    "published_at": published_at,
                    "author": ", ".join(authors),
                    "snippet": summary[:280],
                    "metadata": {"updated": updated},
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
