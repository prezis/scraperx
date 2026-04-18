"""Tier-B semantic layer — generic web search wrapping local_web_search.

Tier A (scraperx/github_analyzer/mentions/) has dedicated adapters for the 6
highest-signal platforms (HN, Reddit, StackOverflow, dev.to, arXiv, PWC).
Tier B (this module) uses a single generic web-search callable — `local_web_search`
from local-ai-mcp on RTX 5090 — to cover Lobsters / Medium / Bluesky /
Substack / ProductHunt / LinkedIn without platform-specific scrapers.

**Dependency injection:** this module does NOT import local-ai-mcp directly
(it's MCP-protocol-only, not a Python package). Callers pass a `web_search_fn`
with signature:

    web_search_fn(query: str, n_results: int = 10) -> list[dict]

where each returned dict has keys `title`, `url`, `snippet`. The pipeline
(T12, synthesis) wires in the real MCP-backed function; tests mock it.

If `web_search_fn` is None (e.g. in the stub pipeline before T12, or when
local-ai-mcp is unreachable), `search()` logs once and returns `[]` —
graceful degradation, never a crash.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from scraperx.github_analyzer.mentions._http import cache_or_fetch, safe_int, safe_str
from scraperx.github_analyzer.schemas import ExternalMention

logger = logging.getLogger(__name__)

SOURCE = "semantic_web"


@dataclass(frozen=True)
class SiteQuery:
    """A site-scoped query target — ranked by per-site signal quality."""

    host: str          # bare hostname for site: filter
    label: str         # human label in logs / metadata
    weight: int = 10   # not used yet; reserved for synthesis ranking


# Ranked by the s14 GPU rubric-judged list (Tier B platforms from landscape audit)
DEFAULT_SITES: tuple[SiteQuery, ...] = (
    SiteQuery("lobste.rs", "Lobsters", 15),
    SiteQuery("substack.com", "Substack", 14),
    SiteQuery("medium.com", "Medium", 13),
    SiteQuery("producthunt.com", "Product Hunt", 12),
    SiteQuery("bsky.app", "Bluesky", 11),
    SiteQuery("linkedin.com", "LinkedIn", 10),
)


def _build_query(owner: str, repo: str, sites: tuple[SiteQuery, ...]) -> str:
    """Compose a `site:a.com OR site:b.com ... "owner/repo"` query.

    Kept as one query (vs one-per-site) to minimise search quota usage. If
    SearXNG/upstream handles OR poorly, T12 can split and re-aggregate.
    """
    sites_expr = " OR ".join(f"site:{s.host}" for s in sites)
    return f'({sites_expr}) "{owner}/{repo}"'


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def search(
    owner: str,
    repo: str,
    web_search_fn: Callable[..., list[dict]] | None = None,
    db=None,
    sites: tuple[SiteQuery, ...] = DEFAULT_SITES,
    n_results: int = 20,
) -> list[ExternalMention]:
    """Return Tier-B semantic mentions via generic web search.

    Args:
        owner, repo: GitHub slug parts.
        web_search_fn: callable matching local_web_search signature. If None,
                       graceful degrade: return [] (with a WARN log once).
        db: optional SocialDB for cache.
        sites: tuple of SiteQuery targets to scope the search.
        n_results: upper bound on results returned by the underlying engine.
    """
    if web_search_fn is None:
        logger.warning(
            "semantic.search: web_search_fn not provided — Tier B disabled. "
            "Caller should inject local_web_search or equivalent."
        )
        return []

    query = _build_query(owner, repo, sites)
    cache_key = f"{owner}/{repo}::{','.join(s.host for s in sites)}"

    def fetch() -> list[dict]:
        try:
            raw = web_search_fn(query=query, n_results=n_results)
        except TypeError:
            # Caller supplied positional-only signature — try again
            try:
                raw = web_search_fn(query, n_results)
            except Exception as e:
                logger.warning("semantic.search positional fallback failed: %s", e)
                return []
        except Exception as e:
            logger.warning("semantic.search web_search_fn failed: %s", e)
            return []

        if not isinstance(raw, list):
            return []

        site_hosts = {s.host for s in sites}
        out = []
        for hit in raw:
            if not isinstance(hit, dict):
                continue
            url = safe_str(hit.get("url"))
            if not url:
                continue
            host = _host_of(url)
            # Keep only hits from the sites we asked about — web_search may
            # leak others through the OR query
            if not any(host == s or host.endswith("." + s) for s in site_hosts):
                continue

            out.append(
                {
                    "source": SOURCE,
                    "title": safe_str(hit.get("title")),
                    "url": url,
                    "score": safe_int(hit.get("score")),
                    "published_at": safe_str(hit.get("published_at")),
                    "author": safe_str(hit.get("author")),
                    "snippet": safe_str(hit.get("snippet"))[:280],
                    "metadata": {
                        "host": host,
                        "label": next(
                            (s.label for s in sites if host == s.host or host.endswith("." + s.host)),
                            host,
                        ),
                    },
                }
            )
        return out

    cached = cache_or_fetch(db, source=SOURCE, query=cache_key, fetch_fn=fetch)
    return [ExternalMention(**x) for x in cached]
