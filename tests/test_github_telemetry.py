"""Tests for scraperx.github_analyzer.telemetry — v1.4.2.

Coverage:
1. _normalise_feedback — all agree/disagree aliases, free text, None/empty.
2. log_verdict — writes valid JSONL, creates directory, graceful on error.
3. prompt_and_log_verdict — non-interactive skip, agree, disagree, Enter skip.
4. Integration: log_verdict fields match GithubReport structure.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scraperx.github_analyzer.schemas import (
    ExternalMention,
    GithubReport,
    RepoTrustScore,
)
from scraperx.github_analyzer.telemetry import (
    _normalise_feedback,
    log_verdict,
    prompt_and_log_verdict,
)

# ---------------------------------------------------------------------------
# Helpers

def _make_report(
    owner="prezis",
    repo="scraperx",
    overall=72,
    bus_factor=60,
    momentum=80,
    health=75,
    readme_quality=70,
    mentions_count=3,
    warnings=None,
    scraperx_version="1.4.2",
) -> GithubReport:
    report = GithubReport(
        owner=owner,
        repo=repo,
        url=f"https://github.com/{owner}/{repo}",
        analyzed_at=time.time(),
        scraperx_version=scraperx_version,
    )
    report.trust = RepoTrustScore(
        bus_factor=bus_factor,
        momentum=momentum,
        health=health,
        readme_quality=readme_quality,
        overall=overall,
    )
    for i in range(mentions_count):
        report.mentions.append(
            ExternalMention(source="hn", title=f"Hit {i}", url=f"https://hn.test/{i}")
        )
    if warnings:
        report.warnings.extend(warnings)
    return report


# ---------------------------------------------------------------------------
# 1. _normalise_feedback

class TestNormaliseFeedback:
    def test_none_returns_none(self):
        assert _normalise_feedback(None) is None

    def test_empty_string_returns_none(self):
        assert _normalise_feedback("") is None

    def test_whitespace_only_returns_none(self):
        assert _normalise_feedback("   ") is None

    @pytest.mark.parametrize("alias", ["y", "Y", "yes", "Yes", "YES", "agree", "Agree", "ok", "yep", "yup", "si", "tak"])
    def test_agree_aliases(self, alias):
        assert _normalise_feedback(alias) == "agree"

    @pytest.mark.parametrize("alias", ["n", "N", "no", "No", "NO", "disagree", "Disagree", "nope", "nie", "nah"])
    def test_disagree_aliases(self, alias):
        assert _normalise_feedback(alias) == "disagree"

    def test_free_text_returned_stripped(self):
        raw = "  disagree: stars=50k but verdict=45  "
        assert _normalise_feedback(raw) == "disagree: stars=50k but verdict=45"

    def test_free_text_not_matching_alias(self):
        raw = "seems too low for an Anthropic project"
        assert _normalise_feedback(raw) == "seems too low for an Anthropic project"


# ---------------------------------------------------------------------------
# 2. log_verdict

class TestLogVerdict:
    def test_creates_verdicts_file(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            result = log_verdict(report)
        assert result is True
        assert verdicts_file.exists()

    def test_written_line_is_valid_json(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report)
        line = verdicts_file.read_text(encoding="utf-8").strip()
        event = json.loads(line)
        assert event["repo"] == "prezis/scraperx"

    def test_event_fields_present(self, tmp_path):
        report = _make_report(warnings=["README not found"])
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report)
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert event["url"] == "https://github.com/prezis/scraperx"
        assert event["overall"] == 72
        assert event["sub_scores"]["bus_factor"] == 60
        assert event["sub_scores"]["momentum"] == 80
        assert event["sub_scores"]["health"] == 75
        assert event["sub_scores"]["readme_quality"] == 70
        assert event["mentions_count"] == 3
        assert event["warnings_count"] == 1
        assert "README not found" in event["warnings"]
        assert event["scraperx_version"] == "1.4.2"
        assert event["feedback"] is None
        assert event["timestamp"].endswith("Z")

    def test_feedback_normalised_in_event(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report, feedback="y")
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert event["feedback"] == "agree"

    def test_disagree_feedback_stored(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report, feedback="n")
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert event["feedback"] == "disagree"

    def test_free_text_feedback_stored(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report, feedback="disagree: rust-lang org should be +20")
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert event["feedback"] == "disagree: rust-lang org should be +20"

    def test_appends_multiple_events(self, tmp_path):
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(_make_report(repo="a"))
            log_verdict(_make_report(repo="b"))
        lines = verdicts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        repos = {json.loads(ln)["repo"].split("/")[1] for ln in lines}
        assert repos == {"a", "b"}

    def test_warnings_capped_at_5(self, tmp_path):
        report = _make_report(warnings=[f"w{i}" for i in range(10)])
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report)
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert len(event["warnings"]) == 5
        assert event["warnings_count"] == 10  # full count preserved

    def test_returns_false_on_permission_error(self, tmp_path):
        report = _make_report()
        bad_dir = tmp_path / "no_write"
        bad_file = bad_dir / "verdicts.jsonl"
        # Patch Path.mkdir to simulate a permission error at directory creation
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", bad_dir), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", bad_file), \
             patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            result = log_verdict(report)
        assert result is False

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        verdicts_file = nested / "verdicts.jsonl"
        report = _make_report()
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", nested), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            result = log_verdict(report)
        assert result is True
        assert nested.exists()


# ---------------------------------------------------------------------------
# 3. prompt_and_log_verdict

class TestPromptAndLogVerdict:
    def test_non_interactive_logs_only_scoring_event(self, tmp_path):
        """When stdin is not a TTY (_input_fn returns None), one event is logged."""
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            prompt_and_log_verdict(report, _input_fn=lambda _: None)
        lines = verdicts_file.read_text(encoding="utf-8").strip().splitlines()
        # One scoring event only — no feedback event because input returned None
        assert len(lines) == 1
        assert json.loads(lines[0])["feedback"] is None

    def test_enter_skip_logs_only_scoring_event(self, tmp_path):
        """User hits Enter (empty string) → one event, feedback=None."""
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            prompt_and_log_verdict(report, _input_fn=lambda _: "")
        lines = verdicts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["feedback"] is None

    def test_agree_logs_two_events(self, tmp_path):
        """'y' → scoring event + feedback event, second has feedback='agree'."""
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            prompt_and_log_verdict(report, _input_fn=lambda _: "y")
        lines = verdicts_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        events = [json.loads(ln) for ln in lines]
        # First: scoring event (no feedback); second: feedback event
        assert events[0]["feedback"] is None
        assert events[1]["feedback"] == "agree"

    def test_disagree_with_reason(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            prompt_and_log_verdict(report, _input_fn=lambda _: "disagree: stars=50k but verdict=45")
        lines = verdicts_file.read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(ln) for ln in lines]
        assert events[-1]["feedback"] == "disagree: stars=50k but verdict=45"

    def test_prompt_text_passed_to_input_fn(self, tmp_path):
        """Verify the prompt string reaches the input function."""
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        captured_prompts: list[str] = []

        def mock_input(prompt: str) -> str:
            captured_prompts.append(prompt)
            return ""  # skip

        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            prompt_and_log_verdict(report, _input_fn=mock_input)

        assert len(captured_prompts) == 1
        assert "Agree?" in captured_prompts[0]


# ---------------------------------------------------------------------------
# 4. Integration: field round-trip

class TestFieldRoundTrip:
    def test_report_fields_survive_jsonl_round_trip(self, tmp_path):
        report = _make_report(
            owner="rust-lang",
            repo="rustfmt",
            overall=88,
            bus_factor=75,
            momentum=95,
            health=85,
            readme_quality=80,
            mentions_count=7,
            scraperx_version="1.4.2",
        )
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report)
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        assert event["repo"] == "rust-lang/rustfmt"
        assert event["url"] == "https://github.com/rust-lang/rustfmt"
        assert event["overall"] == 88
        assert event["sub_scores"] == {
            "bus_factor": 75,
            "momentum": 95,
            "health": 85,
            "readme_quality": 80,
        }
        assert event["mentions_count"] == 7
        assert event["scraperx_version"] == "1.4.2"

    def test_timestamp_is_iso8601_z(self, tmp_path):
        report = _make_report()
        verdicts_file = tmp_path / "verdicts.jsonl"
        with patch("scraperx.github_analyzer.telemetry.VERDICTS_DIR", tmp_path), \
             patch("scraperx.github_analyzer.telemetry.VERDICTS_FILE", verdicts_file):
            log_verdict(report)
        event = json.loads(verdicts_file.read_text(encoding="utf-8").strip())
        ts = event["timestamp"]
        # Must parse as ISO 8601 and end with Z
        from datetime import datetime
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert ts.endswith("Z")
