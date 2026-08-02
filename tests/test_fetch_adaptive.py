"""The cascade learns which leg works PER HOST — proof, not assertion.

Until now `smart_fetch` walked a frozen constant: jina, urllib, playwright,
stealth. On a walled host that means 403, 403, 403, 200 — in that order, every
time, forever. Three wasted attempts per URL for the life of the project, while
a self-learning ledger sat in the tree wired only into `reddit.py`.

These tests matter because the FULL SUITE PASSES WITHOUT THEM: with an empty
ledger `preferred_order` returns the default order unchanged, so every existing
test exercises exactly the old behaviour and proves nothing about the new one.
Each test here seeds the ledger first, so the reordering is actually observed.

Ledger isolation is inherited from the autouse fixture in conftest.py — it
already redirected the ledger to tmp_path when reddit.py got telemetry, so this
wiring cost nothing to make test-safe.
"""

from __future__ import annotations

import pytest

from scraperx import fetch as fetch_mod
from scraperx.fetch import _CASCADE_DEFAULT, smart_fetch
from scraperx.method_telemetry import MIN_SAMPLES, record

HOST = "walled.example.com"
URL = f"https://{HOST}/page"


@pytest.fixture()
def legs(monkeypatch):
    """Record which legs run, in order. Only `winner` returns content."""
    calls: list[str] = []

    def mk(name, ok):
        def leg(url, timeout, **kw):
            calls.append(name)
            if ok:
                return f"<html>{name}</html>", 200
            raise RuntimeError(f"{name} refused (403)")
        return leg

    state = {"winner": "scrapling_stealth"}

    for mode, attr in [
        ("jina", "_fetch_jina"),
        ("urllib", "_fetch_urllib"),
        ("playwright", "_fetch_playwright"),
        ("scrapling_stealth", "_fetch_scrapling_stealth"),
    ]:
        monkeypatch.setattr(
            fetch_mod, attr,
            (lambda m: lambda url, timeout, **kw: mk(m, m == state["winner"])(url, timeout))(mode),
        )
    return calls, state


def _seed(method: str, *, success: bool, host: str = HOST, n: int = MIN_SAMPLES):
    """Write enough events for the scorer to leave NEUTRAL_PRIOR behind."""
    for _ in range(n):
        record("smart_fetch", method, host, success, latency_ms=10.0)


def _run(tmp_path, **kw):
    # allow_private=True skips the SSRF guard: these hosts do not resolve, and
    # the guard fails CLOSED on unresolvable names (correctly).
    return smart_fetch(URL, db_path=str(tmp_path / "t.db"), no_cache=True,
                       allow_private=True, **kw)


def test_empty_ledger_keeps_the_default_order(legs, tmp_path):
    """No history => behave exactly as before. The safety property."""
    calls, _ = legs
    _run(tmp_path)
    assert calls == list(_CASCADE_DEFAULT)


def test_a_proven_leg_is_promoted_to_first(legs, tmp_path):
    """THE test. After the ledger learns, the winner goes first."""
    calls, _ = legs
    _seed("jina", success=False)
    _seed("urllib", success=False)
    _seed("playwright", success=False)
    _seed("scrapling_stealth", success=True)

    _run(tmp_path)
    assert calls[0] == "scrapling_stealth", (
        f"ledger says stealth is the only thing that works on {HOST}, "
        f"yet the cascade still opened with {calls[0]}"
    )
    assert len(calls) == 1, "the winning leg should end the cascade immediately"


def test_learning_is_scoped_per_host(legs, tmp_path):
    """One host's lesson must not be applied to an unrelated host.

    A global average would hide that jina is perfect for site A and useless for
    site B — and would then be wrong for both.
    """
    calls, _ = legs
    _seed("jina", success=False)
    _seed("scrapling_stealth", success=True)

    smart_fetch("https://other.example.com/x", db_path=str(tmp_path / "t.db"),
                no_cache=True, allow_private=True)
    assert calls[0] == "jina", (
        "a different host has no history and must start from the default order"
    )


def test_explicit_prefer_is_an_instruction_not_a_suggestion(legs, tmp_path):
    """Telemetry may reorder the FALLBACKS; it must never demote a named leg."""
    calls, state = legs
    state["winner"] = "playwright"
    _seed("jina", success=False)
    _seed("scrapling_stealth", success=True)

    _run(tmp_path, prefer="jina")
    assert calls[0] == "jina", "the caller named jina — telemetry does not overrule that"
    assert calls[1] == "scrapling_stealth", "but the ledger DOES order what comes after"


def test_adaptive_false_restores_the_frozen_order(legs, tmp_path):
    calls, _ = legs
    _seed("jina", success=False)
    _seed("scrapling_stealth", success=True)

    _run(tmp_path, adaptive=False)
    assert calls == list(_CASCADE_DEFAULT)


def test_below_min_samples_does_not_move_anything(legs, tmp_path):
    """One lucky win is noise, not evidence."""
    calls, _ = legs
    _seed("scrapling_stealth", success=True, n=MIN_SAMPLES - 1)
    _run(tmp_path)
    assert calls[0] == "jina"


def test_success_and_failure_are_both_recorded(legs, tmp_path, monkeypatch):
    """A ledger nobody writes to can never teach anything."""
    from scraperx import method_telemetry as mt

    written: list[tuple] = []
    real = mt.record
    monkeypatch.setattr(
        fetch_mod, "_telemetry_record",
        lambda *a, **kw: (written.append(a), real(*a, **kw))[1],
    )
    _run(tmp_path)
    methods = [(a[1], a[3]) for a in written]
    assert ("jina", False) in methods, "the failing legs must be recorded"
    assert ("scrapling_stealth", True) in methods, "and so must the winner"
    assert all(a[2] == HOST for a in written), "every event must carry the host"


def test_empty_body_counts_as_failure(legs, tmp_path, monkeypatch):
    """A leg that answers with nothing is not a success — demote it next time."""
    from scraperx import method_telemetry as mt

    monkeypatch.setattr(fetch_mod, "_fetch_jina", lambda url, timeout, **kw: ("   ", 200))
    written: list[tuple] = []
    real = mt.record
    monkeypatch.setattr(
        fetch_mod, "_telemetry_record",
        lambda *a, **kw: (written.append(a), real(*a, **kw))[1],
    )
    _run(tmp_path)
    assert ("jina", False) in [(a[1], a[3]) for a in written]


def test_telemetry_failure_never_breaks_the_fetch(legs, tmp_path, monkeypatch):
    """Fail-open: instrumentation must never be able to take the scraper down.

    Both the read side (ranking) and the write side (recording) are replaced with
    functions that raise. The fetch must still succeed.
    """
    calls, _ = legs

    def boom(*a, **kw):
        raise OSError("ledger disk full")

    monkeypatch.setattr(fetch_mod, "_telemetry_record", boom)
    monkeypatch.setattr(fetch_mod, "_preferred_order", boom)

    r = _run(tmp_path)
    assert r.ok, "a broken ledger must not break fetching"
    assert calls, "the cascade must still have run"


def test_strict_still_runs_exactly_one_leg(legs, tmp_path):
    calls, _ = legs
    _run(tmp_path, prefer="urllib", strict=True)
    assert calls == ["urllib"]
