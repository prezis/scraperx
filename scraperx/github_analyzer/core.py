"""GithubAnalyzer — public entry point gluing T3-T12 into one pipeline.

analyze_repo(spec, deep=False) runs:
    1. parse_repo_url                           (local, cheap)
    2. github_api.get_repo                      (REST)
    3. github_api.get_contributors              (REST, parallel-safe)
    4. github_api.get_recent_commits            (REST)
    5. github_api.get_readme                    (REST, optional — 404 ok)
    6. github_api.get_top_forks                 (REST, optional)
    7. github_api.get_advisories                (REST, optional — 404 ok)
    8. scoring.*                                (pure)
    9. mentions.ALL_SOURCES                     (HTTP, per-platform)
   10. semantic.search                          (only if web_search_fn given)
   11. synthesis.synthesize                     (LLM verdict or heuristic)

All network steps catch exceptions → graceful degrade via `report.warnings`.
The caller (CLI, MCP tool) ALWAYS gets a GithubReport back, even on partial
failure.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from scraperx.github_analyzer.github_api import (
    GithubAPI,
    GithubAPIError,
    RepoNotFoundError,
)
from scraperx.github_analyzer.mentions import ALL_SOURCES
from scraperx.github_analyzer.schemas import (
    ContributorInfo,
    ForkInfo,
    GithubReport,
    RepoTrustScore,
    SecurityAdvisory,
)
from scraperx.github_analyzer.scoring import (
    bus_factor_score,
    health_score,
    momentum_score,
    readme_quality_score,
)
from scraperx.github_analyzer.semantic import search as semantic_search
from scraperx.github_analyzer.synthesis import synthesize

logger = logging.getLogger(__name__)

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
    """Normalize a GitHub URL / shorthand / SSH spec → RepoRef.

    Accepts:
        owner/repo
        https://github.com/owner/repo[.git]
        https://github.com/owner/repo/tree/main/sub
        git@github.com:owner/repo.git
    """
    s = (spec or "").strip()
    if not s:
        raise InvalidRepoUrlError("empty spec")

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


# ---------------------------------------------------------------------------
# Helpers


def _decode_readme(payload: dict) -> str:
    """GitHub readme payload: {content: base64, encoding: 'base64', ...}."""
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content") or ""
    if payload.get("encoding") == "base64":
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return content if isinstance(content, str) else ""


def _commits_last_90d(commits: list[dict]) -> list[dict]:
    """Filter commits to last 90 days via author.date."""
    if not commits:
        return []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=90)
    out = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        date_str = author.get("date") or ""
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(c)
    return out


def _extract_contributors(payload: list) -> list[ContributorInfo]:
    if not isinstance(payload, list):
        return []
    out = []
    for c in payload[:30]:
        if not isinstance(c, dict):
            continue
        out.append(
            ContributorInfo(
                handle=c.get("login", "") or "",
                commits=int(c.get("contributions", 0) or 0),
                profile_url=c.get("html_url", "") or "",
            )
        )
    return out


def _extract_forks(payload: list) -> list[ForkInfo]:
    if not isinstance(payload, list):
        return []
    out = []
    for f in payload[:10]:
        if not isinstance(f, dict):
            continue
        out.append(
            ForkInfo(
                full_name=f.get("full_name", "") or "",
                stars=int(f.get("stargazers_count", 0) or 0),
                pushed_at=f.get("pushed_at", "") or "",
                url=f.get("html_url", "") or "",
            )
        )
    return out


def _extract_advisories(payload: list) -> list[SecurityAdvisory]:
    if not isinstance(payload, list):
        return []
    out = []
    for a in payload[:10]:
        if not isinstance(a, dict):
            continue
        out.append(
            SecurityAdvisory(
                ghsa_id=a.get("ghsa_id", "") or "",
                severity=a.get("severity", "") or "",
                summary=(a.get("summary") or "")[:280],
                published_at=a.get("published_at", "") or "",
                url=a.get("html_url", "") or "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# GithubAnalyzer


class GithubAnalyzer:
    """End-to-end repo analyzer. One instance per session — shares API
    client state and optional SocialDB cache across multiple analyze_repo
    calls.

    Args:
        github_token: PAT for higher rate limit (5000/h). Falls back to
                      GITHUB_TOKEN env var.
        db: optional SocialDB for caching mentions + GitHub payloads.
        web_search_fn: local_web_search callable for Tier B. None → Tier B
                       silently skipped.
        local_llm_fn: local_llm callable for synthesis verdict. None →
                      heuristic fallback.
    """

    def __init__(
        self,
        github_token: str | None = None,
        db=None,
        web_search_fn: Callable[..., list[dict]] | None = None,
        local_llm_fn: Callable[..., str] | None = None,
    ) -> None:
        self.api = GithubAPI(token=github_token)
        self.db = db
        self.web_search_fn = web_search_fn
        self.local_llm_fn = local_llm_fn

    def analyze_repo(
        self,
        spec: str,
        deep: bool = False,
        skip_mentions: bool = False,
    ) -> GithubReport:
        """Run the full pipeline for one repo. Never raises on network
        failures — errors are captured in `report.warnings`.

        Args:
            spec: URL / shorthand / SSH accepted by parse_repo_url
            deep: route synthesis via qwen3.5:27b instead of qwen3:4b
            skip_mentions: debug flag — skip Tier A + B for faster runs
        """
        ref = parse_repo_url(spec)
        # Late import of __version__ to dodge the init circular — scraperx's
        # __init__.py imports github_analyzer, so we can't import at module top.
        try:
            from scraperx import __version__ as _sxv
        except ImportError:
            _sxv = "unknown"
        report = GithubReport(
            owner=ref.owner,
            repo=ref.repo,
            url=ref.url,
            analyzed_at=time.time(),
            scraperx_version=_sxv,
        )

        # 1. Core repo metadata (fatal if missing — no point continuing)
        try:
            repo_payload = self.api.get_repo(ref.owner, ref.repo)
        except RepoNotFoundError:
            report.warnings.append("Repository not found (404) — aborting pipeline")
            return report
        except GithubAPIError as e:
            report.warnings.append(f"Core metadata fetch failed: {e}")
            return report

        report.description = repo_payload.get("description", "") or ""
        report.stars = int(repo_payload.get("stargazers_count", 0) or 0)
        report.forks_count = int(repo_payload.get("forks_count", 0) or 0)
        report.open_issues = int(repo_payload.get("open_issues_count", 0) or 0)
        report.language = repo_payload.get("language", "") or ""
        license_info = repo_payload.get("license") or {}
        report.license_key = (license_info.get("key") if isinstance(license_info, dict) else "") or ""
        report.archived = bool(repo_payload.get("archived", False))
        report.pushed_at = repo_payload.get("pushed_at", "") or ""
        report.created_at = repo_payload.get("created_at", "") or ""
        report.default_branch = repo_payload.get("default_branch", "") or ""

        # 2. Contributors
        contributors_raw: list = []
        try:
            contributors_raw = self.api.get_contributors(ref.owner, ref.repo)
            report.contributors = _extract_contributors(contributors_raw)
        except GithubAPIError as e:
            report.warnings.append(f"Contributors fetch failed: {e}")

        # 3. Recent commits (for momentum)
        commits: list = []
        try:
            commits = self.api.get_recent_commits(ref.owner, ref.repo, per_page=100)
        except GithubAPIError as e:
            report.warnings.append(f"Commits fetch failed: {e}")

        # 4. README
        readme_text = ""
        try:
            readme_payload = self.api.get_readme(ref.owner, ref.repo)
            readme_text = _decode_readme(readme_payload)
        except RepoNotFoundError:
            report.warnings.append("README not found")
        except GithubAPIError as e:
            report.warnings.append(f"README fetch failed: {e}")

        # 5. Notable forks
        try:
            forks_payload = self.api.get_top_forks(ref.owner, ref.repo, per_page=20)
            report.notable_forks = _extract_forks(forks_payload)
        except GithubAPIError as e:
            report.warnings.append(f"Forks fetch failed: {e}")

        # 6. Security advisories (Q4 scope)
        try:
            adv_payload = self.api.get_advisories(ref.owner, ref.repo)
            report.advisories = _extract_advisories(adv_payload)
        except RepoNotFoundError:
            pass  # Many repos don't enable GHSA — silent skip
        except GithubAPIError as e:
            report.warnings.append(f"Advisories fetch failed: {e}")

        # 7. Scoring — pure functions, never raise
        report.trust = RepoTrustScore(
            bus_factor=bus_factor_score(contributors_raw),
            momentum=momentum_score(_commits_last_90d(commits), stars_delta_90d=0),
            health=health_score(repo_payload),
            readme_quality=readme_quality_score(readme_text),
        )

        # 8. External mentions — Tier A (unless skipped)
        if not skip_mentions:
            for source_key, search_fn in ALL_SOURCES.items():
                try:
                    hits = search_fn(ref.owner, ref.repo, db=self.db)
                    report.mentions.extend(hits)
                except Exception as e:
                    report.warnings.append(f"Tier-A {source_key} failed: {e}")

            # 9. Tier B semantic layer (if wired)
            if self.web_search_fn is not None:
                try:
                    sem_hits = semantic_search(
                        ref.owner,
                        ref.repo,
                        web_search_fn=self.web_search_fn,
                        db=self.db,
                    )
                    report.mentions.extend(sem_hits)
                except Exception as e:
                    report.warnings.append(f"Tier-B semantic failed: {e}")

        # 10. Synthesis (LLM verdict or heuristic)
        synthesize(report, local_llm_fn=self.local_llm_fn, deep=deep)

        return report


def analyze_repo(spec: str, deep: bool = False) -> GithubReport:
    """Module-level convenience — one-shot analysis with defaults (no
    token, no cache, no LLM, no Tier B). Returns a GithubReport whose
    `trust.overall` comes from the heuristic fallback.

    For real use, instantiate `GithubAnalyzer` with concrete dependencies.
    """
    return GithubAnalyzer().analyze_repo(spec, deep=deep)
