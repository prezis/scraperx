"""smart_fetch — universal URL fetcher with Jina Reader → urllib → Playwright cascade.

Designed for the wojak-wojtek wiki research stack: any "fetch URL → maybe-Cloudflare-walled" call.
Each cascade leg trades speed for resilience.

Modes (ordered by speed/quality tradeoff):
    1. jina:       https://r.jina.ai/<url> — clean markdown extraction, Cloudflare-aware
                   Best for: research articles, docs, news. Returns rendered text content.
    2. urllib:     raw HTTP via stdlib — fast, plain HTML, no JS execution
                   Best for: static pages, JSON endpoints, RSS feeds.
    3. playwright: full headless Chromium render — slowest but bypasses bot-walls
                   Best for: JS-heavy SPAs, sites that 403 plain HTTP.

Cache: per-URL in ``~/.scraperx/social.db`` (``web_fetch_cache`` table). Default TTL 24h.

Usage:
    from scraperx.fetch import smart_fetch
    result = smart_fetch("https://example.com")
    print(result.content[:200], result.mode_used, result.elapsed_ms)

    # Skip cache (force refetch):
    result = smart_fetch(url, no_cache=True)

    # Force a specific mode (skip cascade):
    result = smart_fetch(url, prefer="urllib", strict=True)

Cache hits are cheap; cascade misses fall through silently (errors collected on result.errors).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Public — re-exported via scraperx/__init__.py
__all__ = ["FetchResult", "smart_fetch", "FetchMode"]

FetchMode = Literal["jina", "urllib", "playwright"]
_CASCADE_DEFAULT: tuple[FetchMode, ...] = ("jina", "urllib", "playwright")

DEFAULT_DB_PATH = os.path.expanduser("~/.scraperx/social.db")
DEFAULT_TIMEOUT = 30
DEFAULT_TTL = 86400  # 24h, matches WEB_FETCH_TTL in social_db.py
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 scraperx-fetch/1.0"
)
JINA_BASE = "https://r.jina.ai/"

# Schema mirror — defense in depth, matches social_db.py._SCHEMA.
# Every consumer guarantees its own table exists; idempotent CREATE IF NOT EXISTS.
_FETCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_fetch_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    content TEXT NOT NULL,
    mode_used TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 86400,
    http_status INTEGER,
    elapsed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_web_fetch_url ON web_fetch_cache(url);
CREATE INDEX IF NOT EXISTS idx_web_fetch_fetched_at ON web_fetch_cache(fetched_at);
"""


@dataclass
class FetchResult:
    """Result of a smart_fetch call.

    Attributes:
        url: The URL that was requested (verbatim).
        content: The fetched body. Empty string on total failure.
        mode_used: Which cascade leg succeeded ("jina"/"urllib"/"playwright"/"cache").
                   Empty string if all legs failed.
        elapsed_ms: Wall-clock time spent on the actual fetch (excluding cache lookup).
        was_cached: True if the result came from web_fetch_cache.
        http_status: HTTP status code of the successful leg, or None for jina/playwright.
        errors: List of (mode, error_message) tuples for legs that failed before success.
    """

    url: str
    content: str = ""
    mode_used: str = ""
    elapsed_ms: int = 0
    was_cached: bool = False
    http_status: int | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff at least one cascade leg returned non-empty content."""
        return bool(self.content) and bool(self.mode_used)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _url_hash(url: str) -> str:
    """Stable hash of a URL — sha256 hex digest. Caller doesn't need to normalize."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


# Per-path singleton connection cache. Opening/closing SQLite per fetch is an
# anti-pattern (running CREATE TABLE IF NOT EXISTS on every call, lock contention
# under threads). One Connection per db_path, guarded by a lock for write paths.
_DB_CONN_CACHE: dict[str, sqlite3.Connection] = {}
_DB_CONN_LOCK = threading.Lock()


def _open_db(db_path: str | None = None) -> sqlite3.Connection:
    """Return a process-wide cached Connection for db_path; first call sets up schema.

    Uses ``check_same_thread=False`` so a single Connection can serve threaded
    callers; the module-level lock serializes writes. This is fine because the
    write workload is light (one row per cache miss).
    """
    path = db_path or DEFAULT_DB_PATH
    cached = _DB_CONN_CACHE.get(path)
    if cached is not None:
        return cached
    with _DB_CONN_LOCK:
        # Double-checked locking — another thread may have raced ahead
        cached = _DB_CONN_CACHE.get(path)
        if cached is not None:
            return cached
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            from scraperx._sqlite_pragmas import apply_pragmas
            apply_pragmas(conn)
        except ImportError:
            # Tolerate older scraperx layouts; basic WAL is enough for fetch cache.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_FETCH_SCHEMA)
        _DB_CONN_CACHE[path] = conn
        return conn


