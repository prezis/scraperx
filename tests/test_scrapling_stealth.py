"""Tests for scraperx.scrapling_stealth — Cloudflare-bypass cascade leg.

Network-free + Scrapling-free: we monkeypatch the lazy ``StealthyFetcher``
import-path so the test runs even on machines that don't have ``scrapling``
installed (which is the whole point of the ``[stealth]`` opt-in extra).

The leg's contract — ``(url, timeout) -> (content, http_status)`` — is what
``scraperx.fetch.smart_fetch`` depends on. Anything that breaks that signature
breaks the cascade silently in production, so we lock it down here.
"""

from __future__ import annotations

import sys
import types

import pytest

from scraperx import fetch as fetch_mod
from scraperx import scrapling_stealth as ss_mod
from scraperx.fetch import FetchResult, smart_fetch
from scraperx.scrapling_stealth import (
    DEFAULT_STEALTH_TIMEOUT,
    ScraplingNotAvailable,
    fetch_stealth,
)


# ---------------------------------------------------------------------------
# Module-level helpers — fake StealthyFetcher we can swap in
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Mimics scrapling.engines.toolbelt.custom.Response for our needs:
    just .body (bytes) and .status (int)."""

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status


class _FakeStealthyFetcher:
    """Captures fetch() kwargs so the test can assert on them.

    Instance-level state (not class-level) so two tests running in the same
    process — including with pytest-xdist — never see each other's kwargs.
    The fixture builds one fresh instance per test and assigns the bound
    ``fetch`` method as the patched attribute.
    """

    def __init__(self):
        self.last_kwargs: dict = {}
        self.last_url: str = ""
        self.next_response: _FakeResponse | None = None
        self.next_exception: Exception | None = None

    def fetch(self, url, **kwargs):
        self.last_url = url
        self.last_kwargs = kwargs
        if self.next_exception is not None:
            raise self.next_exception
        return self.next_response or _FakeResponse(b"<html>fake</html>")


@pytest.fixture
def fake_stealthy(monkeypatch):
    """Replace the lazy-imported StealthyFetcher with our fake instance.

    We patch the module's accessor (``_get_stealthy_fetcher``) rather than
    poking at sys.modules['scrapling.fetchers'] — keeps the test isolated
    from real Scrapling install state. ``_get_stealthy_fetcher`` returns a
    class-like object and the production code calls ``.fetch(url, **kw)`` on
    it — instance with a ``fetch`` method satisfies that duck-type.
    """
    fake = _FakeStealthyFetcher()
    monkeypatch.setattr(ss_mod, "_get_stealthy_fetcher", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# fetch_stealth — direct API contract
# ---------------------------------------------------------------------------


def test_fetch_stealth_returns_str_body_and_status(fake_stealthy):
    fake_stealthy.next_response = _FakeResponse(b"<!DOCTYPE html><body>hi</body>", status=200)
    content, status = fetch_stealth("https://example.com")
    assert isinstance(content, str)
    assert "<body>hi</body>" in content
    assert status == 200


def test_fetch_stealth_passes_solve_cloudflare_default_true(fake_stealthy):
    fetch_stealth("https://intel.arkm.com/explorer/entity/fomo")
    assert fake_stealthy.last_kwargs["solve_cloudflare"] is True
    # Scrapling expects timeout in MILLISECONDS — caller passed seconds.
    assert fake_stealthy.last_kwargs["timeout"] == DEFAULT_STEALTH_TIMEOUT * 1000
    assert fake_stealthy.last_kwargs["headless"] is True
    assert fake_stealthy.last_kwargs["network_idle"] is True


def test_fetch_stealth_seconds_to_milliseconds_conversion(fake_stealthy):
    fetch_stealth("https://example.com", timeout=45)
    assert fake_stealthy.last_kwargs["timeout"] == 45_000


def test_fetch_stealth_enforces_timeout_floor(fake_stealthy):
    # Even a 1-second budget gets bumped to 5_000 ms minimum (Playwright's
    # browser-launch overhead alone is ~2-3s; sub-5s is meaningless).
    fetch_stealth("https://example.com", timeout=1)
    assert fake_stealthy.last_kwargs["timeout"] == 5_000


def test_fetch_stealth_extra_kwargs_override(fake_stealthy):
    fetch_stealth(
        "https://example.com",
        extra_kwargs={"proxy": "http://127.0.0.1:8080", "headless": False, "locale": "de-DE"},
    )
    assert fake_stealthy.last_kwargs["proxy"] == "http://127.0.0.1:8080"
    assert fake_stealthy.last_kwargs["headless"] is False  # caller wins
    assert fake_stealthy.last_kwargs["locale"] == "de-DE"


def test_fetch_stealth_decodes_utf8_by_default(fake_stealthy):
    # Multi-byte content: u201c left-double-quote, u00e9 e-acute
    fake_stealthy.next_response = _FakeResponse("café \u201cfoo\u201d".encode("utf-8"))
    content, _ = fetch_stealth("https://example.com")
    assert "café" in content
    assert "\u201cfoo\u201d" in content


def test_fetch_stealth_falls_back_to_latin1_on_bad_utf8(fake_stealthy):
    # Pure latin-1 bytes that are NOT valid UTF-8 (\xe9 alone)
    fake_stealthy.next_response = _FakeResponse(b"caf\xe9", status=200)
    content, _ = fetch_stealth("https://example.com")
    # latin-1 round-trip leaves the original byte legible
    assert "caf" in content
    assert len(content) == 4  # 'c','a','f', + decoded \xe9


def test_fetch_stealth_propagates_browser_failure(fake_stealthy):
    fake_stealthy.next_exception = RuntimeError("turnstile timeout after 90s")
    with pytest.raises(RuntimeError, match="turnstile timeout"):
        fetch_stealth("https://example.com")


def test_fetch_stealth_raises_when_scrapling_missing(monkeypatch):
    """If scrapling.fetchers can't be imported, raise ScraplingNotAvailable
    with an actionable install hint — not a generic ImportError."""
    # Force the lazy-import to fail by hiding the module
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", None)
    # And also block top-level scrapling so the import inside _get_stealthy_fetcher() fails
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _block(name, *args, **kwargs):
        if name.startswith("scrapling"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block)
    with pytest.raises(ScraplingNotAvailable, match=r"pip install 'scraperx\[stealth\]'"):
        fetch_stealth("https://example.com")


# ---------------------------------------------------------------------------
# Cascade integration — smart_fetch routes to the new leg
# ---------------------------------------------------------------------------


def test_cascade_includes_scrapling_stealth_after_playwright():
    """Don't pin to absolute last position — future cheaper legs may be added.
    The load-bearing invariant is "scrapling_stealth runs AFTER plain playwright"
    because it's heavier per-call; that's what we lock in."""
    from scraperx.fetch import _CASCADE_DEFAULT
    assert "scrapling_stealth" in _CASCADE_DEFAULT
    assert _CASCADE_DEFAULT.index("playwright") < _CASCADE_DEFAULT.index("scrapling_stealth")


def test_cascade_calls_stealth_when_other_legs_fail(tmp_path, monkeypatch):
    """Full cascade behavior: jina + urllib + playwright fail, stealth wins."""
    db = str(tmp_path / "test.db")

    def _boom(_url, _timeout):
        raise RuntimeError("walled")

    def _stealth_ok(_url, _timeout):
        return "<html>stealth-content</html>", 200

    monkeypatch.setattr(fetch_mod, "_fetch_jina", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _stealth_ok)

    r = smart_fetch("https://example.com", db_path=db)
    assert r.ok
    assert r.mode_used == "scrapling_stealth"
    assert "stealth-content" in r.content
    # All three earlier legs should be recorded as failures
    failed_legs = {mode for mode, _ in r.errors}
    assert failed_legs == {"jina", "urllib", "playwright"}


def test_cascade_records_scrapling_not_available(tmp_path, monkeypatch):
    """If scrapling isn't installed, the leg surfaces ScraplingNotAvailable
    and the cascade records it — does NOT crash the whole call."""
    db = str(tmp_path / "test.db")

    def _boom(_url, _timeout):
        raise RuntimeError("walled")

    def _no_scrapling(_url, _timeout):
        raise ScraplingNotAvailable("scrapling not installed (test-injected)")

    monkeypatch.setattr(fetch_mod, "_fetch_jina", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _boom)
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _no_scrapling)

    r = smart_fetch("https://example.com", db_path=db)
    assert not r.ok
    assert r.content == ""
    # Should have all 4 leg failures recorded
    assert len(r.errors) == 4
    assert any(mode == "scrapling_stealth" and "scrapling not installed" in err for mode, err in r.errors)


def test_cascade_strict_scrapling_stealth(tmp_path, monkeypatch):
    """strict=True with prefer='scrapling_stealth' runs ONLY that leg."""
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(fetch_mod, "_fetch_jina", lambda *a: pytest.fail("jina should not run"))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", lambda *a: pytest.fail("urllib should not run"))
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", lambda *a: pytest.fail("pw should not run"))
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", lambda *a: ("<html>stealth-only</html>", 200))

    r = smart_fetch("https://example.com", prefer="scrapling_stealth", strict=True, db_path=db)
    assert r.ok
    assert r.mode_used == "scrapling_stealth"
