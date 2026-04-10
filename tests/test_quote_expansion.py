"""Tests for automatic quoted tweet and article expansion."""
import json
from unittest.mock import patch

import pytest

from scraperx.scraper import (
    XScraper,
    Tweet,
    _parse_quoted_tweet,
    _extract_article,
    _MAX_QUOTE_DEPTH,
)


# --- Fixtures: FxTwitter API responses ---

def _make_fxtwitter_response(tweet_data: dict) -> dict:
    return {"code": 200, "tweet": tweet_data}


SIMPLE_TWEET = {
    "text": "Just a normal tweet",
    "author": {"name": "Alice", "screen_name": "alice", "avatar_url": "", "id": "1"},
    "likes": 10,
    "retweets": 2,
    "replies": 1,
    "views": 500,
}

QUOTED_TWEET_DATA = {
    "url": "https://x.com/bob/status/200",
    "id": "200",
    "text": "I am the quoted tweet",
    "author": {"name": "Bob", "screen_name": "bob", "avatar_url": "", "id": "2"},
    "likes": 50,
    "retweets": 10,
    "replies": 3,
    "views": 2000,
}

TWEET_WITH_QUOTE = {
    **SIMPLE_TWEET,
    "text": "Check this out",
    "quote": QUOTED_TWEET_DATA,
}

ARTICLE_DATA = {
    "title": "How We Topped the Challenge",
    "preview_text": "TLDR summary",
    "content": {
        "blocks": [
            {"text": "First paragraph of the article."},
            {"text": "Second paragraph with details."},
            {"text": "Third paragraph conclusion."},
        ]
    },
}

QUOTED_WITH_ARTICLE = {
    **QUOTED_TWEET_DATA,
    "id": "300",
    "text": "Read our article",
    "article": ARTICLE_DATA,
}

TWEET_WITH_QUOTE_ARTICLE = {
    **SIMPLE_TWEET,
    "text": "Interesting read",
    "quote": QUOTED_WITH_ARTICLE,
}

# Nested: tweet quotes tweet which quotes tweet
INNERMOST_QUOTE = {
    "id": "100",
    "text": "I am the innermost",
    "author": {"name": "Charlie", "screen_name": "charlie", "avatar_url": "", "id": "3"},
    "likes": 5,
    "retweets": 1,
    "replies": 0,
    "views": 100,
}

MIDDLE_QUOTE = {
    "id": "200",
    "text": "I quote charlie",
    "author": {"name": "Bob", "screen_name": "bob", "avatar_url": "", "id": "2"},
    "likes": 20,
    "retweets": 3,
    "replies": 1,
    "views": 800,
    "quote": INNERMOST_QUOTE,
}

NESTED_TWEET = {
    **SIMPLE_TWEET,
    "text": "I quote bob who quotes charlie",
    "quote": MIDDLE_QUOTE,
}


# --- _extract_article ---

class TestExtractArticle:
    def test_no_article(self):
        title, text = _extract_article({})
        assert title is None
        assert text is None

    def test_article_with_blocks(self):
        title, text = _extract_article({"article": ARTICLE_DATA})
        assert title == "How We Topped the Challenge"
        assert "First paragraph" in text
        assert "Second paragraph" in text
        assert "Third paragraph" in text

    def test_article_preview_only(self):
        title, text = _extract_article({
            "article": {"title": "T", "preview_text": "Preview only"}
        })
        assert title == "T"
        assert text == "Preview only"

    def test_article_empty_blocks(self):
        title, text = _extract_article({
            "article": {
                "title": "T",
                "preview_text": "Fallback",
                "content": {"blocks": []},
            }
        })
        assert title == "T"
        assert text == "Fallback"


# --- _parse_quoted_tweet ---

