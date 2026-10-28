"""Keyless X: search through Nitter RSS (scraperx.nitter), timelines and replies through FxTwitter v2
(scraperx.x_timeline), and the search_tweets auto order. Network-free: synthetic payloads in the shapes read live
2026-10-28. Live smoke tests behind SCRAPERX_LIVE=1."""

from __future__ import annotations

import json
import os

import pytest

from scraperx import nitter, x_timeline
from scraperx import search as search_mod
from scraperx.scraper import Tweet

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <title>Search results for "#demo"</title>
      <item>
        <title>Pool drained in one tx #demo</title>
        <dc:creator>@alice_demo</dc:creator>
        <description><![CDATA[<p>Pool drained in <a href="http://nit.example/search?q=%23one">one</a> tx<br>
<a href="http://nit.example/search?f=tweets&q=%23demo">#demo</a></p>
<img src="http://nit.example/pic/media%2FAbCdEf.jpg" style="max-width:250px;" />]]></description>
        <pubDate>Wed, 28 Oct 2026 16:06:27 GMT</pubDate>
        <guid isPermaLink="false">2115475290071712145</guid>
        <link>http://nit.example/alice_demo/status/2115475290071712145#m</link>
      </item>
      <item>
        <title>RT by @bob_demo: second &amp; last</title>
        <dc:creator>@carol_demo</dc:creator>
        <description><![CDATA[<p>second &amp; last</p>]]></description>
        <pubDate>Wed, 28 Oct 2026 15:00:00 GMT</pubDate>
        <guid isPermaLink="false">2115475290071712100</guid>
        <link>http://nit.example/carol_demo/status/2115475290071712100#m</link>
      </item>
  </channel>
