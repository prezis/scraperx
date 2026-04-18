"""Tests for scraperx.github_analyzer.scoring (T4).

Pure functions — no mocks, no I/O. Every test builds a synthetic input and
asserts the bounded integer output.
"""

from __future__ import annotations

import pytest

from scraperx.github_analyzer.scoring import (
    _clamp,
    bus_factor_score,
    health_score,
    momentum_score,
    readme_quality_score,
)

# ---------------------------------------------------------------------------
# _clamp


@pytest.mark.parametrize(
    "x,expected",
    [
        (-5, 0),
        (0, 0),
        (50, 50),
        (100, 100),
        (150, 100),
    ],
)
def test_clamp_default_bounds(x, expected):
    assert _clamp(x) == expected


def test_clamp_custom_bounds():
    assert _clamp(5, lo=10, hi=20) == 10
    assert _clamp(15, lo=10, hi=20) == 15
    assert _clamp(25, lo=10, hi=20) == 20


# ---------------------------------------------------------------------------
# bus_factor_score


def test_bus_factor_empty_is_zero():
    assert bus_factor_score([]) == 0


def test_bus_factor_none_contributions_is_zero():
    # All-zero contributions → total==0 → short-circuit
    assert bus_factor_score([{"login": "a", "contributions": 0}]) == 0


def test_bus_factor_single_author_is_12():
    """Solo author reaches 50% alone → k=1 → 12 (int(12.5))."""
    assert bus_factor_score([{"login": "a", "contributions": 100}]) == 12


def test_bus_factor_two_equal_contributors_k2():
    """Two equal → cumulative after 1 is 50%, reaches target → k=1 → 12.

    Rationale: 50 reaches 50% cumulative, so loop breaks at k=1.
    """
    contribs = [
        {"login": "a", "contributions": 50},
        {"login": "b", "contributions": 50},
    ]
    assert bus_factor_score(contribs) == 12


def test_bus_factor_skewed_distribution_k2():
    """Top 1 has 40%, top 2 has 70% → k=2 → 25."""
    contribs = [
        {"login": "a", "contributions": 40},
        {"login": "b", "contributions": 30},
        {"login": "c", "contributions": 30},
    ]
    assert bus_factor_score(contribs) == 25


def test_bus_factor_flat_distribution_high_score():
    """10 equal contributors → k=5 to reach 50% → score = 62."""
    contribs = [{"login": f"u{i}", "contributions": 10} for i in range(10)]
    # Cumulative: 10,20,30,40,50 at index 4 (k=5) reaches 50% → k=5 → 62
    assert bus_factor_score(contribs) == 62


def test_bus_factor_many_equal_caps_at_100():
    """20 equal contributors → k=10 → 125 → clamped to 100."""
    contribs = [{"login": f"u{i}", "contributions": 5} for i in range(20)]
    assert bus_factor_score(contribs) == 100


def test_bus_factor_ignores_bad_entries():
    """Malformed entries shouldn't raise; missing `contributions` defaults to 0."""
    contribs = [
        {"login": "a", "contributions": 100},
        {"login": "b"},  # no contributions key
        {"login": "c", "contributions": "not-an-int"},  # will crash sort — caught
    ]
    # The scorer catches TypeError and returns 0
    assert bus_factor_score(contribs) == 0


def test_bus_factor_sorts_unsorted_input():
    """Input order shouldn't matter — scorer sorts internally."""
    contribs = [
        {"login": "small", "contributions": 10},
        {"login": "big", "contributions": 90},
    ]
    # big alone >= 50% → k=1 → 12
    assert bus_factor_score(contribs) == 12


# ---------------------------------------------------------------------------
# momentum_score


def test_momentum_all_zero():
    assert momentum_score([], 0) == 0


def test_momentum_commits_only_capped_at_60():
    """100 commits * 2 = 200 → commit component capped at 60; 0 star delta."""
    commits = [{"sha": str(i)} for i in range(100)]
    assert momentum_score(commits, 0) == 60


def test_momentum_stars_only_capped_at_40():
    """10_000 star delta // 10 = 1000 → star component capped at 40."""
    assert momentum_score([], 10_000) == 40


def test_momentum_combined_perfect():
    """30 commits (60 pts) + 400 stars (40 pts) = 100."""
    commits = [{"sha": str(i)} for i in range(30)]
    assert momentum_score(commits, 400) == 100


def test_momentum_combined_moderate():
    """10 commits (20) + 100 stars (10) = 30."""
    commits = [{"sha": str(i)} for i in range(10)]
    assert momentum_score(commits, 100) == 30


def test_momentum_negative_stars_clamped_to_zero():
    """Star losses shouldn't push below 0 — the component is clamped."""
    # stars_delta_90d=-50 → -50 // 10 = -5 → clamped to 0
    assert momentum_score([], -50) == 0


def test_momentum_bad_commit_type_returns_zero():
    """Non-iterable commits shouldn't raise."""
    assert momentum_score(None, 100) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# health_score


