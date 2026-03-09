"""Tests for social_db.py — uses tmp_path, never touches real data."""

import time

import pytest

from scraperx.scraper import Tweet
from scraperx.profile import XProfile
from scraperx.social_db import SocialDB


@pytest.fixture
def db(tmp_path):
    """Return a SocialDB backed by a temp file."""
    path = str(tmp_path / "test_social.db")
    sdb = SocialDB(db_path=path)
    yield sdb
    sdb.close()


def _make_tweet(**overrides) -> Tweet:
    defaults = dict(
        id="123456",
        text="Hello $SOL world",
        author="Tester",
        author_handle="tester",
        likes=10,
        retweets=5,
        replies=2,
        views=1000,
        media_urls=["https://img.example.com/a.jpg"],
        article_title="Some article",
        source_method="fxtwitter",
    )
    defaults.update(overrides)
    return Tweet(**defaults)


def _make_profile(**overrides) -> XProfile:
    defaults = dict(
        handle="alice",
        name="Alice",
        bio="Crypto researcher",
        followers=5000,
        following=200,
        tweets_count=1200,
        likes_count=800,
        joined="2020-01-15",
        location="NYC",
        website="https://alice.xyz",
        verified=True,
    )
    defaults.update(overrides)
    return XProfile(**defaults)


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------

def test_schema_created(db):
    """All four tables should exist after init."""
    cur = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = sorted(r["name"] for r in cur.fetchall())
    assert "profiles" in tables
    assert "search_cache" in tables
    assert "token_mentions" in tables
    assert "tweets" in tables


# ------------------------------------------------------------------
# Tweets
# ------------------------------------------------------------------

def test_save_and_get_tweet(db):
    tw = _make_tweet()
    db.save_tweet(tw)
    loaded = db.get_tweet("123456")
    assert loaded is not None
    assert loaded.id == "123456"
    assert loaded.text == "Hello $SOL world"
    assert loaded.author_handle == "tester"
    assert loaded.likes == 10
    assert loaded.media_urls == ["https://img.example.com/a.jpg"]
    assert loaded.article_title == "Some article"
    assert loaded.source_method == "fxtwitter"


def test_get_tweet_not_found(db):
    assert db.get_tweet("nonexistent") is None


def test_duplicate_tweet_replace(db):
    tw1 = _make_tweet(likes=10)
    db.save_tweet(tw1)

    tw2 = _make_tweet(likes=99)
    db.save_tweet(tw2)

    loaded = db.get_tweet("123456")
    assert loaded.likes == 99


# ------------------------------------------------------------------
# Profiles
# ------------------------------------------------------------------

def test_save_and_get_profile(db):
    prof = _make_profile()
    db.save_profile(prof)
    loaded = db.get_profile("alice")
    assert loaded is not None
    assert loaded.handle == "alice"
    assert loaded.name == "Alice"
    assert loaded.followers == 5000
    assert loaded.verified is True
    assert loaded.website == "https://alice.xyz"


def test_get_profile_not_found(db):
    assert db.get_profile("nobody") is None


def test_profile_staleness(db):
    prof = _make_profile()
    db.save_profile(prof)

    # Manually backdate scraped_at by 10 days
    ten_days_ago = time.time() - 10 * 86400
    db._conn.execute(
        "UPDATE profiles SET scraped_at = ? WHERE handle = ?",
        (ten_days_ago, "alice"),
    )
    db._conn.commit()

    # Default max_age_days=7 should return None
    assert db.get_profile("alice") is None

    # But asking for 14-day window should still work
    assert db.get_profile("alice", max_age_days=14) is not None


# ------------------------------------------------------------------
# Token mentions
# ------------------------------------------------------------------

def test_token_mention_and_buzz(db):
    tw = _make_tweet(id="t1", likes=50, retweets=20, replies=5, views=500)
    db.save_tweet(tw)
    db.save_token_mention("t1", "SOL", "cashtag")

    tw2 = _make_tweet(
        id="t2", author_handle="other", likes=10, retweets=2, replies=1, views=100,
    )
    db.save_tweet(tw2)
    db.save_token_mention("t2", "SOL", "text_match")

    buzz = db.get_token_buzz("SOL", hours=1)
    assert buzz["mention_count"] == 2
    assert buzz["unique_authors"] == 2  # tester + other
    assert buzz["total_engagement"] == 50 + 20 + 5 + 500 + 10 + 2 + 1 + 100
    assert len(buzz["tweets"]) == 2


def test_token_buzz_empty(db):
    buzz = db.get_token_buzz("UNKNOWN")
    assert buzz["mention_count"] == 0
    assert buzz["tweets"] == []


# ------------------------------------------------------------------
# Search cache
# ------------------------------------------------------------------

def test_search_cache_hit(db):
    db.save_search_cache("solana news", ["t1", "t2", "t3"], ttl=3600)
    result = db.get_search_cache("solana news")
    assert result == ["t1", "t2", "t3"]


def test_search_cache_miss(db):
    assert db.get_search_cache("nothing cached") is None


def test_search_cache_expiry(db):
    db.save_search_cache("old query", ["t1"], ttl=1)

    # Backdate searched_at so the entry is expired
    db._conn.execute(
        "UPDATE search_cache SET searched_at = ?",
        (time.time() - 10,),
    )
    db._conn.commit()

    assert db.get_search_cache("old query") is None
