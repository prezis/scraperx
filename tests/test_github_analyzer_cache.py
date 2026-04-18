"""Tests for the github_analyzer SQLite cache layer (T2).

Uses tmp_path + a monkeypatched time.time so we can fast-forward past TTL
windows without real sleeps. Never touches the real ~/.scraperx/social.db.
"""

from __future__ import annotations

import pytest

from scraperx import social_db as social_db_module
from scraperx.social_db import (
    GITHUB_TTL_COMMITS,
    GITHUB_TTL_FORKS,
    GITHUB_TTL_MENTIONS,
    GITHUB_TTL_REPO,
    SocialDB,
)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "gh_cache.db")
    sdb = SocialDB(db_path=path)
    yield sdb
    sdb.close()


@pytest.fixture
def fixed_time(monkeypatch):
    """A mutable clock — set fixed_time.now to advance it."""

    class Clock:
        now = 1_000_000.0  # arbitrary epoch

        def __call__(self):
            return self.now

    clk = Clock()
    monkeypatch.setattr(social_db_module.time, "time", clk)
    return clk


# ---------------------------------------------------------------------------
# github_repo_cache


def test_repo_cache_roundtrip(db, fixed_time):
    payload = {"stars": 123, "description": "a repo"}
    db.save_repo_cache("yt-dlp/yt-dlp", "repo", payload)
    got = db.get_repo_cache("yt-dlp/yt-dlp", "repo")
    assert got == payload


def test_repo_cache_miss_returns_none(db, fixed_time):
    assert db.get_repo_cache("does/not-exist", "repo") is None
    assert db.get_repo_cache("owner/repo", "contributors") is None


def test_repo_cache_kind_isolation(db, fixed_time):
    """Different kinds for the same repo are independent rows."""
    db.save_repo_cache("o/r", "repo", {"stars": 1})
    db.save_repo_cache("o/r", "contributors", [{"handle": "a"}])
    db.save_repo_cache("o/r", "commits", [{"sha": "abc"}])

    assert db.get_repo_cache("o/r", "repo") == {"stars": 1}
    assert db.get_repo_cache("o/r", "contributors") == [{"handle": "a"}]
    assert db.get_repo_cache("o/r", "commits") == [{"sha": "abc"}]


def test_repo_cache_insert_or_replace_on_rewrite(db, fixed_time):
    db.save_repo_cache("o/r", "repo", {"stars": 1})
    db.save_repo_cache("o/r", "repo", {"stars": 2})  # overwrite
    assert db.get_repo_cache("o/r", "repo") == {"stars": 2}


def test_repo_cache_honours_repo_kind_default_ttl(db, fixed_time):
    """kind=repo should use the 24h default TTL."""
    db.save_repo_cache("o/r", "repo", {"x": 1})

    # Still fresh at 23h 59m
    fixed_time.now += GITHUB_TTL_REPO - 60
    assert db.get_repo_cache("o/r", "repo") == {"x": 1}

    # Stale at 24h 1m
    fixed_time.now += 120
    assert db.get_repo_cache("o/r", "repo") is None


def test_repo_cache_honours_commits_kind_shorter_ttl(db, fixed_time):
    """kind=commits should use the 6h default (shorter than repo)."""
    db.save_repo_cache("o/r", "commits", [{"sha": "deadbeef"}])

    # Fresh well past the repo-kind 24h boundary won't apply — this is 6h
    fixed_time.now += GITHUB_TTL_COMMITS - 60
    assert db.get_repo_cache("o/r", "commits") == [{"sha": "deadbeef"}]

    fixed_time.now += 120
    assert db.get_repo_cache("o/r", "commits") is None


def test_repo_cache_explicit_ttl_override(db, fixed_time):
    """Caller-supplied ttl overrides the per-kind default."""
    db.save_repo_cache("o/r", "repo", {"x": 1}, ttl=10)  # 10 seconds
    fixed_time.now += 5
    assert db.get_repo_cache("o/r", "repo") == {"x": 1}
    fixed_time.now += 20
    assert db.get_repo_cache("o/r", "repo") is None


def test_repo_cache_unknown_kind_falls_back_to_repo_ttl(db, fixed_time):
    db.save_repo_cache("o/r", "custom_kind", {"x": 1})
    fixed_time.now += GITHUB_TTL_REPO - 60
    assert db.get_repo_cache("o/r", "custom_kind") == {"x": 1}
    fixed_time.now += 120
    assert db.get_repo_cache("o/r", "custom_kind") is None


# ---------------------------------------------------------------------------
# github_fork_cache


def test_fork_cache_roundtrip(db, fixed_time):
    forks = [
        {"full_name": "a/yt-dlp", "stars": 10},
        {"full_name": "b/yt-dlp", "stars": 5},
    ]
    db.save_fork_cache("yt-dlp/yt-dlp", forks)
    assert db.get_fork_cache("yt-dlp/yt-dlp") == forks


