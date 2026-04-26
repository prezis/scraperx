"""Thin GitHub REST client for scraperx.github_analyzer.

Stdlib-only (urllib + json). Returns raw-JSON shapes — never reshapes.
Scoring and enrichment live in scoring.py / core.py so this module stays a
transport layer.

Auth: pass token= explicitly or set GITHUB_TOKEN. Unauthed = 60 req/h, authed = 5000.

Rate-limit policy:
    This module is fail-fast. When x-ratelimit-remaining hits 0 AND reset is
    in the future, the next _get() raises RateLimitExceededError *without*
    hitting the network. Backoff / sleep is the caller's responsibility — the
    analyzer pipeline knows whether to degrade gracefully or wait.

Draft provenance: initial draft from local qwen3.5:27b (GPU, free), then
reviewed + fixed (RateLimitExceededError dataclass conflict, exception
chaining, stricter header default types).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"


# ---------------------------------------------------------------------------
# Exceptions


class GithubAPIError(Exception):
    """Base for anything that goes wrong hitting the GitHub API."""


class RepoNotFoundError(GithubAPIError):
    """HTTP 404 on a repo/resource we expected to exist."""


class RateLimitExceededError(GithubAPIError):
    """x-ratelimit-remaining hit 0. `.reset_at` is a unix epoch seconds float."""

    def __init__(self, reset_at: float) -> None:
        super().__init__(f"GitHub rate limit exhausted; resets at {reset_at}")
        self.reset_at = float(reset_at)


# ---------------------------------------------------------------------------
# Client


class GithubAPI:
    """Stdlib-only GitHub REST client.

    One instance is cheap — reuse across calls in the same analyze_repo()
    pass so rate-limit state (populated from response headers) stays current.

    Attributes set after the first successful call (and on 403 rate errors):
        rate_remaining: requests left in the current window
        rate_reset:     unix epoch seconds when the window resets
        rate_limit:     total quota for the current window
    """

    def __init__(
        self,
        token: str | None = None,
        user_agent: str = "scraperx/github-analyzer",
        timeout: float = 10.0,
    ) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.user_agent = user_agent
        self.timeout = timeout

        # Rate-limit state (updated from response headers). -1 = unknown yet.
        self.rate_remaining: int = -1
        self.rate_reset: int = 0
        self.rate_limit: int = -1

    @property
    def authenticated(self) -> bool:
        """True if a token was supplied — computed live so tests can mutate
        self.token and see the effect."""
        return bool(self.token)

    @property
    def rate_exhausted(self) -> bool:
        """True when we know the window is empty AND reset is still in the future."""
        return self.rate_remaining == 0 and self.rate_reset > time.time()

    # ----------------------------- internals --------------------------------

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{API_BASE}{path}"
        if params:
            # Sort keys for deterministic URLs (aids caching + test assertions)
            qs = urllib.parse.urlencode(sorted(params.items()))
            url = f"{url}?{qs}"
        return url

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": self.user_agent,
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @staticmethod
    def _int_header(headers, name: str, default: int = -1) -> int:
        """HTTPResponse.getheader / HTTPError.headers.get both return Optional[str]."""
        v = None
        # HTTPResponse has .getheader; HTTPMessage has .get — support both
        if hasattr(headers, "getheader"):
            v = headers.getheader(name)
        else:
            v = headers.get(name)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _absorb_rate_headers(self, headers) -> None:
        self.rate_remaining = self._int_header(headers, "X-RateLimit-Remaining", self.rate_remaining)
        self.rate_reset = self._int_header(headers, "X-RateLimit-Reset", self.rate_reset)
        self.rate_limit = self._int_header(headers, "X-RateLimit-Limit", self.rate_limit)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One-shot GET. See module docstring for rate-limit policy."""
        if self.rate_exhausted:
            raise RateLimitExceededError(self.rate_reset)

        url = self._build_url(path, params)
        req = urllib.request.Request(url, headers=self._headers())
        logger.debug("GET %s", url)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._absorb_rate_headers(resp.headers)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # HTTPError must be caught BEFORE URLError (it's a subclass).
            self._absorb_rate_headers(e.headers)
            if e.code == 403 and self.rate_remaining == 0:
                raise RateLimitExceededError(self.rate_reset) from e
            if e.code == 404:
                raise RepoNotFoundError(f"GitHub 404 for {path}") from e
            raise GithubAPIError(f"GitHub HTTP {e.code} for {path}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise GithubAPIError(f"Network error for {path}: {e.reason}") from e

        if not raw:
            raise GithubAPIError(f"Empty response body for {path}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GithubAPIError(f"Invalid JSON from {path}: {e}") from e

        if not isinstance(parsed, (dict, list)):
            raise GithubAPIError(f"Unexpected JSON root type {type(parsed).__name__} for {path}")
        return parsed

    # ----------------------------- public API -------------------------------

    def get_repo(self, owner: str, repo: str) -> dict:
        """GET /repos/{owner}/{repo} — core metadata payload."""
        return self._get(f"/repos/{owner}/{repo}")

    def get_contributors(self, owner: str, repo: str, per_page: int = 30) -> list[dict]:
        """GET /repos/{owner}/{repo}/contributors — sorted by commit count, default branch."""
        return self._get(f"/repos/{owner}/{repo}/contributors", {"per_page": per_page})

    def get_recent_commits(self, owner: str, repo: str, per_page: int = 30) -> list[dict]:
        """GET /repos/{owner}/{repo}/commits — default branch, most-recent-first."""
        return self._get(f"/repos/{owner}/{repo}/commits", {"per_page": per_page})

    def get_releases(self, owner: str, repo: str, per_page: int = 10) -> list[dict]:
        """GET /repos/{owner}/{repo}/releases — for version cadence signals."""
        return self._get(f"/repos/{owner}/{repo}/releases", {"per_page": per_page})

    def get_top_forks(self, owner: str, repo: str, per_page: int = 30) -> list[dict]:
        """GET /repos/{owner}/{repo}/forks?sort=stargazers — most-starred forks first."""
        return self._get(
            f"/repos/{owner}/{repo}/forks",
            {"sort": "stargazers", "per_page": per_page},
        )

    def get_readme(self, owner: str, repo: str) -> dict:
        """GET /repos/{owner}/{repo}/readme — payload has `content` (base64). Caller decodes."""
        return self._get(f"/repos/{owner}/{repo}/readme")

    def get_workflows(self, owner: str, repo: str) -> dict:
        """GET /repos/{owner}/{repo}/actions/workflows — CI-detection heuristic.

        404 is NOT fatal; catch `RepoNotFoundError` and treat as "no workflows".
        """
        return self._get(f"/repos/{owner}/{repo}/actions/workflows")

    def get_advisories(self, owner: str, repo: str, per_page: int = 10) -> list[dict]:
        """GET /repos/{owner}/{repo}/security-advisories — GHSA (Q4 scope add).

        Returns a list of published advisories. 404 on repos that haven't
        opted in to the feature — caller should catch `RepoNotFoundError`.
        """
        return self._get(
            f"/repos/{owner}/{repo}/security-advisories",
            {"per_page": per_page},
        )

    def search_repositories(
        self,
        query: str,
        *,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 30,
        page: int = 1,
    ) -> dict:
        """GET /search/repositories — repo discovery via GitHub's search API.

        Args:
            query: GitHub search query string. Supports the qualifiers documented
                at https://docs.github.com/search-github/searching-on-github/searching-for-repositories
                (topic:X, stars:>N, pushed:>YYYY-MM-DD, language:Y, etc).
            sort: One of "stars", "forks", "help-wanted-issues", "updated".
                Default "stars" — quality bar leans on community popularity.
            order: "desc" (default) or "asc".
            per_page: 1-100. GitHub caps the API at 1000 total results regardless
                of pagination, so > 30 only helps for narrow queries.
            page: 1-indexed page number for pagination.

        Returns:
            Raw JSON envelope: ``{"total_count": int, "incomplete_results": bool,
            "items": [<repo>, ...]}``. Caller is responsible for shape coercion.

        Notes:
            Search has its own (lower) rate limit: 10 req/min unauthed,
            30 req/min authed. The shared rate-headers tracked by this client
            are the *core* API limit — search exhaustion may surface as a
            403 with a different X-RateLimit-Resource header.
        """
        return self._get(
            "/search/repositories",
            {"q": query, "sort": sort, "order": order, "per_page": per_page, "page": page},
        )
