"""gh_discover — topic-first GitHub repo discovery for research workflows.

Encodes the lesson from wojak-wojtek s22 round 2 search: **topics > keywords**.
Keyword search returns thousands of off-topic hits; topic-tagged repos cluster
the actual ecosystem (e.g. `topic:macroeconomics+topic:python` returns the
4-5 repos serious people actually use).

Public API:

    from scraperx import discover_repos
    candidates = discover_repos(
        topics=["macroeconomics", "python"],
        min_stars=100,
        recency_months=12,
        exclude_owners=["lb-tokenomiapro"],
        limit=20,
    )

CLI:
    scraperx gh-discover --topic macroeconomics --topic python --min-stars 100 --json
    scraperx gh-discover --topic regime-detection --recency-months 6 --limit 10

Optional analyze chain (pipes top-N into github_analyzer):
    scraperx gh-discover --topic onchain --analyze-top 3

Auth: same as github_analyzer — pass token= or set GITHUB_TOKEN. Unauthed
search rate limit is 10 req/min (vs 30/min authed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Iterable

# GitHub topic naming rules: lowercase letters, digits, hyphens. Length 1-50.
# https://docs.github.com/repositories/classifying-your-repositories
_VALID_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

from scraperx.github_analyzer.github_api import (
    GithubAPIError,
    GithubAPI,
    RateLimitExceededError,
)

logger = logging.getLogger(__name__)

__all__ = ["RepoCandidate", "discover_repos", "build_search_query", "main_gh_discover"]


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoCandidate:
    """Compact view of one repo from the discovery search.

    Drop-in for json.dumps via asdict(). Keeps only the fields that matter
    for "is this worth investigating?" — full payload available via
    github_analyzer.analyze_repo() if the candidate makes the shortlist.
    """

    full_name: str  # "owner/repo"
    stars: int
    forks: int
    description: str
    topics: tuple[str, ...]  # frozen for hashability
    language: str
    pushed_at: str  # ISO8601 from GitHub
    url: str
    license_spdx: str = ""  # may be empty if repo has no recognised license

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]


# ---------------------------------------------------------------------------
# Query builder — topics > keywords
# ---------------------------------------------------------------------------


def build_search_query(
    topics: Iterable[str],
    *,
    min_stars: int | None = None,
    recency_months: int | None = None,
    language: str | None = None,
    extra_qualifiers: Iterable[str] = (),
) -> str:
    """Compose a GitHub search-API query string.

    Topics are AND-combined (topic:A topic:B). Extra qualifiers are appended
    verbatim — caller is responsible for proper formatting.

    Returns: the query string suitable for passing to ``search_repositories(q=...)``.

    Raises ValueError on empty topics (defensive — silent empty-query searches
    return random GitHub trending which is never what discover_repos wants).
    """
    topic_list = [t.strip().lower() for t in topics if t.strip()]
    if not topic_list:
        raise ValueError("at least one non-empty topic is required")
    bad = [t for t in topic_list if not _VALID_TOPIC_RE.match(t)]
    if bad:
        raise ValueError(
            f"invalid topic(s) {bad}: GitHub topics are lowercase a-z 0-9 -, "
            "1-50 chars. No spaces, slashes, commas, or uppercase."
        )

    parts = [f"topic:{t}" for t in topic_list]
    if min_stars is not None and min_stars > 0:
        parts.append(f"stars:>={min_stars}")
    if recency_months is not None and recency_months > 0:
        cutoff = dt.date.today() - dt.timedelta(days=int(recency_months * 30.5))
        parts.append(f"pushed:>{cutoff.isoformat()}")
    if language:
        parts.append(f"language:{language}")
    parts.extend(extra_qualifiers)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Discovery pipeline
# ---------------------------------------------------------------------------


def _to_candidate(item: dict) -> RepoCandidate:
    """Coerce one /search/repositories item into a RepoCandidate."""
    license_obj = item.get("license") or {}
    return RepoCandidate(
        full_name=item.get("full_name", ""),
        stars=int(item.get("stargazers_count", 0) or 0),
        forks=int(item.get("forks_count", 0) or 0),
        description=(item.get("description") or "").strip(),
        topics=tuple(item.get("topics") or ()),
        language=item.get("language") or "",
        pushed_at=item.get("pushed_at") or "",
        url=item.get("html_url") or "",
        license_spdx=(license_obj.get("spdx_id") or "") if isinstance(license_obj, dict) else "",
    )


def discover_repos(
    topics: Iterable[str],
    *,
    min_stars: int = 0,
    recency_months: int | None = None,
    language: str | None = None,
    exclude_owners: Iterable[str] = (),
    limit: int = 30,
    client: GithubAPI | None = None,
    extra_qualifiers: Iterable[str] = (),
) -> list[RepoCandidate]:
    """Topic-first GitHub repo discovery, sorted by stars desc.

    Args:
        topics: One or more topic tags (https://github.com/topics). All ANDed.
        min_stars: Minimum stargazer count. 0 = no floor.
        recency_months: Drop repos with no pushed_at within last N months.
            None = no recency filter (good for the long-tail of "stable" libs).
        language: Optional GitHub language filter (e.g. "Python", "Rust").
        exclude_owners: Owner login(s) to drop post-fetch (case-insensitive).
            Used to filter out forks of your own work or known-irrelevant orgs.
        limit: Max candidates to return after filtering. Capped at 100 because
            GitHub's search per_page max is 100.
        client: Optional GithubAPI — pass for tests with mocked network.
            If None, a fresh client is constructed (uses GITHUB_TOKEN if set).
        extra_qualifiers: Free-form qualifiers appended to the query (e.g.
            ``("archived:false", "is:public")``). Defensive default: no extras.

    Returns:
        List of RepoCandidate, sorted by stars desc, len ≤ limit.

    Raises:
        ValueError: empty topics.
        GithubAPIError / RateLimitExceededError: propagated from the client.
    """
    if client is None:
        client = GithubAPI()
    # GitHub Search caps per_page at 100 and total results at 1000. We page
    # internally until limit is met, the well runs dry, or the 1000-result
    # ceiling is hit (whichever comes first).
    per_page = max(1, min(int(limit), 100))
    query = build_search_query(
        topics,
        min_stars=min_stars or None,
        recency_months=recency_months,
        language=language,
        extra_qualifiers=extra_qualifiers,
    )
    logger.debug("gh-discover query: %s", query)

    excludes = {o.strip().lower() for o in exclude_owners if o and o.strip()}
    out: list[RepoCandidate] = []
    seen: set[str] = set()

    page = 1
    max_total_results = 1000  # GitHub's hard ceiling for search/repositories
    while len(out) < limit:
        envelope = client.search_repositories(query=query, per_page=per_page, page=page)
        items = envelope.get("items", []) if isinstance(envelope, dict) else []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            cand = _to_candidate(item)
            if not cand.full_name or cand.full_name in seen:
                continue
            if cand.owner.lower() in excludes:
                continue
            if cand.stars < min_stars:
                continue
            seen.add(cand.full_name)
            out.append(cand)
            if len(out) >= limit:
                break
        # If GitHub returned fewer than per_page items, no more pages exist
        if len(items) < per_page:
            break
        # Respect the 1000-result ceiling (page * per_page > 1000)
        if page * per_page >= max_total_results:
            break
        page += 1

    out.sort(key=lambda c: (-c.stars, c.full_name))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_human(candidates: list[RepoCandidate]) -> str:
    if not candidates:
        return "(no candidates matched the query)"
    lines = []
    for c in candidates:
        topics_str = ",".join(c.topics) if c.topics else "—"
        lines.append(
            f"{c.stars:>6}★  {c.full_name:<40}  {c.language or '—':<10}  "
            f"{c.pushed_at[:10] or '—'}  topics={topics_str}"
        )
        if c.description:
            lines.append(f"        {c.description[:120]}")
    return "\n".join(lines)


def _format_json(candidates: list[RepoCandidate]) -> str:
    return json.dumps([asdict(c) for c in candidates], indent=2, sort_keys=True)


def main_gh_discover(argv: list[str] | None = None) -> int:
    """CLI entry point — wired by scraperx/__main__.py via the gh-discover subcommand."""
    parser = argparse.ArgumentParser(
        prog="scraperx gh-discover",
        description="Topic-first GitHub repo discovery (topics > keywords).",
    )
    # Consume the subcommand verb when invoked via `scraperx gh-discover ...`
    # (argv via __main__.py still contains "gh-discover" at position 0).
    parser.add_argument("_cmd", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--topic", action="append", required=True, dest="topics",
        help="GitHub topic tag. Repeatable — multiple --topic flags are ANDed.",
    )
    parser.add_argument("--min-stars", type=int, default=0, help="Minimum stargazers (0=no floor).")
    parser.add_argument(
        "--recency-months", type=int, default=None,
        help="Drop repos not pushed within last N months. Default: no recency filter.",
    )
    parser.add_argument("--language", default=None, help="GitHub language filter (e.g. Python).")
    parser.add_argument(
        "--exclude-owner", action="append", default=[], dest="exclude_owners",
        help="Owner login to skip. Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max candidates returned (≤100).")
    parser.add_argument(
        "--analyze-top", type=int, default=0, metavar="N",
        help="Optionally pipe top-N candidates through github_analyzer.analyze_repo().",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human format.")
    parser.add_argument("--query", action="store_true", help="Print the constructed query and exit.")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args(argv)

    log_level = logging.WARNING
    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose == 1:
        log_level = logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.query:
        try:
            print(build_search_query(
                args.topics,
                min_stars=args.min_stars or None,
                recency_months=args.recency_months,
                language=args.language,
            ))
            return 0
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    try:
        candidates = discover_repos(
            topics=args.topics,
            min_stars=args.min_stars,
            recency_months=args.recency_months,
            language=args.language,
            exclude_owners=args.exclude_owners,
            limit=args.limit,
        )
    except RateLimitExceededError as e:
        print(f"error: GitHub rate limit exhausted (resets at {e.reset_at})", file=sys.stderr)
        return 3
    except GithubAPIError as e:
        print(f"error: GitHub API: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(_format_json(candidates))
    else:
        print(_format_human(candidates))

    if args.analyze_top > 0 and candidates:
        try:
            from scraperx.github_analyzer import analyze_repo
        except ImportError:
            print("(--analyze-top requested but github_analyzer not importable)", file=sys.stderr)
            return 0
        n = min(args.analyze_top, len(candidates))
        print(f"\n--- analyze-top: running github_analyzer on top {n} ---", file=sys.stderr)
        for c in candidates[:n]:
            try:
                report = analyze_repo(c.url)
            except Exception as e:  # noqa: BLE001 — best-effort chain
                print(f"  {c.full_name}: analyze failed: {e}", file=sys.stderr)
                continue
            score = getattr(report, "trust_score", None)
            verdict = getattr(report, "verdict", "")
            print(f"  {c.full_name}: trust_score={score} verdict={verdict}", file=sys.stderr)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_gh_discover())
