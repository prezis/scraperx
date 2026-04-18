"""Tests for scraperx.github_analyzer.synthesis (T12).

Dependency-injected local_llm_fn — no real GPU call here. Tests cover:
- Heuristic fallback (no LLM / LLM errors / LLM unparseable)
- Happy-path LLM output with clean JSON
- JSON-inside-prose extraction (qwen sometimes wraps in commentary)
- JSON-inside-code-fence extraction
- Sanity clamping on overall score
- archived repos get low heuristic
- Prompt contains key signals (sub-scores, mentions, advisories)
"""

from __future__ import annotations

import json

from scraperx.github_analyzer.schemas import (
    ContributorInfo,
    ExternalMention,
    GithubReport,
    RepoTrustScore,
    SecurityAdvisory,
)
from scraperx.github_analyzer.synthesis import (
    _build_prompt,
    _extract_json,
    _heuristic_overall,
    _heuristic_rationale,
    synthesize,
)

# ---------------------------------------------------------------------------
# Fixtures


def _make_report(**overrides) -> GithubReport:
    defaults = {
        "owner": "yt-dlp",
        "repo": "yt-dlp",
        "url": "https://github.com/yt-dlp/yt-dlp",
        "description": "feature-rich CLI downloader",
        "stars": 80000,
        "forks_count": 5000,
        "open_issues": 500,
        "language": "Python",
        "license_key": "unlicense",
        "archived": False,
        "pushed_at": "2026-04-15T00:00:00Z",
        "contributors": [
            ContributorInfo(handle="alice", commits=500),
            ContributorInfo(handle="bob", commits=300),
        ],
        "mentions": [
            ExternalMention(source="hn", title="Amazing tool", url="https://n.ycombinator.com/item?id=1", score=500),
            ExternalMention(source="reddit", title="r/python loves it", url="https://reddit.com/r/python/a", score=100),
        ],
        "trust": RepoTrustScore(
            bus_factor=62,
            momentum=80,
            health=85,
            readme_quality=90,
        ),
    }
    defaults.update(overrides)
    return GithubReport(**defaults)


# ---------------------------------------------------------------------------
# _extract_json


def test_extract_clean_json():
    s = '{"overall": 87, "rationale": "healthy", "verdict_markdown": "- a\\n- b\\n- c"}'
    assert _extract_json(s)["overall"] == 87


def test_extract_json_wrapped_in_prose():
    s = 'Here is my verdict:\n\n{"overall": 75, "rationale": "good"}\n\nThat is all.'
    assert _extract_json(s) == {"overall": 75, "rationale": "good"}


def test_extract_json_wrapped_in_code_fence():
    s = '```json\n{"overall": 60, "rationale": "mixed"}\n```'
    assert _extract_json(s) == {"overall": 60, "rationale": "mixed"}


def test_extract_json_handles_nested_braces():
    s = 'The result: {"overall": 50, "rationale": "ok", "extra": {"nested": true}}'
    result = _extract_json(s)
    assert result["overall"] == 50
    assert result["extra"]["nested"] is True


def test_extract_json_empty_input():
    assert _extract_json("") == {}
    assert _extract_json(None) == {}  # type: ignore[arg-type]


def test_extract_json_no_json_in_text():
    assert _extract_json("Just some prose here, no braces at all.") == {}


def test_extract_json_malformed_returns_empty():
    # Unbalanced braces
    assert _extract_json("{ not valid json }}}") == {}


# ---------------------------------------------------------------------------
# _heuristic_overall


def test_heuristic_overall_good_repo():
    report = _make_report()  # high sub-scores, 2 mentions
    score = _heuristic_overall(report)
    assert 70 <= score <= 100


def test_heuristic_overall_archived_is_low():
    report = _make_report(archived=True)
    assert _heuristic_overall(report) == 5


def test_heuristic_overall_empty_sub_scores():
    report = _make_report(trust=RepoTrustScore(), mentions=[])
    assert _heuristic_overall(report) == 0


def test_heuristic_rationale_mentions_key_signals():
    rep = _make_report(trust=RepoTrustScore(bus_factor=10, momentum=10, health=20, readme_quality=40))
    r = _heuristic_rationale(rep)
    assert "unhealthy" in r
    assert "stalled" in r
    assert "single-author" in r


# ---------------------------------------------------------------------------
# synthesize — heuristic fallback (no LLM)


def test_synthesize_with_no_llm_uses_heuristic():
    report = _make_report()
    out = synthesize(report, local_llm_fn=None)
    assert out.trust.overall > 0
    assert out.trust.rationale != ""
    assert "heuristic" in out.trust.rationale.lower()
    assert len(out.warnings) == 1
    assert "LLM synthesis unavailable" in out.warnings[0]


def test_synthesize_with_no_llm_sets_analyzed_at():
    report = _make_report()
    assert report.analyzed_at == 0.0
    synthesize(report, local_llm_fn=None)
    assert report.analyzed_at > 0


