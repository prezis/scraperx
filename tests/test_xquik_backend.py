"""Tests for the optional Xquik backend."""

from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

import scraperx.xquik_backend as backend_mod
from scraperx.xquik_backend import XquikBackend


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_search_maps_xquik_tweets(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.headers["X-api-key"]
        seen["timeout"] = timeout
        return _Response(
            {
                "tweets": [
                    {
                        "id": "123",
                        "text": "hello",
                        "createdAt": "2026-01-02T03:04:05Z",
                        "likeCount": 7,
                        "retweetCount": 2,
                        "replyCount": 1,
                        "viewCount": 99,
                        "isReply": True,
                        "isQuoteStatus": False,
                        "conversationId": "100",
                        "source": "Twitter Web App",
                        "lang": "en",
                        "media": [{"mediaUrl": "https://pbs.twimg.com/media/a.jpg"}],
                        "author": {
                            "id": "42",
                            "username": "alice",
                            "name": "Alice",
                            "followers": 10,
                            "following": 3,
                            "verified": True,
                            "verifiedType": "Business",
                            "profilePicture": "https://pbs.twimg.com/profile_images/a.jpg",
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr(backend_mod, "urlopen", fake_urlopen)

    backend = XquikBackend("test-key", timeout=3)
    tweets = backend.search("launch", limit=5, query_type="Top")

    assert len(tweets) == 1
    assert tweets[0].id == "123"
    assert tweets[0].author_handle == "alice"
    assert tweets[0].likes == 7
    assert tweets[0].created_timestamp == 1767323045
    assert tweets[0].source_method == "xquik"
    assert tweets[0].media_urls == ["https://pbs.twimg.com/media/a.jpg"]
    assert "q=launch" in seen["url"]
    assert "queryType=Top" in seen["url"]
    assert seen["key"] == "test-key"
    assert seen["timeout"] == 3


def test_requires_api_key(monkeypatch):
    monkeypatch.delenv("XQUIK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="API key required"):
        XquikBackend()


def test_rejects_invalid_query_type():
    backend = XquikBackend("test-key")

    with pytest.raises(ValueError, match="Latest or Top"):
        backend.search("launch", query_type="Recent")


def test_http_error_omits_api_key(monkeypatch):
    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(backend_mod, "urlopen", fake_urlopen)
    backend = XquikBackend("test-key")

    with pytest.raises(RuntimeError) as exc_info:
        backend.search("launch")

    assert "test-key" not in str(exc_info.value)
    assert "HTTP 401" in str(exc_info.value)
