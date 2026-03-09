"""Tests for twscrape_backend — fully mocked, does NOT require twscrape installed."""
from __future__ import annotations

import sys
import types
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: create a fake twscrape module so we can import twscrape_backend
# regardless of whether twscrape is actually installed.
# ---------------------------------------------------------------------------

def _make_fake_twscrape_module():
    """Create a minimal fake twscrape module with API and gather."""
    mod = types.ModuleType("twscrape")
    mod.API = MagicMock  # type: ignore[attr-defined]
    mod.gather = AsyncMock()  # type: ignore[attr-defined]
    return mod


@pytest.fixture(autouse=True)
def _patch_twscrape_import():
    """Ensure twscrape_backend can be imported with a fake twscrape module."""
    fake = _make_fake_twscrape_module()
    with mock.patch.dict(sys.modules, {"twscrape": fake}):
        # Force reimport of the backend module with our fake
        import importlib
        if "scraperx.twscrape_backend" in sys.modules:
            del sys.modules["scraperx.twscrape_backend"]
        # Patch the TWSCRAPE_AVAILABLE flag after import
        import scraperx.twscrape_backend as backend_mod
        backend_mod.TWSCRAPE_AVAILABLE = True
        backend_mod.API = fake.API
        backend_mod.gather = fake.gather
        yield backend_mod


# ---------------------------------------------------------------------------
# Tests: has_twscrape()
# ---------------------------------------------------------------------------

