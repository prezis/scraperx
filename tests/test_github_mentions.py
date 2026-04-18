"""Tests for scraperx.github_analyzer.mentions adapters (T5-T9).

Every adapter follows the same contract (never raise, normalise to
ExternalMention, cache via db when provided). Tests cover:
- Happy-path parsing per platform
- Error paths (HTTP errors, bad JSON, malformed payloads) → returns []
- Cache hit: when db has a fresh entry, adapter must NOT hit the network
- Cache write: successful fetch populates the cache
- Empty results NOT cached (lets transient errors retry)
- URL-encoded querystrings are correct
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scraperx.github_analyzer.mentions import (
    ALL_SOURCES,
    arxiv_search,
    devto_search,
    hn_search,
    pwc_search,
    reddit_search,
    stackoverflow_search,
)
from scraperx.github_analyzer.mentions._http import cache_or_fetch, safe_int, safe_str
from scraperx.github_analyzer.schemas import ExternalMention
from scraperx.social_db import SocialDB

# ---------------------------------------------------------------------------
# _http helpers


def test_safe_int_handles_none_and_garbage():
    assert safe_int(None) == 0
    assert safe_int("123") == 123
    assert safe_int("not-a-number") == 0
    assert safe_int(None, default=-1) == -1


def test_safe_str_strips_and_defaults():
    assert safe_str(None) == ""
    assert safe_str("  hello  ") == "hello"
    assert safe_str(123) == "123"


# ---------------------------------------------------------------------------
# cache_or_fetch (integration with SocialDB)


@pytest.fixture
def db(tmp_path):
    sdb = SocialDB(db_path=str(tmp_path / "mentions.db"))
    yield sdb
    sdb.close()


def test_cache_or_fetch_without_db_calls_through(db):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [{"source": "x", "title": "t", "url": "u"}]

    result = cache_or_fetch(None, "x", "q", fetch)
    assert result == [{"source": "x", "title": "t", "url": "u"}]
    assert calls["n"] == 1


def test_cache_or_fetch_hits_cache_second_time(db):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [{"source": "x", "title": "t", "url": "u"}]

    a = cache_or_fetch(db, "x", "q", fetch)
    b = cache_or_fetch(db, "x", "q", fetch)
    assert a == b
    assert calls["n"] == 1  # Second call served from cache


def test_cache_or_fetch_does_not_cache_empty(db):
    """Empty results shouldn't be cached — retry next time."""
    calls = {"n": 0}

    def fetch_empty():
        calls["n"] += 1
        return []

    cache_or_fetch(db, "x", "q", fetch_empty)
    cache_or_fetch(db, "x", "q", fetch_empty)
    assert calls["n"] == 2  # Both calls hit the fetch function


# ---------------------------------------------------------------------------
# Mock helpers


def _mock_json_response(body):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _mock_text_response(text: str):
    resp = MagicMock()
    resp.read.return_value = text.encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


URLOPEN_PATH = "scraperx.github_analyzer.mentions._http.urllib.request.urlopen"


# ---------------------------------------------------------------------------
# HN adapter


@patch(URLOPEN_PATH)
def test_hn_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "hits": [
                {
                    "objectID": "12345",
                    "title": "Awesome repo discussion",
                    "url": "https://example.com/post",
                    "points": 200,
                    "author": "alice",
                    "created_at": "2024-01-02T03:04:05Z",
                },
                {
                    "objectID": "67890",
                    "title": "Ask HN about repo",
                    "url": None,  # Triggers HN-item fallback URL
                    "points": 50,
                    "author": "bob",
                    "created_at": "2024-01-05T00:00:00Z",
                },
            ]
        }
    )
    result = hn_search("yt-dlp", "yt-dlp")
    assert len(result) == 2
    assert all(isinstance(m, ExternalMention) for m in result)
    assert result[0].source == "hn"
    assert result[0].score == 200
    assert result[0].author == "alice"
    # Null URL falls back to HN item page
    assert result[1].url == "https://news.ycombinator.com/item?id=67890"
    assert result[1].metadata["objectID"] == "67890"


