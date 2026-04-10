"""Tests for thread scraping module."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from scraperx.thread import Thread, get_thread, _get_parent_id
from scraperx.scraper import Tweet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fxtwitter_response(
    tweet_id: str,
    user: str = "testuser",
    text: str = "Hello",
    replying_to: str | None = None,
) -> dict:
    """Build a minimal FxTwitter API response."""
    tweet_data = {
        "id": tweet_id,
        "text": text,
        "author": {"name": "Test User", "screen_name": user},
        "likes": 10,
        "retweets": 2,
        "replies": 1,
        "views": 100,
    }
    if replying_to:
        tweet_data["replying_to"] = replying_to
    return {"code": 200, "tweet": tweet_data}


# ---------------------------------------------------------------------------
# Thread dataclass tests
# ---------------------------------------------------------------------------

class TestThreadDefaults:
    def test_single_tweet_defaults(self):
        tweet = Tweet(id="1", text="hi", author="A", author_handle="a")
        thread = Thread(root_tweet=tweet)
        assert thread.total_tweets == 1
        assert thread.replies == []
        assert thread.all_tweets == [tweet]

    def test_total_tweets_auto_calculated(self):
        root = Tweet(id="1", text="root", author="A", author_handle="a")
        r1 = Tweet(id="2", text="reply", author="A", author_handle="a")
        r2 = Tweet(id="3", text="reply2", author="A", author_handle="a")
        thread = Thread(root_tweet=root, replies=[r1, r2])
        assert thread.total_tweets == 3
        assert thread.all_tweets == [root, r1, r2]

    def test_explicit_total_tweets_preserved(self):
        tweet = Tweet(id="1", text="hi", author="A", author_handle="a")
        thread = Thread(root_tweet=tweet, total_tweets=5)
        assert thread.total_tweets == 5


# ---------------------------------------------------------------------------
# _get_parent_id tests
# ---------------------------------------------------------------------------

class TestGetParentId:
    def test_replying_to_field(self):
        assert _get_parent_id({"replying_to": "999"}) == "999"

    def test_in_reply_to_status_id(self):
        assert _get_parent_id({"in_reply_to_status_id": 888}) == "888"

    def test_nested_replying_to_status(self):
        assert _get_parent_id({"replying_to_status": {"id": "777"}}) == "777"

    def test_no_parent(self):
        assert _get_parent_id({}) is None

    def test_empty_replying_to(self):
        assert _get_parent_id({"replying_to": ""}) is None


# ---------------------------------------------------------------------------
# get_thread tests
# ---------------------------------------------------------------------------

class TestGetThread:
    @patch("scraperx.thread._http_get_json")
    def test_single_tweet_not_a_thread(self, mock_get):
        """A standalone tweet returns Thread with 1 tweet."""
        mock_get.return_value = _make_fxtwitter_response("123", text="standalone")

        thread = get_thread("https://x.com/testuser/status/123", walk_down=False)

        assert thread.total_tweets == 1
        assert thread.root_tweet.id == "123"
        assert thread.root_tweet.text == "standalone"
        assert thread.replies == []
        mock_get.assert_called_once()

    @patch("scraperx.thread._http_get_json")
    def test_thread_with_three_tweets(self, mock_get):
        """Thread of 3 self-replies: walk up from tweet 3 -> 2 -> 1 (root)."""
        mock_get.side_effect = [
            _make_fxtwitter_response("3", text="third", replying_to="2"),
            _make_fxtwitter_response("2", text="second", replying_to="1"),
            _make_fxtwitter_response("1", text="first"),
        ]

        thread = get_thread("https://x.com/testuser/status/3", walk_down=False)

        assert thread.total_tweets == 3
        assert thread.root_tweet.id == "1"
        assert thread.root_tweet.text == "first"
        assert len(thread.replies) == 2
        assert thread.replies[0].id == "2"
        assert thread.replies[1].id == "3"
        assert [t.id for t in thread.all_tweets] == ["1", "2", "3"]
        assert mock_get.call_count == 3

    @patch("scraperx.thread._http_get_json")
    def test_max_depth_limit(self, mock_get):
        """Walk-up stops at max_depth even if more parents exist."""
        mock_get.side_effect = [
            _make_fxtwitter_response("3", text="d3", replying_to="2"),
            _make_fxtwitter_response("2", text="d2", replying_to="1"),
        ]

        thread = get_thread("https://x.com/testuser/status/3", max_depth=1)

        assert thread.total_tweets == 2
        assert thread.root_tweet.id == "2"
        assert len(thread.replies) == 1
        assert thread.replies[0].id == "3"
        assert mock_get.call_count == 2

    @patch("scraperx.thread._http_get_json")
    def test_stops_at_different_author(self, mock_get):
        """Walk-up stops when parent is by a different author."""
        mock_get.side_effect = [
            _make_fxtwitter_response("2", user="alice", text="reply", replying_to="1"),
            _make_fxtwitter_response("1", user="bob", text="original"),
        ]

        thread = get_thread("https://x.com/alice/status/2")

        assert thread.total_tweets == 1
        assert thread.root_tweet.id == "2"
        assert thread.replies == []

    @patch("scraperx.thread._http_get_json")
    def test_parent_fetch_failure_graceful(self, mock_get):
        """If parent fetch fails, return what we have."""
        mock_get.side_effect = [
            _make_fxtwitter_response("2", text="reply", replying_to="1"),
            Exception("Network error"),
        ]

        thread = get_thread("https://x.com/testuser/status/2")

        assert thread.total_tweets == 1
        assert thread.root_tweet.id == "2"

    @patch("scraperx.thread._http_get_json")
    def test_initial_fetch_failure_raises(self, mock_get):
        """If initial tweet fetch fails, raise RuntimeError."""
        mock_get.side_effect = Exception("Network error")

        with pytest.raises(RuntimeError, match="Failed to fetch tweet"):
            get_thread("https://x.com/testuser/status/1")

    def test_invalid_url_raises(self):
        """Invalid URL raises ValueError."""
        with pytest.raises(ValueError, match="Not a valid tweet URL"):
            get_thread("https://example.com/not-a-tweet")
