"""Tests for scraperx.tv_symbol_resolver — probe cascade + cache.

Network-free: the tvDatafeed probe is injected via the _probe_fn parameter.
Cache uses tmp_path so the user's social.db is never touched.
"""

from __future__ import annotations

import time

import pytest

from scraperx import tv_symbol_resolver as tvr
from scraperx.tv_symbol_resolver import (
    EXCHANGE_PRIORITY,
    SymbolResolution,
    auto_detect_asset_class,
    main_tv_resolve,
    resolve_symbol,
)


# ---------------------------------------------------------------------------
# Auto-detect heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker, expected", [
    ("VIX", "vol"),
    ("VIX9D", "vol"),
    ("VVIX", "vol"),
    ("SKEW", "vol"),
    ("GVZ", "vol"),
    ("EURUSD", "fx"),
    ("USDJPY", "fx"),
    ("BTCUSDT", "crypto"),
    ("ETHUSDC", "crypto"),
    ("ZN", "futures"),
    ("CL", "futures"),
    ("GC", "futures"),
    ("ZQ", "futures"),
    ("SPX", "index"),
    ("NDX", "index"),
    ("TOTAL2", "index"),
    ("AAPL", "auto"),  # equity isn't auto-detected — falls to "auto"
    ("RANDOM", "auto"),
])
def test_auto_detect_asset_class(ticker, expected):
    assert auto_detect_asset_class(ticker) == expected


def test_auto_detect_blank():
    assert auto_detect_asset_class("") == "auto"
    assert auto_detect_asset_class("   ") == "auto"


# ---------------------------------------------------------------------------
# Resolution result invariants
# ---------------------------------------------------------------------------


def test_symbol_resolution_ok_only_when_resolved_with_exchange():
    r = SymbolResolution(ticker="X", symbol="X", exchange="", status="not_found", tried=())
    assert not r.ok
    assert r.tv_symbol == "X"

    r2 = SymbolResolution(ticker="X", symbol="X", exchange="CBOE", status="resolved", tried=("CBOE",))
    assert r2.ok
    assert r2.tv_symbol == "CBOE:X"


# ---------------------------------------------------------------------------
# Probe injection helpers (no network)
# ---------------------------------------------------------------------------


def _make_probe(behavior):
    """Behavior is a dict {exchange: ('resolved'|'empty_no_data'|'not_found', error)}.

    Default for unmapped exchanges is ('not_found', 'unknown').
    """

    def _probe(ticker, exchange):
        return behavior.get(exchange.upper(), ("not_found", "unknown"))

    return _probe


# ---------------------------------------------------------------------------
# Cascade behavior
# ---------------------------------------------------------------------------


def test_first_candidate_wins(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"CBOE": ("resolved", "")})
    res = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe)
    assert res.ok
    assert res.exchange == "CBOE"
    assert res.status == "resolved"
    assert res.tried == ("CBOE",)


def test_falls_through_until_resolved(tmp_path):
    db = str(tmp_path / "tv.db")
    # CBOE empty, TVC resolves
    probe = _make_probe({
        "CBOE": ("empty_no_data", "no bars"),
        "TVC": ("resolved", ""),
    })
    res = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe)
    assert res.ok
    assert res.exchange == "TVC"
    assert res.status == "resolved"
    # The full path is recorded
    assert res.tried[0] == "CBOE"
    assert res.tried[1] == "TVC"


def test_all_fail(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({})  # everything is not_found
    res = resolve_symbol("BOGUS", asset_class="vol", db_path=db, _probe_fn=probe)
    assert not res.ok
    assert res.exchange == ""
    assert res.status == "not_found"
    # The vol asset class was tried in order
    assert res.tried == EXCHANGE_PRIORITY["vol"]


def test_strict_mode_no_fallthrough(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({
        "CBOE": ("empty_no_data", "no bars"),
        "TVC": ("resolved", ""),  # would have worked but strict=True blocks fallback
    })
    res = resolve_symbol(
        "VIX9D", asset_class="vol", strict=True, db_path=db, _probe_fn=probe,
    )
    assert not res.ok
    assert len(res.tried) == 1
    assert res.tried[0] == "CBOE"


def test_custom_candidates_override_asset_class(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"INDEX": ("resolved", "")})
    res = resolve_symbol(
        "X", asset_class="vol", candidates=["INDEX"], db_path=db, _probe_fn=probe,
    )
    assert res.ok
    assert res.exchange == "INDEX"
    assert res.tried == ("INDEX",)


def test_custom_candidates_dedup_uppercase(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"CBOE": ("resolved", "")})
    res = resolve_symbol(
        "VIX9D",
        candidates=["cboe", "CBOE", "  cboe  "],
        db_path=db, _probe_fn=probe,
    )
    assert res.ok
    assert res.tried == ("CBOE",)


def test_empty_ticker():
    res = resolve_symbol("")
    assert not res.ok
    assert res.status == "not_found"
    assert res.error_msg == "empty ticker"