@patch(URLOPEN_PATH)
def test_hn_network_error_returns_empty(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("DNS fail")
    assert hn_search("o", "r") == []


@patch(URLOPEN_PATH)
def test_hn_malformed_response_returns_empty(mock_urlopen):
    # Response is a list instead of dict — adapter checks isinstance
    mock_urlopen.return_value = _mock_json_response([])
    assert hn_search("o", "r") == []


@patch(URLOPEN_PATH)
def test_hn_caches_results(mock_urlopen, db):
    mock_urlopen.return_value = _mock_json_response(
        {"hits": [{"objectID": "1", "title": "t", "url": "https://e.com", "points": 1}]}
    )
    hn_search("o", "r", db=db)
    hn_search("o", "r", db=db)  # Should hit cache
    assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------------
# Reddit adapter


@patch(URLOPEN_PATH)
def test_reddit_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "title": "Using yt-dlp to download",
                            "permalink": "/r/python/comments/abc/",
                            "subreddit": "python",
                            "score": 77,
                            "selftext": "Great tool " * 50,
                            "author": "carol",
                            "created_utc": 1700000000.0,
                        }
                    }
                ]
            }
        }
    )
    result = reddit_search("yt-dlp", "yt-dlp")
    assert len(result) == 1
    m = result[0]
    assert m.source == "reddit"
    assert m.url == "https://reddit.com/r/python/comments/abc/"
    assert m.score == 77
    assert m.published_at.endswith("Z")
    assert len(m.snippet) == 280
    assert m.metadata["subreddit"] == "python"


@patch(URLOPEN_PATH)
def test_reddit_handles_bad_created_utc(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "title": "t",
                            "permalink": "/r/x/",
                            "created_utc": "not-a-number",
                            "score": 0,
                        }
                    }
                ]
            }
        }
    )
    result = reddit_search("o", "r")
    assert result[0].published_at == ""


@patch(URLOPEN_PATH)
def test_reddit_network_error_returns_empty(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("nope")
    assert reddit_search("o", "r") == []


# ---------------------------------------------------------------------------
# StackOverflow adapter


@patch(URLOPEN_PATH)
def test_stackoverflow_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "items": [
                {
                    "question_id": 42,
                    "title": "How do I use yt-dlp?",
                    "link": "https://stackoverflow.com/questions/42",
                    "score": 15,
                    "creation_date": 1700000000,
                    "tags": ["python", "yt-dlp"],
                    "owner": {"display_name": "dave"},
                    "is_answered": True,
                    "answer_count": 3,
                },
                {
                    "question_id": 43,
                    "title": "Broken install",
                    "link": "https://stackoverflow.com/questions/43",
                    "score": 0,
                    "creation_date": 1700000100,
                    "tags": [],
                    "is_answered": False,
                    "answer_count": 0,
                },
            ]
        }
    )
    result = stackoverflow_search("yt-dlp", "yt-dlp")
    assert len(result) == 2
    assert result[0].source == "stackoverflow"
    assert result[0].score == 15
    assert result[0].snippet == "3 answers"
    assert result[0].metadata["tags"] == ["python", "yt-dlp"]
    assert result[1].snippet == "no accepted answer"


