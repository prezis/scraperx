"""Verdict telemetry — write scoring events to ~/.scraperx/verdicts.jsonl.

Each line is a newline-delimited JSON object:
  {
    "timestamp": "2026-04-18T12:34:56Z",
    "repo":      "owner/repo",
    "url":       "https://github.com/owner/repo",
    "overall":   72,
    "sub_scores": {
      "bus_factor": 60, "momentum": 80, "health": 75, "readme_quality": 70
    },
    "mentions_count":  12,
    "warnings_count":  0,
    "warnings":        [],
    "scraperx_version": "1.4.2",
    "feedback":   null   # or "agree" | "disagree" | free-text why
  }

Usage
-----
    from scraperx.github_analyzer.telemetry import log_verdict
    log_verdict(report)                   # scoring event only
    log_verdict(report, feedback="agree") # with user agreement

Interactive CLI helper
----------------------
    from scraperx.github_analyzer.telemetry import prompt_and_log_verdict
    prompt_and_log_verdict(report)        # prints prompt, reads stdin, logs
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scraperx.github_analyzer.schemas import GithubReport

logger = logging.getLogger(__name__)

VERDICTS_DIR = Path.home() / ".scraperx"
VERDICTS_FILE = VERDICTS_DIR / "verdicts.jsonl"

_AGREE_ALIASES = {"y", "yes", "agree", "agreed", "ok", "yep", "yup", "si", "tak"}
_DISAGREE_ALIASES = {"n", "no", "disagree", "nope", "nie", "nah"}


def _normalise_feedback(raw: str | None) -> str | None:
    """Coerce user input to canonical feedback string or None.

    Rules:
    - None / empty / whitespace → None (skip logging)
    - "y", "yes", "agree", "tak" → "agree"
    - "n", "no", "disagree", "nie" → "disagree"
    - Anything else → stripped free-text (e.g. "disagree: 50k stars but score=45")
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    lower = stripped.lower()
    if lower in _AGREE_ALIASES:
        return "agree"
    if lower in _DISAGREE_ALIASES:
        return "disagree"
    return stripped


def log_verdict(report: GithubReport, feedback: str | None = None) -> bool:
    """Append one verdict event to ~/.scraperx/verdicts.jsonl.

    Returns True on success, False on error (errors are logged but not raised
    — telemetry must never crash the caller).
    """
    try:
        VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
        event: dict = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "repo": f"{report.owner}/{report.repo}",
            "url": report.url,
            "overall": report.trust.overall,
            "sub_scores": {
                "bus_factor": report.trust.bus_factor,
                "momentum": report.trust.momentum,
                "health": report.trust.health,
                "readme_quality": report.trust.readme_quality,
            },
            "mentions_count": len(report.mentions),
            "warnings_count": len(report.warnings),
            # cap at 5 lines so the file stays lean
            "warnings": list(report.warnings[:5]),
            "scraperx_version": report.scraperx_version,
            "feedback": _normalise_feedback(feedback),
        }
        with VERDICTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        logger.debug("Verdict logged → %s  (repo=%s/%s)", VERDICTS_FILE, report.owner, report.repo)
        return True
    except Exception as exc:
        logger.warning("Failed to log verdict for %s/%s: %s", report.owner, report.repo, exc)
        return False


def prompt_and_log_verdict(report: GithubReport, *, _input_fn=None) -> None:
    """Print an interactive agree/disagree prompt, read a response, and log.

    The optional `_input_fn` parameter replaces `input()` in tests so we
    can assert on the prompt text + side-effects without touching stdin.

    Calling convention (CLI)::

        prompt_and_log_verdict(report)

    The prompt is printed to stderr so it doesn't corrupt ``--json`` output.
    """
    # First log the scoring event unconditionally (feedback=None).
    log_verdict(report)

    ask = _input_fn or _safe_input

    print(
        f"\nVerdict for {report.owner}/{report.repo}: {report.trust.overall}/100",
        file=sys.stderr,
    )
    raw = ask("Agree? [y/n/<reason>] (Enter to skip): ")
    if raw is None:
        return  # non-interactive / pipe — skip

    normalised = _normalise_feedback(raw)
    if normalised is None:
        return  # user hit Enter — no feedback, scoring event already written

    # Overwrite the last line with the feedback-enriched version.
    # Simpler than in-place edit: just append a second event tagged as feedback.
    log_verdict(report, feedback=normalised)


def _safe_input(prompt: str) -> str | None:
    """Wrapper around input() that returns None when stdin is not a TTY."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
