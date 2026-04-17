"""Tests for X/Twitter scraper."""

from unittest.mock import patch

import pytest

from scraperx.scraper import Tweet, TweetNotFoundError, XScraper, _best_media_url, _strip_html, parse_tweet_url

# --- URL parsing ---


class TestParseUrl:
    def test_x_com(self):
        assert parse_tweet_url("https://x.com/user/status/123") == ("user", "123")

    def test_twitter_com(self):
        assert parse_tweet_url("https://twitter.com/foo/status/999") == ("foo", "999")

    def test_fxtwitter(self):
        assert parse_tweet_url("https://fxtwitter.com/bar/status/42") == ("bar", "42")

    def test_fixupx(self):
        assert parse_tweet_url("https://fixupx.com/baz/status/7") == ("baz", "7")

    def test_invalid(self):
        with pytest.raises(ValueError, match="Not a valid"):
            parse_tweet_url("https://google.com/foo")


# --- FxTwitter method ---

FXTWITTER_RESPONSE = {
    "code": 200,
    "tweet": {
        "text": "Hello world",
        "author": {"name": "Test User", "screen_name": "testuser"},
        "likes": 10,
        "retweets": 5,
        "replies": 2,
        "views": 100,
        "media": {"all": [{"url": "https://pbs.twimg.com/img.jpg"}]},
    },
}


class TestFxTwitter:
    @patch("scraperx.scraper._http_get_json")
    def test_success(self, mock_get):
        mock_get.return_value = FXTWITTER_RESPONSE
        scraper = XScraper()
        tweet = scraper._via_fxtwitter("testuser", "123")
        assert tweet.text == "Hello world"
        assert tweet.author == "Test User"
        assert tweet.likes == 10
        assert len(tweet.media_urls) == 1

    @patch("scraperx.scraper._http_get_json")
    def test_error_code(self, mock_get):
        mock_get.return_value = {"code": 404, "message": "Not found"}
        scraper = XScraper()
        with pytest.raises(ValueError, match="404"):
            scraper._via_fxtwitter("testuser", "123")


# --- vxTwitter method ---

VXTWITTER_RESPONSE = {
    "text": "Hello vx",
    "user_name": "VX User",
    "user_screen_name": "vxuser",
    "likes": 3,
    "retweets": 1,
    "replies": 0,
    "views": 50,
    "mediaURLs": ["https://pbs.twimg.com/vx.jpg"],
}


class TestVxTwitter:
    @patch("scraperx.scraper._http_get_json")
    def test_success(self, mock_get):
        mock_get.return_value = VXTWITTER_RESPONSE
        scraper = XScraper()
        tweet = scraper._via_vxtwitter("vxuser", "456")
        assert tweet.text == "Hello vx"
        assert tweet.author_handle == "vxuser"
        assert tweet.media_urls == ["https://pbs.twimg.com/vx.jpg"]


# --- Fallback chain ---


class TestFallbackChain:
    @patch("scraperx.scraper._http_get_json")
    def test_fxtwitter_first(self, mock_get):
        mock_get.return_value = FXTWITTER_RESPONSE
        scraper = XScraper()
        tweet = scraper.get_tweet("https://x.com/testuser/status/123")
        assert tweet.source_method == "fxtwitter"

    @patch.object(XScraper, "_via_oembed", side_effect=RuntimeError("oembed down"))
    @patch.object(XScraper, "_via_ytdlp", side_effect=RuntimeError("no yt-dlp"))
    @patch.object(XScraper, "_via_vxtwitter")
    @patch.object(XScraper, "_via_fxtwitter", side_effect=Exception("fx down"))
    def test_fallback_to_vx(self, mock_fx, mock_vx, mock_yt, mock_oe):
        mock_vx.return_value = Tweet(id="789", text="fallback", author="u", author_handle="u")
        scraper = XScraper()
        tweet = scraper.get_tweet("https://x.com/u/status/789")
        assert tweet.source_method == "vxtwitter"

    @patch.object(XScraper, "_via_oembed", side_effect=RuntimeError("fail"))
    @patch.object(XScraper, "_via_ytdlp", side_effect=RuntimeError("fail"))
    @patch.object(XScraper, "_via_vxtwitter", side_effect=RuntimeError("fail"))
    @patch.object(XScraper, "_via_fxtwitter", side_effect=RuntimeError("fail"))
    def test_all_fail(self, *mocks):
        scraper = XScraper()
        with pytest.raises(RuntimeError, match="All scraping methods failed"):
            scraper.get_tweet("https://x.com/u/status/1")

    @patch.object(XScraper, "_via_oembed", side_effect=RuntimeError("HTTP Error 404"))
    @patch.object(XScraper, "_via_ytdlp", side_effect=RuntimeError("yt-dlp is not installed"))
    @patch.object(XScraper, "_via_vxtwitter", side_effect=RuntimeError("not found 404"))
    @patch.object(XScraper, "_via_fxtwitter", side_effect=ValueError("FxTwitter returned code 404"))
    def test_all_404_raises_tweet_not_found(self, *mocks):
        scraper = XScraper()
        with pytest.raises(TweetNotFoundError, match="not found"):
            scraper.get_tweet("https://x.com/u/status/1")


