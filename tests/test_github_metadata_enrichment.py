"""Tests for v1.4.1 metadata enrichment — the baseline-worker/domain-expert
converging insight from /reason: let qwen do implicit weighting by seeing
per-platform authority signals in the prompt (karma, subscribers, reputation
etc.) instead of hardcoded weight math.

These tests verify:
1. Each adapter captures the additional authority fields into ExternalMention.metadata.
2. synthesis._format_mentions() surfaces those fields into the LLM prompt.
3. _compact_num produces human-dense output qwen can parse.
4. Absent metadata doesn't break the prompt (graceful degradation).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from scraperx.github_analyzer.mentions.devto import search as devto_search
from scraperx.github_analyzer.mentions.hn import search as hn_search
from scraperx.github_analyzer.mentions.reddit import search as reddit_search
from scraperx.github_analyzer.mentions.stackoverflow import search as stackoverflow_search
from scraperx.github_analyzer.schemas import ExternalMention
from scraperx.github_analyzer.synthesis import (
    _authority_blurb,
    _compact_num,
    _format_mentions,
)

URLOPEN_PATH = "scraperx.github_analyzer.mentions._http.urllib.request.urlopen"


def _mock_json_response(body):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ---------------------------------------------------------------------------
# _compact_num


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (42, "42"),
        (999, "999"),
        (1_000, "1k"),
        (1_500, "1.5k"),
        (4_500, "4.5k"),
        (12_345, "12.3k"),
        (950_000, "950k"),      # stays in k-tier (far enough below boundary)
        (999_499, "999.5k"),    # boundary-adjacent, still k (not misleading "1000k")
        (999_500, "1M"),        # boundary — rounds to 1.0M at :.1f, promotes to M
        (999_999, "1M"),        # fix: was wrongly "1000k" in pre-review version
        (1_000_000, "1M"),
        (1_300_000, "1.3M"),
        (5_000_000, "5M"),
        (12_345_678, "12.3M"),
    ],
)
def test_compact_num_formatting(n, expected):
    assert _compact_num(n) == expected


def test_compact_num_handles_garbage():
    assert _compact_num(None) == "None"
    assert _compact_num("not-a-number") == "not-a-number"


# ---------------------------------------------------------------------------
# Adapters capture the extra authority metadata


@patch(URLOPEN_PATH)
def test_hn_captures_num_comments(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "hits": [
                {
                    "objectID": "1",
                    "title": "Long discussion",
                    "url": "https://example.com",
                    "points": 500,
                    "author": "alice",
                    "created_at": "2024-01-01T00:00:00Z",
                    "num_comments": 312,
                }
            ]
        }
    )
    result = hn_search("yt-dlp", "yt-dlp")
    assert result[0].metadata["num_comments"] == 312


@patch(URLOPEN_PATH)
def test_reddit_captures_subreddit_authority(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "title": "Nice tool",
                            "permalink": "/r/python/comments/abc/",
                            "subreddit": "python",
                            "subreddit_subscribers": 1_300_000,
                            "score": 250,
                            "num_comments": 45,
                            "upvote_ratio": 0.94,
                            "author": "carol",
                            "created_utc": 1700000000.0,
                        }
                    }
                ]
            }
        }
    )
    m = reddit_search("yt-dlp", "yt-dlp")[0]
    assert m.metadata["subreddit_subscribers"] == 1_300_000
    assert m.metadata["num_comments"] == 45
    assert m.metadata["upvote_ratio"] == 0.94


@patch(URLOPEN_PATH)
def test_stackoverflow_captures_reputation(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "items": [
                {
                    "question_id": 42,
                    "title": "How to use",
                    "link": "https://stackoverflow.com/questions/42",
                    "score": 15,
                    "creation_date": 1700000000,
                    "tags": ["python"],
                    "owner": {"display_name": "dave", "reputation": 12345},
                    "is_answered": True,
                    "answer_count": 3,
                    "view_count": 5000,
                    "accepted_answer_id": 43,
                }
            ]
        }
    )
    m = stackoverflow_search("yt-dlp", "yt-dlp")[0]
    assert m.metadata["asker_reputation"] == 12345
    assert m.metadata["view_count"] == 5000
    assert m.metadata["has_accepted_answer"] is True


@patch(URLOPEN_PATH)
def test_stackoverflow_captures_answer_count(mock_urlopen):
    """An unanswered Q with 7 answers ≠ 0 answers — different signal.
    (Code-reviewer flag, 2026-04-19)."""
    mock_urlopen.return_value = _mock_json_response(
        {
            "items": [
                {
                    "question_id": 100,
                    "title": "Popular but unresolved",
                    "link": "https://stackoverflow.com/questions/100",
                    "score": 20,
                    "creation_date": 1700000000,
                    "tags": ["python"],
                    "owner": {"display_name": "x", "reputation": 500},
                    "is_answered": False,
                    "answer_count": 7,
                    "view_count": 9000,
                }
            ]
        }
    )
    m = stackoverflow_search("o", "r")[0]
    assert m.metadata["answer_count"] == 7
    assert m.metadata["has_accepted_answer"] is False


@patch(URLOPEN_PATH)
def test_reddit_coerces_string_upvote_ratio(mock_urlopen):
    """Reddit's CDN-cached responses sometimes return upvote_ratio as string.
    safe_float coerces so metadata type is honest (code-reviewer flag)."""
    mock_urlopen.return_value = _mock_json_response(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "title": "Post",
                            "permalink": "/r/x/abc/",
                            "subreddit": "x",
                            "score": 10,
                            "upvote_ratio": "0.92",  # STRING from CDN
                            "author": "u",
                            "created_utc": 1700000000.0,
                        }
                    }
                ]
            }
        }
    )
    m = reddit_search("o", "r")[0]
    assert m.metadata["upvote_ratio"] == 0.92
    assert isinstance(m.metadata["upvote_ratio"], float)


@patch(URLOPEN_PATH)
def test_stackoverflow_no_accepted_answer(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        {
            "items": [
                {
                    "question_id": 42,
                    "title": "Broken install",
                    "link": "https://stackoverflow.com/questions/42",
                    "score": 0,
                    "creation_date": 1700000000,
                    "tags": [],
                    "is_answered": False,
                    "answer_count": 0,
                    "view_count": 80,
                    # No accepted_answer_id
                }
            ]
        }
    )
    m = stackoverflow_search("yt-dlp", "yt-dlp")[0]
    assert m.metadata["has_accepted_answer"] is False


@patch(URLOPEN_PATH)
def test_devto_captures_depth_and_engagement(mock_urlopen):
    mock_urlopen.return_value = _mock_json_response(
        [
            {
                "id": 1,
                "title": "Deep dive into yt-dlp/yt-dlp",
                "description": "a long-form tutorial",
                "url": "https://dev.to/a/1",
                "positive_reactions_count": 120,
                "user": {"username": "eve"},
                "tag_list": ["python", "cli"],
                "reading_time_minutes": 18,
                "comments_count": 24,
            }
        ]
    )
    m = devto_search("yt-dlp", "yt-dlp")[0]
    assert m.metadata["reading_time_minutes"] == 18
    assert m.metadata["comments_count"] == 24


# ---------------------------------------------------------------------------
# _authority_blurb — the prompt-level enrichment


def test_authority_blurb_hn_with_comments():
    m = ExternalMention(
        source="hn", title="t", url="u", metadata={"num_comments": 312}
    )
    assert _authority_blurb(m) == " (comments=312)"


def test_authority_blurb_hn_no_comments_is_empty():
    m = ExternalMention(source="hn", title="t", url="u", metadata={})
    assert _authority_blurb(m) == ""


def test_authority_blurb_reddit_full_signals():
    m = ExternalMention(
        source="reddit",
        title="t",
        url="u",
        metadata={
            "subreddit": "python",
            "subreddit_subscribers": 1_300_000,
            "num_comments": 45,
            "upvote_ratio": 0.94,
        },
    )
    blurb = _authority_blurb(m)
    assert "r/python" in blurb
    assert "1.3M" in blurb
    assert "comments=45" in blurb
    assert "upvote=0.94" in blurb


def test_authority_blurb_reddit_no_subscribers_falls_back():
    m = ExternalMention(
        source="reddit",
        title="t",
        url="u",
        metadata={"subreddit": "rust", "num_comments": 10},
    )
    blurb = _authority_blurb(m)
    assert "r/rust" in blurb
    # Shouldn't print "None subs"
    assert "None" not in blurb


def test_authority_blurb_reddit_bad_upvote_ratio_tolerated():
    m = ExternalMention(
        source="reddit",
        title="t",
        url="u",
        metadata={"subreddit": "x", "upvote_ratio": "garbage"},
    )
    blurb = _authority_blurb(m)
    # Doesn't crash, doesn't include the bad value
    assert "upvote" not in blurb


def test_authority_blurb_stackoverflow_full_signals():
    m = ExternalMention(
        source="stackoverflow",
        title="t",
        url="u",
        metadata={
            "asker_reputation": 12_345,
            "view_count": 5_000,
            "has_accepted_answer": True,
        },
    )
    blurb = _authority_blurb(m)
    assert "rep=12.3k" in blurb
    assert "views=5k" in blurb
    assert "answered=Y" in blurb


def test_authority_blurb_stackoverflow_unanswered_omits_flag():
    m = ExternalMention(
        source="stackoverflow",
        title="t",
        url="u",
        metadata={"asker_reputation": 100, "has_accepted_answer": False},
    )
    blurb = _authority_blurb(m)
    assert "rep=100" in blurb
    assert "answered" not in blurb


def test_authority_blurb_devto():
    m = ExternalMention(
        source="devto",
        title="t",
        url="u",
        metadata={"reading_time_minutes": 18, "comments_count": 24},
    )
    blurb = _authority_blurb(m)
    assert "read=18min" in blurb
    assert "comments=24" in blurb


def test_authority_blurb_semantic_web_shows_host():
    m = ExternalMention(
        source="semantic_web",
        title="t",
        url="u",
        metadata={"host": "lobste.rs"},
    )
    assert _authority_blurb(m) == " (lobste.rs)"


def test_authority_blurb_arxiv_and_pwc_produce_empty():
    """arXiv + PWC have no extra authority signals in free payload — blurb empty."""
    for src in ("arxiv", "pwc"):
        m = ExternalMention(source=src, title="t", url="u", metadata={})
        assert _authority_blurb(m) == ""


def test_authority_blurb_unknown_source_is_empty():
    m = ExternalMention(source="twitter", title="t", url="u", metadata={"key": "val"})
    assert _authority_blurb(m) == ""


# ---------------------------------------------------------------------------
# _format_mentions integration — what qwen actually sees


def test_format_mentions_surfaces_authority_to_prompt():
    """End-to-end: richer metadata lands in the synthesis prompt."""
    mentions = [
        ExternalMention(
            source="hn",
            title="Great discussion",
            url="https://n.ycombinator.com/item?id=1",
            score=500,
            metadata={"num_comments": 312},
        ),
        ExternalMention(
            source="reddit",
            title="r/python praises it",
            url="https://reddit.com/r/python/x",
            score=250,
            metadata={
                "subreddit": "python",
                "subreddit_subscribers": 1_300_000,
                "num_comments": 45,
                "upvote_ratio": 0.94,
            },
        ),
        ExternalMention(
            source="stackoverflow",
            title="Q: install error",
            url="https://so.com/q/42",
            score=15,
            metadata={
                "asker_reputation": 12_345,
                "has_accepted_answer": True,
            },
        ),
    ]
    prompt = _format_mentions(mentions)

    # Authority signals are visible to qwen now
    assert "comments=312" in prompt
    assert "r/python" in prompt
    assert "1.3M" in prompt
    assert "upvote=0.94" in prompt
    assert "rep=12.3k" in prompt
    assert "answered=Y" in prompt

    # Plus the original fields still present
    assert "Great discussion" in prompt
    assert "score=500" in prompt


def test_format_mentions_empty_metadata_still_renders_cleanly():
    """When a mention has no authority metadata, the blurb collapses —
    no '()' artifacts."""
    m = ExternalMention(
        source="hn",
        title="Plain title",
        url="https://n.ycombinator.com/item?id=1",
        score=50,
    )
    prompt = _format_mentions([m])
    assert "hn()" not in prompt
    assert "hn :" not in prompt  # No space-before-colon either
    # Normal format still works
    assert "Plain title" in prompt


def test_format_mentions_mixed_enriched_and_bare():
    """Realistic: some mentions have rich metadata, others don't."""
    mentions = [
        ExternalMention(source="hn", title="Rich", url="u1", score=100,
                        metadata={"num_comments": 50}),
        ExternalMention(source="arxiv", title="Paper", url="u2", score=0),
        ExternalMention(source="reddit", title="Post", url="u3", score=30,
                        metadata={"subreddit": "rust"}),
    ]
    prompt = _format_mentions(mentions)
    assert "comments=50" in prompt       # hn enriched
    assert "arxiv:" in prompt            # arxiv bare (no blurb)
    assert "r/rust" in prompt            # reddit partially enriched
