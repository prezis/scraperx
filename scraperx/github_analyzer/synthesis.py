"""Synthesis layer — turn a populated GithubReport into a qwen-produced verdict.

Input: a GithubReport that's already had github_api.py + scoring.py + mentions
+ trending run against it. `trust.bus_factor / momentum / health /
readme_quality` are populated. `mentions[]` is normalised across Tier A + B.

Output: the same report, with `verdict_markdown` filled and `trust.overall`
+ `trust.rationale` computed. Caller may also write `.trust.overall` from a
heuristic fallback if the LLM is unreachable.

**Dependency injection**: `local_llm_fn` must match this contract:

    local_llm_fn(
        prompt: str,
        task_type: str = "reasoning",     # "fast" for qwen3:4b
        max_tokens: int = 2000,
    ) -> str

This matches the `local_llm` MCP tool signature in local-ai-mcp. T13 (CLI)
and T15 (MCP tool exposure) wire the real implementation; tests pass fakes.

**Two callable-injection points:**
    - `local_llm_fn`: the qwen LLM (fast vs deep)
    - `rubric_judge_fn`: optional rubric-judged scoring (Anthropic +90% pattern)

Both default to None → graceful degradation with a heuristic fallback so the
CLI never hangs on a missing GPU.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable

from scraperx.github_analyzer.schemas import (
    ExternalMention,
    GithubReport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt construction


_VERDICT_SYSTEM = (
    "You are a senior engineer triaging GitHub repositories for trust + "
    "relevance. Return ONLY a JSON object with keys: "
    '"overall" (int 0-100), "rationale" (single-line string, ≤ 140 chars), '
    '"verdict_markdown" (3 bullets, each ≤ 120 chars, plain markdown, '
    'inline [n] citations referencing the mentions list by 1-indexed number). '
    "Never include anything outside the JSON."
)


def _format_mentions(mentions: list[ExternalMention], limit: int = 8) -> str:
    """Render mentions as a numbered list for citation, with authority-signal
    metadata exposed so qwen can do implicit weighting (v1.4.1 — baseline
    worker's Bitter-Lesson insight from /reason synthesis 2026-04-18).

    The LLM sees per-platform authority signals already in the payload, e.g.
    HN `num_comments`, Reddit `subreddit_subscribers` + `upvote_ratio`,
    StackOverflow `asker_reputation`, dev.to `reading_time_minutes` /
    `comments_count`. No hardcoded weighting math — the LLM is the weighter.
    """
    if not mentions:
        return "(no external mentions found)"
    rows = []
    for i, m in enumerate(mentions[:limit], start=1):
        authority = _authority_blurb(m)
        # Keep each row under ~200 chars so qwen context stays lean at limit=8.
        rows.append(
            f"[{i}] {m.source}{authority}: {m.title[:80]} — score={m.score} — {m.url}"
        )
    return "\n".join(rows)


def _authority_blurb(m: ExternalMention) -> str:
    """Per-platform authority-signal blurb inserted after source name.

    Format: "(key1=val1, key2=val2)" or "" if nothing to show.
    Keep dense — qwen reads faster than it generates, density > verbosity.
    Returns empty string (no parens) when metadata is empty so the prompt
    doesn't print "hn(): ...".
    """
    md = m.metadata or {}
    parts: list[str] = []

    if m.source == "hn":
        c = md.get("num_comments")
        if c:
            parts.append(f"comments={c}")
    elif m.source == "reddit":
        sub = md.get("subreddit")
        subs = md.get("subreddit_subscribers")
        if sub and subs:
            parts.append(f"r/{sub} {_compact_num(subs)} subs")
        elif sub:
            parts.append(f"r/{sub}")
        nc = md.get("num_comments")
        if nc:
            parts.append(f"comments={nc}")
        ur = md.get("upvote_ratio")
        if ur is not None:
            try:
                parts.append(f"upvote={float(ur):.2f}")
            except (TypeError, ValueError):
                pass
    elif m.source == "stackoverflow":
        rep = md.get("asker_reputation")
        if rep:
            parts.append(f"rep={_compact_num(rep)}")
        vc = md.get("view_count")
        if vc:
            parts.append(f"views={_compact_num(vc)}")
        if md.get("has_accepted_answer"):
            parts.append("answered=Y")
    elif m.source == "devto":
        rtm = md.get("reading_time_minutes")
        if rtm:
            parts.append(f"read={rtm}min")
        cc = md.get("comments_count")
        if cc:
            parts.append(f"comments={cc}")
    elif m.source == "semantic_web":
        host = md.get("host")
        if host:
            parts.append(host)

    if not parts:
        return ""
    return f" ({', '.join(parts)})"


def _compact_num(n) -> str:
    """1_300_000 -> '1.3M', 4_500 -> '4.5k', 42 -> '42'. Keeps prompt dense.

    Simple rule: strip trailing '.0' but keep the suffix. `1_000_000 -> '1M'`,
    `1_300_000 -> '1.3M'`, `4_500 -> '4.5k'`, `4_000 -> '4k'`.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        num = f"{n / 1_000_000:.1f}"
        if num.endswith(".0"):
            num = num[:-2]
        return f"{num}M"
    if n >= 1_000:
        num = f"{n / 1_000:.1f}"
        if num.endswith(".0"):
            num = num[:-2]
        return f"{num}k"
    return str(n)


def _build_prompt(report: GithubReport) -> str:
    """Assemble the single-pass prompt fed to qwen."""
    parts = [
        _VERDICT_SYSTEM,
        "",
        f"REPOSITORY: {report.owner}/{report.repo}",
        f"URL: {report.url}",
        f"DESCRIPTION: {report.description or '(none)'}",
        f"LANGUAGE: {report.language or '(unknown)'}",
        f"LICENSE: {report.license_key or '(none)'}",
        f"STARS: {report.stars}  FORKS: {report.forks_count}  "
        f"OPEN ISSUES: {report.open_issues}",
        f"ARCHIVED: {report.archived}  PUSHED_AT: {report.pushed_at}",
        "",
        "SUB-SCORES (0-100, higher = better):",
        f"  bus_factor:     {report.trust.bus_factor}",
        f"  momentum:       {report.trust.momentum}",
        f"  health:         {report.trust.health}",
        f"  readme_quality: {report.trust.readme_quality}",
        "",
        f"CONTRIBUTORS (top {min(5, len(report.contributors))}):",
    ]
    for c in report.contributors[:5]:
        parts.append(f"  - {c.handle} ({c.commits} commits)")
    parts.append("")
    parts.append(f"SECURITY ADVISORIES: {len(report.advisories)}")
    for a in report.advisories[:3]:
        parts.append(f"  - {a.ghsa_id} ({a.severity})")
    parts.append("")
    parts.append("EXTERNAL MENTIONS (numbered for citation):")
    parts.append(_format_mentions(report.mentions))
    parts.append("")
    parts.append(
        "Produce the JSON object. overall should reflect the sub-scores + "
        "community signal (external mentions with high scores weight "
        "positively; archived = always low; high health + momentum + fresh "
        "mentions = high)."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON extraction (brittle LLM output guard)


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Pull the first {...} block out of an LLM response — qwen sometimes
    wraps its JSON in prose or code fences, so be forgiving.

    Returns empty dict if no valid JSON found.
    """
    if not text:
        return {}

    # First try: direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass

    # Second try: find the first balanced {...} block via a brace counter
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (json.JSONDecodeError, TypeError):
                    break
    return {}


# ---------------------------------------------------------------------------
# Heuristic fallback


def _heuristic_overall(report: GithubReport) -> int:
    """Sub-score weighted average — used when LLM unreachable."""
    t = report.trust
    if report.archived:
        return 5
    # weights tuned to emphasise health + bus_factor
    weighted = (
        t.bus_factor * 0.25
        + t.momentum * 0.20
        + t.health * 0.35
        + t.readme_quality * 0.20
    )
    # Bonus for external mentions (cap at 10)
    bonus = min(10, len(report.mentions))
    return int(min(100, max(0, weighted + bonus)))


def _heuristic_rationale(report: GithubReport) -> str:
    t = report.trust
    signals = []
    if report.archived:
        signals.append("archived")
    if t.health >= 70:
        signals.append("healthy")
    elif t.health <= 30:
        signals.append("unhealthy")
    if t.momentum >= 70:
        signals.append("high momentum")
    elif t.momentum <= 20:
        signals.append("stalled")
    if t.bus_factor <= 20:
        signals.append("single-author")
    if not signals:
        signals.append("mixed signals")
    return f"{report.owner}/{report.repo}: {', '.join(signals)} (heuristic — LLM unavailable)"


def _heuristic_verdict_markdown(report: GithubReport) -> str:
    overall = _heuristic_overall(report)
    t = report.trust
    return (
        f"- Overall heuristic score: {overall}/100 (LLM not reached — sub-score weighted average).\n"
        f"- Sub-scores: bus_factor={t.bus_factor}, momentum={t.momentum}, "
        f"health={t.health}, readme={t.readme_quality}.\n"
        f"- {len(report.mentions)} external mentions gathered across "
        f"{len({m.source for m in report.mentions})} platforms."
    )


# ---------------------------------------------------------------------------
# Main entry


def synthesize(
    report: GithubReport,
    local_llm_fn: Callable[..., str] | None = None,
    deep: bool = False,
    max_tokens: int = 1200,
) -> GithubReport:
    """Fill in `report.verdict_markdown`, `report.trust.overall`, and
    `report.trust.rationale`. Returns the same report (mutated in place) for
    fluency.

    Args:
        report: populated GithubReport (sub-scores already computed).
        local_llm_fn: `local_llm` MCP callable. If None → heuristic fallback.
        deep: when True, use task_type='reasoning' (→ qwen3.5:27b) instead of
              the fast default (→ qwen3:4b). Costs more latency for fewer
              misses.
        max_tokens: upper bound on generation length.
    """
    # Always stamp analyzed_at — useful for cache freshness checks downstream
    if report.analyzed_at == 0.0:
        report.analyzed_at = time.time()

    if local_llm_fn is None:
        logger.info("synthesize: no local_llm_fn — using heuristic fallback")
        report.trust.overall = _heuristic_overall(report)
        report.trust.rationale = _heuristic_rationale(report)
        report.verdict_markdown = _heuristic_verdict_markdown(report)
        report.warnings.append("LLM synthesis unavailable — used heuristic fallback")
        return report

    prompt = _build_prompt(report)
    task_type = "reasoning" if deep else "fast"

    try:
        raw = local_llm_fn(prompt=prompt, task_type=task_type, max_tokens=max_tokens)
    except TypeError:
        # Older signature — try positional
        try:
            raw = local_llm_fn(prompt, task_type, max_tokens)
        except Exception as e:
            logger.warning("synthesize: positional LLM fallback failed: %s", e)
            raw = ""
    except Exception as e:
        logger.warning("synthesize: local_llm_fn error: %s", e)
        raw = ""

    parsed = _extract_json(raw)
    if not parsed:
        logger.warning("synthesize: LLM returned no parseable JSON — using heuristic fallback")
        report.trust.overall = _heuristic_overall(report)
        report.trust.rationale = _heuristic_rationale(report)
        report.verdict_markdown = _heuristic_verdict_markdown(report)
        report.warnings.append("LLM output unparseable — used heuristic fallback")
        return report

    # Accept the LLM's overall iff it's a sane integer 0-100
    try:
        overall = int(parsed.get("overall", _heuristic_overall(report)))
    except (TypeError, ValueError):
        overall = _heuristic_overall(report)
    report.trust.overall = max(0, min(100, overall))

    rationale = parsed.get("rationale", "")
    if isinstance(rationale, str):
        report.trust.rationale = rationale[:200]

    verdict_md = parsed.get("verdict_markdown", "")
    if isinstance(verdict_md, str) and verdict_md.strip():
        report.verdict_markdown = verdict_md.strip()
    else:
        report.verdict_markdown = _heuristic_verdict_markdown(report)

    return report
