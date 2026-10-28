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
<html><body><div id="column_one">
<a href="/r/ethereum/comments/aaa111/alchemy_vs_infura/?utm=x" class="post_title">Alchemy vs Infura</a>
<a href="/r/ethereum/comments/aaa111/alchemy_vs_infura/">57 comments</a>
<a href="/r/ethereum/comments/bbb222/webhook_lag/">Alchemy webhook lag</a>
</div></body></html>
"""

REDLIB_THREAD_HTML = """
<html><body><div id="column_one">
<h1 class="post_title">Thread <b>title</b></h1>
<div class="comment"><div class="comment_body"><div class="md"><p>first comment</p></div></div></div>
<div class="comment"><div class="comment_body"><div class="md"><p>second &amp; last</p></div></div></div>
</div></body></html>
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


REDLIB_ERROR_PAGE = (
    '<html><body><nav><div id="logo">redlib</div></nav><div id="error"><h1>Failed to parse page JSON data: '
    "expected value at line 1 column 1</h1></div></body></html>"
)
ANUBIS_PAGE = "<html><head><title>Making sure you&#39;re not a bot!</title></head><body></body></html>"


@pytest.mark.parametrize("bad_page", [REDLIB_ERROR_PAGE, ANUBIS_PAGE, "<html><body>maintenance</body></html>"])
def test_a_200_that_is_not_a_redlib_page_moves_to_the_next_mirror(monkeypatch, bad_page):
    """safereddit.com answered 200 with no posts and 404 'Failed to parse page JSON data' (2026-10-28): a mirror's
    status says nothing; the redlib page structure (#column_one) does."""
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        if "old.reddit.com" in url:
            return 403, ""
        return 200, bad_page if "m1.example" in url else _fixture("listing.html")

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example", "https://m2.example"), enable_stealth=False, adaptive_tiers=False)
    posts = s.get_subreddit_posts("demo")
    assert len(posts) == 3 and s.last_mirror_used == "https://m2.example"
    assert s._mirror_order()[-1] == "https://m1.example"  # cooled down


def test_a_mirror_that_keeps_failing_goes_last_in_a_new_process(monkeypatch):
    from scraperx.method_telemetry import record

    for _ in range(5):
        record("reddit_mirror_stealth", "https://m1.example", "m1.example", False)
        record("reddit_mirror_stealth", "https://m2.example", "m2.example", True)
    s = _scraper(mirrors=("https://m1.example", "https://m2.example", "https://m3.example"))
    assert s._mirror_order("stealth") == ["https://m2.example", "https://m3.example", "https://m1.example"]
    assert s._mirror_order("plain") == ["https://m1.example", "https://m2.example", "https://m3.example"]


def test_mirror_outcomes_are_recorded_per_access_path(monkeypatch):
    from scraperx.method_telemetry import method_stats

    def fake(url, *, user_agent, timeout):
        if "old.reddit.com" in url:
            return 403, ""
        return 200, ANUBIS_PAGE if "m1.example" in url else _fixture("listing.html")

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example", "https://m2.example"), enable_stealth=False)
    s.get_subreddit_posts("demo")
    stats = method_stats("reddit_mirror_plain")
    assert stats["https://m1.example"]["successes"] == 0 and stats["https://m2.example"]["successes"] == 1


def test_redlib_page_problem_names_the_cause():
    assert reddit_mod._redlib_page_problem(_fixture("listing.html")) is None
    assert reddit_mod._redlib_page_problem(_fixture("thread.html")) is None
    assert reddit_mod._redlib_page_problem(REDLIB_ERROR_PAGE) == "redlib error page"
    assert reddit_mod._redlib_page_problem(ANUBIS_PAGE) == "block page: anubis"


def test_old_reddit_login_wall_is_named(monkeypatch):
    login = "<!doctype html><html><head><title>reddit.com: Log in</title></head><body><form action='/login'>"
    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(200, login)]))
    with pytest.raises(RedditError, match="login page"):
        _scraper()._tier1_json("/r/demo/new.json", {})