</rss>"""


def _rss_page(ids: list[str]) -> str:
    page = RSS
    for old, new in zip(("2115475290071712145", "2115475290071712100"), ids, strict=True):
        page = page.replace(old, new)
    return page


@pytest.fixture(autouse=True)
def _fresh_rotation(monkeypatch):
    monkeypatch.setattr(nitter, "_down_until", {})
    monkeypatch.setattr(nitter, "_next_start", 0)
    monkeypatch.delenv("SCRAPERX_NITTER_INSTANCES", raising=False)
    for name in ("SCRAPERX_BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY", "SCRAPERX_DESEARCH_API_KEY", "DESEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- Nitter ---------------------------------------------------------------


def test_rss_items_become_tweets():
    first, rt = nitter._parse_rss(RSS)
    assert (first.id, first.author_handle, first.reposted_by) == ("2115475290071712145", "alice_demo", None)
    assert first.text == "Pool drained in one tx\n#demo"
    assert first.media_urls == ["https://pbs.twimg.com/media/AbCdEf.jpg"]
    assert first.created_at == "Wed, 28 Oct 2026 16:06:27 GMT" and first.created_timestamp == 1793203587
    assert (rt.author_handle, rt.reposted_by, rt.text) == ("carol_demo", "bob_demo", "second & last")
    assert first.source_method == "nitter"


def test_blocked_instances_are_skipped_and_cooled_down(monkeypatch):
    seen: list[str] = []

    def fake(url, timeout=25):
        seen.append(url)
        if url.startswith("https://a.example"):
            return 403, {}, "<html><head><title>Just a moment...</title></head></html>"
        if url.startswith("https://b.example"):
            return 200, {}, '<meta http-equiv="refresh" content="3; url=/.gandalf/check?gandalf_id=x">'
        return 200, {}, RSS

    monkeypatch.setattr(nitter, "_http_get", fake)
    pool = ("https://a.example", "https://b.example", "https://c.example")
    tweets = nitter.search("#demo", limit=2, instances=pool, delay_s=0)
    assert [t.id for t in tweets] == ["2115475290071712145", "2115475290071712100"]
    assert [u.split("/search")[0] for u in seen] == list(pool)
    assert nitter._instances(pool)[-2:] == ["https://a.example", "https://b.example"]  # both cooling down


def test_cursor_pages_and_query_encoding(monkeypatch):
    seen: list[str] = []

    def fake(url, timeout=25):
        seen.append(url)
        if "cursor=NEXT" in url:
            return 200, {}, _rss_page(["2115475290071712001", "2115475290071712000"])
        return 200, {"Min-Id": "NEXT"}, RSS

    monkeypatch.setattr(nitter, "_http_get", fake)
    tweets = nitter.search('#demo "wallet drained"', limit=4, instances=("https://c.example",), delay_s=0)
    assert len(tweets) == 4
    assert seen[0] == "https://c.example/search/rss?f=tweets&q=%23demo+%22wallet+drained%22"
    assert seen[1].endswith("&cursor=NEXT")


def test_every_instance_failing_names_each(monkeypatch):
    monkeypatch.setattr(nitter, "_http_get", lambda url, timeout=25: (429, {}, ""))
    with pytest.raises(nitter.NitterError, match=r"a\.example: HTTP 429.*b\.example: HTTP 429"):
        nitter.search("x", instances=("https://a.example", "https://b.example"))


def test_instances_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPERX_NITTER_INSTANCES", "https://one.example/, https://two.example")
    assert nitter._instances(None) == ["https://one.example", "https://two.example"]


# --- search_tweets auto order -------------------------------------------------


def test_auto_search_uses_nitter_without_keys_and_enriches(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(search_mod, "_nitter_search", lambda q, limit, tf: nitter._parse_rss(RSS))
    monkeypatch.setattr(search_mod, "_ddg_search", lambda *a, **k: pytest.fail("DDG asked although Nitter answered"))

    class FakeX:
        def get_tweet(self, url):
            calls.append(url)
            if url.endswith("2115475290071712100"):
                raise RuntimeError("gone")
            return Tweet(id="2115475290071712145", text="full", author="Alice", author_handle="alice_demo", likes=7,
                         source_method="fxtwitter")

    monkeypatch.setattr(search_mod, "XScraper", FakeX)
    out = search_mod.search_tweets("#demo", limit=2, delay=0, cache_hours=0)
    assert [(t.source_method, t.likes) for t in out] == [("nitter+fxtwitter", 7), ("nitter", 0)]
    assert out[1].reposted_by == "bob_demo"  # kept from RSS when FxTwitter cannot read the post
    assert calls[0] == "https://x.com/alice_demo/status/2115475290071712145"


def test_time_filter_becomes_since(monkeypatch):
    seen = {}
    monkeypatch.setattr(nitter, "search", lambda q, limit=20: seen.setdefault("q", q) and [])
    search_mod._nitter_search("#demo", 5, "w")
    assert seen["q"].startswith("#demo since:20") and len(seen["q"]) == len("#demo since:2026-10-21")


def test_backend_nitter_raises_when_no_instance_answers(monkeypatch):
    def boom(q, limit, tf):
        raise nitter.NitterError("no Nitter instance answered")

    monkeypatch.setattr(search_mod, "_nitter_search", boom)
    with pytest.raises(nitter.NitterError):
        search_mod.search_tweets("x", backend="nitter")


# --- FxTwitter v2 timelines -------------------------------------------------------


def _status(i: int, *, reposted_by=None, replying_to=None) -> dict:
    return {
        "id": str(2115454791921152200 + i),
        "text": f"post {i}",
        "author": {"screen_name": "demo" if not reposted_by else "other", "name": "Demo", "id": "42", "followers": 10,
                   "verification": {"verified": True, "type": "business"}},
        "likes": 10 + i, "reposts": 2, "replies": 1, "views": 1000 + i,
        "created_at": "Wed Oct 28 14:45:00 +0000 2026", "created_timestamp": 1793198700,
        "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/x.jpg"}]},
        "replying_to": replying_to,
        "reposted_by": {"screen_name": reposted_by} if reposted_by else None,
        "lang": "en",
    }


def test_v2_status_maps_to_tweet():
    t = x_timeline.tweet_from_fx_v2(_status(1, replying_to={"screen_name": "demo", "status": "2115432312343318628"}))
    assert (t.id, t.author_handle, t.likes, t.retweets, t.views) == ("2115454791921152201", "demo", 11, 2, 1001)
    assert t.is_reply and t.in_reply_to_tweet_id == "2115432312343318628" and t.in_reply_to_handle == "demo"
    assert t.author_verified and t.author_verified_type == "business" and t.media_urls
    assert t.reposted_by is None and t.source_method == "fxtwitter_v2"
    assert x_timeline.tweet_from_fx_v2(_status(2, reposted_by="demo")).reposted_by == "demo"


def test_user_timeline_pages_and_drops_reposts(monkeypatch):
    seen: list[dict] = []
    pages = [
        {"results": [_status(9), _status(8, reposted_by="demo"), _status(7)], "cursor": {"bottom": "C1"}},
        {"results": [_status(6, reposted_by="demo"), _status(5)], "cursor": {"bottom": "C2"}},
        {"results": [], "cursor": {"bottom": "C3"}},
    ]

    def fake(path, params, timeout=30):
        seen.append({"path": path, **params})
        return pages[len(seen) - 1]

    monkeypatch.setattr(x_timeline, "_get_json", fake)
    tweets = x_timeline.get_user_tweets("@demo", limit=3, include_reposts=False, delay_s=0)
    assert [t.id[-1] for t in tweets] == ["9", "7", "5"]
    assert seen[0] == {"path": "/profile/demo/statuses", "count": 20} and seen[1]["cursor"] == "C1"


def test_transient_404_is_retried(monkeypatch):
    from urllib.error import HTTPError

    answers = [404, 404, 200]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"code": 200, "results": []}'

    def fake_urlopen(req, timeout):
        code = answers.pop(0)
        if code != 200:
            raise HTTPError(req.full_url, code, "Not Found", None, None)
        return _Resp()

    monkeypatch.setattr(x_timeline, "urlopen", fake_urlopen)
    monkeypatch.setattr(x_timeline.time, "sleep", lambda s: None)
    assert x_timeline._get_json("/profile/demo/statuses", {"count": 20}) == {"code": 200, "results": []}
    answers[:] = [404] * 5
    with pytest.raises(x_timeline.FxV2Error, match=r"HTTP 404 .*\(5 tries\)"):
        x_timeline._get_json("/profile/demo/statuses", {})


def test_a_failing_later_page_keeps_what_was_collected(monkeypatch):
    calls = []

    def fake(path, params, timeout=30):
        calls.append(params.get("cursor"))
        if params.get("cursor"):
            raise x_timeline.FxV2Error("FxTwitter v2 HTTP 404")
        return {"results": [_status(1), _status(2)], "cursor": {"bottom": "C1"}}

    monkeypatch.setattr(x_timeline, "_get_json", fake)
    assert len(x_timeline.get_user_tweets("demo", limit=10, delay_s=0)) == 2 and calls == [None, "C1"]


def test_timeline_falls_back_to_nitter_rss(monkeypatch):
    def down(path, params, timeout=30):
        raise x_timeline.FxV2Error("FxTwitter v2 HTTP 404 (5 tries)")

    seen = {}

    def fake_user_tweets(handle, limit, *, with_replies=False, delay_s=1.0):
        seen.update(handle=handle, limit=limit, with_replies=with_replies)
        return nitter._parse_rss(RSS)

    monkeypatch.setattr(x_timeline, "_get_json", down)
    monkeypatch.setattr(nitter, "user_tweets", fake_user_tweets)
    own = x_timeline.get_user_tweets("demo", limit=5, include_reposts=False, with_replies=True, delay_s=0)
    assert [t.id for t in own] == ["2115475290071712145"] and own[0].source_method == "nitter"
    assert seen == {"handle": "demo", "limit": 15, "with_replies": True}
    with pytest.raises(x_timeline.FxV2Error):
        x_timeline.get_user_tweets("demo", nitter_fallback=False)


def test_replies_from_a_post_url(monkeypatch):
    seen: list[str] = []

    def fake(path, params, timeout=30):
        seen.append(path)
        return {"replies": [_status(1), _status(2)], "cursor": {}}

    monkeypatch.setattr(x_timeline, "_get_json", fake)
    replies = x_timeline.get_replies("https://x.com/demo/status/2115432312343318628?s=20", limit=10, delay_s=0)
    assert seen == ["/conversation/2115432312343318628"] and len(replies) == 2
    with pytest.raises(x_timeline.FxV2Error):
        x_timeline.get_replies("not a post")


def test_cli_x_user_json(monkeypatch, capsys):
    import sys

    from scraperx import __main__ as cli

    monkeypatch.setattr(x_timeline, "_get_json", lambda path, params, timeout=30: {"results": [_status(1)], "cursor": {}})
    monkeypatch.setattr(sys, "argv", ["scraperx", "x", "user", "demo", "-n", "1", "--json"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["url"] == "https://x.com/demo/status/2115454791921152201" and out[0]["views"] == 1001


@pytest.mark.skipif(os.environ.get("SCRAPERX_LIVE") != "1", reason="live network; SCRAPERX_LIVE=1 to run")
def test_live_keyless_x():
    tl = x_timeline.get_user_tweets("solana", limit=25)
    assert len(tl) >= 20 and all(t.id.isdigit() for t in tl)
    hits = nitter.search("#solana", limit=10)
    assert hits and all(t.id.isdigit() and t.author_handle for t in hits)
