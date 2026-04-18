"""End-to-end pipeline integration test (T14).

Verifies that with every external dependency mocked (GitHub REST, mention
adapters, web_search, local_llm), the full pipeline produces a populated
GithubReport — all 11 stages compose correctly, and the final output has
scores, mentions, a verdict, and citations.

This is the "bottom-to-top" integration test that would catch regressions
where individual components drift out of contract (e.g. a mention adapter
returns a tuple instead of a list, or synthesize expects a different shape).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from scraperx.github_analyzer.core import GithubAnalyzer
from scraperx.github_analyzer.schemas import ExternalMention

# ---------------------------------------------------------------------------
# Fake payloads — realistic shape, minimal content


FAKE_REPO = {
    "id": 123,
    "name": "yt-dlp",
    "full_name": "yt-dlp/yt-dlp",
    "description": "feature-rich video downloader",
    "stargazers_count": 85000,
    "forks_count": 6000,
    "open_issues_count": 300,
    "language": "Python",
    "license": {"key": "unlicense", "name": "The Unlicense"},
    "archived": False,
    "has_issues": True,
    "pushed_at": "2026-04-10T00:00:00Z",
    "created_at": "2021-01-01T00:00:00Z",
    "default_branch": "master",
}

FAKE_CONTRIBUTORS = [
    {"login": "alice", "contributions": 500, "html_url": "https://github.com/alice"},
    {"login": "bob", "contributions": 300, "html_url": "https://github.com/bob"},
    {"login": "carol", "contributions": 100, "html_url": "https://github.com/carol"},
]

FAKE_COMMITS = [
    {
        "sha": f"sha{i}",
        "commit": {
            "author": {
                "name": "alice",
                "date": "2026-04-01T00:00:00Z",  # Inside last 90 days
            }
        },
    }
    for i in range(20)
]

FAKE_README = {
    "content": "IyB5dC1kbHAKCiMjIEluc3RhbGwKCmBgYGJhc2gKcGlwIGluc3RhbGwgeXQtZGxwCmBgYAo=",
    "encoding": "base64",
}

FAKE_FORKS = [
    {
        "full_name": "fork-owner/yt-dlp",
        "stargazers_count": 50,
        "pushed_at": "2026-03-15T00:00:00Z",
        "html_url": "https://github.com/fork-owner/yt-dlp",
    }
]

FAKE_ADVISORIES = [
    {
        "ghsa_id": "GHSA-test-1234",
        "severity": "medium",
        "summary": "URL parsing flaw",
        "published_at": "2026-03-01T00:00:00Z",
        "html_url": "https://github.com/yt-dlp/yt-dlp/security/advisories/GHSA-test-1234",
    }
]


# ---------------------------------------------------------------------------
# Full pipeline test


@patch("scraperx.github_analyzer.core.GithubAPI")
def test_full_pipeline_produces_populated_report(mock_api_cls):
    """Every external dep mocked — run analyze_repo and check the output
    carries data from every stage of the pipeline."""
    # GitHub API mock
    api_mock = MagicMock()
    api_mock.get_repo.return_value = FAKE_REPO
    api_mock.get_contributors.return_value = FAKE_CONTRIBUTORS
    api_mock.get_recent_commits.return_value = FAKE_COMMITS
    api_mock.get_readme.return_value = FAKE_README
    api_mock.get_top_forks.return_value = FAKE_FORKS
    api_mock.get_advisories.return_value = FAKE_ADVISORIES
    mock_api_cls.return_value = api_mock

    # Mention adapters — patch at the registry level
    def fake_hn(owner, repo, db=None):
        return [
            ExternalMention(
                source="hn", title="Great tool", url="https://n.ycombinator.com/item?id=1",
                score=200, author="alice",
            )
        ]

    def fake_reddit(owner, repo, db=None):
        return [
            ExternalMention(
                source="reddit", title="r/python loves this",
                url="https://reddit.com/r/python/a", score=150,
            )
        ]

    # Web search (Tier B)
    def fake_web_search(query, n_results=20):
        return [
            {
                "title": "Lobsters thread",
                "url": "https://lobste.rs/s/xyz",
                "snippet": "Insightful discussion",
                "score": 30,
            }
        ]

    # LLM verdict
    def fake_llm(prompt, task_type="fast", max_tokens=1200):
        return json.dumps(
            {
                "overall": 91,
                "rationale": "Mature, well-maintained tool with broad community traction.",
                "verdict_markdown": (
                    "- Strong bus factor + high momentum [1]\n"
                    "- Clean license and active releases\n"
                    "- Positive community reception [2][3]"
                ),
            }
        )

    with patch(
        "scraperx.github_analyzer.core.ALL_SOURCES",
        new={"hn": fake_hn, "reddit": fake_reddit},
    ):
        analyzer = GithubAnalyzer(
            github_token="test-token",
            db=None,
            web_search_fn=fake_web_search,
            local_llm_fn=fake_llm,
        )
        report = analyzer.analyze_repo("yt-dlp/yt-dlp")

    # ---- Identity ----
    assert report.owner == "yt-dlp"
    assert report.repo == "yt-dlp"
    assert report.url == "https://github.com/yt-dlp/yt-dlp"

    # ---- Core metadata absorbed ----
    assert report.description == "feature-rich video downloader"
    assert report.stars == 85000
    assert report.language == "Python"
    assert report.license_key == "unlicense"
    assert report.archived is False
    assert report.default_branch == "master"

    # ---- Contributors / forks / advisories populated ----
    assert len(report.contributors) == 3
    assert report.contributors[0].handle == "alice"
    assert report.contributors[0].commits == 500
    assert len(report.notable_forks) == 1
    assert report.notable_forks[0].full_name == "fork-owner/yt-dlp"
    assert len(report.advisories) == 1
    assert report.advisories[0].ghsa_id == "GHSA-test-1234"

    # ---- Sub-scores computed (all non-zero for a healthy repo) ----
    assert report.trust.bus_factor > 0
    assert report.trust.momentum > 0
    assert report.trust.health > 0
    assert report.trust.readme_quality > 0

    # ---- Mentions collected from Tier A (hn + reddit) + Tier B (web) ----
    sources = {m.source for m in report.mentions}
    assert "hn" in sources
    assert "reddit" in sources
    assert "semantic_web" in sources
    assert len(report.mentions) >= 3

    # ---- Synthesis verdict present ----
    assert report.trust.overall == 91
    assert "Mature" in report.trust.rationale
    assert "[1]" in report.verdict_markdown
    assert report.verdict_markdown.count("-") >= 3  # 3 bullets

    # ---- Provenance stamped ----
    assert report.analyzed_at > 0
    assert report.scraperx_version != ""

    # ---- No critical warnings ----
    # Some adapters not in ALL_SOURCES will trigger "Tier-A X failed" warnings
    # — but no 500s / 404s from the mocks
    for w in report.warnings:
        assert "Core metadata" not in w, f"Unexpected core failure: {w}"


@patch("scraperx.github_analyzer.core.GithubAPI")
def test_pipeline_survives_partial_failures(mock_api_cls):
    """If half the API calls 500, the pipeline still returns a useful
    report with warnings — doesn't crash, doesn't half-populate silently."""
    from scraperx.github_analyzer.github_api import GithubAPIError

    api_mock = MagicMock()
    api_mock.get_repo.return_value = FAKE_REPO  # Core succeeds
    api_mock.get_contributors.side_effect = GithubAPIError("500: down")
    api_mock.get_recent_commits.side_effect = GithubAPIError("500: down")
    api_mock.get_readme.side_effect = GithubAPIError("500: down")
    api_mock.get_top_forks.side_effect = GithubAPIError("500: down")
    api_mock.get_advisories.side_effect = GithubAPIError("500: down")
    mock_api_cls.return_value = api_mock

    with patch("scraperx.github_analyzer.core.ALL_SOURCES", new={}):
        report = GithubAnalyzer(local_llm_fn=None).analyze_repo("o/r")

    # Core fields came through
    assert report.stars == 85000
    # Sub-scores still produced (from empty / zero inputs, clamp to low values)
    assert report.trust.health > 0  # /repos payload alone drives this
    # Contributors / commits / forks / advisories all empty
    assert report.contributors == []
    assert report.notable_forks == []
    assert report.advisories == []
    # Warnings carry the failure info
    assert len([w for w in report.warnings if "500" in w]) >= 4
    # Heuristic fallback kicked in for verdict (no LLM)
    assert report.trust.overall > 0
    assert "heuristic" in report.trust.rationale.lower()


@patch("scraperx.github_analyzer.core.GithubAPI")
def test_pipeline_repo_404_short_circuits(mock_api_cls):
    """A 404 on the core /repos call returns immediately with a warning —
    no point in running scoring against empty data."""
    from scraperx.github_analyzer.github_api import RepoNotFoundError

    api_mock = MagicMock()
    api_mock.get_repo.side_effect = RepoNotFoundError("404")
    mock_api_cls.return_value = api_mock

    report = GithubAnalyzer().analyze_repo("nobody/nope")

    # Report was returned (not raised)
    assert report.owner == "nobody"
    # Warning present
    assert any("not found" in w.lower() for w in report.warnings)
    # Downstream calls NOT made (contributors etc. were never invoked)
    api_mock.get_contributors.assert_not_called()
    api_mock.get_recent_commits.assert_not_called()


@patch("scraperx.github_analyzer.core.GithubAPI")
def test_pipeline_skip_mentions_flag(mock_api_cls):
    """--no-mentions should bypass Tier A and Tier B entirely."""
    api_mock = MagicMock()
    api_mock.get_repo.return_value = FAKE_REPO
    api_mock.get_contributors.return_value = []
    api_mock.get_recent_commits.return_value = []
    api_mock.get_readme.return_value = {"content": "", "encoding": "base64"}
    api_mock.get_top_forks.return_value = []
    api_mock.get_advisories.return_value = []
    mock_api_cls.return_value = api_mock

    web_called = {"n": 0}

    def fake_web_search(query, n_results=20):
        web_called["n"] += 1
        return []

    mention_called = {"n": 0}

    def fake_mention(owner, repo, db=None):
        mention_called["n"] += 1
        return []

    with patch(
        "scraperx.github_analyzer.core.ALL_SOURCES",
        new={"hn": fake_mention, "reddit": fake_mention},
    ):
        report = GithubAnalyzer(web_search_fn=fake_web_search).analyze_repo(
            "o/r", skip_mentions=True
        )

    assert mention_called["n"] == 0  # Tier A skipped
    assert web_called["n"] == 0       # Tier B skipped
    assert report.mentions == []


# ---------------------------------------------------------------------------
# Coverage audit — module-level sanity


def test_all_new_github_analyzer_modules_importable():
    """Every module in github_analyzer/ should import without error."""
    import scraperx.github_analyzer
    import scraperx.github_analyzer.cli
    import scraperx.github_analyzer.core
    import scraperx.github_analyzer.github_api
    import scraperx.github_analyzer.mentions
    import scraperx.github_analyzer.mentions.arxiv
    import scraperx.github_analyzer.mentions.devto
    import scraperx.github_analyzer.mentions.hn
    import scraperx.github_analyzer.mentions.pwc
    import scraperx.github_analyzer.mentions.reddit
    import scraperx.github_analyzer.mentions.stackoverflow
    import scraperx.github_analyzer.schemas
    import scraperx.github_analyzer.scoring
    import scraperx.github_analyzer.semantic
    import scraperx.github_analyzer.synthesis
    import scraperx.github_analyzer.trending

    # Sanity — key classes exist
    assert scraperx.github_analyzer.GithubAnalyzer is scraperx.github_analyzer.core.GithubAnalyzer
    assert callable(scraperx.github_analyzer.cli.main_github)
    assert callable(scraperx.github_analyzer.cli.main_trending)
    assert callable(scraperx.github_analyzer.trending.fetch_trending)


def test_all_sources_matches_registry_count():
    """Confirm 6 Tier-A sources (per plan + Q4 scope)."""
    from scraperx.github_analyzer.mentions import ALL_SOURCES

    assert len(ALL_SOURCES) == 6