def test_stealth_mirror_skips_the_fixed_post_load_wait(monkeypatch):
    seen: list[dict] = []

    def fake_stealth(url, timeout, **kw):
        seen.append(kw)
        if "old.reddit.com" in url:
            return "blocked", 403
        return _fixture("listing.html"), 200

    import scraperx.scrapling_stealth as ss

    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(403, "blocked")]))
    monkeypatch.setattr(ss, "fetch_stealth", fake_stealth)
    s = _scraper(mirrors=("https://mirror.example",), adaptive_tiers=False)
    s.get_subreddit_posts("demo")
    assert s.last_tier_used == "stealth_mirror" and seen[-1] == {"wait_ms": 0}


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
    assert scraperx.search_reddit is reddit_mod.search_reddit
    assert scraperx.get_subreddit_posts is reddit_mod.get_subreddit_posts
    assert isinstance(RedditComment(id="x", author="a", body="b"), RedditComment)


# ---------------------------------------------------------------------------
# Redlib post / comment blocks (markup read from public instances 2026-10-28)
# ---------------------------------------------------------------------------

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "redlib")


def _fixture(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_redlib_listing_reads_post_blocks_not_sidebar_links():
    posts = _parse_redlib_listing_html(_fixture("listing.html"), tier="stealth_mirror")
    assert [p.id for p in posts] == ["aaa111", "bbb222", "ccc333"]  # the sidebar thread link is not a post
    pinned, rug, link = posts
    assert (rug.subreddit, rug.author, rug.score, rug.num_comments) == ("demo", "alice_demo", 57, 7)
    assert rug.created_iso == "2026-10-28T11:51:38Z"
    assert rug.title == "Is this a rug?" and rug.permalink == "/r/demo/comments/bbb222/is_this_a_rug/"
    assert rug.selftext == "Pool emptied in one transaction." and rug.tier == "stealth_mirror"
    assert pinned.stickied and not rug.stickied
    assert pinned.flair == "Announcement" and pinned.title == "Weekly thread & rules"
    assert pinned.score == 0 and pinned.num_comments == 1204  # hidden score; comma in the count
    assert pinned.selftext == "Read the rules first.\nSecond paragraph."
    assert (link.subreddit, link.selftext, link.num_comments) == ("other", "", 1)


def test_redlib_thread_reads_post_and_comment_tree():
    thread = reddit_mod._parse_redlib_thread_html(_fixture("thread.html"), "/r/demo/comments/bbb222/is_this_a_rug")
    post = thread.post
    assert (post.id, post.title, post.flair, post.author, post.score) == ("bbb222", "Is this a rug?", "Question", "alice_demo", 58)
    assert post.is_self and post.selftext == "Pool emptied in one transaction.\nPrice barely moved."
    assert post.created_iso == "2026-10-28T11:51:38Z"
    got = [(c.id, c.depth, c.parent_id, c.author, c.score) for c in thread.comments]
    assert got == [
        ("c0auto", 0, "t3_bbb222", "AutoModerator", 0),
        ("c1top", 0, "t3_bbb222", "carol_demo", 12),
        ("c1a", 1, "t1_c1top", "dave_demo", 4),
        ("c1a1", 2, "t1_c1a", "carol_demo", 2),
        ("c2top", 0, "t3_bbb222", "erin_demo", 1),  # the quote inside c1top's body does not shift the depth
    ]
    assert thread.comments[1].body == "Price barely moved.\nThat is what a liquidity pull looks like."
    assert thread.comments[2].permalink == "/r/demo/comments/bbb222/is_this_a_rug/c1a/"
    assert thread.comments[4].created_utc > thread.comments[0].created_utc


def test_mirrors_rotate_and_the_serving_one_is_named(monkeypatch):
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        return (403, "") if "old.reddit.com" in url else (200, _fixture("listing.html"))

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example", "https://m2.example"), enable_stealth=False, adaptive_tiers=False)
    s.get_subreddit_posts("demo")
    assert s.last_mirror_used == "https://m1.example"
    s.get_subreddit_posts("demo")
    assert s.last_mirror_used == "https://m2.example"  # round-robin spreads the load
    s.get_subreddit_posts("demo")
    assert s.last_mirror_used == "https://m1.example"


def test_failed_mirror_goes_to_the_back(monkeypatch):
    def fake(url, *, user_agent, timeout):
        if "old.reddit.com" in url or "m1.example" in url:
            return 503, ""
        return 200, _fixture("listing.html")

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example", "https://m2.example", "https://m3.example"),
                 enable_stealth=False, adaptive_tiers=False)
    s.get_subreddit_posts("demo")
    assert s.last_mirror_used == "https://m2.example"
    assert s._mirror_order() == ["https://m3.example", "https://m2.example", "https://m1.example"]