def test_synthesize_archived_repo_via_heuristic():
    report = _make_report(archived=True)
    synthesize(report, local_llm_fn=None)
    assert report.trust.overall == 5


# ---------------------------------------------------------------------------
# synthesize — LLM happy path


def test_synthesize_accepts_clean_llm_output():
    def fake_llm(prompt, task_type="fast", max_tokens=1200):
        return json.dumps(
            {
                "overall": 92,
                "rationale": "Huge community, stable team, active releases.",
                "verdict_markdown": "- Huge momentum [1]\n- Stable maintainers [2]\n- Low issue ratio.",
            }
        )

    report = _make_report()
    synthesize(report, local_llm_fn=fake_llm)
    assert report.trust.overall == 92
    assert "Huge community" in report.trust.rationale
    assert "[1]" in report.verdict_markdown
    assert report.warnings == []


def test_synthesize_respects_deep_flag():
    calls = {"task_type": None}

    def fake_llm(prompt, task_type="fast", max_tokens=1200):
        calls["task_type"] = task_type
        return '{"overall": 50, "rationale": "r", "verdict_markdown": "- a"}'

    synthesize(_make_report(), local_llm_fn=fake_llm, deep=False)
    assert calls["task_type"] == "fast"

    synthesize(_make_report(), local_llm_fn=fake_llm, deep=True)
    assert calls["task_type"] == "reasoning"


def test_synthesize_clamps_overall_into_range():
    def llm_out_of_range(prompt, task_type="fast", max_tokens=1200):
        return '{"overall": 9999, "rationale": "r", "verdict_markdown": "- x"}'

    report = _make_report()
    synthesize(report, local_llm_fn=llm_out_of_range)
    assert report.trust.overall == 100


def test_synthesize_rejects_non_int_overall():
    def llm(prompt, task_type="fast", max_tokens=1200):
        return '{"overall": "not-an-int", "rationale": "r", "verdict_markdown": "- x"}'

    report = _make_report()
    synthesize(report, local_llm_fn=llm)
    # Falls back to heuristic for overall
    assert 0 <= report.trust.overall <= 100


# ---------------------------------------------------------------------------
# synthesize — LLM error paths


def test_synthesize_llm_raises_falls_back_to_heuristic():
    def bad_llm(prompt, task_type="fast", max_tokens=1200):
        raise RuntimeError("GPU OOM")

    report = _make_report()
    synthesize(report, local_llm_fn=bad_llm)
    assert report.trust.overall > 0
    assert any("unparseable" in w for w in report.warnings) or any("unavailable" in w for w in report.warnings)


def test_synthesize_llm_returns_garbage():
    def garbage_llm(prompt, task_type="fast", max_tokens=1200):
        return "I could not produce valid JSON, sorry."

    report = _make_report()
    synthesize(report, local_llm_fn=garbage_llm)
    assert 0 <= report.trust.overall <= 100
    assert any("unparseable" in w for w in report.warnings)


def test_synthesize_llm_returns_empty_string():
    def empty_llm(prompt, task_type="fast", max_tokens=1200):
        return ""

    report = _make_report()
    synthesize(report, local_llm_fn=empty_llm)
    assert any("unparseable" in w for w in report.warnings)


def test_synthesize_llm_returns_json_but_missing_fields():
    """Partial JSON — accept what's there, fallback where missing."""

    def partial_llm(prompt, task_type="fast", max_tokens=1200):
        return '{"overall": 77}'  # No rationale, no verdict_markdown

    report = _make_report()
    synthesize(report, local_llm_fn=partial_llm)
    assert report.trust.overall == 77
    assert report.verdict_markdown != ""  # Heuristic fallback for missing


def test_synthesize_positional_only_llm():
    """Some callers may pass a positional-only function."""

    def positional(prompt, task_type, max_tokens):
        return '{"overall": 42, "rationale": "r", "verdict_markdown": "- a"}'

    report = _make_report()
    synthesize(report, local_llm_fn=positional)
    assert report.trust.overall == 42


# ---------------------------------------------------------------------------
# Prompt content


def test_prompt_contains_all_sub_scores():
    report = _make_report()
    prompt = _build_prompt(report)
    assert "bus_factor" in prompt
    assert "62" in prompt  # The bus_factor value
    assert "momentum" in prompt
    assert "health" in prompt
    assert "readme_quality" in prompt


def test_prompt_includes_numbered_mentions():
    report = _make_report()
    prompt = _build_prompt(report)
    assert "[1]" in prompt
    assert "[2]" in prompt
    assert "hn" in prompt
    assert "reddit" in prompt


def test_prompt_includes_advisories():
    report = _make_report(
        advisories=[
            SecurityAdvisory(ghsa_id="GHSA-abc-123", severity="high", summary="critical bug"),
        ]
    )
    prompt = _build_prompt(report)
    assert "GHSA-abc-123" in prompt
    assert "high" in prompt


def test_prompt_handles_empty_mentions():
    report = _make_report(mentions=[])
    prompt = _build_prompt(report)
    assert "no external mentions found" in prompt
