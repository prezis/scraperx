"""Fingerprint self-audit — does scraperx's stealth actually present a COHERENT
browser fingerprint across every layer, or does it leak?

Educational grounding: `~/ai/global-graph/domains/research/2026-07-16-automation-vs-
antibot-arms-race.md` §4. The lesson that motivates this module:

  - **JA3 is dead** (Chrome 110+ permutes its ClientHello, so JA3 is no longer stable).
    Modern WAFs (Cloudflare, Akamai, AWS) fingerprint with **JA4 / JA4H** (header order)
    and the **HTTP/2 SETTINGS + pseudo-header order** (~99.7% browser-family accuracy).
  - A fetch that spoofs the *User-Agent* to "Chrome" but whose *TLS handshake* says
    "Python" is flagged instantly — "no matter how residential the IP."
  - `curl_cffi impersonate=` forges the whole handshake, but there is **no self-check**
    that it actually took effect. This module is that check.

Two uses:
  1. `audit_fingerprint()` — before hitting a walled target, verify the stealth config
     presents a real-browser fingerprint (UA family == TLS family, JA4 present, no
     library UA leak). Run it in CI / a pre-flight.
  2. `diagnose_403(url, ...)` — when a target 403s, turn the guess into a diagnosis:
     fingerprint-leak vs IP-type vs "neither (→ behavior/account, the frontier)".

Network-only (hits a public TLS echo, default `tls.peet.ws`). curl_cffi is optional —
absent → a clear `CurlCffiNotAvailable`, mirroring `ScraplingNotAvailable`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_ECHO_URL = "https://tls.peet.ws/api/all"
DEFAULT_IP_ECHO = "https://api.ipify.org?format=json"
DEFAULT_TIMEOUT = 20.0

# User-Agent substrings that betray a non-browser HTTP client (an instant WAF flag).
_LIBRARY_UA_MARKERS = (
    "python-requests", "python-httpx", "httpx/", "aiohttp", "urllib",
    "curl/", "go-http-client", "okhttp", "libwww", "scrapy", "java/",
)


class CurlCffiNotAvailable(RuntimeError):
    """Raised when curl_cffi isn't installed (the fingerprint fetch needs it)."""


@dataclass
class FingerprintAudit:
    """What a server actually sees from a stealth fetch, + a coherence verdict."""

    ok: bool
    coherent: bool
    requested_impersonate: str
    ua_family: str = "unknown"          # firefox | chrome | safari | library | unknown
    expected_family: str = ""           # derived from `requested_impersonate`
    ja3_hash: str = ""
    ja4: str = ""
    http2_fingerprint: str = ""         # Akamai HTTP/2 fingerprint
    user_agent: str = ""
    egress_ip: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def report(self) -> str:
        """Human-readable one-screen report."""
        mark = "✅ COHERENT" if self.coherent else "❌ INCOHERENT (would be flagged)"
        lines = [
            f"fingerprint audit — {mark}",
            f"  requested impersonate : {self.requested_impersonate}  (expect family: {self.expected_family or '?'})",
            f"  server sees UA family : {self.ua_family}",
            f"  JA4 (modern TLS)      : {self.ja4 or '—'}",
            f"  JA3 hash (legacy)     : {self.ja3_hash or '—'}",
            f"  HTTP/2 (Akamai)       : {self.http2_fingerprint or '—'}",
            f"  egress IP             : {self.egress_ip or '—'}",
            f"  user-agent            : {self.user_agent[:80] or '—'}",
        ]
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def _family_from_impersonate(impersonate: str) -> str:
    s = (impersonate or "").lower()
    if s.startswith(("firefox", "ff")):
        return "firefox"
    if s.startswith("safari"):
        return "safari"
    if s.startswith(("chrome", "chromium", "edge")):
        return "chrome"
    return ""


def _ua_family(ua: str) -> str:
    u = (ua or "").lower()
    if not u:
        return "unknown"
    if any(m in u for m in _LIBRARY_UA_MARKERS):
        return "library"
    # order matters: Edge/Chrome both contain "chrome"; Safari excludes Chrome.
    if "firefox/" in u or "fxios" in u:
        return "firefox"
    if "chrome/" in u or "chromium" in u or "edg/" in u:
        return "chrome"
    if "safari/" in u and "chrome/" not in u:
        return "safari"
    return "unknown"


def _dig(d: dict, *paths, default=""):
    """First non-empty value across dotted paths, e.g. _dig(data, 'tls.ja4', 'ja4')."""
    for path in paths:
        cur = d
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur:
            return cur
    return default


def _fetch_echo(url: str, impersonate: str, proxy, timeout: float) -> dict:
    """Fetch the TLS-echo JSON THROUGH the exact stealth config under audit.

    Isolated so tests can monkeypatch it network-free.
    """
    try:
        import curl_cffi.requests as cr
    except ImportError as e:  # pragma: no cover - env dependent
        raise CurlCffiNotAvailable(
            "curl_cffi is required for fingerprint auditing: pip install curl_cffi"
        ) from e
    kw: dict = {"impersonate": impersonate, "timeout": timeout}
    if proxy:
        kw["proxies"] = proxy
    return cr.get(url, **kw).json()


