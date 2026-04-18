"""Dataclasses for scraperx.github_analyzer.

Stdlib-only (no Pydantic) to match scraperx core discipline. All dataclasses
are frozen=False so adapters can enrich them in passes (REST → scoring →
mentions → synthesis), but callers should treat them as value objects once
an analyze_repo() call returns.

Shape-contract stability:
- GithubReport is the *public* top-level object — downstream MCP tools and
  CLI JSON output serialize it. Keep field names stable; if you need to
  evolve, bump scraperx minor version and update CHANGELOG.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ContributorInfo:
    """A repo contributor as reported by /contributors."""

    handle: str
    commits: int = 0
    profile_url: str = ""


@dataclass
class ForkInfo:
    """A notable fork — surface when a fork has more recent activity or more
    stars than the parent (possible "community took over" signal)."""

    full_name: str
    stars: int = 0
    pushed_at: str = ""  # ISO8601
    ahead_by: int = 0  # commits ahead of parent default branch (optional, 0 if unknown)
    url: str = ""


@dataclass
class ExternalMention:
    """A mention of the repo on an external platform.

    source: canonical short key — "hn", "reddit", "stackoverflow", "devto",
    "arxiv", "pwc", "semantic_web", "youtube", "x", etc. Tier-B generic
    (local_web_search) mentions use "semantic_web" with the actual host in
    `metadata["host"]`.
    """

    source: str
    title: str
    url: str
    score: int = 0  # upvotes / reactions — platform-normalized
    published_at: str = ""  # ISO8601 when known
    author: str = ""
    snippet: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrendingRepo:
    """A row from github.com/trending."""

    full_name: str
    description: str = ""
    language: str = ""
    stars: int = 0
    stars_today: int = 0
    url: str = ""


@dataclass
class SecurityAdvisory:
    """A GHSA advisory attached to the repo (Q4 scope addition)."""

    ghsa_id: str
    severity: str = ""  # low | medium | high | critical
    summary: str = ""
    published_at: str = ""
    url: str = ""


@dataclass
class RepoTrustScore:
    """Sub-scores feeding the final verdict. Each is 0-100.

    `overall` is set by synthesis.py (qwen-verdict) or a heuristic fallback.
    """

    bus_factor: int = 0
    momentum: int = 0
    health: int = 0
    readme_quality: int = 0
    overall: int = 0
    rationale: str = ""  # one-line human-readable summary


@dataclass
class GithubReport:
    """The top-level object returned by analyze_repo()."""

    # Identity
    owner: str
    repo: str
    url: str
    default_branch: str = ""

    # Raw metadata (subset of /repos payload — don't mirror the whole thing)
    description: str = ""
    stars: int = 0
    forks_count: int = 0
    open_issues: int = 0
    language: str = ""
    license_key: str = ""  # SPDX id when available
    archived: bool = False
    pushed_at: str = ""  # ISO8601 of last push
    created_at: str = ""

    # Derived structures
    contributors: list[ContributorInfo] = field(default_factory=list)
    notable_forks: list[ForkInfo] = field(default_factory=list)
    mentions: list[ExternalMention] = field(default_factory=list)
    advisories: list[SecurityAdvisory] = field(default_factory=list)

    # Scoring + verdict
    trust: RepoTrustScore = field(default_factory=RepoTrustScore)
    verdict_markdown: str = ""  # synthesized by synthesis.py

    # Provenance
    analyzed_at: float = 0.0  # unix epoch
    scraperx_version: str = ""
    warnings: list[str] = field(default_factory=list)  # graceful-degrade notes

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict (used by CLI --json and MCP tool)."""
        return asdict(self)
