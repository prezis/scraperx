"""Stack Overflow mentions via StackExchange API 2.3 (unauthed, 300/day)."""

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

SOURCE = "stackoverflow"
API_URL = "https://api.stackexchange.com/2.3/search/advanced"


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
        params = {
            "order": "desc",
            "sort": "relevance",
            "q": query,
            "site": "stackoverflow",
            "pagesize": 25,
        }
        try:
            response = http_get_json(API_URL, params=params)
        except Exception as e:
            logger.warning("Error fetching from %s: %s", SOURCE, e)
            return []

        items = response.get("items", []) if isinstance(response, dict) else []
        out = []
        for hit in items:
            if not isinstance(hit, dict):
                continue
            owner_info = hit.get("owner") or {}
            is_answered = bool(hit.get("is_answered"))
            answer_count = safe_int(hit.get("answer_count"))
            snippet = f"{answer_count} answers" if is_answered else "no accepted answer"

            # Authority signals (v1.4.1 — already in StackExchange /search/advanced):
            # owner.reputation = THE canonical authority signal on SO (10k+ = expert).
            # view_count = audience size (indicates topic popularity, not opinion quality).
            # has_accepted_answer = resolved vs. still-open question.
            out.append(
                {
                    "source": SOURCE,
                    "title": hit.get("title") or "",
                    "url": hit.get("link") or "",
                    "score": safe_int(hit.get("score")),
                    "published_at": _epoch_to_iso(hit.get("creation_date")),
                    "author": safe_str(owner_info.get("display_name")),
                    "snippet": snippet,
                    "metadata": {
                        "tags": hit.get("tags", []),
                        "question_id": hit.get("question_id"),
                        "asker_reputation": safe_int(owner_info.get("reputation")),
                        "view_count": safe_int(hit.get("view_count")),
                        "has_accepted_answer": bool(hit.get("accepted_answer_id")),
                        # 7 answers unaccepted ≠ 0 answers — meaningfully different signal
                        "answer_count": safe_int(hit.get("answer_count")),
                    },
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=query, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
