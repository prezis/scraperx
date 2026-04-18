"""Scoring heuristics for scraperx.github_analyzer.

Pure functions, stdlib only, deterministic. Each function documents its inputs
and formula. No exceptions — malformed/empty input returns a sensible low
score rather than raising, so the pipeline never aborts due to a missing
field in a repo payload.

Draft provenance: initial draft from local qwen3.5:27b (GPU, free), reviewed +
docstring added Claude-side. Formulae are the session's explicit spec — tweak
them here when the verdict rubric evolves.

Sub-scores:
    bus_factor_score(contributors)       — concentration of commit authorship
    momentum_score(commits_90d, delta)   — recent activity signal
    health_score(repo_payload)           — archived/license/issues/forks checks
    readme_quality_score(readme_text)    — length + structure + code + links

Aggregate to `RepoTrustScore.overall` in synthesis.py (T12), not here.
"""

from __future__ import annotations


def _clamp(x: int, lo: int = 0, hi: int = 100) -> int:
    """Clamp an integer value between lo and hi (inclusive)."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def bus_factor_score(contributors: list[dict]) -> int:
    """Concentration of commits among top contributors.

    Args:
        contributors: list of `{login, contributions, ...}` dicts
                      (GitHub /repos/:o/:r/contributors shape).

    Returns:
        int 0-100.  Higher = safer (more people hold the project together).
        k = smallest number of top contributors whose cumulative commits
        reach 50% of total.  Score = k * 12.5 clamped to [0, 100].
    """
    if not contributors:
        return 0

    try:
        sorted_contributors = sorted(
            contributors,
            key=lambda c: c.get("contributions", 0),
            reverse=True,
        )
        total = sum(c.get("contributions", 0) for c in sorted_contributors)
        if total == 0:
            return 0

        target = total * 0.5
        cumulative = 0
        k = 0
        for contributor in sorted_contributors:
            cumulative += contributor.get("contributions", 0)
            k += 1
            if cumulative >= target:
                break

        return _clamp(int(k * 12.5), 0, 100)
    except (TypeError, AttributeError, KeyError):
        return 0


def momentum_score(commits_90d: list[dict], stars_delta_90d: int) -> int:
    """Recent activity signal — is the repo still moving?

    Args:
        commits_90d: subset of /commits JSON filtered to last 90 days.
                     Caller parses dates; we just count.
        stars_delta_90d: new stars over the last 90 days.

    Returns:
        int 0-100.  60 pts from commits (capped at ~30 commits), 40 from
        stars (capped at ~400 new stars).
    """
    try:
        commit_component = _clamp(len(commits_90d) * 2, 0, 60)
        star_component = _clamp(stars_delta_90d // 10, 0, 40)
        return _clamp(commit_component + star_component, 0, 100)
    except (TypeError, AttributeError):
        return 0


def health_score(repo_payload: dict) -> int:
    """Repo hygiene from the core /repos payload.

    Args:
        repo_payload: dict from GET /repos/{owner}/{repo}.

    Returns:
        int 0-100.  Archived repos short-circuit to 0.  Otherwise start at
        50 and adjust for has_issues, license presence, issue/star ratio,
        and fork/star ratio.
    """
    if not isinstance(repo_payload, dict):
        return 0

    try:
        if repo_payload.get("archived"):
            return 0

        score = 50

        if repo_payload.get("has_issues"):
            score += 10

        license_info = repo_payload.get("license")
        if isinstance(license_info, dict) and license_info.get("key"):
            score += 15

        open_issues = repo_payload.get("open_issues_count", 0)
        stargazers = repo_payload.get("stargazers_count", 0)
        try:
            issue_ratio = open_issues / max(1, stargazers)
        except (TypeError, ZeroDivisionError):
            issue_ratio = 0

        if issue_ratio < 0.01:
            score += 15
        elif issue_ratio < 0.05:
            score += 5
        elif issue_ratio >= 0.15:
            score -= 10

        forks = repo_payload.get("forks_count", 0)
        if forks and forks > 0:
            try:
                fork_ratio = forks / max(1, stargazers)
                if fork_ratio < 0.5:
                    score += 10
            except (TypeError, ZeroDivisionError):
                pass

        return _clamp(score, 0, 100)
    except (TypeError, AttributeError, KeyError):
        return 0


def readme_quality_score(readme_text: str) -> int:
    """Content-structure heuristic — does the README look maintained?

    Args:
        readme_text: already-decoded readme content (caller b64-decodes the
                     GitHub payload before passing).

    Returns:
        int 0-100.  40 pts length + 30 pts headings + 10 code fence +
        10 link + 10 install keyword.
    """
    if not readme_text or not isinstance(readme_text, str):
        return 0

    try:
        length_component = _clamp(len(readme_text) // 40, 0, 40)

        heading_count = 0
        for line in readme_text.splitlines():
            if line.lstrip().startswith("#"):
                heading_count += 1
        heading_component = _clamp(heading_count * 5, 0, 30)

        has_code_fence = 10 if "```" in readme_text else 0
        has_link = 10 if "](http" in readme_text else 0

        readme_lower = readme_text.lower()
        install_keywords = ("install", "pip ", "npm ", "cargo ", "go get", "brew ")
        has_install = 10 if any(kw in readme_lower for kw in install_keywords) else 0

        total = length_component + heading_component + has_code_fence + has_link + has_install
        return _clamp(total, 0, 100)
    except (TypeError, AttributeError):
        return 0