# --- Tweet dataclass ---


class TestTweet:
    def test_defaults(self):
        t = Tweet(id="1", text="hi", author="A", author_handle="a")
        assert t.likes == 0
        assert t.media_urls == []
        assert t.source_method == ""


# --- oembed method ---

OEMBED_RESPONSE = {
    "author_name": "Oembed User",
    "author_url": "https://twitter.com/oembeduser",
    "html": "<blockquote><p>This is the tweet text</p>&mdash; Oembed User</blockquote>",
}


class TestOembed:
    @patch("scraperx.scraper._http_get_json")
    def test_success(self, mock_get):
        mock_get.return_value = OEMBED_RESPONSE
        scraper = XScraper()
        tweet = scraper._via_oembed("oembeduser", "999")
        assert "tweet text" in tweet.text
        assert tweet.author == "Oembed User"
        assert tweet.author_handle == "oembeduser"
        assert tweet.likes == 0  # oembed has no engagement stats

    @patch("scraperx.scraper._http_get_json")
    def test_html_parsing(self, mock_get):
        mock_get.return_value = {
            "author_name": "Test",
            "author_url": "https://twitter.com/test",
            "html": "<p>Hello <b>bold</b> &amp; world</p>",
        }
        scraper = XScraper()
        tweet = scraper._via_oembed("test", "1")
        assert "Hello" in tweet.text
        assert "bold" in tweet.text


# --- Media quality ---


class TestMediaQuality:
    def test_video_highest_bitrate(self):
        media = {
            "type": "video",
            "url": "https://video.twimg.com/low.mp4",
            "variants": [
                {"url": "https://video.twimg.com/low.mp4", "bitrate": 832000},
                {"url": "https://video.twimg.com/high.mp4", "bitrate": 2176000},
                {"url": "https://video.twimg.com/med.mp4", "bitrate": 1280000},
            ],
        }
        assert _best_media_url(media) == "https://video.twimg.com/high.mp4"

    def test_photo_large_suffix(self):
        media = {
            "type": "photo",
            "url": "https://pbs.twimg.com/media/abc123.jpg",
        }
        assert _best_media_url(media).endswith(":large")

    def test_photo_already_large(self):
        media = {
            "type": "photo",
            "url": "https://pbs.twimg.com/media/abc123.jpg:large",
        }
        assert _best_media_url(media) == "https://pbs.twimg.com/media/abc123.jpg:large"

    def test_no_variants_fallback(self):
        media = {"url": "https://example.com/video.mp4"}
        assert _best_media_url(media) == "https://example.com/video.mp4"

    def test_thumbnail_fallback(self):
        media = {"thumbnail_url": "https://pbs.twimg.com/thumb.jpg"}
        result = _best_media_url(media)
        assert "thumb.jpg" in result


# --- Strip HTML ---


class TestStripHtml:
    def test_basic(self):
        assert _strip_html("<p>Hello</p>") == "Hello"

    def test_nested(self):
        assert "bold" in _strip_html("<p>Hello <b>bold</b> world</p>")

    def test_empty(self):
        assert _strip_html("") == ""
