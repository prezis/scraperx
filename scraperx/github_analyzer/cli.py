"""CLI handlers for `scraperx github ...` and `scraperx trending ...`.

Kept in a sub-module (not __main__.py) to avoid cluttering the top-level
CLI with github-specific code. __main__.py's `main()` dispatcher just
imports `_main_github` and `_main_trending` from here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable

from scraperx.github_analyzer.core import (
    GithubAnalyzer,
    InvalidRepoUrlError,
)
from scraperx.github_analyzer.schemas import GithubReport
from scraperx.github_analyzer.telemetry import prompt_and_log_verdict
from scraperx.github_analyzer.trending import fetch_trending
from scraperx.social_db import SocialDB

# ---------------------------------------------------------------------------
# Dependency wiring — optional, best-effort


def _try_get_local_llm_fn() -> Callable[..., str] | None:
    """Attempt to import local_ai.tools.llm.local_llm for synthesis.

    Returns None if local-ai-mcp isn't installed as a Python package (the
    common case — it runs as an MCP server). CLI degrades to heuristic.
    """
    try:
        from local_ai.tools.llm import local_llm  # type: ignore[import-not-found]

        return local_llm
    except ImportError:
        return None


def _try_get_web_search_fn() -> Callable[..., list[dict]] | None:
    """Attempt to import local_ai.tools.web_research.local_web_search.

    Returns None if local-ai-mcp isn't installed as a Python package.
    """
    try:
        from local_ai.tools.web_research import local_web_search  # type: ignore[import-not-found]

        return local_web_search
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Rendering


def render_markdown(report: GithubReport) -> str:
    """Human-readable report. Opposite of --json."""
    lines = [
        f"# {report.owner}/{report.repo}",
        "",
        f"**URL:** {report.url}",
    ]
    if report.description:
        lines.append(f"**Description:** {report.description}")
    lines.append(
        f"**Language:** {report.language or '(unknown)'}   "
        f"**License:** {report.license_key or '(none)'}   "
        f"**Stars:** {report.stars:,}   **Forks:** {report.forks_count:,}   "
        f"**Open issues:** {report.open_issues:,}"
    )
    if report.archived:
        lines.append("**⚠ ARCHIVED**")
    lines.append("")
    lines.append(
        f"## Trust verdict: {report.trust.overall}/100"
    )
    if report.trust.rationale:
        lines.append(f"> {report.trust.rationale}")
    lines.append("")
    lines.append("### Sub-scores")
    lines.append(
        f"- bus_factor: {report.trust.bus_factor}/100"
    )
    lines.append(
        f"- momentum:   {report.trust.momentum}/100"
    )
    lines.append(
        f"- health:     {report.trust.health}/100"
    )
    lines.append(
        f"- readme:     {report.trust.readme_quality}/100"
    )
    if report.verdict_markdown:
        lines.append("")
        lines.append("### Verdict")
        lines.append(report.verdict_markdown)
    if report.mentions:
        lines.append("")
        lines.append(f"### External mentions ({len(report.mentions)})")
        for i, m in enumerate(report.mentions[:10], start=1):
            lines.append(f"[{i}] ({m.source}) {m.title[:80]} — {m.url}")
    if report.advisories:
        lines.append("")
        lines.append(f"### Security advisories ({len(report.advisories)})")
        for a in report.advisories[:5]:
            lines.append(f"- {a.ghsa_id} ({a.severity}): {a.summary}")
    if report.warnings:
        lines.append("")
        lines.append("### Warnings")
        for w in report.warnings:
            lines.append(f"- {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommands


def main_github(argv: list[str] | None = None) -> int:
    """Entry point for `scraperx github OWNER/REPO ...`."""
    parser = argparse.ArgumentParser(
        prog="scraperx github",
        description="Deep analysis of a GitHub repository — trust score + community signals.",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "github"
    parser.add_argument("spec", help="owner/repo, or full GitHub URL")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--deep", action="store_true", help="Use qwen3.5:27b (slower, higher quality)")
    parser.add_argument("--no-mentions", action="store_true", help="Skip external mention aggregation")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable SQLite cache for this run",
    )
    parser.add_argument(
        "--log-verdict", action="store_true",
        help=(
            "Append scoring event to ~/.scraperx/verdicts.jsonl "
            "and optionally prompt for agree/disagree feedback."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    db = None if args.no_cache else SocialDB()
    try:
        analyzer = GithubAnalyzer(
            db=db,
            web_search_fn=_try_get_web_search_fn(),
            local_llm_fn=_try_get_local_llm_fn(),
        )
        report = analyzer.analyze_repo(
            args.spec,
            deep=args.deep,
            skip_mentions=args.no_mentions,
        )
    except InvalidRepoUrlError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(render_markdown(report))

    # Telemetry — runs after output so it doesn't delay the user
    if args.log_verdict:
        prompt_and_log_verdict(report)
    return 0


def main_trending(argv: list[str] | None = None) -> int:
    """Entry point for `scraperx trending ...`."""
    parser = argparse.ArgumentParser(
        prog="scraperx trending",
        description="List github.com/trending for a time window + language.",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "trending"
    parser.add_argument(
        "--since", choices=["daily", "weekly", "monthly"], default="daily",
        help="Trending window (default: daily)",
    )
    parser.add_argument("--lang", default="", help='Language slug (e.g. "python", "rust"; default: all)')
    parser.add_argument("--spoken", default="", help='Spoken-language filter (e.g. "en"; default: any)')
    parser.add_argument("--limit", type=int, default=25, help="Max rows to print (default: 25)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    repos = fetch_trending(
        since=args.since,
        language=args.lang,
        spoken_language_code=args.spoken,
    )[: args.limit]

    if args.json:
        out = [
            {
                "full_name": r.full_name,
                "description": r.description,
                "language": r.language,
                "stars": r.stars,
                "stars_today": r.stars_today,
                "url": r.url,
            }
            for r in repos
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if not repos:
        print(f"No trending repos found for since={args.since}, lang={args.lang or '(all)'}", file=sys.stderr)
        return 0

    window = args.since
    lang = args.lang or "all"
    print(f"Trending on GitHub — {window}, {lang}:\n")
    for i, r in enumerate(repos, 1):
        stars_today = f" +{r.stars_today}↑" if r.stars_today else ""
        lang_part = f"  ({r.language})" if r.language else ""
        print(f"[{i}] {r.full_name}{lang_part}  {r.stars:,}⭐{stars_today}")
        if r.description:
            print(f"    {r.description[:120]}")
        print(f"    {r.url}")
        print()
    return 0
