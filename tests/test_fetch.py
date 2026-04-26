"""Tests for scraperx.fetch — smart_fetch + cache layer.

Network-free: all cascade legs are monkeypatched. The cache uses a tmp_path DB
so the user's ~/.scraperx/social.db is never touched.
"""

from __future__ import annotations

import time

import pytest

from scraperx import fetch as fetch_mod
from scraperx.fetch import FetchResult, _cache_get, _cache_put, _url_hash, smart_fetch


# ---------------------------------------------------------------------------
# FetchResult invariants
# ---------------------------------------------------------------------------


def test_fetch_result_defaults():
    r = FetchResult(url="https://example.com")
    assert r.content == ""
    assert r.mode_used == ""
    assert r.errors == []
    assert r.was_cached is False
    assert r.ok is False


def test_fetch_result_ok_requires_content_and_mode():
    r = FetchResult(url="https://example.com", content="hi")
    assert r.ok is False  # missing mode_used
    r.mode_used = "jina"
    assert r.ok is True


def test_url_hash_stable_and_distinct():
    a = _url_hash("https://example.com/foo")
    b = _url_hash("https://example.com/foo")
    c = _url_hash("https://example.com/bar")
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


def test_cache_roundtrip(tmp_path):
    db = str(tmp_path / "test.db")
    r = FetchResult(
        url="https://example.com",
        content="hello world",
        mode_used="jina",
        elapsed_ms=42,
        http_status=200,
    )
    assert _cache_get("https://example.com", db_path=db) is None

    _cache_put(r, ttl=3600, db_path=db)
    got = _cache_get("https://example.com", db_path=db)
    assert got is not None
    assert got.content == "hello world"
    assert got.mode_used == "cache"  # tagged explicitly on read
    assert got.was_cached is True
    assert got.http_status == 200


def test_cache_does_not_persist_failed(tmp_path):
    db = str(tmp_path / "test.db")
    failed = FetchResult(url="https://example.com", errors=[("jina", "boom")])
    _cache_put(failed, db_path=db)  # no-op
    assert _cache_get("https://example.com", db_path=db) is None


def test_cache_respects_ttl(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    r = FetchResult(
        url="https://example.com", content="x", mode_used="urllib", http_status=200
    )
    _cache_put(r, ttl=1, db_path=db)
    # Pretend two seconds have passed
    real_time = time.time
    monkeypatch.setattr("scraperx.fetch.time.time", lambda: real_time() + 2)
    assert _cache_get("https://example.com", db_path=db) is None


# ---------------------------------------------------------------------------
# Cascade behavior — monkeypatch the legs
# ---------------------------------------------------------------------------


def _ok_leg(content: str, status: int | None = 200):
    def _leg(_url, _timeout):
        return content, status
    return _leg


def _fail_leg(msg: str):
    def _leg(_url, _timeout):
        raise RuntimeError(msg)
    return _leg


def test_smart_fetch_uses_preferred_first(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _ok_leg("from-jina"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _fail_leg("should-not-be-called"))
    r = smart_fetch("https://example.com", prefer="jina", db_path=db)
    assert r.ok
    assert r.content == "from-jina"
    assert r.mode_used == "jina"
    assert r.errors == []


def test_smart_fetch_falls_through_to_urllib(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _fail_leg("jina down"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("from-urllib"))
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _fail_leg("not-tried"))
    r = smart_fetch("https://example.com", prefer="jina", db_path=db)
    assert r.ok
    assert r.mode_used == "urllib"
    assert r.content == "from-urllib"
    # First leg's failure should be recorded
    assert any(mode == "jina" and "jina down" in err for mode, err in r.errors)


def test_smart_fetch_strict_no_fallthrough(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _fail_leg("nope"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("would-have-worked"))
    r = smart_fetch("https://example.com", prefer="jina", strict=True, db_path=db)
    assert not r.ok
    assert r.mode_used == ""
    assert len(r.errors) == 1
    assert r.errors[0][0] == "jina"


def test_smart_fetch_all_legs_fail(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _fail_leg("a"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _fail_leg("b"))
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _fail_leg("c"))
    r = smart_fetch("https://example.com", db_path=db)
    assert not r.ok
    assert r.content == ""
    assert len(r.errors) == 3


def test_smart_fetch_uses_cache(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    # First call populates cache
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _ok_leg("hello"))
    r1 = smart_fetch("https://example.com", db_path=db)
    assert r1.ok
    assert r1.was_cached is False

    # Second call: any leg call would explode if it were exercised
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _fail_leg("should-not-call"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _fail_leg("should-not-call"))
    r2 = smart_fetch("https://example.com", db_path=db)
    assert r2.ok
    assert r2.was_cached is True
    assert r2.mode_used == "cache"


def test_smart_fetch_no_cache_skips_lookup(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    # Pre-populate
    _cache_put(
        FetchResult(url="https://example.com", content="cached", mode_used="jina"),
        db_path=db,
    )
    # With no_cache=True, leg must run
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _ok_leg("fresh"))
    r = smart_fetch("https://example.com", no_cache=True, db_path=db)
    assert r.content == "fresh"
    assert r.mode_used == "jina"
    assert r.was_cached is False


def test_smart_fetch_rejects_unknown_prefer(tmp_path):
    with pytest.raises(ValueError, match="prefer must be one of"):
        smart_fetch("https://example.com", prefer="bogus", db_path=str(tmp_path / "x.db"))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def test_ssrf_blocks_localhost(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    # Even if a leg would succeed, SSRF guard fires before any leg is called
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("oops"))
    r = smart_fetch("http://localhost:8080/admin", db_path=db)
    assert not r.ok
    assert any(mode == "ssrf_guard" for mode, _ in r.errors)


def test_ssrf_blocks_rfc1918(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("oops"))
    r = smart_fetch("http://192.168.1.1/", db_path=db)
    assert not r.ok
    assert any("private/loopback" in err for mode, err in r.errors)


def test_ssrf_blocks_loopback_v6(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("oops"))
    r = smart_fetch("http://[::1]/", db_path=db)
    assert not r.ok


def test_ssrf_blocks_unsupported_scheme(tmp_path):
    db = str(tmp_path / "test.db")
    r = smart_fetch("file:///etc/passwd", db_path=db)
    assert not r.ok
    assert any("scheme" in err for mode, err in r.errors)


def test_ssrf_allow_private_lets_localhost_through(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _ok_leg("internal-page"))
    r = smart_fetch(
        "http://localhost:8080/", prefer="urllib", strict=True,
        allow_private=True, db_path=db,
    )
    assert r.ok
    assert r.content == "internal-page"


# ---------------------------------------------------------------------------
# Connection caching — verify singleton behavior
# ---------------------------------------------------------------------------


def test_db_connection_is_cached_per_path(tmp_path):
    db = str(tmp_path / "cached.db")
    c1 = fetch_mod._open_db(db)
    c2 = fetch_mod._open_db(db)
    assert c1 is c2  # same Connection object reused
