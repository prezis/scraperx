"""GithubAnalyzer — public entry point for scraperx.github_analyzer.

T1 scope (this commit): stub only. The class exists, parse_repo_url() is
implemented (pure stdlib, tested), and analyze_repo() raises
NotImplementedError pointing callers at the future T3+ implementation.

T3+ will fill in the pipeline:
    1. REST fetch (github_api.py)
    2. Forks + contributors
    3. Scoring heuristics (scoring.py)
    4. External mentions (Tier A + Tier B)
    5. Security advisories (GHSA)
    6. Synthesis (qwen3:4b default, qwen3.5:27b on deep=True)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from scraperx.github_analyzer.schemas import GithubReport

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class InvalidRepoUrlError(ValueError):
    """Raised when the URL/spec isn't a recognizable GitHub owner/repo."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


def parse_repo_url(spec: str) -> RepoRef:
    """Accept any of:
        - "owner/repo"
        - "https://github.com/owner/repo"
        - "https://github.com/owner/repo.git"
        - "https://github.com/owner/repo/tree/main/sub"
        - "git@github.com:owner/repo.git"

    Return RepoRef(owner, repo). Raise InvalidRepoUrlError on garbage.

    Trailing ".git" is stripped. Sub-paths (/tree, /blob, /issues) are ignored.
    """
    s = (spec or "").strip()
    if not s:
        raise InvalidRepoUrlError("empty spec")

    # SSH form: git@github.com:owner/repo.git
    if s.startswith("git@"):
        _, _, tail = s.partition(":")
        owner_repo = tail
    elif "://" in s:
        parsed = urlparse(s)
        host = (parsed.netloc or "").lower()
        if host and "github.com" not in host:
            raise InvalidRepoUrlError(f"not a github.com URL: {host}")
        owner_repo = parsed.path.lstrip("/")
    else:
        owner_repo = s

    # Strip trailing .git and sub-paths beyond owner/repo
    parts = [p for p in owner_repo.split("/") if p]
    if len(parts) < 2:
        raise InvalidRepoUrlError(f"expected owner/repo, got: {spec!r}")

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]

    if not _OWNER_REPO_RE.match(owner) or not _OWNER_REPO_RE.match(repo):
        raise InvalidRepoUrlError(f"invalid owner/repo characters: {owner}/{repo}")

    return RepoRef(owner=owner, repo=repo)


class GithubAnalyzer:
    """Pipeline runner — instance reused across analyze_repo() calls so that
    rate-limit state and DB handle can be shared.

    Stub until T3. Keep the public surface minimal and stable; downstream
    MCP tool and CLI both go through analyze_repo().
    """

    def __init__(
        self,
        github_token: str | None = None,
        db_path: str | None = None,
        use_local_web: bool = True,
    ) -> None:
        self.github_token = github_token
        self.db_path = db_path
        self.use_local_web = use_local_web

    def analyze_repo(self, spec: str, deep: bool = False) -> GithubReport:
        """Parse spec, run the pipeline, return a GithubReport.

        Not implemented yet — T3 fills in github_api.py, T4 adds scoring,
        T5-T9 add mentions, T10 adds semantic layer, T12 adds synthesis.
        """
        ref = parse_repo_url(spec)  # always run — cheap, validates input
        raise NotImplementedError(
            "GithubAnalyzer.analyze_repo() is a T1 stub. "
            f"Validated ref={ref.full_name}. "
            "T3 will implement the REST adapter, then T4+ the full pipeline."
        )


def analyze_repo(spec: str, deep: bool = False) -> GithubReport:
    """Module-level convenience wrapper — one-shot analysis with defaults."""
    return GithubAnalyzer().analyze_repo(spec, deep=deep)
