"""Tests for scraperx.search module."""

import logging
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from scraperx import search as search_mod
from scraperx.scraper import Tweet
from scraperx.search import (
    _brave_query,
    _brave_search,
    _cache_key,
    _ddg_search,
    _extract_tweet_urls,
    _get_cached,
    _set_cache,
    search_tweets,
)


@pytest.fixture(autouse=True)
def _no_brave_key(monkeypatch):
    """No test reads a real Brave key from the machine it runs on; a test that needs one sets it."""
    for name in ("SCRAPERX_BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)

# --- HTML extraction tests ---

SAMPLE_DDG_HTML = """
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2FAlice%2Fstatus%2F111111&amp;rut=abc">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2FBob%2Fstatus%2F222222&amp;rut=def">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2FPolymarket&amp;rut=ghi">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2FAlice%2Fstatus%2F111111&amp;rut=dup">
"""


def test_extract_tweet_urls_from_ddg():
    urls = _extract_tweet_urls(SAMPLE_DDG_HTML)
    assert len(urls) == 2  # Deduplicated, profile URL excluded
    assert "https://x.com/Alice/status/111111" in urls
    assert "https://x.com/Bob/status/222222" in urls


def test_extract_tweet_urls_direct_hrefs():
    html = '<a href="https://x.com/Charlie/status/333333">tweet</a>'
    urls = _extract_tweet_urls(html)
    assert len(urls) == 1
    assert urls[0] == "https://x.com/Charlie/status/333333"


def test_extract_tweet_urls_twitter_domain():
    html = "uddg=https%3A%2F%2Ftwitter.com%2FDave%2Fstatus%2F444444&rut=x"
    urls = _extract_tweet_urls(html)
    assert len(urls) == 1
    assert "444444" in urls[0]


def test_extract_tweet_urls_empty():
    assert _extract_tweet_urls("") == []
    assert _extract_tweet_urls("<html><body>No results</body></html>") == []


# --- Cache tests ---


def test_cache_roundtrip(tmp_path):
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        urls = ["https://x.com/A/status/1", "https://x.com/B/status/2"]
        _set_cache("test query", None, urls)
        result = _get_cached("test query", None, max_age=60)
        assert result == urls


def test_cache_expired(tmp_path):
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        _set_cache("old query", None, ["https://x.com/A/status/1"])
        # max_age=0 means always expired
        result = _get_cached("old query", None, max_age=0)
        assert result is None


def test_cache_miss(tmp_path):
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        result = _get_cached("nonexistent", None)
        assert result is None


def test_cache_key_varies_with_time_filter():
    k1 = _cache_key("query", None)
    k2 = _cache_key("query", "d")
    assert k1 != k2


# --- search_tweets tests (mocked DDG) ---


def test_search_tweets_enrich(tmp_path):
    cached_urls = [
        "https://x.com/TestUser/status/999999",
    ]

    mock_tweet = Tweet(
        id="999999",
        text="Test tweet content",
        author="Test User",
        author_handle="TestUser",
        likes=42,
        retweets=5,
        views=1000,
        source_method="fxtwitter",
    )

    with patch("scraperx.search._CACHE_DIR", tmp_path):
        _set_cache("site:x.com test", None, cached_urls)

        with patch("scraperx.search.XScraper") as MockScraper:
            instance = MockScraper.return_value
            instance.get_tweet.return_value = mock_tweet

            results = search_tweets("test", limit=1, delay=0)
            assert len(results) == 1
            assert results[0].text == "Test tweet content"
            assert results[0].likes == 42
            assert "ddg+" in results[0].source_method


def test_search_tweets_no_enrich(tmp_path):
    cached_urls = [
        "https://x.com/Alice/status/111",
        "https://x.com/Bob/status/222",
    ]

    with patch("scraperx.search._CACHE_DIR", tmp_path):
        _set_cache("site:x.com keywords", None, cached_urls)
        results = search_tweets("keywords", limit=2, enrich=False)
        assert len(results) == 2
        assert results[0].id == "111"
        assert results[0].author_handle == "Alice"
        assert results[0].source_method == "ddg_stub"


def test_search_tweets_empty(tmp_path):
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        with patch("scraperx.search._ddg_search_urllib", return_value="<html></html>"):
            with patch("scraperx.search._ddg_search_curl", return_value="<html></html>"):
                results = search_tweets("nothing_here_xyz", limit=5, delay=0)
                assert results == []


def test_search_tweets_error_handling(tmp_path):
    cached_urls = [
        "https://x.com/A/status/1",
        "https://x.com/B/status/2",
        "https://x.com/C/status/3",
    ]

    with patch("scraperx.search._CACHE_DIR", tmp_path):
        _set_cache("site:x.com errors", None, cached_urls)

        with patch("scraperx.search.XScraper") as MockScraper:
            instance = MockScraper.return_value
            # First call fails, second succeeds
            instance.get_tweet.side_effect = [
                RuntimeError("API error"),
                Tweet(id="2", text="ok", author="B", author_handle="B", source_method="fxtwitter"),
                Tweet(id="3", text="ok2", author="C", author_handle="C", source_method="fxtwitter"),
            ]

            results = search_tweets("errors", limit=2, delay=0)
            assert len(results) == 2  # Skipped error, got 2 good ones


# --- Brave Search API backend (mocked; response shape per the Brave web-search docs: web.results[].url) ---


def _brave_page(urls, more=False):
    return {
        "type": "search",
        "query": {"more_results_available": more},
        "web": {"type": "search", "results": [{"title": "t", "url": u, "description": "d"} for u in urls]},
    }


def test_brave_query_from_user_becomes_a_site_path():
    assert _brave_query("from:alice pump.fun") == "site:x.com/alice pump.fun"
    assert _brave_query("from:@Bob_1") == "site:x.com/Bob_1"
    assert _brave_query("pump.fun") == "site:x.com pump.fun"
    assert _brave_query('site:x.com "exact phrase"') == 'site:x.com "exact phrase"'


def test_brave_search_reads_web_results_and_maps_freshness(tmp_path, monkeypatch):
    seen = []

    def fake_get(params, key, timeout=15):
        seen.append((params, key))
        return _brave_page([
            "https://x.com/Alice/status/111",
            "https://x.com/Polymarket",
            "https://twitter.com/Bob/status/222",
            "https://x.com/Alice/status/111",
        ])

    monkeypatch.setattr(search_mod, "_brave_get", fake_get)
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        urls = _brave_search("pump.fun", max_results=10, time_filter="w", key="k-test")
    assert urls == ["https://x.com/Alice/status/111", "https://x.com/Bob/status/222"]  # profile and duplicate dropped
    assert len(seen) == 1  # no more_results_available: one call
    params, key = seen[0]
    assert key == "k-test" and params["q"] == "site:x.com pump.fun" and params["freshness"] == "pw"
    assert params["count"] == 20 and params["offset"] == 0


def test_brave_search_stops_at_the_page_cap(tmp_path, monkeypatch):
    calls = []

    def fake_get(params, key, timeout=15):
        calls.append(params["offset"])
        return _brave_page([f"https://x.com/U/status/{params['offset']}{i}" for i in range(3)], more=True)

    monkeypatch.setattr(search_mod, "_brave_get", fake_get)
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        urls = _brave_search("anything", max_results=50, key="k")
    assert calls == list(range(search_mod._BRAVE_MAX_PAGES))
    assert len(urls) == 3 * search_mod._BRAVE_MAX_PAGES


def test_brave_without_a_key_makes_no_call(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "_brave_get", lambda *a, **k: pytest.fail("called without a key"))
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        assert _brave_search("anything") == []


def test_auto_prefers_brave_when_a_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPERX_BRAVE_API_KEY", "k")
    monkeypatch.setattr(search_mod, "_brave_get", lambda params, key, timeout=15: _brave_page(["https://x.com/Alice/status/111"]))
    monkeypatch.setattr(search_mod, "_ddg_search", lambda *a, **k: pytest.fail("DDG asked although Brave answered"))
    with patch("scraperx.search._CACHE_DIR", tmp_path):
        results = search_tweets("pump.fun", limit=1, enrich=False)
    assert [(t.id, t.author_handle, t.source_method) for t in results] == [("111", "Alice", "brave_stub")]


def test_auto_falls_back_to_ddg_when_brave_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "bad")

    def refuse(params, key, timeout=15):
        raise HTTPError("https://api.search.brave.com", 401, "Unauthorized", None, None)

    monkeypatch.setattr(search_mod, "_brave_get", refuse)
    with patch("scraperx.search._CACHE_DIR", tmp_path), patch("scraperx.search._ddg_search_urllib", return_value=SAMPLE_DDG_HTML):
        results = search_tweets("fallback", limit=2, enrich=False)
    assert [t.source_method for t in results] == ["ddg_stub", "ddg_stub"]


