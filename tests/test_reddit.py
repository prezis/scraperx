"""Tests for scraperx.reddit — tiered no-login Reddit scraper.

Network-free: the module-level ``_http_get`` seam is monkeypatched with
canned JSON/HTML fixtures, so the tier ladder (old.reddit JSON → redlib
mirrors → stealth) can be exercised without touching the network.

One LIVE smoke test at the bottom is gated behind ``SCRAPERX_LIVE=1``.
"""

from __future__ import annotations

import json
import os

import pytest

from scraperx import reddit as reddit_mod
from scraperx.reddit import (
    RedditComment,
    RedditError,
    RedditPost,
    RedditScraper,
    RedditThread,
    _parse_listing,
    _parse_redlib_listing_html,
    _parse_thread,
    _walk_comments,
    normalize_permalink,
)

# ---------------------------------------------------------------------------
# Fixtures — canned Reddit JSON payloads
# ---------------------------------------------------------------------------


def _t3(post_id: str, title: str, **over) -> dict:
    d = {
        "id": post_id,
        "title": title,
        "author": "someone",
        "subreddit": "ethereum",
        "permalink": f"/r/ethereum/comments/{post_id}/slug/",
        "url": f"https://www.reddit.com/r/ethereum/comments/{post_id}/slug/",
        "selftext": "body text",
        "score": 42,
        "upvote_ratio": 0.93,
        "num_comments": 7,
        "created_utc": 1770000000.0,
        "is_self": True,
        "over_18": False,
        "link_flair_text": "Discussion",
    }
    d.update(over)
    return {"kind": "t3", "data": d}


def _listing(children: list) -> dict:
    return {"kind": "Listing", "data": {"children": children}}


def _t1(comment_id: str, body: str, *, replies=None, **over) -> dict:
    d = {
        "id": comment_id,
        "author": "commenter",
        "body": body,
        "score": 5,
        "parent_id": "t3_abc123",
        "created_utc": 1770000100.0,
        "permalink": f"/r/ethereum/comments/abc123/slug/{comment_id}/",
        "replies": _listing(replies) if replies else "",
    }
    d.update(over)
    return {"kind": "t1", "data": d}


SEARCH_JSON = _listing([_t3("aaa111", "Alchemy vs Infura"), _t3("bbb222", "Alchemy webhook lag")])

THREAD_JSON = [
    _listing([_t3("abc123", "Thread title")]),
    _listing(
        [
            _t1("c1", "top-level one", replies=[_t1("c1a", "nested reply", parent_id="t1_c1")]),
            _t1("c2", "top-level two"),
            {"kind": "more", "data": {"count": 12, "children": ["x", "y"]}},
        ]
    ),
]

REDLIB_SEARCH_HTML = """
<html><body>
<a href="/r/ethereum/comments/aaa111/alchemy_vs_infura/?utm=x" class="post_title">Alchemy vs Infura</a>
<a href="/r/ethereum/comments/aaa111/alchemy_vs_infura/">57 comments</a>
<a href="/r/ethereum/comments/bbb222/webhook_lag/">Alchemy webhook lag</a>
</body></html>
"""

REDLIB_THREAD_HTML = """
<html><body>
<h1 class="post_title">Thread <b>title</b></h1>
<div class="comment"><div class="comment_body"><div class="md"><p>first comment</p></div></div></div>
<div class="comment"><div class="comment_body"><div class="md"><p>second &amp; last</p></div></div></div>
</body></html>
"""


def _scraper(**kw) -> RedditScraper:
    """Politeness + backoff zeroed so tests run instantly."""
    kw.setdefault("min_interval_s", 0)
    kw.setdefault("jitter_s", 0)
    kw.setdefault("retry_backoff_s", 0)
    return RedditScraper(**kw)


def _fake_http(responses):
    """Build a fake _http_get: list of (status, body) popped per call, last repeats."""
    calls: list[str] = []

    def fake(url, *, user_agent, timeout):
        calls.append(url)
        status, body = responses[min(len(calls) - 1, len(responses) - 1)]
        return status, body

    fake.calls = calls
    return fake