def test_auto_class_falls_back_to_sweep(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"NASDAQ": ("resolved", "")})
    # AAPL doesn't auto-detect; sweeps through INDEX, TVC, NASDAQ, ...
    res = resolve_symbol("AAPL", db_path=db, _probe_fn=probe)
    assert res.ok
    assert res.exchange == "NASDAQ"


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def test_cache_hit_skips_probe(tmp_path):
    db = str(tmp_path / "tv.db")
    probe_a = _make_probe({"CBOE": ("resolved", "")})
    res1 = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe_a)
    assert res1.status == "resolved"
    assert not res1.was_cached

    # Second call: any probe call would explode
    def boom(_t, _e):
        raise AssertionError("probe should not be called on cache hit")

    res2 = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=boom)
    assert res2.ok
    assert res2.status == "cache_hit"
    assert res2.was_cached
    assert res2.exchange == "CBOE"


def test_negative_cache_skips_known_dead_exchange(tmp_path):
    db = str(tmp_path / "tv.db")
    # First run: CBOE empty, TVC resolves
    probe1 = _make_probe({
        "CBOE": ("empty_no_data", "no bars"),
        "TVC": ("resolved", ""),
    })
    resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe1)

    # Second run: CBOE probe must NOT fire (negative cached)
    probe_calls = []

    def tracked(t, e):
        probe_calls.append(e.upper())
        if e.upper() == "TVC":
            return "resolved", ""
        return "not_found", "tracker"

    res = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=tracked)
    assert res.ok
    assert res.exchange == "TVC"
    # CBOE must have been skipped
    assert "CBOE" not in probe_calls


def test_no_cache_disables_lookup_and_write(tmp_path):
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"CBOE": ("resolved", "")})
    resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe)

    # Now with no_cache=True, even though "CBOE resolved" is cached, probe runs again
    calls = []

    def tracked(t, e):
        calls.append(e.upper())
        return "resolved", ""

    res = resolve_symbol(
        "VIX9D", asset_class="vol", no_cache=True, db_path=db, _probe_fn=tracked,
    )
    assert res.ok
    assert calls == ["CBOE"]  # probe DID run despite cache
    assert not res.was_cached


def test_cache_ttl_expiry(tmp_path, monkeypatch):
    """When TTL expires, probe is rerun."""
    db = str(tmp_path / "tv.db")
    probe = _make_probe({"CBOE": ("resolved", "")})
    resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=probe)

    # Pretend 8 days have passed (TTL_RESOLVED = 7d)
    real_time = time.time
    monkeypatch.setattr(
        "scraperx.tv_symbol_resolver.time.time",
        lambda: real_time() + 8 * 24 * 3600,
    )

    calls = []

    def tracked(t, e):
        calls.append(e.upper())
        return "resolved", ""

    res = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=tracked)
    assert res.ok
    assert calls == ["CBOE"]  # probe ran again despite previous cache


# ---------------------------------------------------------------------------
# Optional dep error path
# ---------------------------------------------------------------------------


def test_tvdatafeed_not_installed_returns_not_found(tmp_path, monkeypatch):
    """If tvDatafeed is missing, _probe_one raises TvDatafeedNotInstalled,
    which resolve_symbol catches and surfaces as a clear error_msg.
    """
    from scraperx.tv_symbol_resolver import TvDatafeedNotInstalled

    def raising_probe(t, e):
        raise TvDatafeedNotInstalled("simulated missing dep")

    db = str(tmp_path / "tv.db")
    res = resolve_symbol("VIX9D", asset_class="vol", db_path=db, _probe_fn=raising_probe)
    assert not res.ok
    assert "simulated missing dep" in res.error_msg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_human_format_resolved(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "tv.db")
    monkeypatch.setattr(tvr, "DEFAULT_DB_PATH", db)

    def probe(t, e):
        return ("resolved", "") if e.upper() == "CBOE" else ("not_found", "x")

    monkeypatch.setattr(tvr, "_probe_one", probe)
    rc = main_tv_resolve(["tv-resolve", "VIX9D", "--asset-class", "vol"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CBOE:VIX9D" in out


def test_cli_json_output(tmp_path, monkeypatch, capsys):
    import json

    db = str(tmp_path / "tv.db")
    monkeypatch.setattr(tvr, "DEFAULT_DB_PATH", db)

    def probe(t, e):
        return ("resolved", "") if e.upper() == "TVC" else ("not_found", "x")

    monkeypatch.setattr(tvr, "_probe_one", probe)
    rc = main_tv_resolve(["tv-resolve", "SKEW", "--asset-class", "vol", "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0
    # Eventually resolves on TVC after CBOE returns not_found
    assert parsed["exchange"] == "TVC"
    assert parsed["ticker"] == "SKEW"


def test_cli_unresolved_returns_nonzero(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "tv.db")
    monkeypatch.setattr(tvr, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(tvr, "_probe_one", lambda t, e: ("not_found", "x"))

    rc = main_tv_resolve(["tv-resolve", "BOGUS", "--asset-class", "vol"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "unresolved" in out