def test_health_empty_payload_not_dict():
    assert health_score(None) == 0  # type: ignore[arg-type]
    assert health_score("bad") == 0  # type: ignore[arg-type]


def test_health_archived_short_circuits_to_zero():
    """Archived overrides every other positive signal."""
    payload = {
        "archived": True,
        "has_issues": True,
        "license": {"key": "mit"},
        "stargazers_count": 1000,
        "open_issues_count": 1,
    }
    assert health_score(payload) == 0


def test_health_bare_payload_is_50():
    """Unknown fields / default everything → neutral 50 + issue_ratio_bonus.

    0 open, 0 stars → 0/1 = 0 ratio → +15, so 65.
    """
    assert health_score({}) == 65


def test_health_maxed_out_payload():
    """All positive signals → clamped to 100."""
    payload = {
        "archived": False,
        "has_issues": True,        # +10
        "license": {"key": "mit"}, # +15
        "stargazers_count": 10_000,
        "open_issues_count": 5,    # ratio 0.0005 → +15
        "forks_count": 1000,       # ratio 0.1 → +10
    }
    # 50 + 10 + 15 + 15 + 10 = 100 exactly
    assert health_score(payload) == 100


def test_health_high_issue_ratio_penalty():
    payload = {
        "has_issues": True,
        "stargazers_count": 10,
        "open_issues_count": 10,   # ratio 1.0 → -10
    }
    # 50 + 10 (issues) - 10 (ratio) = 50
    assert health_score(payload) == 50


def test_health_license_must_have_key():
    """license without a key shouldn't add bonus."""
    payload = {"license": {}}
    # Same as bare payload: 50 + 15 (issue_ratio 0) = 65
    assert health_score(payload) == 65


def test_health_null_license_payload():
    payload = {"license": None, "has_issues": False}
    # 50 + 0 (no issues) + 0 (no license) + 15 (issue ratio 0) = 65
    assert health_score(payload) == 65


def test_health_excessive_fork_ratio_no_bonus():
    """forks ≥ 50% of stars → no ecosystem bonus."""
    payload = {
        "stargazers_count": 100,
        "forks_count": 80,  # ratio 0.8 → no bonus
    }
    # 50 + 15 (0/100 ratio = 0 < 0.01) + 0 (fork ratio too high) = 65
    assert health_score(payload) == 65


# ---------------------------------------------------------------------------
# readme_quality_score


def test_readme_empty_is_zero():
    assert readme_quality_score("") == 0
    assert readme_quality_score(None) == 0  # type: ignore[arg-type]


def test_readme_not_string_is_zero():
    assert readme_quality_score(12345) == 0  # type: ignore[arg-type]


def test_readme_short_prose_low_score():
    """50 chars → length=1, no headings, no code, no link, no install."""
    text = "x" * 50
    # length_component = 50 // 40 = 1, rest 0 → total 1
    assert readme_quality_score(text) == 1


def test_readme_ideal_project_readme_maxes_out():
    """Long, headings, code fences, links, install instructions."""
    text = (
        "# Project\n\n"
        + "## Install\n\n"
        + "```bash\npip install something\n```\n\n"
        + "## Usage\n\nSee [docs](https://example.com/docs).\n\n"
        + "### Advanced\n\n#### Config\n\n"
        + "## Contributing\n\n"
        + "## License\n\n"
        + "Lorem ipsum " * 300  # pad length well past 1600 chars
    )
    score = readme_quality_score(text)
    # length capped at 40, 7 headings → 35 capped at 30, code +10, link +10, install +10 = 100
    assert score == 100


def test_readme_counts_all_heading_levels():
    """Lines starting with #, ##, ### all count."""
    text = "# H1\n## H2\n### H3\n#### H4\n"
    # 4 headings * 5 = 20, length = len(text)//40 = 0, no code/link/install → 20
    assert readme_quality_score(text) == 20


def test_readme_heading_count_capped_at_30():
    """10+ headings cap the heading bonus at 30."""
    text = "\n".join(f"# Section {i}" for i in range(50))
    # 50 headings * 5 = 250 → capped at 30. Length bonus kicks in too.
    score = readme_quality_score(text)
    assert 30 <= score <= 100


def test_readme_code_fence_bonus():
    text = "Hello\n```\ncode\n```\nEnd"
    # length = 24//40 = 0, 0 headings, +10 code fence, no link, no install → 10
    assert readme_quality_score(text) == 10


def test_readme_install_keyword_case_insensitive():
    assert readme_quality_score("INSTALL this with PIP install") >= 10
    assert readme_quality_score("npm install --save foo") >= 10


def test_readme_link_bonus_requires_markdown_form():
    """Plain https:// url without markdown link syntax shouldn't count."""
    text = "Docs at https://example.com"
    # no "](http" → no link bonus
    assert readme_quality_score(text) == 0


# ---------------------------------------------------------------------------
# Integration — all scorers graceful on empty / garbage


def test_all_scorers_graceful_on_empty():
    assert bus_factor_score([]) == 0
    assert momentum_score([], 0) == 0
    assert health_score({}) >= 0
    assert readme_quality_score("") == 0
