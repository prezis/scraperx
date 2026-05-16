"""scrapling_stealth — Cloudflare-Turnstile-bypass fetcher leg for the smart_fetch cascade.

Wraps Scrapling's ``StealthyFetcher`` (patchright-patched Chromium + browserforge
fingerprints + apify-fingerprint-datapoints) with the same ``(url, timeout) ->
(content, http_status)`` shape as the rest of ``scraperx.fetch`` cascade legs.

Position in the cascade (see ``scraperx/fetch.py``):

    jina  →  urllib  →  playwright  →  scrapling_stealth  →  fail

By the time we reach this leg, the cheaper paths have been tried and failed —
typically because the upstream sits behind Cloudflare Turnstile / Interstitial,
DataDome-lite, or a JS challenge that vanilla headless Chromium triggers.
``StealthyFetcher`` runs Chromium with patches that defeat the common
``navigator.webdriver`` / canvas / WebGL / TLS fingerprint checks AND can
auto-solve the Turnstile widget when called with ``solve_cloudflare=True``.

Why this is opt-in (``[stealth]`` extra, not a base dep):
- ``scrapling[fetchers]`` pulls in patchright (~46 MB), browserforge, apify
  fingerprint datapoints, msgspec, w3lib, protego — non-trivial install footprint
- patchright requires its own Chromium binary (``scrapling install`` or
  ``patchright install chromium``) — not a pure-Python add
- Most scraperx callers don't hit Cloudflare-walled sites; pay only when needed

Usage from a caller that already knows the cascade:
    from scraperx.fetch import smart_fetch
    result = smart_fetch(url)  # cascade auto-tries scrapling_stealth as last leg

Usage as a one-shot bypass (skip the cascade):
    from scraperx.scrapling_stealth import fetch_stealth, ScraplingNotAvailable
    try:
        content, status = fetch_stealth(url, timeout=90)
    except ScraplingNotAvailable as e:
        print(f"Install: pip install 'scraperx[stealth]' && scrapling install -- {e}")

Ground-truth proof (s33, 2026-05-06): cracked
``https://intel.arkm.com/explorer/entity/fomo`` after Jina + plain-Playwright
both got Turnstile-walled. StealthyFetcher returned 200 with full HTML in ~65s.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ScraplingNotAvailable",
    "fetch_stealth",
    "fetch_stealth_xhr",
    "DEFAULT_STEALTH_TIMEOUT",
]

# Default per-call timeout in SECONDS for the smart_fetch leg signature.
# Internally converted to milliseconds for StealthyFetcher (Playwright API).
# 90s default reflects Cloudflare-solver round-trip: page load + JS challenge +
# Turnstile token mint + post-challenge re-navigate. Tune via caller's timeout=.
DEFAULT_STEALTH_TIMEOUT = 90


class ScraplingNotAvailable(RuntimeError):
    """Raised when Scrapling or its browser binary aren't installed.

    Install path:
        pip install 'scraperx[stealth]'    # pulls scrapling[fetchers]
        scrapling install                   # downloads patchright Chromium

    Without this dep the cascade leg returns a clear error to the caller
    instead of crashing the whole smart_fetch call.
    """


def _get_stealthy_fetcher() -> Any:
    """Lazy-import StealthyFetcher; raise ScraplingNotAvailable on missing dep.

    Importing scrapling.fetchers eagerly would force every scraperx user to
    install patchright + browserforge — defeats the opt-in design.
    """
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as e:
        raise ScraplingNotAvailable(
            f"scrapling not installed ({e}). "
            "Install with: pip install 'scraperx[stealth]' && scrapling install"
        ) from e
    return StealthyFetcher


def fetch_stealth(
    url: str,
    timeout: int = DEFAULT_STEALTH_TIMEOUT,
    *,
    solve_cloudflare: bool = True,
    headless: bool = True,
    network_idle: bool = True,
    wait_ms: int = 2_000,
    page_action: Optional[Callable] = None,
    capture_xhr: Optional[str] = None,
    wait_selector: Optional[str] = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[str, int | None]:
    """Fetch a URL via Scrapling's StealthyFetcher.

    Matches the ``(url, timeout) -> (content, http_status)`` contract used by
    the other legs in ``scraperx.fetch._fetch_*`` so it slots into the cascade
    via ``leg_fns["scrapling_stealth"] = _fetch_stealth_leg`` (see
    ``fetch.py`` for the cascade dispatch).

    Args:
        url: Target URL. SSRF guard runs in ``smart_fetch`` BEFORE this is called.
        timeout: Total per-attempt budget in SECONDS. Internally x1000 for
                 Scrapling's millisecond Playwright timeout. Default 90s
                 because Cloudflare's auto-solver routinely needs 30-60s.
        solve_cloudflare: Try to auto-solve Turnstile/Interstitial. Default True.
                          Set False to fail fast on Cloudflare-walled sites if
                          you want to short-circuit the leg as "wall confirmed".
        headless: Hide the browser. Default True (safe for servers/CI).
        network_idle: Wait until network quiets. Default True — most JS-rendered
                      pages need this for the SPA to hydrate the data we want.
        wait_ms: Extra wait AFTER network_idle, in MILLISECONDS. Default 2000.
                 Cloudflare sometimes re-injects the challenge on hydrate.
        extra_kwargs: Escape hatch for less-common StealthyFetcher options
                      (e.g. ``proxy``, ``cookies``, ``locale``, ``cdp_url``).
                      Merged on top of the curated defaults; caller-supplied
                      keys win.

    Returns:
        (content, http_status). content is the full HTML body as a str
        (decoded from page.body bytes). http_status is the HTTP response code
        (e.g. 200, 404). Raises on transport failure — the cascade caller
        catches and records the error.

    Raises:
        ScraplingNotAvailable: Scrapling not installed or browser missing.
        Exception: Network errors, browser launch failures, page timeouts —
                   bubble up to the cascade so the caller records (mode, err).
    """
    StealthyFetcher = _get_stealthy_fetcher()

    timeout_ms = max(int(timeout) * 1000, 5_000)

    kwargs: dict[str, Any] = {
        "headless": headless,
        "network_idle": network_idle,
        "solve_cloudflare": solve_cloudflare,
        "timeout": timeout_ms,
        "wait": int(wait_ms),
    }
    # Forward optional XHR-capture / page-action / wait_selector hooks if the
    # caller wants them. These map straight into Scrapling's StealthSession
    # TypedDict (see scrapling.engines._browsers._types) — page_action runs
    # AFTER navigation, capture_xhr accepts a URL-substring filter and stashes
    # matching responses on page._xhr_responses, wait_selector blocks until a
    # CSS selector resolves (use to wait for SPA-hydrated tables).
    if page_action is not None:
        kwargs["page_action"] = page_action
    if capture_xhr is not None:
        kwargs["capture_xhr"] = capture_xhr
    if wait_selector is not None:
        kwargs["wait_selector"] = wait_selector
    if extra_kwargs:
        # Caller-supplied keys win — escape hatch is intentional.
        kwargs.update(extra_kwargs)

    logger.debug("scrapling_stealth fetching %s with kwargs=%s", url, kwargs)
    page = StealthyFetcher.fetch(url, **kwargs)

    # page.body is bytes (per scrapling docs since v0.4); decode for cascade contract.
    body_bytes = getattr(page, "body", b"") or b""
    if isinstance(body_bytes, bytes):
        # Try UTF-8, fall back to latin-1 (never raises). Mirrors _fetch_urllib.
        try:
            content = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = body_bytes.decode("latin-1", errors="replace")
    else:
        content = str(body_bytes)

    status = getattr(page, "status", None)
    return content, status


def fetch_stealth_xhr(
    url: str,
    xhr_pattern: str,
    timeout: int = DEFAULT_STEALTH_TIMEOUT,
    *,
    solve_cloudflare: bool = True,
    headless: bool = True,
    network_idle: bool = True,
    wait_ms: int = 2_000,
    page_action: Optional[Callable] = None,
    wait_selector: Optional[str] = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[str, int | None, list[Any]]:
    """Fetch a URL via StealthyFetcher and capture matching XHR/fetch responses.

    Same Cloudflare-bypass leg as ``fetch_stealth`` but returns the SPA's
    hydration XHRs alongside the HTML, so the caller can read structured JSON
    payloads that never appear in the SSR HTML. Use this for SPA tables (Arkham
    Intel wallet lists, Dune query embeds, Etherscan address transfers, etc.)
    where the data lives behind a client-side fetch.

    Args:
        url: Target URL.
        xhr_pattern: Regex pattern matched against XHR/fetch response URLs.
                     Substring match works (re.search semantics). Examples:
                     ``"/api/"``, ``r"intel\\.arkm\\.com/api"``,
                     ``r"\\.json\\?"``.
        timeout: Total per-attempt budget in SECONDS.
        solve_cloudflare: Auto-solve Turnstile/Interstitial. Default True.
        headless: Hide the browser. Default True.
        network_idle: Wait for network quiet. Default True (lets the SPA
                      finish hydrating before we read response.captured_xhr).
        wait_ms: Extra wait AFTER network_idle. Default 2000 ms.
        page_action: Optional ``Callable[[Page], Page]`` (sync) that runs
                     AFTER navigation but BEFORE response collection finishes.
                     Use to scroll, click pagination, or wait for a specific
                     element — anything that triggers ADDITIONAL XHRs you want
                     captured.
        wait_selector: Optional CSS selector to wait for before returning.
                       Useful when ``network_idle`` fires too early on long-poll
                       SPAs.
        extra_kwargs: Escape hatch for less-common StealthyFetcher options.

    Returns:
        ``(content, http_status, captured_xhr)``. ``captured_xhr`` is a list
        of Scrapling ``Response`` objects (see scrapling.engines.toolbelt.custom)
        each with ``.url``, ``.status``, ``.headers``, ``.body`` (bytes), and
        ``.json()``. Empty list if no XHR matched the pattern.

    Raises:
        ScraplingNotAvailable: Scrapling not installed.
        Exception: Network / browser / timeout errors bubble up.

    Example — capture Arkham SPA's wallet-table XHRs:
        >>> content, status, xhrs = fetch_stealth_xhr(
        ...     "https://intel.arkm.com/explorer/entity/fomo",
        ...     xhr_pattern=r"intel\\.arkm\\.com/(api|_next)",
        ...     timeout=120,
        ... )
        >>> for r in xhrs:
        ...     print(r.url, r.status)
    """
    StealthyFetcher = _get_stealthy_fetcher()

    timeout_ms = max(int(timeout) * 1000, 5_000)

    kwargs: dict[str, Any] = {
        "headless": headless,
        "network_idle": network_idle,
        "solve_cloudflare": solve_cloudflare,
        "timeout": timeout_ms,
        "wait": int(wait_ms),
        "capture_xhr": xhr_pattern,
    }
    if page_action is not None:
        kwargs["page_action"] = page_action
    if wait_selector is not None:
        kwargs["wait_selector"] = wait_selector
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    logger.debug("scrapling_stealth_xhr fetching %s with kwargs=%s", url, kwargs)
    page = StealthyFetcher.fetch(url, **kwargs)

    body_bytes = getattr(page, "body", b"") or b""
    if isinstance(body_bytes, bytes):
        try:
            content = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = body_bytes.decode("latin-1", errors="replace")
    else:
        content = str(body_bytes)

    status = getattr(page, "status", None)
    captured = list(getattr(page, "captured_xhr", []) or [])
    return content, status, captured
