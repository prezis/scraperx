"""Tests for scraperx.fingerprint_audit — network-free.

We monkeypatch ``_fetch_echo`` so no real request to tls.peet.ws is made; the tests
assert the coherence logic (§4 of the arms-race research: UA-family vs TLS, library-UA
leak, missing JA4).
"""
from __future__ import annotations

import pytest

from scraperx import fingerprint_audit as fa

_FIREFOX_ECHO = {
    "ip": "185.10.20.30",
    "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "tls": {"ja3_hash": "aa11bb22", "ja4": "t13d1715h2_5b57614c22b0_abc123", "ja3": "771,4865-4866"},
    "http2": {"akamai_fingerprint": "1:65536;4:131072;5:16384|12517377|3:0:0:201|m,a,s,p"},
}

_LIBRARY_ECHO = {
    "ip": "34.1.2.3",
    "user_agent": "python-requests/2.31.0",
    "tls": {"ja3_hash": "deadbeef", "ja4": "t13d..", "ja3": "771,.."},
    "http2": {},
}

_CHROME_ECHO = {
    "ip": "185.10.20.30",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "tls": {"ja3_hash": "cc33", "ja4": "t13d1516h2_xxx", "ja3": "771,.."},
    "http2": {"akamai_fingerprint": "1:65536|m,s,a,p"},
}


def _patch(monkeypatch, echo):
    monkeypatch.setattr(fa, "_fetch_echo", lambda *a, **k: echo)


def test_family_helpers():
    assert fa._family_from_impersonate("firefox144") == "firefox"
    assert fa._family_from_impersonate("chrome120") == "chrome"
    assert fa._family_from_impersonate("safari180") == "safari"
    assert fa._ua_family("Mozilla/5.0 Firefox/144.0") == "firefox"
    assert fa._ua_family("python-requests/2.31") == "library"
    assert fa._ua_family("") == "unknown"


def test_coherent_firefox(monkeypatch):
    _patch(monkeypatch, _FIREFOX_ECHO)
    a = fa.audit_fingerprint(impersonate="firefox144")
    assert a.ok and a.coherent
    assert a.ua_family == "firefox" and a.expected_family == "firefox"
    assert a.ja4 and a.http2_fingerprint and a.egress_ip == "185.10.20.30"
    assert a.warnings == []
    assert "COHERENT" in a.report()


def test_library_ua_leak_is_incoherent(monkeypatch):
    _patch(monkeypatch, _LIBRARY_ECHO)
    a = fa.audit_fingerprint(impersonate="firefox144")
    assert not a.coherent
    assert a.ua_family == "library"
    assert any("library" in w.lower() for w in a.warnings)


def test_family_mismatch_is_incoherent(monkeypatch):
    _patch(monkeypatch, _CHROME_ECHO)
    a = fa.audit_fingerprint(impersonate="firefox144")  # asked FF, server sees Chrome
    assert not a.coherent
    assert a.ua_family == "chrome" and a.expected_family == "firefox"
    assert any("!=" in w for w in a.warnings)


def test_missing_ja4_warns(monkeypatch):
    echo = dict(_FIREFOX_ECHO, tls={"ja3_hash": "x"})  # no ja4
    _patch(monkeypatch, echo)
    a = fa.audit_fingerprint(impersonate="firefox144")
    assert any("JA4" in w for w in a.warnings)


def test_curl_cffi_absent_raises(monkeypatch):
    # simulate curl_cffi not installed inside the real _fetch_echo
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("curl_cffi"):
            raise ImportError("no curl_cffi")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(fa.CurlCffiNotAvailable):
        fa._fetch_echo("https://x", "firefox144", None, 5)