def test_fork_cache_miss(db, fixed_time):
    assert db.get_fork_cache("missing/parent") is None


def test_fork_cache_default_6h_ttl(db, fixed_time):
    db.save_fork_cache("o/r", [{"full_name": "f/r"}])
    fixed_time.now += GITHUB_TTL_FORKS - 60
    assert db.get_fork_cache("o/r") == [{"full_name": "f/r"}]
    fixed_time.now += 120
    assert db.get_fork_cache("o/r") is None


def test_fork_cache_overwrite(db, fixed_time):
    db.save_fork_cache("o/r", [{"x": 1}])
    db.save_fork_cache("o/r", [{"x": 2}, {"x": 3}])
    assert db.get_fork_cache("o/r") == [{"x": 2}, {"x": 3}]


# ---------------------------------------------------------------------------
# github_mentions_cache


def test_mentions_cache_roundtrip(db, fixed_time):
    hits = [{"title": "yt-dlp thread", "url": "https://news.ycombinator.com/item?id=1"}]
    db.save_mentions_cache("hn", "yt-dlp/yt-dlp", hits)
    assert db.get_mentions_cache("hn", "yt-dlp/yt-dlp") == hits


def test_mentions_cache_source_isolation(db, fixed_time):
    """Same query, different source = different rows."""
    db.save_mentions_cache("hn", "q", [{"i": "hn1"}])
    db.save_mentions_cache("reddit", "q", [{"i": "r1"}])
    assert db.get_mentions_cache("hn", "q") == [{"i": "hn1"}]
    assert db.get_mentions_cache("reddit", "q") == [{"i": "r1"}]


def test_mentions_cache_query_hashing_case_insensitive(db, fixed_time):
    """_query_hash lowercases + strips — 'Yt-Dlp' and 'yt-dlp' should collide."""
    db.save_mentions_cache("hn", "Yt-Dlp", [{"canonical": True}])
    assert db.get_mentions_cache("hn", "yt-dlp") == [{"canonical": True}]
    assert db.get_mentions_cache("hn", "  yt-dlp  ") == [{"canonical": True}]


def test_mentions_cache_default_4h_ttl(db, fixed_time):
    db.save_mentions_cache("hn", "q", [{"x": 1}])
    fixed_time.now += GITHUB_TTL_MENTIONS - 60
    assert db.get_mentions_cache("hn", "q") == [{"x": 1}]
    fixed_time.now += 120
    assert db.get_mentions_cache("hn", "q") is None


# ---------------------------------------------------------------------------
# purge_expired_github_cache


def test_purge_expired_removes_stale_rows_only(db, fixed_time):
    db.save_repo_cache("o/r", "repo", {"x": 1}, ttl=10)
    db.save_fork_cache("o/r", [{"y": 2}], ttl=10)
    db.save_mentions_cache("hn", "q", [{"z": 3}], ttl=10)
    db.save_repo_cache("o/r2", "repo", {"x": 999})  # uses 24h default

    # Fast-forward past 10s TTL but before 24h
    fixed_time.now += 100

    deleted = db.purge_expired_github_cache()
    assert deleted == 3

    # Stale ones gone
    assert db.get_repo_cache("o/r", "repo") is None
    assert db.get_fork_cache("o/r") is None
    assert db.get_mentions_cache("hn", "q") is None
    # Fresh one survives
    assert db.get_repo_cache("o/r2", "repo") == {"x": 999}


def test_purge_expired_is_idempotent(db, fixed_time):
    """Calling purge twice on the same DB returns 0 deletes the second time."""
    db.save_repo_cache("o/r", "repo", {"x": 1}, ttl=10)
    fixed_time.now += 100
    assert db.purge_expired_github_cache() == 1
    assert db.purge_expired_github_cache() == 0


# ---------------------------------------------------------------------------
# Schema migration / backwards compat


def test_existing_social_data_still_works(db, fixed_time):
    """Adding github tables must not break existing tweet/profile paths."""
    from scraperx.scraper import Tweet

    tw = Tweet(
        id="1",
        text="hello",
        author="Alice",
        author_handle="alice",
        likes=1,
        retweets=0,
        replies=0,
        views=0,
        media_urls=[],
    )
    db.save_tweet(tw)
    got = db.get_tweet("1")
    assert got is not None
    assert got.text == "hello"


def test_schema_is_idempotent(tmp_path, fixed_time):
    """Reopening the same DB must not error (IF NOT EXISTS + migrations)."""
    path = str(tmp_path / "gh2.db")
    a = SocialDB(db_path=path)
    a.save_repo_cache("o/r", "repo", {"x": 1})
    a.close()

    b = SocialDB(db_path=path)
    assert b.get_repo_cache("o/r", "repo") == {"x": 1}
    b.close()
