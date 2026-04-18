"""Smoke tests for scraperx.github_analyzer skeleton (T1).

Covers:
- Dataclass defaults + asdict round-trip
- parse_repo_url() across URL shapes (https, ssh, shorthand, .git, sub-path)
- Error paths (empty, wrong host, missing repo, bad chars)
- analyze_repo() stub raises NotImplementedError AFTER validating the spec
- Top-level scraperx package re-exports the public API

T3+ will replace the NotImplementedError behavior with a live pipeline; the
URL-parsing and schema tests should survive unchanged.
"""

from __future__ import annotations

import dataclasses

import pytest

from scraperx.github_analyzer import (
    ContributorInfo,
    ExternalMention,
    ForkInfo,
    GithubAnalyzer,
    GithubReport,
    InvalidRepoUrlError,
    RepoTrustScore,
    SecurityAdvisory,
    TrendingRepo,
    analyze_repo,
    parse_repo_url,
)

# ---------------------------------------------------------------------------
# Schemas


def test_github_report_defaults_and_serializable():
    r = GithubReport(owner="yt-dlp", repo="yt-dlp", url="https://github.com/yt-dlp/yt-dlp")
    assert r.stars == 0
    assert r.contributors == []
    assert r.mentions == []
    assert r.advisories == []
    assert isinstance(r.trust, RepoTrustScore)
    assert r.trust.overall == 0

    d = r.to_dict()
    # Nested dataclasses must also be dicts (asdict recurses)
    assert isinstance(d, dict)
    assert d["owner"] == "yt-dlp"
    assert isinstance(d["trust"], dict)
    assert d["trust"]["overall"] == 0
    assert d["contributors"] == []


def test_external_mention_metadata_is_per_instance():
    """Regression guard: default_factory must prevent shared-dict mutation."""
    a = ExternalMention(source="hn", title="t", url="u")
    b = ExternalMention(source="reddit", title="t", url="u")
    a.metadata["k"] = 1
    assert b.metadata == {}


def test_all_dataclasses_instantiable_with_minimum_required():
    # None of these should raise; every field either has a default or we pass it
    ContributorInfo(handle="octocat")
    ForkInfo(full_name="fork/repo")
    ExternalMention(source="hn", title="t", url="u")
    TrendingRepo(full_name="owner/repo")
    SecurityAdvisory(ghsa_id="GHSA-xxxx-yyyy-zzzz")
    RepoTrustScore()


def test_repo_report_roundtrip_with_nested_items():
    r = GithubReport(
        owner="o",
        repo="r",
        url="https://github.com/o/r",
        contributors=[ContributorInfo(handle="alice", commits=10)],
        mentions=[ExternalMention(source="hn", title="t", url="u", score=42)],
        advisories=[SecurityAdvisory(ghsa_id="GHSA-1", severity="high")],
    )
    d = dataclasses.asdict(r)
    assert d["contributors"][0]["handle"] == "alice"
    assert d["mentions"][0]["score"] == 42
    assert d["advisories"][0]["severity"] == "high"


# ---------------------------------------------------------------------------
# parse_repo_url


@pytest.mark.parametrize(
    "spec,owner,repo",
    [
        ("owner/repo", "owner", "repo"),
        ("https://github.com/owner/repo", "owner", "repo"),
        ("https://github.com/owner/repo.git", "owner", "repo"),
        ("https://github.com/owner/repo/tree/main/subdir", "owner", "repo"),
        ("https://github.com/owner/repo/issues/42", "owner", "repo"),
        ("http://github.com/owner/repo", "owner", "repo"),
        ("git@github.com:owner/repo.git", "owner", "repo"),
        ("  owner/repo  ", "owner", "repo"),
        # Dots, dashes, underscores allowed in repo name
        ("https://github.com/yt-dlp/yt-dlp", "yt-dlp", "yt-dlp"),
        ("https://github.com/owner/some.repo_name", "owner", "some.repo_name"),
    ],
)
def test_parse_repo_url_valid(spec, owner, repo):
    ref = parse_repo_url(spec)
    assert ref.owner == owner
    assert ref.repo == repo
    assert ref.full_name == f"{owner}/{repo}"
    assert ref.url == f"https://github.com/{owner}/{repo}"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "just-owner",
        "https://gitlab.com/owner/repo",
        "https://github.com/",
        "https://github.com/only-owner",
        "owner/repo with space",
        "owner/ ",
    ],
)
def test_parse_repo_url_rejects_garbage(bad):
    with pytest.raises(InvalidRepoUrlError):
        parse_repo_url(bad)


# ---------------------------------------------------------------------------
# GithubAnalyzer stub


def test_analyze_repo_validates_before_raising_not_implemented():
    """The stub should still run URL validation — that way CLI/MCP can
    surface "bad URL" errors now (before T3 ships) rather than a confusing
    NotImplementedError."""
    with pytest.raises(InvalidRepoUrlError):
        analyze_repo("not-a-url")


def test_analyze_repo_stub_raises_after_valid_url():
    with pytest.raises(NotImplementedError) as exc:
        analyze_repo("https://github.com/owner/repo")
    assert "owner/repo" in str(exc.value)


def test_github_analyzer_instance_has_expected_attrs():
    ga = GithubAnalyzer(github_token="tok", db_path="/tmp/x.db", use_local_web=False)
    assert ga.github_token == "tok"
    assert ga.db_path == "/tmp/x.db"
    assert ga.use_local_web is False


# ---------------------------------------------------------------------------
# Top-level re-exports (ensures wiring in scraperx/__init__.py stayed intact)


def test_top_level_reexports():
    import scraperx

    # Names exist
    assert hasattr(scraperx, "GithubAnalyzer")
    assert hasattr(scraperx, "GithubReport")
    assert hasattr(scraperx, "InvalidRepoUrlError")
    assert hasattr(scraperx, "analyze_github_repo")
    assert hasattr(scraperx, "parse_github_repo_url")

    # And they point at the same objects as the submodule
    from scraperx import github_analyzer as ga_mod

    assert scraperx.GithubAnalyzer is ga_mod.GithubAnalyzer
    assert scraperx.analyze_github_repo is ga_mod.analyze_repo
    assert scraperx.parse_github_repo_url is ga_mod.parse_repo_url
