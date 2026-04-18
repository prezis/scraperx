"""Tests for scraperx.github_analyzer.semantic (T10).

Tier-B semantic layer — wraps an injected web_search callable. We never call
the real local_web_search here; tests pass in fakes.
"""

from __future__ import annotations

import pytest

from scraperx.github_analyzer.schemas import ExternalMention
from scraperx.github_analyzer.semantic import (
    DEFAULT_SITES,
    SOURCE,
    SiteQuery,
    _build_query,
    _host_of,
    search,
)
from scraperx.social_db import SocialDB

# ---------------------------------------------------------------------------
# Helpers


@pytest.fixture
def db(tmp_path):
    sdb = SocialDB(db_path=str(tmp_path / "semantic.db"))
    yield sdb
    sdb.close()


def _fake_search(results):
    """Build a web_search_fn that returns the provided list."""
    calls = {"n": 0, "last_query": None, "last_n": None}

    def fn(query: str, n_results: int = 10):
        calls["n"] += 1
        calls["last_query"] = query
        calls["last_n"] = n_results
        return results

    return fn, calls


# ---------------------------------------------------------------------------
# Query builder


def test_build_query_site_or_format():
    sites = (SiteQuery("lobste.rs", "Lobsters"), SiteQuery("medium.com", "Medium"))
    q = _build_query("yt-dlp", "yt-dlp", sites)
    assert "site:lobste.rs" in q
    assert "site:medium.com" in q
    assert " OR " in q
    assert '"yt-dlp/yt-dlp"' in q


def test_default_sites_are_ordered():
    """All DEFAULT_SITES are SiteQuery instances with weight attrs."""
    assert len(DEFAULT_SITES) == 6
    assert all(isinstance(s, SiteQuery) for s in DEFAULT_SITES)
    labels = [s.label for s in DEFAULT_SITES]
    assert "Lobsters" in labels
    assert "Medium" in labels
    assert "Bluesky" in labels


def test_host_of_parses_or_empty():
    assert _host_of("https://lobste.rs/s/abc") == "lobste.rs"
    assert _host_of("https://www.medium.com/foo") == "www.medium.com"
    assert _host_of("not-a-url") == ""
    assert _host_of("") == ""


# ---------------------------------------------------------------------------
# Graceful degradation


def test_search_returns_empty_when_no_web_search_fn():
    result = search("o", "r", web_search_fn=None)
    assert result == []


def test_search_returns_empty_when_fn_raises():
    def bad_fn(query, n_results=10):
        raise RuntimeError("backend down")

    assert search("o", "r", web_search_fn=bad_fn) == []


def test_search_returns_empty_on_non_list_response():
    def bad_fn(query, n_results=10):
        return {"not": "a list"}

    assert search("o", "r", web_search_fn=bad_fn) == []


# ---------------------------------------------------------------------------
# Happy path + filtering


def test_search_filters_by_site_allowlist():
    """Hits from hosts outside DEFAULT_SITES are dropped (defense against
    search engines leaking through the OR query)."""
    results = [
        {
            "title": "Lobsters post",
            "url": "https://lobste.rs/s/abc",
            "snippet": "good project",
            "score": 15,
        },
        {
            "title": "Off-topic result",
            "url": "https://random-site.example.com/x",
            "snippet": "unrelated",
        },
        {
            "title": "Medium article",
            "url": "https://medium.com/@user/article",
            "snippet": "tutorial",
        },
    ]
    fn, _ = _fake_search(results)
    out = search("o", "r", web_search_fn=fn)
    assert len(out) == 2
    hosts = {m.metadata["host"] for m in out}
    assert "lobste.rs" in hosts
    assert "medium.com" in hosts
    assert "random-site.example.com" not in hosts


