"""Tests for scraperx.search module."""
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from scraperx.search import (
    _extract_tweet_urls,
    _ddg_search,
    _cache_key,
    _get_cached,
    _set_cache,
    search_tweets,
)
from scraperx.scraper import Tweet


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
    html = 'uddg=https%3A%2F%2Ftwitter.com%2FDave%2Fstatus%2F444444&rut=x'
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
                Tweet(id="2", text="ok", author="B", author_handle="B",
                      source_method="fxtwitter"),
                Tweet(id="3", text="ok2", author="C", author_handle="C",
                      source_method="fxtwitter"),
            ]

            results = search_tweets("errors", limit=2, delay=0)
            assert len(results) == 2  # Skipped error, got 2 good ones