def audit_fingerprint(
    impersonate: str = "firefox144",
    proxy: dict | None = None,
    echo_url: str = DEFAULT_ECHO_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> FingerprintAudit:
    """Verify a stealth config presents a coherent real-browser fingerprint.

    Args:
        impersonate: the curl_cffi impersonate profile to audit (what your scrapers use).
        proxy: same proxies dict your scrapers use (audit through the real egress).
        echo_url: a TLS-echo returning JA3/JA4/HTTP2/UA (default tls.peet.ws).
        timeout: request timeout (s).

    Returns a `FingerprintAudit`. `coherent=False` means a real WAF would very likely
    flag this fetch regardless of IP — fix the impersonate/transport before scraping.
    """
    expected = _family_from_impersonate(impersonate)
    data = _fetch_echo(echo_url, impersonate, proxy, timeout)

    ja4 = _dig(data, "tls.ja4", "ja4")
    ja3_hash = _dig(data, "tls.ja3_hash", "ja3_hash")
    http2 = _dig(data, "http2.akamai_fingerprint", "akamai_fingerprint", "http2.akamai")
    ua = _dig(data, "user_agent", "http1.headers.user-agent", "tls.user_agent")
    ip = _dig(data, "ip", "tls.ip")
    fam = _ua_family(ua)

    warnings: list[str] = []
    coherent = True

    if fam == "library":
        coherent = False
        warnings.append("User-Agent reveals a library (python/curl/go/…) — impersonation NOT applied")
    if expected and fam not in ("unknown", "library") and fam != expected:
        coherent = False
        warnings.append(f"UA family '{fam}' != requested impersonate family '{expected}'")
    if not ja4:
        warnings.append("echo returned no JA4 — cannot verify the MODERN TLS fingerprint (JA3 alone is dead since Chrome 110)")
    if not http2:
        warnings.append("no HTTP/2 fingerprint seen — HTTP/1.1 fallback is itself a bot tell on h2 sites")
    if fam == "unknown" and not ua:
        warnings.append("echo returned no User-Agent — check the echo endpoint/schema")

    return FingerprintAudit(
        ok=True,
        coherent=coherent,
        requested_impersonate=impersonate,
        ua_family=fam,
        expected_family=expected,
        ja3_hash=ja3_hash,
        ja4=ja4,
        http2_fingerprint=http2,
        user_agent=ua,
        egress_ip=ip,
        warnings=warnings,
        raw=data,
    )


@dataclass
class Diagnosis403:
    """Root-cause classification for a 403 from a stealth fetch."""

    status: int
    likely_cause: str            # "fingerprint" | "ip" | "behavior-or-account" | "not-403"
    fingerprint: FingerprintAudit
    detail: str = ""

    def report(self) -> str:
        return f"403 root-cause: {self.likely_cause}\n{self.detail}\n\n{self.fingerprint.report()}"


def diagnose_403(
    url: str,
    impersonate: str = "firefox144",
    proxy: dict | None = None,
    headers: dict | None = None,
    cookies: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    echo_url: str = DEFAULT_ECHO_URL,
) -> Diagnosis403:
    """Fetch `url`; if it 403s, classify WHY instead of guessing.

    - fingerprint  → the stealth config leaks (UA/TLS mismatch, library UA, no JA4).
    - ip           → fingerprint is coherent but the body/headers point at IP/geo block.
    - behavior-or-account → coherent fingerprint + non-IP 403 = the frontier layer
      (behavioral detection or an account/token ban — exactly the FOMO-style case).
    """
    try:
        import curl_cffi.requests as cr
    except ImportError as e:  # pragma: no cover
        raise CurlCffiNotAvailable("curl_cffi required: pip install curl_cffi") from e

    kw: dict = {"impersonate": impersonate, "timeout": timeout}
    if proxy:
        kw["proxies"] = proxy
    if headers:
        kw["headers"] = headers
    if cookies:
        kw["cookies"] = cookies
    r = cr.get(url, **kw)
    body = (r.text or "")[:2000].lower()

    fp = audit_fingerprint(impersonate=impersonate, proxy=proxy, echo_url=echo_url, timeout=timeout)

    if r.status_code != 403:
        return Diagnosis403(r.status_code, "not-403", fp, detail=f"HTTP {r.status_code} — not a 403.")
    if not fp.coherent:
        return Diagnosis403(403, "fingerprint", fp,
                            detail="Your fingerprint is incoherent (see warnings) — fix impersonate/transport first.")
    ip_markers = ("blocked in your country", "not available in your region", "vpn", "proxy detected",
                  "access from your location", "geo", "datacenter")
    if any(m in body for m in ip_markers):
        return Diagnosis403(403, "ip", fp,
                            detail="Fingerprint is coherent but the 403 body points at IP/geo — rotate to a residential/mobile IP.")
    return Diagnosis403(403, "behavior-or-account", fp,
                        detail="Fingerprint coherent + non-IP 403 → the frontier layer: behavioral detection or an "
                               "account/token ban (§4/§6). A new token/fingerprint won't help — this is the FOMO-style case.")


def _main() -> int:  # pragma: no cover - CLI
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="Audit scraperx's own TLS/HTTP2/UA fingerprint.")
    ap.add_argument("--impersonate", default="firefox144")
    ap.add_argument("--proxy", default="", help="e.g. socks5h://10.64.0.1:1080")
    ap.add_argument("--diagnose", default="", help="a URL that's 403ing — root-cause it")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    proxy = {"https": args.proxy, "http": args.proxy} if args.proxy else None
    if args.diagnose:
        d = diagnose_403(args.diagnose, impersonate=args.impersonate, proxy=proxy)
        print(_json.dumps(d.__dict__, default=lambda o: o.__dict__) if args.json else d.report())
    else:
        a = audit_fingerprint(impersonate=args.impersonate, proxy=proxy)
        print(_json.dumps(a.__dict__, default=str) if args.json else a.report())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