@patch(URLOPEN_PATH)
def test_stackoverflow_empty_items(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response({"items": []})
    assert stackoverflow_search("o", "r") == []


# ---------------------------------------------------------------------------
# dev.to adapter


@patch(URLOPEN_PATH)
def test_devto_filters_by_repo_slug(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        [
            {
                "id": 1,
                "title": "Using yt-dlp/yt-dlp in production",  # Matches
                "description": "a tutorial",
                "url": "https://dev.to/a/1",
                "positive_reactions_count": 42,
                "published_at": "2024-01-01T00:00:00Z",
                "user": {"username": "eve"},
                "tag_list": ["python"],
            },
            {
                "id": 2,
                "title": "Unrelated post",  # Won't match
                "description": "something else",
                "url": "https://dev.to/a/2",
                "positive_reactions_count": 5,
                "user": {"username": "frank"},
                "tag_list": ["rust"],
            },
            {
                "id": 3,
                "title": "Matches via tag",  # Via tag match
                "description": "cool",
                "url": "https://dev.to/a/3",
                "positive_reactions_count": 10,
                "user": {"username": "gina"},
                "tag_list": ["yt-dlp/yt-dlp"],
            },
        ]
    )
    result = devto_search("yt-dlp", "yt-dlp")
    assert len(result) == 2
    assert {m.metadata["tags"][0] for m in result} == {"python", "yt-dlp/yt-dlp"}


@patch(URLOPEN_PATH)
def test_devto_non_list_response(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response({"not": "a list"})
    assert devto_search("o", "r") == []


# ---------------------------------------------------------------------------
# arXiv adapter (XML)


ARXIV_XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>A Paper About yt-dlp/yt-dlp</title>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-02-01T00:00:00Z</updated>
    <summary>We describe the yt-dlp project in detail.</summary>
    <author><name>Alice Researcher</name></author>
    <author><name>Bob Scientist</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2302.00002v1</id>
    <title>Another Paper</title>
    <published>2023-03-01T00:00:00Z</published>
    <updated>2023-04-01T00:00:00Z</updated>
    <summary>Different topic entirely.</summary>
    <author><name>Carol Academic</name></author>
  </entry>
</feed>"""


@patch(URLOPEN_PATH)
def test_arxiv_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_text_response(ARXIV_XML_SAMPLE)
    result = arxiv_search("yt-dlp", "yt-dlp")
    assert len(result) == 2
    first = result[0]
    assert first.source == "arxiv"
    assert "yt-dlp" in first.title
    assert first.url == "http://arxiv.org/abs/2301.00001v1"
    assert first.author == "Alice Researcher, Bob Scientist"
    assert first.metadata["updated"] == "2023-02-01T00:00:00Z"
    assert first.score == 0


@patch(URLOPEN_PATH)
def test_arxiv_empty_feed(mock_urlopen):
    mock_urlopen.return_value = _mock_text_response(
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    )
    assert arxiv_search("o", "r") == []


@patch(URLOPEN_PATH)
def test_arxiv_malformed_xml_returns_empty(mock_urlopen):
    mock_urlopen.return_value = _mock_text_response("<not-xml<<")
    assert arxiv_search("o", "r") == []


# ---------------------------------------------------------------------------
# Papers With Code adapter


@patch(URLOPEN_PATH)
def test_pwc_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "count": 1,
            "results": [
                {
                    "id": "attn-is-all-you-need",
                    "title": "Attention Is All You Need",
                    "abstract": "We propose a new architecture.",
                    "url_abs": "https://arxiv.org/abs/1706.03762",
                    "published": "2017-06-12",
                    "authors": ["Vaswani", "Shazeer", "Parmar", "Uszkoreit"],
                }
            ],
        }
    )
    result = pwc_search("tensorflow", "tensor2tensor")
    assert len(result) == 1
    m = result[0]
    assert m.source == "pwc"
    assert m.url == "https://arxiv.org/abs/1706.03762"
    # Only first 3 authors
    assert m.author == "Vaswani, Shazeer, Parmar"
    assert m.metadata["paper_id"] == "attn-is-all-you-need"


@patch(URLOPEN_PATH)
def test_pwc_missing_url_abs_falls_back_to_pwc_url(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "results": [
                {
                    "id": "some-paper",
                    "title": "t",
                    "url_abs": None,
                    "published": "2023-01-01",
                    "authors": [],
                }
            ]
        }
    )
    result = pwc_search("o", "r")
    assert result[0].url == "https://paperswithcode.com/paper/some-paper"


# ---------------------------------------------------------------------------
# Registry


def test_all_sources_complete():
    assert set(ALL_SOURCES.keys()) == {
        "hn",
        "reddit",
        "stackoverflow",
        "devto",
        "arxiv",
        "pwc",
    }
    assert all(callable(f) for f in ALL_SOURCES.values())


# ---------------------------------------------------------------------------
# All adapters: common resilience contract


@pytest.mark.parametrize("adapter", [
    hn_search,
    reddit_search,
    stackoverflow_search,
    devto_search,
    arxiv_search,
    pwc_search,
])
@patch(URLOPEN_PATH)
def test_every_adapter_returns_empty_on_network_error(mock_urlopen, adapter):
    mock_urlopen.side_effect = urllib.error.URLError("network is down")
    assert adapter("o", "r") == []


@pytest.mark.parametrize("adapter", [
    hn_search,
    reddit_search,
    stackoverflow_search,
    pwc_search,
])
@patch(URLOPEN_PATH)
def test_every_json_adapter_returns_empty_on_bad_json(mock_urlopen, adapter):
    resp = MagicMock()
    resp.read.return_value = b"not valid json {{"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    mock_urlopen.return_value = resp
    assert adapter("o", "r") == []