def _cache_get(url: str, db_path: str | None = None) -> FetchResult | None:
    """Return cached FetchResult if fresh (within ttl_seconds), else None."""
    conn = _open_db(db_path)
    row = conn.execute(
        """SELECT url, content, mode_used, fetched_at, ttl_seconds,
                  http_status, elapsed_ms
           FROM web_fetch_cache WHERE url_hash = ?""",
        (_url_hash(url),),
    ).fetchone()
    if row is None:
        return None
    age = time.time() - row["fetched_at"]
    if age > row["ttl_seconds"]:
        return None
    return FetchResult(
        url=row["url"],
        content=row["content"],
        mode_used="cache",  # tag explicitly so caller can distinguish
        elapsed_ms=row["elapsed_ms"] or 0,
        was_cached=True,
        http_status=row["http_status"],
    )


def _cache_put(
    result: FetchResult,
    ttl: int = DEFAULT_TTL,
    db_path: str | None = None,
) -> None:
    """Persist a successful FetchResult to web_fetch_cache. No-op on failed results."""
    if not result.ok:
        return
    conn = _open_db(db_path)
    with _DB_CONN_LOCK:
        conn.execute(
            """INSERT OR REPLACE INTO web_fetch_cache
               (url_hash, url, content, mode_used, fetched_at, ttl_seconds,
                http_status, elapsed_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _url_hash(result.url),
                result.url,
                result.content,
                result.mode_used,
                time.time(),
                ttl,
                result.http_status,
                result.elapsed_ms,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def _is_private_or_loopback(host: str) -> bool:
    """True if host (literal IP or hostname) resolves to a private/loopback/link-local IP.

    Blocks RFC1918 (10/8, 172.16/12, 192.168/16), 127.0.0.0/8, ::1, link-local
    (169.254/16, fe80::/10), multicast, and reserved ranges. Used to prevent
    SSRF abuse where smart_fetch could be tricked into probing internal infra.
    """
    if not host:
        return True  # paranoid: empty host = block
    try:
        # Resolve hostname → all addresses; literal IPs pass through resolve too
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True  # can't resolve = treat as unsafe
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def _check_ssrf(url: str, *, allow_private: bool = False) -> str | None:
    """Returns None if URL is safe, else a string describing why it's blocked.

    The Jina Reader leg always goes to r.jina.ai (already safe) so this only
    matters for the urllib + Playwright legs. Tests can pass allow_private=True.
    """
    if allow_private:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme: {parsed.scheme!r}"
    host = parsed.hostname or ""
    if _is_private_or_loopback(host):
        return f"refusing to fetch private/loopback host: {host!r}"
    return None


# ---------------------------------------------------------------------------
# Cascade legs
# ---------------------------------------------------------------------------


def _fetch_jina(url: str, timeout: int) -> tuple[str, int | None]:
    """Fetch via Jina Reader. Returns (content, None). Raises on failure."""
    # r.jina.ai expects a URL-encoded target appended to base; it returns clean
    # markdown extraction. Free, no API key required.
    target = JINA_BASE + quote(url, safe=":/?&=#")
    req = Request(target, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/plain"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted host
        body = resp.read()
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            raise RuntimeError("jina returned empty body")
        # Jina doesn't expose the upstream HTTP status — return None.
        return text, None


def _fetch_urllib(url: str, timeout: int) -> tuple[str, int]:
    """Fetch via stdlib urllib. Returns (content, http_status). Raises on failure."""
    req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — caller-controlled URL
        status = getattr(resp, "status", None) or resp.getcode()
        body = resp.read()
        # Encoding cascade: header_charset → utf-8 → latin-1 (never raises).
        # Some servers misdeclare the charset; if header says ISO-8859-1 but
        # body is mostly multi-byte, the utf-8 fallback usually catches it.
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            return body.decode(charset, errors="strict"), int(status)
        except (LookupError, UnicodeDecodeError):
            pass
        try:
            return body.decode("utf-8", errors="strict"), int(status)
        except UnicodeDecodeError:
            return body.decode("latin-1", errors="replace"), int(status)


def _fetch_playwright(url: str, timeout: int) -> tuple[str, int | None]:
    """Fetch via headless Playwright Chromium. Slowest leg, bypasses bot-walls.

    Raises PlaywrightNotAvailable if optional dep missing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover — optional dep
        raise RuntimeError(f"playwright not installed: {e}") from e

    timeout_ms = max(timeout * 1000, 5000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=DEFAULT_USER_AGENT)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = response.status if response is not None else None
            content = page.content()
            return content, status
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def smart_fetch(
    url: str,
    *,
    prefer: FetchMode = "jina",
    timeout: int = DEFAULT_TIMEOUT,
    ttl: int = DEFAULT_TTL,
    no_cache: bool = False,
    strict: bool = False,
    allow_private: bool = False,
    db_path: str | None = None,
) -> FetchResult:
    """Fetch a URL with the Jina → urllib → Playwright cascade.

    The ``prefer`` argument REORDERS the entire cascade: the chosen leg runs
    first, then the remaining legs in default order ("jina", "urllib",
    "playwright"). With ``strict=True`` only the preferred leg runs.

    Args:
        url: The URL to fetch.
        prefer: Which cascade leg to try first. Default "jina" (best for content
                extraction). Other legs run as fallbacks if the preferred fails,
                unless ``strict=True``.
        timeout: Per-leg timeout in seconds. Default 30.
        ttl: Cache TTL for successful fetches, in seconds. Default 86400 (24h).
        no_cache: If True, skip cache lookup AND skip cache write.
        strict: If True, ONLY try ``prefer``; do not fall through. Useful for
                tests or when caller knows exactly which mode they want.
        allow_private: If True, skip the SSRF guard and allow private/loopback
                       hosts. Off by default — defense against URL injection.
        db_path: Override the cache DB path (default ~/.scraperx/social.db).
                 Mostly for tests.

    Returns:
        FetchResult — check ``result.ok`` to see if the fetch succeeded.
        On total failure, content is empty and errors lists each leg's error.

    Notes:
        - Network errors per-leg are caught and recorded on result.errors;
          the next leg is tried automatically (unless strict=True).
        - smart_fetch never raises on transport errors — only on programmer
          error (e.g. unknown ``prefer`` value).
        - Cache key is sha256(url); callers don't need to normalize.
    """
    if prefer not in _CASCADE_DEFAULT:
        raise ValueError(
            f"prefer must be one of {_CASCADE_DEFAULT}, got {prefer!r}"
        )

    # SSRF guard — applies to ALL legs (Jina honors the URL too)
    ssrf_reason = _check_ssrf(url, allow_private=allow_private)
    if ssrf_reason is not None:
        result = FetchResult(url=url)
        result.errors.append(("ssrf_guard", ssrf_reason))
        return result

    # Cache lookup
    if not no_cache:
        cached = _cache_get(url, db_path=db_path)
        if cached is not None:
            return cached

    # Build cascade order: preferred first, then the rest in default order.
    if strict:
        cascade: tuple[FetchMode, ...] = (prefer,)
    else:
        rest = tuple(m for m in _CASCADE_DEFAULT if m != prefer)
        cascade = (prefer,) + rest

    result = FetchResult(url=url)

    leg_fns = {
        "jina": _fetch_jina,
        "urllib": _fetch_urllib,
        "playwright": _fetch_playwright,
    }

    for mode in cascade:
        leg = leg_fns[mode]
        t0 = time.monotonic()
        try:
            content, status = leg(url, timeout)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if not content or not content.strip():
                result.errors.append((mode, "empty body"))
                continue
            result.content = content
            result.mode_used = mode
            result.elapsed_ms = elapsed_ms
            result.http_status = status
            break
        except Exception as e:  # noqa: BLE001 — record + advance to next leg
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            err = f"{type(e).__name__}: {e}"
            result.errors.append((mode, err))
            logger.debug("smart_fetch %s leg failed in %dms: %s", mode, elapsed_ms, err)
            continue

    # Cache only on success
    if result.ok and not no_cache:
        _cache_put(result, ttl=ttl, db_path=db_path)

    return result
