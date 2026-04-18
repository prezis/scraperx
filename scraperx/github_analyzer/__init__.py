"""scraperx.github_analyzer — deep repo trust analysis.

Public API:
    from scraperx.github_analyzer import analyze_repo, GithubAnalyzer
    from scraperx.github_analyzer import GithubReport, ExternalMention

T1 ships the skeleton; analyze_repo() raises NotImplementedError until T3.
"""

from scraperx.github_analyzer.core import (
    GithubAnalyzer,
    InvalidRepoUrlError,
    RepoRef,
    analyze_repo,
    parse_repo_url,
)
from scraperx.github_analyzer.schemas import (
    ContributorInfo,
    ExternalMention,
    ForkInfo,
    GithubReport,
    RepoTrustScore,
    SecurityAdvisory,
    TrendingRepo,
)

__all__ = [
    "ContributorInfo",
    "ExternalMention",
    "ForkInfo",
    "GithubAnalyzer",
    "GithubReport",
    "InvalidRepoUrlError",
    "RepoRef",
    "RepoTrustScore",
    "SecurityAdvisory",
    "TrendingRepo",
    "analyze_repo",
    "parse_repo_url",
]