def test_search_normalises_to_external_mention():
    results = [
        {
            "title": "Great project discussion",
            "url": "https://lobste.rs/s/abc",
            "snippet": "Nice work",
            "score": 42,
            "published_at": "2024-01-01",
            "author": "alice",
        }
    ]
    fn, _ = _fake_search(results)
    out = search("yt-dlp", "yt-dlp", web_search_fn=fn)
    assert len(out) == 1
    m = out[0]
    assert isinstance(m, ExternalMention)
    assert m.source == SOURCE
    assert m.title == "Great project discussion"
    assert m.url == "https://lobste.rs/s/abc"
    assert m.score == 42
    assert m.published_at == "2024-01-01"
    assert m.author == "alice"
    assert m.metadata["host"] == "lobste.rs"
    assert m.metadata["label"] == "Lobsters"


def test_search_accepts_subdomain_of_allowlisted_site():
    """www.medium.com / subdomain.medium.com should both pass the filter."""
    results = [
        {"title": "T1", "url": "https://www.medium.com/a/b", "snippet": "x"},
        {"title": "T2", "url": "https://user.substack.com/p/post", "snippet": "x"},
    ]
    fn, _ = _fake_search(results)
    out = search("o", "r", web_search_fn=fn)
    assert len(out) == 2
    hosts = {m.metadata["host"] for m in out}
    assert "www.medium.com" in hosts
    assert "user.substack.com" in hosts


def test_search_drops_hits_without_url():
    results = [
        {"title": "No URL", "url": "", "snippet": "x"},
        {"title": "No URL key", "snippet": "x"},
        {"title": "Good", "url": "https://medium.com/a", "snippet": "x"},
    ]
    fn, _ = _fake_search(results)
    out = search("o", "r", web_search_fn=fn)
    assert len(out) == 1
    assert out[0].title == "Good"


def test_search_truncates_snippet_to_280():
    results = [
        {
            "title": "T",
            "url": "https://lobste.rs/s/x",
            "snippet": "a" * 500,
        }
    ]
    fn, _ = _fake_search(results)
    out = search("o", "r", web_search_fn=fn)
    assert len(out[0].snippet) == 280


# ---------------------------------------------------------------------------
# Query content + caching


def test_search_passes_n_results_to_fn():
    fn, calls = _fake_search([])
    search("o", "r", web_search_fn=fn, n_results=42)
    assert calls["last_n"] == 42


def test_search_query_contains_both_sites_and_slug():
    fn, calls = _fake_search([])
    search("yt-dlp", "yt-dlp", web_search_fn=fn)
    q = calls["last_query"]
    assert '"yt-dlp/yt-dlp"' in q
    assert "site:lobste.rs" in q


def test_search_caches_via_db(db):
    """Second call with same repo should hit cache, not the fn."""
    results = [{"title": "T", "url": "https://lobste.rs/s/x", "snippet": "x"}]
    fn, calls = _fake_search(results)
    search("o", "r", web_search_fn=fn, db=db)
    search("o", "r", web_search_fn=fn, db=db)
    assert calls["n"] == 1  # Cached second time


def test_search_different_sites_invalidate_cache(db):
    """Cache key includes the sites list, so a different site tuple misses."""
    results = [{"title": "T", "url": "https://lobste.rs/s/x", "snippet": "x"}]
    fn, calls = _fake_search(results)
    search("o", "r", web_search_fn=fn, db=db, sites=DEFAULT_SITES)
    search("o", "r", web_search_fn=fn, db=db, sites=(SiteQuery("lobste.rs", "L"),))
    # Different `sites` → different cache key → second fetch happens
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Positional-signature tolerance


def test_search_handles_positional_only_fn():
    """If caller's fn only accepts positional args, the wrapper falls back."""

    def positional_fn(query, n_results):
        return [{"title": "ok", "url": "https://lobste.rs/s/1", "snippet": "s"}]

    out = search("o", "r", web_search_fn=positional_fn)
    assert len(out) == 1


def test_search_empty_results_not_cached(db):
    """Empty results shouldn't poison the cache."""
    fn, calls = _fake_search([])
    search("o", "r", web_search_fn=fn, db=db)
    search("o", "r", web_search_fn=fn, db=db)
    assert calls["n"] == 2  # Re-fetched, not cached
