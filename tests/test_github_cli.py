"""Tests for scraperx.github_analyzer.cli (T13).

Tests the CLI entry points end-to-end with mocked pipeline dependencies.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from scraperx.github_analyzer.cli import (
    main_github,
    main_trending,
    render_markdown,
)
from scraperx.github_analyzer.schemas import (
    ExternalMention,
    GithubReport,
    RepoTrustScore,
    SecurityAdvisory,
    TrendingRepo,
)

# ---------------------------------------------------------------------------
# render_markdown


def test_render_markdown_basic_report():
    report = GithubReport(
        owner="yt-dlp",
        repo="yt-dlp",
        url="https://github.com/yt-dlp/yt-dlp",
        description="downloader",
        stars=85000,
        forks_count=6000,
        open_issues=300,
        language="Python",
        license_key="unlicense",
        trust=RepoTrustScore(
            bus_factor=62, momentum=80, health=85, readme_quality=90,
            overall=88, rationale="Healthy and active",
        ),
        verdict_markdown="- Point 1\n- Point 2\n- Point 3",
    )
    md = render_markdown(report)
    assert "# yt-dlp/yt-dlp" in md
    assert "**Stars:** 85,000" in md
    assert "88/100" in md
    assert "Healthy and active" in md
    assert "Point 1" in md
    assert "bus_factor: 62/100" in md


def test_render_markdown_archived_warning():
    report = GithubReport(
        owner="o", repo="r", url="u", archived=True, trust=RepoTrustScore(overall=5)
    )
    md = render_markdown(report)
    assert "ARCHIVED" in md


def test_render_markdown_includes_mentions():
    report = GithubReport(
        owner="o", repo="r", url="u",
        mentions=[
            ExternalMention(source="hn", title="HN post", url="https://news.ycombinator.com/item?id=1"),
            ExternalMention(source="reddit", title="Reddit post", url="https://reddit.com/r/x/a"),
        ],
    )
    md = render_markdown(report)
    assert "External mentions (2)" in md
    assert "HN post" in md
    assert "(hn)" in md


def test_render_markdown_includes_advisories():
    report = GithubReport(
        owner="o", repo="r", url="u",
        advisories=[
            SecurityAdvisory(ghsa_id="GHSA-X", severity="high", summary="bad bug"),
        ],
    )
    md = render_markdown(report)
    assert "GHSA-X" in md
    assert "high" in md


def test_render_markdown_includes_warnings():
    report = GithubReport(
        owner="o", repo="r", url="u",
        warnings=["LLM unavailable", "Contributors fetch failed"],
    )
    md = render_markdown(report)
    assert "### Warnings" in md
    assert "LLM unavailable" in md


# ---------------------------------------------------------------------------
# main_github CLI


@pytest.fixture
def mock_analyzer():
    """Patch GithubAnalyzer so tests don't hit the real network."""
    with patch("scraperx.github_analyzer.cli.GithubAnalyzer") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def test_main_github_rejects_garbage_spec(capsys, monkeypatch):
    """Invalid URL → exit code 2, error on stderr."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    rc = main_github(["github", "not-a-url"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "ERROR" in captured.err


def test_main_github_human_output(mock_analyzer, capsys):
    mock_analyzer.analyze_repo.return_value = GithubReport(
        owner="yt-dlp", repo="yt-dlp", url="https://github.com/yt-dlp/yt-dlp",
        stars=85000, trust=RepoTrustScore(overall=88, rationale="good"),
    )
    rc = main_github(["github", "yt-dlp/yt-dlp"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "yt-dlp/yt-dlp" in captured.out
    assert "88/100" in captured.out


def test_main_github_json_output(mock_analyzer, capsys):
    mock_analyzer.analyze_repo.return_value = GithubReport(
        owner="o", repo="r", url="https://github.com/o/r",
        stars=42, trust=RepoTrustScore(overall=55),
    )
    rc = main_github(["github", "o/r", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert parsed["owner"] == "o"
    assert parsed["stars"] == 42
    assert parsed["trust"]["overall"] == 55


def test_main_github_deep_flag_passed_through(mock_analyzer):
    mock_analyzer.analyze_repo.return_value = GithubReport(owner="o", repo="r", url="u")
    main_github(["github", "o/r", "--deep"])
    call_kwargs = mock_analyzer.analyze_repo.call_args.kwargs
    assert call_kwargs["deep"] is True


def test_main_github_no_mentions_flag_passed_through(mock_analyzer):
    mock_analyzer.analyze_repo.return_value = GithubReport(owner="o", repo="r", url="u")
    main_github(["github", "o/r", "--no-mentions"])
    call_kwargs = mock_analyzer.analyze_repo.call_args.kwargs
    assert call_kwargs["skip_mentions"] is True


def test_main_github_no_cache_skips_db():
    """When --no-cache is set, GithubAnalyzer is constructed with db=None."""
    with patch("scraperx.github_analyzer.cli.GithubAnalyzer") as m:
        m.return_value.analyze_repo.return_value = GithubReport(owner="o", repo="r", url="u")
        main_github(["github", "o/r", "--no-cache"])
        assert m.call_args.kwargs["db"] is None


# ---------------------------------------------------------------------------
# main_trending CLI


TRENDING_FIXTURE = [
    TrendingRepo(
        full_name="yt-dlp/yt-dlp",
        description="downloader",
        language="Python",
        stars=85000,
        stars_today=200,
        url="https://github.com/yt-dlp/yt-dlp",
    ),
    TrendingRepo(
        full_name="rust-lang/rust",
        description="safe systems language",
        language="Rust",
        stars=100000,
        stars_today=150,
        url="https://github.com/rust-lang/rust",
    ),
]


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_human_output(mock_fetch, capsys):
    mock_fetch.return_value = TRENDING_FIXTURE
    rc = main_trending(["trending"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Trending on GitHub" in captured.out
    assert "yt-dlp/yt-dlp" in captured.out
    assert "+200↑" in captured.out
    assert "Python" in captured.out


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_json_output(mock_fetch, capsys):
    mock_fetch.return_value = TRENDING_FIXTURE
    rc = main_trending(["trending", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 2
    assert parsed[0]["full_name"] == "yt-dlp/yt-dlp"
    assert parsed[0]["stars_today"] == 200


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_passes_filters(mock_fetch):
    mock_fetch.return_value = []
    main_trending(["trending", "--since", "weekly", "--lang", "python", "--spoken", "en"])
    mock_fetch.assert_called_once_with(
        since="weekly",
        language="python",
        spoken_language_code="en",
    )


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_limit_truncates(mock_fetch, capsys):
    # Return 3 repos but limit to 1
    three = [
        *TRENDING_FIXTURE,
        TrendingRepo(full_name="third/repo", stars=1, url="https://github.com/third/repo"),
    ]
    mock_fetch.return_value = three
    rc = main_trending(["trending", "--limit", "1", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 1


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_empty_result(mock_fetch, capsys):
    mock_fetch.return_value = []
    rc = main_trending(["trending"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No trending" in captured.err


@patch("scraperx.github_analyzer.cli.fetch_trending")
def test_main_trending_invalid_since_rejected_by_argparse(mock_fetch):
    """argparse enforces choices=[daily, weekly, monthly] → SystemExit."""
    with pytest.raises(SystemExit):
        main_trending(["trending", "--since", "hourly"])


# ---------------------------------------------------------------------------
# Dependency injection helpers


def test_try_get_local_llm_fn_returns_none_when_unavailable():
    """local-ai-mcp isn't installed as a Python package on this machine →
    the helper returns None and pipeline uses heuristic."""
    from scraperx.github_analyzer.cli import _try_get_local_llm_fn

    # Should not raise regardless of environment
    result = _try_get_local_llm_fn()
    assert result is None or callable(result)


def test_try_get_web_search_fn_returns_none_when_unavailable():
    from scraperx.github_analyzer.cli import _try_get_web_search_fn

    result = _try_get_web_search_fn()
    assert result is None or callable(result)


# ---------------------------------------------------------------------------
# __main__ dispatch


def test_main_module_dispatches_github_subcommand(monkeypatch, capsys):
    """Verify that `python -m scraperx github ...` routes to main_github."""
    import scraperx.__main__ as entry

    with patch("scraperx.github_analyzer.cli.main_github") as m:
        m.return_value = 0
        monkeypatch.setattr("sys.argv", ["scraperx", "github", "o/r"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 0
        m.assert_called_once()


def test_main_module_dispatches_trending_subcommand(monkeypatch):
    import scraperx.__main__ as entry

    with patch("scraperx.github_analyzer.cli.main_trending") as m:
        m.return_value = 0
        monkeypatch.setattr("sys.argv", ["scraperx", "trending"])
        with pytest.raises(SystemExit) as exc:
            entry.main()
        assert exc.value.code == 0
        m.assert_called_once()
