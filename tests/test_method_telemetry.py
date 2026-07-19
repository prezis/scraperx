"""Tests for scraperx.method_telemetry — self-learning method-order ledger.

Coverage:
1. record — writes valid JSONL, creates dir, fail-open on unwritable path.
2. preferred_order — default on no/empty ledger; <MIN_SAMPLES falls back to
   default; ≥MIN_SAMPLES failures demote; successes promote; exponential
   decay lets a once-failing method recover; corrupt lines are skipped.
3. method_stats — raw per-method aggregates.
4. Reddit wiring — tier order ADAPTS: after repeated tier1 failures the
   mirror tier is tried FIRST on subsequent calls (env-var ledger seam).

All network-free; the ledger lives in a tmpdir (explicit path or the
autouse conftest env-var fixture).
"""

from __future__ import annotations

import json
import time

import pytest

from scraperx import method_telemetry as mt
from scraperx.method_telemetry import method_stats, preferred_order, record

DEFAULT = ("old_json", "redlib", "stealth", "stealth_mirror")


def _ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


def _write_event(path, method, success, *, age_s=0.0, scraper="reddit", domain="d"):
    """Append a hand-crafted event with a controllable timestamp."""
    ev = {
        "timestamp": "2026-07-19T00:00:00Z",
        "ts_epoch": time.time() - age_s,
        "scraper": scraper,
        "method": method,
        "target_domain": domain,
        "success": success,
        "latency_ms": 100.0,
        "http_status": 200 if success else 403,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def test_record_writes_valid_jsonl_and_creates_dir(tmp_path):
    path = tmp_path / "nested" / "ledger.jsonl"
    assert record(
        "reddit", "old_json", "old.reddit.com", False,
        latency_ms=412.66, http_status=403, ledger_path=path,
    )
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["scraper"] == "reddit"
    assert ev["method"] == "old_json"
    assert ev["target_domain"] == "old.reddit.com"
    assert ev["success"] is False
    assert ev["latency_ms"] == 412.7  # rounded to 1 decimal
    assert ev["http_status"] == 403
    assert ev["timestamp"].endswith("Z")
    assert isinstance(ev["ts_epoch"], float)


def test_record_optional_fields_default_to_null(tmp_path):
    path = _ledger(tmp_path)
    assert record("reddit", "redlib", "redlib-mirrors", True, ledger_path=path)
    ev = json.loads(path.read_text().strip())
    assert ev["latency_ms"] is None
    assert ev["http_status"] is None


def test_record_fail_open_on_unwritable_path(tmp_path):
    # Path IS a directory → open("a") fails → False, never raises.
    assert record("reddit", "old_json", "d", True, ledger_path=tmp_path) is False


def test_record_respects_env_var_seam(tmp_path, monkeypatch):
    path = tmp_path / "env-ledger.jsonl"
    monkeypatch.setenv(mt.ENV_PATH_VAR, str(path))
    assert record("reddit", "old_json", "d", True)
    assert path.exists()


# ---------------------------------------------------------------------------
# preferred_order
# ---------------------------------------------------------------------------


def test_preferred_order_default_when_no_ledger(tmp_path):
    order = preferred_order("reddit", DEFAULT, ledger_path=tmp_path / "missing.jsonl")
    assert order == list(DEFAULT)


def test_preferred_order_default_under_min_samples(tmp_path):
    path = _ledger(tmp_path)
    for _ in range(mt.MIN_SAMPLES - 1):  # 4 failures — below trust threshold
        _write_event(path, "old_json", False)
    assert preferred_order("reddit", DEFAULT, ledger_path=path) == list(DEFAULT)


def test_preferred_order_demotes_failing_method_at_min_samples(tmp_path):
    path = _ledger(tmp_path)
    for _ in range(mt.MIN_SAMPLES):  # 5 failures — old_json score → 0.0
        _write_event(path, "old_json", False)
    order = preferred_order("reddit", DEFAULT, ledger_path=path)
    assert order[-1] == "old_json"
    assert order[:3] == ["redlib", "stealth", "stealth_mirror"]  # default order kept


def test_preferred_order_promotes_proven_method_above_untried(tmp_path):
    path = _ledger(tmp_path)
    for _ in range(mt.MIN_SAMPLES):
        _write_event(path, "stealth_mirror", True)  # score → 1.0 > 0.5 prior
    order = preferred_order("reddit", DEFAULT, ledger_path=path)
    assert order[0] == "stealth_mirror"
    assert order[1:] == ["old_json", "redlib", "stealth"]


def test_preferred_order_exponential_decay_recovers_method(tmp_path):
    path = _ledger(tmp_path)
    # old_json: 10 failures TWO DAYS ago (weight ≈ 0.5^8 each) + 6 fresh
    # successes → decayed rate ≈ 0.99 → back on top of everything.
    for _ in range(10):
        _write_event(path, "old_json", False, age_s=48 * 3600)
    for _ in range(6):
        _write_event(path, "old_json", True)
    # redlib: 5 FRESH failures → 0.0 → dead last.
    for _ in range(5):
        _write_event(path, "redlib", False)
    order = preferred_order("reddit", DEFAULT, ledger_path=path)
    assert order[0] == "old_json"
    assert order[-1] == "redlib"


def test_preferred_order_scoped_by_scraper(tmp_path):
    path = _ledger(tmp_path)
    for _ in range(mt.MIN_SAMPLES):
        _write_event(path, "old_json", False, scraper="youtube")
    # reddit's view of the ledger is untouched by youtube's failures.
    assert preferred_order("reddit", DEFAULT, ledger_path=path) == list(DEFAULT)


def test_preferred_order_target_domain_filter(tmp_path):
    path = _ledger(tmp_path)
    for _ in range(mt.MIN_SAMPLES):
        _write_event(path, "old_json", False, domain="old.reddit.com")
        _write_event(path, "old_json", True, domain="other.example")
    order = preferred_order(
        "reddit", DEFAULT, target_domain="old.reddit.com", ledger_path=path
    )
    assert order[-1] == "old_json"
    # Unfiltered: 5 fail + 5 success = 0.5 == neutral prior → default order.
    assert preferred_order("reddit", DEFAULT, ledger_path=path) == list(DEFAULT)


def test_preferred_order_skips_corrupt_lines_fail_open(tmp_path):
    path = _ledger(tmp_path)
    path.write_text('{"broken json\nnot json at all\n[]\n')
    for _ in range(mt.MIN_SAMPLES):
        _write_event(path, "old_json", False)
    order = preferred_order("reddit", DEFAULT, ledger_path=path)
    assert order[-1] == "old_json"  # good lines still counted, no raise


def test_preferred_order_returns_new_list_with_all_methods(tmp_path):
    default = ["a", "b", "c"]
    order = preferred_order("reddit", default, ledger_path=_ledger(tmp_path))
    assert order == default and order is not default


# ---------------------------------------------------------------------------
# method_stats
# ---------------------------------------------------------------------------


def test_method_stats_aggregates(tmp_path):
    path = _ledger(tmp_path)
    for ok in (True, True, False):
        _write_event(path, "old_json", ok)
    stats = method_stats("reddit", ledger_path=path)
    s = stats["old_json"]
    assert s["n"] == 3 and s["successes"] == 2
    assert s["success_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert s["avg_latency_ms"] == pytest.approx(100.0)


def test_method_stats_empty_on_missing_ledger(tmp_path):
    assert method_stats("reddit", ledger_path=tmp_path / "missing.jsonl") == {}


# ---------------------------------------------------------------------------
# Reddit wiring — the tier order ADAPTS
# ---------------------------------------------------------------------------


def _reddit_scraper(**kw):
    from scraperx.reddit import RedditScraper

    kw.setdefault("min_interval_s", 0)
    kw.setdefault("jitter_s", 0)
    kw.setdefault("retry_backoff_s", 0)
    kw.setdefault("enable_stealth", False)
    return RedditScraper(**kw)


REDLIB_HTML = """
<html><body>
<a href="/r/ethereum/comments/aaa111/alchemy_vs_infura/">Alchemy vs Infura</a>
</body></html>
"""


def test_reddit_records_tier_attempts_to_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "wired.jsonl"
    monkeypatch.setenv(mt.ENV_PATH_VAR, str(ledger))
    from scraperx import reddit as reddit_mod

    def fake(url, *, user_agent, timeout):
        if "old.reddit.com" in url:
            return 500, "down"
        return 200, REDLIB_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _reddit_scraper()
    posts = s.search_subreddit("ethereum", "alchemy")
    assert posts and s.last_tier_used == "redlib"

    events = [json.loads(x) for x in ledger.read_text().strip().splitlines()]
    by_method = {e["method"]: e for e in events}
    assert by_method["old_json"]["success"] is False
    assert by_method["old_json"]["http_status"] == 500  # recovered from error msg
    assert by_method["redlib"]["success"] is True
    assert by_method["redlib"]["target_domain"] == "redlib-mirrors"
    assert all(e["scraper"] == "reddit" for e in events)


def test_reddit_tier_order_adapts_after_recorded_failures(tmp_path, monkeypatch):
    """The headline behavior: a blocked tier1 gets DEMOTED on later calls."""
    ledger = tmp_path / "adapt.jsonl"
    monkeypatch.setenv(mt.ENV_PATH_VAR, str(ledger))
    from scraperx import reddit as reddit_mod

    calls: list[str] = []

    def fake(url, *, user_agent, timeout):
        calls.append(url)
        if "old.reddit.com" in url:
            return 403, "blocked"
        return 200, REDLIB_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _reddit_scraper()

    # Warm-up: MIN_SAMPLES calls; each records an old_json failure + a
    # redlib success. Every one still tries old.reddit.com FIRST.
    for _ in range(mt.MIN_SAMPLES):
        calls.clear()
        s.search_subreddit("ethereum", "alchemy")
        assert "old.reddit.com" in calls[0]

    # Ledger now trusts the data: redlib(1.0) > untried(0.5) > old_json(0.0).
    assert preferred_order("reddit", reddit_mod.TIER_DEFAULT_ORDER)[0] == "redlib"
    assert preferred_order("reddit", reddit_mod.TIER_DEFAULT_ORDER)[-1] == "old_json"

    # Adapted call: the FIRST request now goes to a mirror, not reddit.
    calls.clear()
    s.search_subreddit("ethereum", "alchemy")
    assert "old.reddit.com" not in calls[0]
    assert s.last_tier_used == "redlib"


def test_reddit_adaptive_off_keeps_canonical_order(tmp_path, monkeypatch):
    ledger = tmp_path / "off.jsonl"
    monkeypatch.setenv(mt.ENV_PATH_VAR, str(ledger))
    for _ in range(mt.MIN_SAMPLES):  # ledger says old_json is dead...
        _write_event(ledger, "old_json", False)
    from scraperx import reddit as reddit_mod

    calls: list[str] = []

    def fake(url, *, user_agent, timeout):
        calls.append(url)
        if "old.reddit.com" in url:
            return 403, "blocked"
        return 200, REDLIB_HTML

    monkeypatch.setattr(reddit_mod, "_http_get", fake)
    s = _reddit_scraper(adaptive_tiers=False)
    s.search_subreddit("ethereum", "alchemy")
    assert "old.reddit.com" in calls[0]  # ...but adaptive off ignores it
    # And records nothing:
    assert ledger.read_text().strip().count("\n") + 1 == mt.MIN_SAMPLES


def test_reddit_telemetry_failure_never_breaks_scrape(tmp_path, monkeypatch):
    """Fail-open end-to-end: unwritable ledger path → scrape still works."""
    monkeypatch.setenv(mt.ENV_PATH_VAR, str(tmp_path))  # a DIRECTORY → append fails
    from scraperx import reddit as reddit_mod

    monkeypatch.setattr(
        reddit_mod, "_http_get", lambda url, *, user_agent, timeout: (200, json.dumps(
            {"kind": "Listing", "data": {"children": [
                {"kind": "t3", "data": {"id": "x1", "title": "ok"}}
            ]}}
        ))
    )
    s = _reddit_scraper()
    posts = s.search_subreddit("ethereum", "alchemy")
    assert posts[0].id == "x1"
    assert s.last_tier_used == "old_json"