def test_thread_on_a_mirror_keeps_the_trailing_slash(monkeypatch):
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        return (403, "") if "old.reddit.com" in url else (200, _fixture("thread.html"))

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example",), enable_stealth=False, adaptive_tiers=False)
    thread = s.get_thread("https://www.reddit.com/r/demo/comments/bbb222/is_this_a_rug/?ref=share")
    assert seen[-1] == "https://m1.example/r/demo/comments/bbb222/is_this_a_rug/"
    assert thread.post.permalink == "/r/demo/comments/bbb222/is_this_a_rug" and len(thread.comments) == 5


def test_mirror_listing_follows_next_pages(monkeypatch):
    page1 = _fixture("listing.html").replace(
        "</main>", '<footer><a href="?sort=hot&amp;t=&amp;after=t3_ccc333" accesskey="N">NEXT</a></footer></main>'
    )
    page2 = _fixture("listing.html").replace("aaa111", "ddd444").replace("bbb222", "eee555").replace("ccc333", "fff666")
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        if "old.reddit.com" in url:
            return 403, ""
        return 200, page2 if "after=t3_ccc333" in url else page1

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example",), enable_stealth=False, adaptive_tiers=False)
    posts = s.get_subreddit_posts("demo", sort="hot", limit=5)
    assert [p.id for p in posts] == ["aaa111", "bbb222", "ccc333", "ddd444", "eee555"]
    assert seen[-1] == "https://m1.example/r/demo/hot?after=t3_ccc333"
    s.search("rug", limit=3)  # one page is enough: no second fetch
    assert sum("/search?" in u for u in seen) == 1


def test_tier1_success_names_no_mirror(monkeypatch):
    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(200, json.dumps(SEARCH_JSON))]))
    s = _scraper()
    s.search_subreddit("ethereum", "alchemy")
    assert s.last_tier_used == "old_json" and s.last_mirror_used is None


def test_site_wide_search_urls_and_validation(monkeypatch):
    seen: list[str] = []

    def fake(url, *, user_agent, timeout):
        seen.append(url)
        return (403, "") if "old.reddit.com" in url else (200, _fixture("listing.html"))

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _scraper(mirrors=("https://m1.example",), enable_stealth=False, adaptive_tiers=False)
    posts = s.search("solana wallet drained", limit=2, sort="new", time_filter="week")
    assert [p.id for p in posts] == ["aaa111", "bbb222"]
    assert seen[0].startswith("https://old.reddit.com/search.json?")
    assert "q=solana+wallet+drained" in seen[0] and "t=week" in seen[0] and "type=link" in seen[0]
    assert seen[-1].startswith("https://m1.example/search?q=solana+wallet+drained&type=link&sort=new&t=week")
    with pytest.raises(RedditError):
        s.search("x", time_filter="decade")
    with pytest.raises(RedditError):
        s.search("   ")


def test_cli_reddit_search_json(monkeypatch, capsys):
    import sys

    from scraperx import __main__ as cli

    monkeypatch.setattr(reddit_mod, "_http_get", _fake_http([(200, json.dumps(SEARCH_JSON))]))
    monkeypatch.setattr(sys, "argv", ["scraperx", "reddit", "search", "alchemy", "--sub", "ethereum", "--json"])
    cli.main()
    out = capsys.readouterr()
    assert [p["id"] for p in json.loads(out.out)] == ["aaa111", "bbb222"]
    assert "source: old_json" in out.err


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