# ---------------------------------------------------------------------------
# normalize_permalink
# ---------------------------------------------------------------------------


def test_normalize_permalink_full_url():
    assert (
        normalize_permalink("https://www.reddit.com/r/ethereum/comments/abc123/slug/?ref=share")
        == "/r/ethereum/comments/abc123/slug"
    )


def test_normalize_permalink_old_reddit_and_json_suffix():
    assert (
        normalize_permalink("https://old.reddit.com/r/ethereum/comments/abc123/slug.json")
        == "/r/ethereum/comments/abc123/slug"
    )


def test_normalize_permalink_bare_no_slug():
    assert normalize_permalink("/r/ethereum/comments/abc123") == "/r/ethereum/comments/abc123"


def test_normalize_permalink_rejects_garbage():
    with pytest.raises(RedditError):
        normalize_permalink("https://example.com/not/reddit")


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------


def test_parse_listing_maps_t3_fields():
    posts = _parse_listing(SEARCH_JSON)
    assert len(posts) == 2
    p = posts[0]
    assert isinstance(p, RedditPost)
    assert p.id == "aaa111"
    assert p.title == "Alchemy vs Infura"
    assert p.score == 42
    assert p.upvote_ratio == pytest.approx(0.93)
    assert p.num_comments == 7
    assert p.created_iso.startswith("2026-")
    assert p.flair == "Discussion"
    assert p.full_url == "https://www.reddit.com/r/ethereum/comments/aaa111/slug/"


def test_parse_listing_skips_non_t3_children():
    payload = _listing([{"kind": "t5", "data": {"id": "sub"}}, _t3("ccc333", "keep me")])
    posts = _parse_listing(payload)
    assert [p.id for p in posts] == ["ccc333"]


def test_parse_listing_rejects_non_dict():
    with pytest.raises(RedditError):
        _parse_listing(["not", "a", "listing"])


def test_walk_comments_flattens_depth_first_and_counts_more():
    children = THREAD_JSON[1]["data"]["children"]
    comments, more = _walk_comments(children)
    assert [c.id for c in comments] == ["c1", "c1a", "c2"]
    assert [c.depth for c in comments] == [0, 1, 0]
    assert comments[1].parent_id == "t1_c1"
    assert more == 12


def test_walk_comments_more_without_count_falls_back_to_children_len():
    comments, more = _walk_comments([{"kind": "more", "data": {"children": ["a", "b", "c"]}}])
    assert comments == []
    assert more == 3


def test_parse_thread_builds_post_plus_comments():
    thread = _parse_thread(THREAD_JSON)
    assert isinstance(thread, RedditThread)
    assert thread.post.id == "abc123"
    assert len(thread.comments) == 3
    assert thread.more_count == 12


def test_parse_thread_rejects_single_listing():
    with pytest.raises(RedditError):
        _parse_thread([_listing([_t3("abc123", "x")])])


def test_parse_thread_rejects_missing_post():
    with pytest.raises(RedditError):
        _parse_thread([_listing([]), _listing([])])


# ---------------------------------------------------------------------------
# TIER 2 — redlib HTML parsing
# ---------------------------------------------------------------------------


def test_parse_redlib_listing_dedups_by_post_id_and_strips_query():
    posts = _parse_redlib_listing_html(REDLIB_SEARCH_HTML)
    assert [p.id for p in posts] == ["aaa111", "bbb222"]
    assert posts[0].title == "Alchemy vs Infura"
    assert posts[0].permalink == "/r/ethereum/comments/aaa111/alchemy_vs_infura/"
    assert posts[0].tier == "redlib"


def test_parse_redlib_thread_html_extracts_title_and_comments():
    thread = reddit_mod._parse_redlib_thread_html(
        REDLIB_THREAD_HTML, "/r/ethereum/comments/abc123/slug"
    )
    assert thread.post.title == "Thread title"
    assert thread.post.id == "abc123"
    assert [c.body for c in thread.comments] == ["first comment", "second & last"]
    assert thread.tier == "redlib"


# ---------------------------------------------------------------------------
# Tier ladder — search_subreddit
# ---------------------------------------------------------------------------


