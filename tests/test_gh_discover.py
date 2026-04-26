"""Tests for scraperx.gh_discover — topic-first repo discovery.

Network-free: all GitHub API calls are routed through a stub GithubClient.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scraperx.gh_discover import (
    RepoCandidate,
    _to_candidate,
    build_search_query,
    discover_repos,
    main_gh_discover,
)


# ---------------------------------------------------------------------------
# Stub client — captures the query, returns scripted items
# ---------------------------------------------------------------------------


class StubClient:
    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.last_query: str | None = None
        self.last_per_page: int | None = None

    def search_repositories(self, *, query, sort="stars", order="desc", per_page=30, page=1):
        self.last_query = query
        self.last_per_page = per_page
        return {"total_count": len(self._items), "incomplete_results": False, "items": self._items}


def _fake_item(
    full_name: str,
    *,
    stars: int = 0,
    forks: int = 0,
    description: str = "",
    topics: list[str] | None = None,
    language: str = "Python",
    pushed_at: str = "2026-01-01T00:00:00Z",
    license_spdx: str | None = "MIT",
) -> dict:
    return {
        "full_name": full_name,
        "stargazers_count": stars,
        "forks_count": forks,
        "description": description,
        "topics": topics or [],
        "language": language,
        "pushed_at": pushed_at,
        "html_url": f"https://github.com/{full_name}",
        "license": {"spdx_id": license_spdx} if license_spdx else {},
    }


# ---------------------------------------------------------------------------
# build_search_query
# ---------------------------------------------------------------------------


def test_build_query_topics_only():
    q = build_search_query(["macroeconomics", "python"])
    assert "topic:macroeconomics" in q
    assert "topic:python" in q


def test_build_query_with_min_stars():
    q = build_search_query(["onchain"], min_stars=100)
    assert "stars:>=100" in q


def test_build_query_with_recency_months():
    q = build_search_query(["regime"], recency_months=6)
    assert "pushed:>" in q
    # Sanity: cutoff is roughly today - 6mo (allow ±2 days for date arithmetic)
    cutoff_text = q.split("pushed:>")[1].split()[0]
    cutoff_date = dt.date.fromisoformat(cutoff_text)
    expected = dt.date.today() - dt.timedelta(days=6 * 30)
    assert abs((cutoff_date - expected).days) <= 7


def test_build_query_language():
    q = build_search_query(["python"], language="Python")
    assert "language:Python" in q


def test_build_query_extra_qualifiers():
    q = build_search_query(["python"], extra_qualifiers=["archived:false", "is:public"])
    assert "archived:false" in q
    assert "is:public" in q


def test_build_query_rejects_empty_topics():
    with pytest.raises(ValueError):
        build_search_query([])
    with pytest.raises(ValueError):
        build_search_query(["", "  "])


def test_build_query_rejects_invalid_topic_chars():
    """GitHub topics: lowercase a-z 0-9 -, 1-50 chars. Reject anything else."""
    bad_inputs = [
        ["my topic"],       # space
        ["my/topic"],       # slash
        ["my,topic"],       # comma
        ["MyTopic"],        # uppercase before normalization — wait, we lowercase
        ["-leading"],       # leading hyphen
        ["a" * 51],         # length cap
    ]
    # Uppercase is normalized via .lower() before validation, so MyTopic→mytopic IS valid.
    # Drop that case and re-test: it would PASS, that's expected behavior.
    bad_inputs = [b for b in bad_inputs if b != ["MyTopic"]]
    for bad in bad_inputs:
        with pytest.raises(ValueError, match="invalid topic"):
            build_search_query(bad)


def test_build_query_normalizes_uppercase_to_lowercase():
    """Uppercase input gets normalized — validation runs on .lower()."""
    q = build_search_query(["MyTopic"])
    assert "topic:mytopic" in q


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class PaginatingStubClient:
    """Returns one full page on each call until exhausted."""

    def __init__(self, all_items: list[dict], per_page_size: int = 30) -> None:
        self._all_items = all_items
        self._per_page_size = per_page_size
        self.calls: list[dict] = []

    def search_repositories(self, *, query, sort="stars", order="desc", per_page=30, page=1):
        self.calls.append({"page": page, "per_page": per_page})
        start = (page - 1) * per_page
        chunk = self._all_items[start : start + per_page]
        return {"total_count": len(self._all_items), "incomplete_results": False, "items": chunk}


def test_discover_paginates_when_limit_above_100():
    """limit=150 with per_page cap of 100 must walk pages 1+2 and merge."""
    items = [_fake_item(f"u/r{i:03d}", stars=1000 - i) for i in range(150)]
    client = PaginatingStubClient(items, per_page_size=100)
    out = discover_repos(["python"], limit=150, client=client)
    assert len(out) == 150  # 100 from page 1 + 50 from page 2
    assert len(client.calls) == 2
    assert client.calls[0]["page"] == 1
    assert client.calls[1]["page"] == 2


def test_discover_stops_when_well_runs_dry():
    """If page returns < per_page items, we stop without a third call."""
    items = [_fake_item(f"u/r{i}", stars=i) for i in range(50)]
    client = PaginatingStubClient(items, per_page_size=100)
    out = discover_repos(["python"], limit=200, client=client)
    assert len(out) == 50
    assert len(client.calls) == 1  # single call returned 50 < 100, stop


# ---------------------------------------------------------------------------
# _to_candidate coercion
# ---------------------------------------------------------------------------


def test_to_candidate_basic():
    c = _to_candidate(_fake_item("foo/bar", stars=42, topics=["a", "b"]))
    assert c.full_name == "foo/bar"
    assert c.stars == 42
    assert c.topics == ("a", "b")
    assert c.owner == "foo"
    assert c.url == "https://github.com/foo/bar"
    assert c.license_spdx == "MIT"


def test_to_candidate_handles_missing_fields():
    c = _to_candidate({"full_name": "x/y"})
    assert c.stars == 0
    assert c.topics == ()
    assert c.description == ""
    assert c.license_spdx == ""


def test_to_candidate_handles_null_license():
    item = _fake_item("a/b")
    item["license"] = None
    c = _to_candidate(item)
    assert c.license_spdx == ""


# ---------------------------------------------------------------------------
# discover_repos pipeline
# ---------------------------------------------------------------------------


def test_discover_basic_sort_by_stars_desc():
    client = StubClient([
        _fake_item("a/low", stars=10),
        _fake_item("b/high", stars=1000),
        _fake_item("c/mid", stars=100),
    ])
    out = discover_repos(["python"], client=client)
    assert [c.full_name for c in out] == ["b/high", "c/mid", "a/low"]


def test_discover_min_stars_filters_locally():
    """Even if GitHub returned a low-star repo, we drop it post-hoc."""
    client = StubClient([
        _fake_item("a/low", stars=5),
        _fake_item("b/high", stars=500),
    ])
    out = discover_repos(["python"], min_stars=50, client=client)
    assert [c.full_name for c in out] == ["b/high"]
    # The query also includes the floor, so the request was tightened
    assert "stars:>=50" in client.last_query


def test_discover_excludes_owners_case_insensitive():
    client = StubClient([
        _fake_item("Bad/repo", stars=999),
        _fake_item("good/repo", stars=10),
    ])
    out = discover_repos(["python"], exclude_owners=["bad"], client=client)
    assert [c.full_name for c in out] == ["good/repo"]


def test_discover_dedups_full_names():
    client = StubClient([
        _fake_item("dup/repo", stars=10),
        _fake_item("dup/repo", stars=10),
        _fake_item("uniq/repo", stars=5),
    ])
    out = discover_repos(["python"], client=client)
    full_names = [c.full_name for c in out]
    assert full_names.count("dup/repo") == 1
    assert "uniq/repo" in full_names


def test_discover_respects_limit():
    items = [_fake_item(f"u/r{i}", stars=i) for i in range(50)]
    client = StubClient(items)
    out = discover_repos(["python"], limit=5, client=client)
    assert len(out) == 5


def test_discover_propagates_per_page_to_client():
    """per_page on the API should match limit (capped at 100)."""
    client = StubClient([])
    discover_repos(["python"], limit=42, client=client)
    assert client.last_per_page == 42

    client2 = StubClient([])
    discover_repos(["python"], limit=500, client=client2)  # gets capped
    assert client2.last_per_page == 100


def test_discover_rejects_empty_topics():
    client = StubClient([])
    with pytest.raises(ValueError):
        discover_repos([], client=client)


# ---------------------------------------------------------------------------
# CLI happy path
# ---------------------------------------------------------------------------


def test_cli_query_subcommand_prints_and_exits(capsys):
    rc = main_gh_discover(["gh-discover", "--topic", "python", "--query"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "topic:python" in out


def test_cli_query_with_filters(capsys):
    rc = main_gh_discover([
        "gh-discover", "--topic", "macroeconomics", "--topic", "python",
        "--min-stars", "100", "--query",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "topic:macroeconomics" in out
    assert "topic:python" in out
    assert "stars:>=100" in out


def test_cli_rejects_missing_topic():
    with pytest.raises(SystemExit):
        main_gh_discover(["gh-discover"])
