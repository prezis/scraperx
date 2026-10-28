"""scraperx.bluesky — Bluesky without an account. Network-free: `_get` is monkeypatched with objects in the shapes
read live 2026-10-28. Live smoke test behind SCRAPERX_LIVE=1."""

from __future__ import annotations

import json
import os

import pytest

from scraperx import bluesky as bs
from scraperx.bluesky import BlueskyError, get_author_posts, get_profile, get_thread, search_posts

DID = "did:plc:z72i7hdynmk6r22z27h6tvur"


def _p(rkey: str, text: str, *, handle: str = "alice.example", reply_parent: str = "", embed=None, facets=None) -> dict:
    record = {"$type": "app.bsky.feed.post", "createdAt": "2026-10-28T18:45:00.647Z", "text": text,
              "facets": facets or []}
    if reply_parent:
        record["reply"] = {"parent": {"uri": reply_parent}, "root": {"uri": reply_parent}}
    post = {"uri": f"at://{DID}/app.bsky.feed.post/{rkey}", "cid": "bafy" + rkey, "record": record,
            "author": {"did": DID, "handle": handle, "displayName": "Alice"}, "likeCount": 7, "repostCount": 2,
            "replyCount": 1, "quoteCount": 0, "indexedAt": "2026-10-28T18:47:22.674Z"}
    if embed:
        post["embed"] = embed
    return post


def test_search_maps_posts_and_uses_the_search_appview(monkeypatch):
    seen = {}

    def fake(base, nsid, params, timeout=30):
        seen.update(base=base, nsid=nsid, **params)
        link = {"features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com/a"}],
                "index": {"byteStart": 0, "byteEnd": 5}}
        images = {"$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.example/full.jpg"}]}
        return {"posts": [_p("3myxfz7bfp223", "wallet drained", facets=[link], embed=images)], "cursor": "1",
                "hitsTotal": 5779}

    monkeypatch.setattr(bs, "_get", fake)
    (p,) = search_posts("solana wallet", limit=500, sort="top")
    assert seen["base"] == bs.SEARCH_APPVIEW and seen["nsid"] == "app.bsky.feed.searchPosts"
    assert seen["limit"] == 100 and seen["sort"] == "top"  # one page, capped at 100
    assert p.url == "https://bsky.app/profile/alice.example/post/3myxfz7bfp223"
    assert (p.likes, p.reposts, p.replies) == (7, 2, 1) and p.text == "wallet drained"
    assert p.links == ["https://example.com/a"] and p.images == ["https://cdn.example/full.jpg"]
    with pytest.raises(BlueskyError):
        search_posts("  ")
    with pytest.raises(BlueskyError):
        search_posts("x", sort="hot")


def test_author_feed_pages_and_marks_reposts(monkeypatch):
    pages = [
        {"feed": [{"post": _p("a1", "own")}, {"post": _p("a2", "theirs", handle="bob.example"),
                                              "reason": {"$type": "app.bsky.feed.defs#reasonRepost",
                                                         "by": {"handle": "alice.example"}}}],
         "cursor": "C1"},
        {"feed": [{"post": _p("a3", "a reply", reply_parent=f"at://{DID}/app.bsky.feed.post/zz")}], "cursor": None},
    ]
    calls: list[dict] = []

    def fake(base, nsid, params, timeout=30):
        calls.append(dict(params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(bs, "_get", fake)
    posts = get_author_posts("@alice.example", limit=10, with_replies=True, delay_s=0)
    assert [p.uri[-2:] for p in posts] == ["a1", "a2", "a3"]
    assert posts[1].reposted_by == "alice.example" and posts[2].reply_to.endswith("/zz")
    assert calls[0]["filter"] == "posts_with_replies" and calls[1]["cursor"] == "C1"
    calls.clear()
    own = get_author_posts("https://bsky.app/profile/alice.example", limit=10, include_reposts=False, delay_s=0)
    assert [p.uri[-2:] for p in own] == ["a1", "a3"] and calls[0]["actor"] == "alice.example"


def test_thread_from_a_post_url_resolves_the_handle_and_flattens(monkeypatch):
    seen: list[tuple] = []

    def fake(base, nsid, params, timeout=30):
        seen.append((nsid, dict(params)))
        if nsid == "com.atproto.identity.resolveHandle":
            return {"did": DID}
        return {"thread": {"post": _p("root", "root post"), "replies": [
            {"post": _p("r1", "first reply"), "replies": [{"post": _p("r1a", "nested")}]},
            {"$type": "app.bsky.feed.defs#blockedPost", "uri": "at://x"},
            {"post": _p("r2", "second reply"), "replies": []},
        ]}}

    monkeypatch.setattr(bs, "_get", fake)
    posts = get_thread("https://bsky.app/profile/alice.example/post/root")
    assert [(p.uri.rsplit("/", 1)[-1], p.depth) for p in posts] == [("root", 0), ("r1", 1), ("r1a", 2), ("r2", 1)]
    assert seen[1][1]["uri"] == f"at://{DID}/app.bsky.feed.post/root"
    with pytest.raises(BlueskyError):
        get_thread("https://example.com/not-bluesky")


def test_profile(monkeypatch):
    monkeypatch.setattr(bs, "_get", lambda base, nsid, params, timeout=30: {
        "did": DID, "handle": "bsky.app", "displayName": "Bluesky", "followersCount": 35421407, "postsCount": 901})
    pr = get_profile("bsky.app")
    assert (pr.handle, pr.display_name, pr.followers, pr.posts) == ("bsky.app", "Bluesky", 35421407, 901)


def test_cli_bluesky_search_json(monkeypatch, capsys):
    import sys

    from scraperx import __main__ as cli

    monkeypatch.setattr(bs, "_get", lambda base, nsid, params, timeout=30: {"posts": [_p("k1", "hello")]})
    monkeypatch.setattr(sys, "argv", ["scraperx", "bluesky", "search", "solana", "wallet", "--json"])
    cli.main()
    assert json.loads(capsys.readouterr().out)[0]["text"] == "hello"


@pytest.mark.skipif(os.environ.get("SCRAPERX_LIVE") != "1", reason="live network; SCRAPERX_LIVE=1 to run")
def test_live_bluesky():
    assert search_posts("solana", limit=5)
    assert get_profile("bsky.app").followers > 1_000_000
    assert len(get_author_posts("bsky.app", limit=5)) == 5
