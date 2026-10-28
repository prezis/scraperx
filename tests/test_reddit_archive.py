"""scraperx.reddit_archive — Arctic Shift comment search and user history. Network-free: `_get` / `urlopen` are
monkeypatched with objects in the archive's shape (read live 2026-10-28)."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from scraperx import reddit_archive as ra
from scraperx.reddit import RedditError


def _c(i: int, created: int) -> dict:
    return {"id": f"c{i}", "author": "alice_demo", "body": f"drained {i}", "score": 3, "created_utc": created,
            "parent_id": "t3_p1", "link_id": "t3_p1", "link_title": "Ledger scam", "subreddit": "demo",
            "permalink": f"/r/demo/comments/p1/x/c{i}/", "retrieved_on": created + 30, "updated_on": None}


def _p(i: int, created: int) -> dict:
    return {"id": f"p{i}", "title": f"post {i}", "author": "alice_demo", "subreddit": "demo", "score": 1,
            "num_comments": 1, "created_utc": created, "permalink": f"/r/demo/comments/p{i}/x/",
            "retrieved_on": created + 24, "_meta": {"retrieved_2nd_on": created + 130000}}


def test_comment_search_maps_and_pages_back(monkeypatch):
    calls: list[dict] = []
    pages = [[_c(i, 2000 - i) for i in range(100)], [_c(100 + i, 1800 - i) for i in range(20)]]

    def fake(path, params, timeout=40):
        calls.append({"path": path, **params})
        return pages[len(calls) - 1]

    monkeypatch.setattr(ra, "_get", fake)
    out = ra.search_comments('"wallet drained"', subreddit="demo", limit=150, delay_s=0)
    assert len(out) == 120 and out[0].id == "c0" and out[0].link_title == "Ledger scam"
    assert out[0].subreddit == "demo" and out[0].score_as_of == 2030.0
    assert calls[0] == {"path": "/comments/search", "subreddit": "demo", "author": None, "body": '"wallet drained"',
                        "link_id": None, "after": None, "before": None, "sort": "desc", "limit": 100}
    assert calls[1]["before"] == 1901 and calls[1]["limit"] == 50


def test_posts_take_the_latest_archive_read_as_score_time(monkeypatch):
    monkeypatch.setattr(ra, "_get", lambda path, params, timeout=40: [_p(1, 1000)])
    (post,) = ra.search_posts(author="alice_demo", limit=1)
    assert post.tier == "arctic_shift" and post.score_as_of == 131000.0 and post.title == "post 1"


def test_full_text_needs_a_scope():
    with pytest.raises(RedditError, match="needs subreddit= or author="):
        ra.search_comments("drained")
    with pytest.raises(RedditError):
        ra.search_posts(query="rug")


def test_user_history_reads_both(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ra, "_get", lambda path, params, timeout=40: seen.append(path) or [])
    assert ra.user_history("u/alice_demo", limit=5, delay_s=0) == ([], [])
    assert seen == ["/posts/search", "/comments/search"]


def test_422_timeout_is_retried_once(monkeypatch):
    attempts: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise HTTPError(req.full_url, 422, "Unprocessable", None,
                            io.BytesIO(b'{"error": "Timeout. Maybe slow down a bit"}'))
        return _Resp(json.dumps({"data": [_c(1, 1000)]}).encode())

    monkeypatch.setattr(ra, "urlopen", fake_urlopen)
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)
    assert [d["id"] for d in ra._get("/comments/search", {"subreddit": "demo", "body": "x", "author": None})] == ["c1"]
    assert len(attempts) == 2 and "author" not in attempts[0]
