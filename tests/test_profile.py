"""Tests for X/Twitter profile scraper."""

from unittest.mock import patch

import pytest

from scraperx.profile import (
    XProfile,
    get_profile,
    parse_profile_url,
)

# --- Mock data ---

FXTWITTER_PROFILE_RESPONSE = {
    "code": 200,
    "message": "OK",
    "user": {
        "screen_name": "testuser",
        "id": "123456",
        "name": "Test User",
        "description": "Hello I am a test user",
        "followers": 5000,
        "following": 200,
        "tweets": 1234,
        "likes": 9876,
        "media_count": 50,
        "location": "Internet",
        "banner_url": "https://pbs.twimg.com/banner.jpg",
        "avatar_url": "https://pbs.twimg.com/avatar.jpg",
        "joined": "Mon Jan 01 00:00:00 +0000 2020",
        "protected": False,
        "website": "https://example.com",
        "verification": {"verified": True, "type": "blue"},
    },
}


# --- Profile fetch ---


class TestGetProfile:
    @patch("scraperx.profile._http_get_json")
    def test_success(self, mock_get):
        mock_get.return_value = FXTWITTER_PROFILE_RESPONSE
        profile = get_profile("testuser")

        assert profile.handle == "testuser"
        assert profile.name == "Test User"
        assert profile.bio == "Hello I am a test user"
        assert profile.followers == 5000
        assert profile.following == 200
        assert profile.tweets_count == 1234
        assert profile.likes_count == 9876
        assert profile.joined == "Mon Jan 01 00:00:00 +0000 2020"
        assert profile.location == "Internet"
        assert profile.avatar_url == "https://pbs.twimg.com/avatar.jpg"
        assert profile.banner_url == "https://pbs.twimg.com/banner.jpg"
        assert profile.website == "https://example.com"
        assert profile.verified is True
        assert profile.source_method == "fxtwitter"
        assert profile.raw == FXTWITTER_PROFILE_RESPONSE

        mock_get.assert_called_once_with("https://api.fxtwitter.com/testuser", 15)

    @patch("scraperx.profile._http_get_json")
    def test_error_code_404(self, mock_get):
        mock_get.return_value = {"code": 404, "message": "User not found"}
        with pytest.raises(ValueError, match="404"):
            get_profile("nonexistent")

    @patch("scraperx.profile._http_get_json")
    def test_handle_strips_at_prefix(self, mock_get):
        mock_get.return_value = FXTWITTER_PROFILE_RESPONSE
        get_profile("@testuser")
        mock_get.assert_called_once_with("https://api.fxtwitter.com/testuser", 15)

    @patch("scraperx.profile._http_get_json")
    def test_custom_timeout(self, mock_get):
        mock_get.return_value = FXTWITTER_PROFILE_RESPONSE
        get_profile("testuser", timeout=30)
        mock_get.assert_called_once_with("https://api.fxtwitter.com/testuser", 30)

    @patch("scraperx.profile._http_get_json")
    def test_no_verification_field(self, mock_get):
        """Profile without verification data defaults to verified=False."""
        resp = {
            "code": 200,
            "message": "OK",
            "user": {
                "screen_name": "nocheck",
                "name": "No Check",
                "description": "",
                "followers": 10,
                "following": 5,
                "tweets": 1,
                "likes": 0,
            },
        }
        mock_get.return_value = resp
        profile = get_profile("nocheck")
        assert profile.verified is False
        assert profile.website is None


# --- URL parsing ---


class TestParseProfileUrl:
    def test_x_com(self):
        assert parse_profile_url("https://x.com/elonmusk") == "elonmusk"

    def test_twitter_com(self):
        assert parse_profile_url("https://twitter.com/jack") == "jack"

    def test_trailing_slash(self):
        assert parse_profile_url("https://x.com/vitalikbuterin/") == "vitalikbuterin"

    def test_no_protocol(self):
        assert parse_profile_url("x.com/solana") == "solana"

    def test_http(self):
        assert parse_profile_url("http://twitter.com/user123") == "user123"

    def test_rejects_tweet_url(self):
        """Profile URL regex must NOT match tweet URLs (those have /status/)."""
        with pytest.raises(ValueError, match="Not a valid profile URL"):
            parse_profile_url("https://x.com/user/status/123456")

    def test_rejects_invalid(self):
        with pytest.raises(ValueError, match="Not a valid profile URL"):
            parse_profile_url("https://google.com/foo")


# --- XProfile dataclass ---


class TestXProfileDefaults:
    def test_minimal(self):
        p = XProfile(handle="test")
        assert p.handle == "test"
        assert p.name == ""
        assert p.bio == ""
        assert p.followers == 0
        assert p.following == 0
        assert p.tweets_count == 0
        assert p.likes_count == 0
        assert p.joined == ""
        assert p.location == ""
        assert p.avatar_url == ""
        assert p.banner_url == ""
        assert p.website is None
        assert p.verified is False
        assert p.source_method == "fxtwitter"
        assert p.raw == {}