class TestParseQuotedTweet:
    def test_basic_quote(self):
        qt = _parse_quoted_tweet(QUOTED_TWEET_DATA)
        assert qt.id == "200"
        assert qt.text == "I am the quoted tweet"
        assert qt.author == "Bob"
        assert qt.author_handle == "bob"
        assert qt.likes == 50
        assert qt.quoted_tweet is None

    def test_quote_with_article(self):
        qt = _parse_quoted_tweet(QUOTED_WITH_ARTICLE)
        assert qt.article_title == "How We Topped the Challenge"
        assert "First paragraph" in qt.article_text
        assert qt.quoted_tweet is None

    def test_nested_quotes(self):
        qt = _parse_quoted_tweet(MIDDLE_QUOTE)
        assert qt.text == "I quote charlie"
        assert qt.quoted_tweet is not None
        assert qt.quoted_tweet.text == "I am the innermost"
        assert qt.quoted_tweet.author_handle == "charlie"
        assert qt.quoted_tweet.quoted_tweet is None

    def test_article_text_used_when_no_text(self):
        data = {
            "id": "999",
            "text": "",
            "author": {"name": "X", "screen_name": "x"},
            "article": ARTICLE_DATA,
        }
        qt = _parse_quoted_tweet(data)
        assert "First paragraph" in qt.text


# --- Max recursion depth ---

class TestMaxRecursionDepth:
    def _build_deep_chain(self, depth: int) -> dict:
        """Build a chain of quotes `depth` levels deep."""
        current = {
            "id": "0",
            "text": f"Level 0",
            "author": {"name": "Deep", "screen_name": "deep"},
            "likes": 0, "retweets": 0, "replies": 0, "views": 0,
        }
        for i in range(1, depth + 1):
            current = {
                "id": str(i),
                "text": f"Level {i}",
                "author": {"name": "Deep", "screen_name": "deep"},
                "likes": 0, "retweets": 0, "replies": 0, "views": 0,
                "quote": current,
            }
        return current

    def test_respects_max_depth(self):
        # Build chain deeper than max
        chain = self._build_deep_chain(_MAX_QUOTE_DEPTH + 2)
        qt = _parse_quoted_tweet(chain, depth=0)

        # Walk down to count actual depth
        depth = 0
        current = qt
        while current.quoted_tweet is not None:
            depth += 1
            current = current.quoted_tweet

        assert depth == _MAX_QUOTE_DEPTH

    def test_depth_3_exactly(self):
        chain = self._build_deep_chain(3)
        qt = _parse_quoted_tweet(chain, depth=0)
        assert qt.quoted_tweet is not None
        assert qt.quoted_tweet.quoted_tweet is not None
        assert qt.quoted_tweet.quoted_tweet.quoted_tweet is not None
        assert qt.quoted_tweet.quoted_tweet.quoted_tweet.quoted_tweet is None


# --- FxTwitter integration ---

class TestFxTwitterQuoteExpansion:
    @patch("scraperx.scraper._http_get_json")
    def test_no_quote(self, mock_get):
        mock_get.return_value = _make_fxtwitter_response(SIMPLE_TWEET)
        scraper = XScraper()
        tweet = scraper._via_fxtwitter("alice", "100")
        assert tweet.quoted_tweet is None
        assert tweet.text == "Just a normal tweet"

    @patch("scraperx.scraper._http_get_json")
    def test_with_quote(self, mock_get):
        mock_get.return_value = _make_fxtwitter_response(TWEET_WITH_QUOTE)
        scraper = XScraper()
        tweet = scraper._via_fxtwitter("alice", "100")
        assert tweet.text == "Check this out"
        assert tweet.quoted_tweet is not None
        assert tweet.quoted_tweet.id == "200"
        assert tweet.quoted_tweet.text == "I am the quoted tweet"
        assert tweet.quoted_tweet.author_handle == "bob"

    @patch("scraperx.scraper._http_get_json")
    def test_quote_with_article(self, mock_get):
        mock_get.return_value = _make_fxtwitter_response(TWEET_WITH_QUOTE_ARTICLE)
        scraper = XScraper()
        tweet = scraper._via_fxtwitter("alice", "100")
        qt = tweet.quoted_tweet
        assert qt is not None
        assert qt.article_title == "How We Topped the Challenge"
        assert "First paragraph" in qt.article_text
        assert "Third paragraph" in qt.article_text

    @patch("scraperx.scraper._http_get_json")
    def test_nested_quotes(self, mock_get):
        mock_get.return_value = _make_fxtwitter_response(NESTED_TWEET)
        scraper = XScraper()
        tweet = scraper._via_fxtwitter("alice", "100")
        assert tweet.quoted_tweet is not None
        assert tweet.quoted_tweet.text == "I quote charlie"
        assert tweet.quoted_tweet.quoted_tweet is not None
        assert tweet.quoted_tweet.quoted_tweet.text == "I am the innermost"