class TestHasTwscrape:
    def test_returns_true_when_available(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        mod.TWSCRAPE_AVAILABLE = True
        assert mod.has_twscrape() is True

    def test_returns_false_when_unavailable(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        mod.TWSCRAPE_AVAILABLE = False
        assert mod.has_twscrape() is False


# ---------------------------------------------------------------------------
# Tests: _tw_to_tweet conversion
# ---------------------------------------------------------------------------

class TestTwToTweet:
    def test_basic_conversion(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        tw = MagicMock()
        tw.id = 1234567890
        tw.rawContent = "Hello world"
        tw.user = MagicMock()
        tw.user.displayname = "Test User"
        tw.user.username = "testuser"
        tw.likeCount = 42
        tw.retweetCount = 5
        tw.replyCount = 3
        tw.viewCount = 1000
        tw.media = None

        tweet = mod._tw_to_tweet(tw)

        assert tweet.id == "1234567890"
        assert tweet.text == "Hello world"
        assert tweet.author == "Test User"
        assert tweet.author_handle == "testuser"
        assert tweet.likes == 42
        assert tweet.retweets == 5
        assert tweet.replies == 3
        assert tweet.views == 1000
        assert tweet.source_method == "twscrape"
        assert tweet.media_urls == []

    def test_with_photo_media_dict(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        tw = MagicMock()
        tw.id = 999
        tw.rawContent = "Photo tweet"
        tw.user = MagicMock()
        tw.user.displayname = "Photog"
        tw.user.username = "photog"
        tw.likeCount = 10
        tw.retweetCount = 0
        tw.replyCount = 0
        tw.viewCount = 50
        tw.media = {
            "photos": [
                {"url": "https://pbs.twimg.com/media/abc.jpg"},
                {"url": "https://pbs.twimg.com/media/def.jpg"},
            ],
            "videos": [],
        }

        tweet = mod._tw_to_tweet(tw)

        assert len(tweet.media_urls) == 2
        assert "abc.jpg" in tweet.media_urls[0]

    def test_with_video_media_dict(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        tw = MagicMock()
        tw.id = 888
        tw.rawContent = "Video tweet"
        tw.user = MagicMock()
        tw.user.displayname = "Vlogger"
        tw.user.username = "vlogger"
        tw.likeCount = 0
        tw.retweetCount = 0
        tw.replyCount = 0
        tw.viewCount = 0
        tw.media = {
            "photos": [],
            "videos": [{
                "variants": [
                    {"contentType": "video/mp4", "bitrate": 256000, "url": "https://video.twimg.com/low.mp4"},
                    {"contentType": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/high.mp4"},
                    {"contentType": "application/x-mpegURL", "url": "https://video.twimg.com/playlist.m3u8"},
                ],
            }],
        }

        tweet = mod._tw_to_tweet(tw)

        assert len(tweet.media_urls) == 1
        assert "high.mp4" in tweet.media_urls[0]

    def test_no_user(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        tw = MagicMock()
        tw.id = 777
        tw.rawContent = "Orphan tweet"
        tw.user = None
        tw.likeCount = 0
        tw.retweetCount = 0
        tw.replyCount = 0
        tw.viewCount = 0
        tw.media = None

        tweet = mod._tw_to_tweet(tw)

        assert tweet.author == ""
        assert tweet.author_handle == ""

    def test_fallback_to_text_attr(self, _patch_twscrape_import):
        """If rawContent is missing, fall back to .text attribute."""
        mod = _patch_twscrape_import
        tw = MagicMock(spec=[])  # no auto-attributes
        tw.id = 666
        tw.rawContent = ""
        tw.text = "Fallback text"
        tw.user = MagicMock()
        tw.user.displayname = "User"
        tw.user.username = "user"
        tw.likeCount = 0
        tw.retweetCount = 0
        tw.replyCount = 0
        tw.viewCount = 0
        tw.media = None

        tweet = mod._tw_to_tweet(tw)

        assert tweet.text == "Fallback text"


# ---------------------------------------------------------------------------
# Tests: TwscrapeBackend
# ---------------------------------------------------------------------------

class TestTwscrapeBackend:
    def test_raises_import_error_when_not_available(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        mod.TWSCRAPE_AVAILABLE = False

        with pytest.raises(ImportError, match="twscrape is not installed"):
            mod.TwscrapeBackend()

    def test_init_creates_api(self, _patch_twscrape_import):
        mod = _patch_twscrape_import
        mock_api_cls = MagicMock()
        mod.API = mock_api_cls

        backend = mod.TwscrapeBackend(db_path="test.db")

        mock_api_cls.assert_called_once_with("test.db")
        assert backend._api is mock_api_cls.return_value

    def test_get_tweet(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_tw = MagicMock()
        mock_tw.id = 12345
        mock_tw.rawContent = "Test tweet content"
        mock_tw.user = MagicMock()
        mock_tw.user.displayname = "Tester"
        mock_tw.user.username = "tester"
        mock_tw.likeCount = 7
        mock_tw.retweetCount = 2
        mock_tw.replyCount = 1
        mock_tw.viewCount = 500
        mock_tw.media = None

        mock_api = MagicMock()
        mock_api.tweet_details = AsyncMock(return_value=mock_tw)
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        tweet = backend.get_tweet("12345")

        assert tweet.id == "12345"
        assert tweet.text == "Test tweet content"
        assert tweet.author == "Tester"
        assert tweet.likes == 7

    def test_get_tweet_not_found(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_api = MagicMock()
        mock_api.tweet_details = AsyncMock(return_value=None)
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()

        with pytest.raises(ValueError, match="not found"):
            backend.get_tweet("99999")

    def test_get_profile(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_user = MagicMock()
        mock_user.dict.return_value = {
            "id": 123,
            "username": "testuser",
            "displayname": "Test User",
        }

        mock_api = MagicMock()
        mock_api.user_by_login = AsyncMock(return_value=mock_user)
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        profile = backend.get_profile("testuser")

        assert profile["username"] == "testuser"
        assert profile["displayname"] == "Test User"

    def test_get_profile_not_found(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_api = MagicMock()
        mock_api.user_by_login = AsyncMock(return_value=None)
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()

        with pytest.raises(ValueError, match="not found"):
            backend.get_profile("nonexistent")

    def test_search(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_tw1 = MagicMock()
        mock_tw1.id = 1
        mock_tw1.rawContent = "Result 1"
        mock_tw1.user = MagicMock()
        mock_tw1.user.displayname = "User1"
        mock_tw1.user.username = "user1"
        mock_tw1.likeCount = 1
        mock_tw1.retweetCount = 0
        mock_tw1.replyCount = 0
        mock_tw1.viewCount = 10
        mock_tw1.media = None

        mock_tw2 = MagicMock()
        mock_tw2.id = 2
        mock_tw2.rawContent = "Result 2"
        mock_tw2.user = MagicMock()
        mock_tw2.user.displayname = "User2"
        mock_tw2.user.username = "user2"
        mock_tw2.likeCount = 5
        mock_tw2.retweetCount = 0
        mock_tw2.replyCount = 0
        mock_tw2.viewCount = 20
        mock_tw2.media = None

        mock_api = MagicMock()
        mod.gather = AsyncMock(return_value=[mock_tw1, mock_tw2])
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        results = backend.search("solana", limit=10)

        assert len(results) == 2
        assert results[0].text == "Result 1"
        assert results[1].text == "Result 2"

    def test_is_configured_true(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_api = MagicMock()
        mock_api.pool.accounts_info = AsyncMock(return_value=[{"user": "test"}])
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        assert backend.is_configured() is True

    def test_is_configured_false_empty(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_api = MagicMock()
        mock_api.pool.accounts_info = AsyncMock(return_value=[])
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        assert backend.is_configured() is False

    def test_is_configured_handles_error(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_api = MagicMock()
        mock_api.pool.accounts_info = AsyncMock(side_effect=RuntimeError("db error"))
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        assert backend.is_configured() is False

    def test_get_user_tweets(self, _patch_twscrape_import):
        mod = _patch_twscrape_import

        mock_user = MagicMock()
        mock_user.id = 42

        mock_tw = MagicMock()
        mock_tw.id = 100
        mock_tw.rawContent = "User tweet"
        mock_tw.user = MagicMock()
        mock_tw.user.displayname = "Timeline"
        mock_tw.user.username = "timeline"
        mock_tw.likeCount = 0
        mock_tw.retweetCount = 0
        mock_tw.replyCount = 0
        mock_tw.viewCount = 0
        mock_tw.media = None

        mock_api = MagicMock()
        mock_api.user_by_login = AsyncMock(return_value=mock_user)
        mod.gather = AsyncMock(return_value=[mock_tw])
        mod.API = MagicMock(return_value=mock_api)

        backend = mod.TwscrapeBackend()
        results = backend.get_user_tweets("timeline", limit=5)

        assert len(results) == 1
        assert results[0].text == "User tweet"
