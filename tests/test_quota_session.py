"""Tests for scraperx.quota_session — auth-header hygiene + rate-limit acquire.

The optional deps ``requests-cache`` and ``pyrate-limiter`` are NOT required
for the test suite. We test the parts of the module that don't depend on
those imports (config + ignored-parameter normalisation), then probe the
rate-limit acquire path with fakes that mimic both old and new pyrate-limiter
APIs.
"""

from __future__ import annotations

import pytest

from scraperx.quota_session import (
    DEFAULT_AUTH_HEADERS,
    QuotaSession,
    QuotaSessionConfig,
)


# ---------------------------------------------------------------------------
# Config / defaults
# ---------------------------------------------------------------------------


def test_default_auth_headers_includes_authorization():
    assert "Authorization" in DEFAULT_AUTH_HEADERS
    assert "X-Finnhub-Token" in DEFAULT_AUTH_HEADERS


def test_quota_session_config_defaults():
    cfg = QuotaSessionConfig()
    assert cfg.bucket_name == "default"
    assert cfg.cache_expire_s == 86_400
    assert isinstance(cfg.auth_headers, tuple)


def test_quota_session_config_custom_path():
    cfg = QuotaSessionConfig(cache_path="/tmp/foo.sqlite", bucket_name="finnhub-free")
    assert cfg.cache_path == "/tmp/foo.sqlite"
    assert cfg.bucket_name == "finnhub-free"


# ---------------------------------------------------------------------------
# Auth-header lowercasing — works WITHOUT requests-cache installed
# ---------------------------------------------------------------------------


class _NullSession:
    """Stand-in for requests-cache CachedSession when the real dep is missing."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_auth_headers_lowercased_for_cache(monkeypatch):
    """Verify QuotaSession lowercases configured headers before forwarding."""
    # Bypass requests-cache import — replace _build_session with a stub
    monkeypatch.setattr(QuotaSession, "_build_session", lambda self: _NullSession(
        ignored_parameters=list(self._ignored_params)
    ))
    monkeypatch.setattr(QuotaSession, "_build_limiter", lambda self, rates: None)

    sess = QuotaSession(
        cache_path="/tmp/qs-test.sqlite",
        auth_headers=("Authorization", "X-CUSTOM-Token"),
    )
    assert sess.ignored_parameters == ("authorization", "x-custom-token")
    assert "authorization" in sess._session.kwargs["ignored_parameters"]


# ---------------------------------------------------------------------------
# _acquire — both API paths (modern ratelimit and legacy try_acquire)
# ---------------------------------------------------------------------------


class _ModernLimiter:
    """Mimics pyrate-limiter v3+: has ratelimit() that blocks if delay=True."""
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []
    def ratelimit(self, name: str, delay: bool = False):
        self.calls.append((name, delay))


class _LegacyLimiter:
    """Mimics older pyrate-limiter: only try_acquire() exists, returns bool."""
    def __init__(self, accept_after: int = 0):
        self.calls = 0
        self._accept_after = accept_after
    def try_acquire(self, _name: str):
        self.calls += 1
        return self.calls > self._accept_after


def _make_session_with_limiter(monkeypatch, limiter):
    monkeypatch.setattr(QuotaSession, "_build_session", lambda self: _NullSession())
    monkeypatch.setattr(QuotaSession, "_build_limiter", lambda self, rates: limiter)
    return QuotaSession(cache_path="/tmp/qs-test.sqlite", bucket_name="bucket-x")


def test_acquire_uses_ratelimit_when_available(monkeypatch):
    lim = _ModernLimiter()
    sess = _make_session_with_limiter(monkeypatch, lim)
    sess._acquire()
    assert lim.calls == [("bucket-x", True)]


def test_acquire_falls_back_to_try_acquire_loop(monkeypatch):
    """Legacy limiter (no ratelimit method) must succeed via try_acquire poll."""
    lim = _LegacyLimiter(accept_after=0)  # accepts on first call
    sess = _make_session_with_limiter(monkeypatch, lim)
    sess._acquire()
    assert lim.calls == 1


def test_acquire_blocks_until_legacy_limiter_yields(monkeypatch):
    """Legacy limiter that rejects 2× then accepts — _acquire must keep polling.

    We monkeypatch ``time.sleep`` inside the module so the test stays fast.
    """
    import scraperx.quota_session as qs_mod

    # Sleep should be called at least twice (after each rejection).
    sleeps: list[float] = []
    monkeypatch.setattr(qs_mod, "time", type("T", (), {"sleep": lambda s: sleeps.append(s)}))

    lim = _LegacyLimiter(accept_after=2)
    sess = _make_session_with_limiter(monkeypatch, lim)
    sess._acquire()
    assert lim.calls == 3  # 2 rejections + 1 success
    assert len(sleeps) >= 2


def test_acquire_noop_without_limiter(monkeypatch):
    sess = _make_session_with_limiter(monkeypatch, None)
    # Should not raise, should not block
    sess._acquire()


def test_acquire_propagates_ratelimit_errors_clearly(monkeypatch):
    class _Boom:
        def ratelimit(self, *a, **kw):
            raise RuntimeError("network died")
    sess = _make_session_with_limiter(monkeypatch, _Boom())
    with pytest.raises(RuntimeError, match="ratelimit"):
        sess._acquire()