# --- vxTwitter ---

class TestVxTwitterQuoteExpansion:
    @patch("scraperx.scraper._http_get_json")
    def test_vx_with_quote(self, mock_get):
        mock_get.return_value = {
            "text": "VX tweet with quote",
            "user_name": "VX User",
            "user_screen_name": "vxuser",
            "likes": 5, "retweets": 1, "replies": 0, "views": 100,
            "quote": QUOTED_TWEET_DATA,
        }
        scraper = XScraper()
        tweet = scraper._via_vxtwitter("vxuser", "456")
        assert tweet.quoted_tweet is not None
        assert tweet.quoted_tweet.text == "I am the quoted tweet"

    @patch("scraperx.scraper._http_get_json")
    def test_vx_no_quote(self, mock_get):
        mock_get.return_value = {
            "text": "VX plain",
            "user_name": "VX User",
            "user_screen_name": "vxuser",
            "likes": 5, "retweets": 1, "replies": 0, "views": 100,
        }
        scraper = XScraper()
        tweet = scraper._via_vxtwitter("vxuser", "456")
        assert tweet.quoted_tweet is None


# --- JSON serialization ---

class TestJsonSerialization:
    """Test that tweets with quoted_tweet serialize properly via _tweet_to_dict."""

    def test_simple_serialization(self):
        from scraperx.__main__ import _tweet_to_dict

        tweet = Tweet(
            id="1", text="hello", author="A", author_handle="a",
            quoted_tweet=Tweet(
                id="2", text="quoted", author="B", author_handle="b",
                article_title="Art", article_text="Full text",
            ),
        )
        d = _tweet_to_dict(tweet)
        assert d["quoted_tweet"]["id"] == "2"
        assert d["quoted_tweet"]["text"] == "quoted"
        assert d["quoted_tweet"]["article_title"] == "Art"
        assert d["quoted_tweet"]["article_text"] == "Full text"

    def test_nested_serialization(self):
        from scraperx.__main__ import _tweet_to_dict

        tweet = Tweet(
            id="1", text="top", author="A", author_handle="a",
            quoted_tweet=Tweet(
                id="2", text="mid", author="B", author_handle="b",
                quoted_tweet=Tweet(
                    id="3", text="bottom", author="C", author_handle="c",
                ),
            ),
        )
        d = _tweet_to_dict(tweet)
        assert d["quoted_tweet"]["quoted_tweet"]["id"] == "3"
        # Verify it's valid JSON
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["quoted_tweet"]["quoted_tweet"]["text"] == "bottom"

    def test_no_quote_no_key(self):
        from scraperx.__main__ import _tweet_to_dict

        tweet = Tweet(id="1", text="solo", author="A", author_handle="a")
        d = _tweet_to_dict(tweet)
        assert "quoted_tweet" not in d


# --- Tweet dataclass ---

class TestTweetDataclassQuoteField:
    def test_default_none(self):
        t = Tweet(id="1", text="hi", author="A", author_handle="a")
        assert t.quoted_tweet is None

    def test_with_quoted_tweet(self):
        inner = Tweet(id="2", text="inner", author="B", author_handle="b")
        outer = Tweet(id="1", text="outer", author="A", author_handle="a", quoted_tweet=inner)
        assert outer.quoted_tweet is inner
        assert outer.quoted_tweet.text == "inner"