def test_search_tier1_success(monkeypatch):
    fake = _fake_http([(200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper()
    posts = s.search_subreddit("ethereum", "alchemy", limit=10)
    assert len(posts) == 2
    assert s.last_tier_used == "old_json"
    url = fake.calls[0]
    assert url.startswith("https://old.reddit.com/r/ethereum/search.json?")
    assert "restrict_sr=1" in url and "q=alchemy" in url and "sort=new" in url


def test_search_tier1_retries_once_on_transient_403(monkeypatch):
    fake = _fake_http([(403, "blocked"), (200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(retry_backoff_s=0.001)
    posts = s.search_subreddit("ethereum", "alchemy")
    assert len(posts) == 2
    assert len(fake.calls) == 2  # first 403, retry 200 — no mirror demotion


def test_search_falls_to_tier2_when_tier1_blocked(monkeypatch):
    # tier1 old.reddit 403 (x2 incl. retry), then mirror serves HTML
    def fake(url, *, user_agent, timeout):
        if "old.reddit.com" in url:
            return 403, "blocked"
        return 200, REDLIB_SEARCH_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(enable_stealth=False)
    posts = s.search_subreddit("ethereum", "alchemy", limit=1)
    assert s.last_tier_used == "redlib"
    assert len(posts) == 1  # limit respected
    assert posts[0].tier == "redlib"


def test_search_all_tiers_fail_raises_with_per_tier_errors(monkeypatch):
    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(500, "boom")]))
    s = _scraper(enable_stealth=False)
    with pytest.raises(RedditError) as ei:
        s.search_subreddit("ethereum", "alchemy")
    msg = str(ei.value)
    assert "tier1 old_json" in msg and "tier2 redlib" in msg and "tier3 stealth" in msg


def test_search_validates_inputs():
    s = _scraper()
    with pytest.raises(RedditError):
        s.search_subreddit("bad name!", "q")
    with pytest.raises(RedditError):
        s.search_subreddit("ethereum", "   ")
    with pytest.raises(RedditError):
        s.search_subreddit("ethereum", "q", sort="kebab")


def test_search_accepts_r_prefix(monkeypatch):
    fake = _fake_http([(200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    _scraper().search_subreddit("r/ethereum", "alchemy")
    assert "/r/ethereum/search.json" in fake.calls[0]


# ---------------------------------------------------------------------------
# Tier ladder — get_thread
# ---------------------------------------------------------------------------


def test_get_thread_tier1_success(monkeypatch):
    fake = _fake_http([(200, json.dumps(THREAD_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper()
    thread = s.get_thread("https://www.reddit.com/r/ethereum/comments/abc123/slug/")
    assert thread.post.id == "abc123"
    assert len(thread.comments) == 3
    assert thread.more_count == 12
    assert s.last_tier_used == "old_json"
    assert fake.calls[0].startswith(
        "https://old.reddit.com/r/ethereum/comments/abc123/slug.json?"
    )


def test_get_thread_falls_to_tier2_html(monkeypatch):
    def fake(url, *, user_agent, timeout):
        if "old.reddit.com" in url:
            return 429, "rate limited"
        return 200, REDLIB_THREAD_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(enable_stealth=False)
    thread = s.get_thread("/r/ethereum/comments/abc123/slug")
    assert s.last_tier_used == "redlib"
    assert thread.post.title == "Thread title"
    assert len(thread.comments) == 2


# ---------------------------------------------------------------------------
# Tier ladder — get_subreddit_posts
# ---------------------------------------------------------------------------


def test_listing_tier1_success_and_sort_in_url(monkeypatch):
    fake = _fake_http([(200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper()
    posts = s.get_subreddit_posts("ethereum", sort="new", limit=5)
    assert len(posts) == 2
    assert fake.calls[0].startswith("https://old.reddit.com/r/ethereum/new.json?")


def test_listing_rejects_bad_sort():
    with pytest.raises(RedditError):
        _scraper().get_subreddit_posts("ethereum", sort="best")


def test_listing_limit_clamped_to_100(monkeypatch):
    fake = _fake_http([(200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    _scraper().get_subreddit_posts("ethereum", limit=9999)
    assert "limit=100" in fake.calls[0]


def test_tier2_probes_next_mirror_on_failure(monkeypatch):
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        if "old.reddit.com" in url:
            return 403, ""
        if "mirror-one" in url:
            raise OSError("connection refused")
        return 200, REDLIB_SEARCH_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(
        mirrors=("https://mirror-one.example", "https://mirror-two.example"),
        enable_stealth=False,
    )
    posts = s.get_subreddit_posts("ethereum", sort="hot")
    assert posts and s.last_tier_used == "redlib"
    assert any("mirror-one" in u for u in seen) and any("mirror-two" in u for u in seen)


# ---------------------------------------------------------------------------
# TIER 3 — stealth JSON extraction (fetch_stealth mocked)
# ---------------------------------------------------------------------------


def test_tier3_stealth_parses_pre_wrapped_json(monkeypatch):
    body = f"<html><body><pre>{json.dumps(SEARCH_JSON)}</pre></body></html>"

    def fake_stealth(url, timeout, **kw):
        return body, 200

    import scraperx.scrapling_stealth as ss

    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(500, "down")]))
    monkeypatch.setattr(ss, "fetch_stealth", fake_stealth)
    s = _scraper(mirrors=())  # no mirrors → tier2 fails instantly
    posts = s.search_subreddit("ethereum", "alchemy")
    assert len(posts) == 2
    assert s.last_tier_used == "stealth"
    assert posts[0].tier == "stealth"


def test_tier3b_stealth_mirror_html_when_stealth_json_ip_blocked(monkeypatch):
    """reddit.com IP-blocks even the stealth browser → mirror-via-stealth wins."""

    def fake_stealth(url, timeout, **kw):
        if "old.reddit.com" in url:
            return "blocked", 403
        return REDLIB_SEARCH_HTML, 200

    import scraperx.scrapling_stealth as ss

    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(403, "blocked")]))
    monkeypatch.setattr(ss, "fetch_stealth", fake_stealth)
    s = _scraper(mirrors=("https://mirror.example",))
    posts = s.search_subreddit("ethereum", "alchemy")
    assert s.last_tier_used == "stealth_mirror"
    assert posts and posts[0].tier == "stealth_mirror"


def test_tier3_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(500, "down")]))
    s = _scraper(mirrors=(), enable_stealth=False)
    with pytest.raises(RedditError) as ei:
        s.search_subreddit("ethereum", "alchemy")
    assert "stealth tier disabled" in str(ei.value)


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------


def test_module_level_wrappers_share_default_scraper(monkeypatch):
    fake = _fake_http([(200, json.dumps(SEARCH_JSON))])
    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    monkeypatch.setattr(reddit_mod, "_default_scraper", None)
    monkeypatch.setattr(
        reddit_mod, "RedditScraper",
        lambda **kw: RedditScraper(min_interval_s=0, jitter_s=0, retry_backoff_s=0),
    )
    posts = reddit_mod.search_subreddit("ethereum", "alchemy", limit=2)
    assert len(posts) == 2


def test_package_exports():
    import scraperx

    assert scraperx.RedditScraper is RedditScraper
    # scraperx.get_thread stays the X/Twitter reconstructor; Reddit is aliased.
    assert scraperx.get_reddit_thread is reddit_mod.get_thread
    assert scraperx.search_subreddit is reddit_mod.search_subreddit
    assert scraperx.get_subreddit_posts is reddit_mod.get_subreddit_posts
    assert isinstance(RedditComment(id="x", author="a", body="b"), RedditComment)


# ---------------------------------------------------------------------------
# LIVE smoke test — opt-in via SCRAPERX_LIVE=1
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("SCRAPERX_LIVE") != "1",
    reason="live network smoke test; set SCRAPERX_LIVE=1 to run",
)
def test_live_search_ethereum_for_alchemy():
    s = RedditScraper()
    posts = s.search_subreddit("ethereum", "alchemy", limit=5)
    assert posts, "live search returned no posts"
    assert all(p.title for p in posts)
    assert s.last_tier_used in ("old_json", "redlib", "stealth", "stealth_mirror")