def test_backend_brave_without_a_key_raises(tmp_path):
    with patch("scraperx.search._CACHE_DIR", tmp_path), pytest.raises(RuntimeError, match="SCRAPERX_BRAVE_API_KEY"):
        search_tweets("x", backend="brave")


def test_backend_ddg_never_asks_brave(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPERX_BRAVE_API_KEY", "k")
    monkeypatch.setattr(search_mod, "_brave_get", lambda *a, **k: pytest.fail("Brave asked with backend=ddg"))
    with patch("scraperx.search._CACHE_DIR", tmp_path), patch("scraperx.search._ddg_search_urllib", return_value=SAMPLE_DDG_HTML):
        assert len(search_tweets("ddg only", limit=2, enrich=False, backend="ddg")) == 2


def test_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="backend"):
        search_tweets("x", backend="google")


def test_ddg_bot_check_page_is_named_not_read_as_no_results(tmp_path, caplog):
    block = "<html><body><form id='challenge-form'>bots use DuckDuckGo too</form></body></html>"
    with (
        patch("scraperx.search._CACHE_DIR", tmp_path),
        patch("scraperx.search._ddg_search_urllib", return_value=block),
        patch("scraperx.search._ddg_search_curl", return_value=block),
        caplog.at_level(logging.WARNING, logger="scraperx.search"),
    ):
        assert _ddg_search("blocked query") == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("bot-check page" in m and "SCRAPERX_BRAVE_API_KEY" in m for m in msgs)
