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

Ground-truth proof (2026-05-31): cracked DexScreener Base trending —
``https://dexscreener.com/base?rankBy=trendingScoreH6`` returns 403 via Jina/curl;
``fetch_stealth(solve_cloudflare=True)`` returned 200 (1.4 MB) -> 94 Base trending token
CAs. Recipe: regex ``"baseToken":{"symbol":"..","address":"0x.."}`` on the rendered HTML
(the embedded JSON has ``"totalSupply":undefined`` so ``json.loads`` fails -- use regex).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ScraplingNotAvailable",
    "fetch_stealth",
    "fetch_stealth_xhr",
    "stealth_session",
    "fetch_stealth_session",
    "StealthPageResult",
    "StealthSessionHandle",
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


def _get_stealthy_session() -> Any:
    """Lazy-import StealthySession — the accessor seam for the session surface.

    Mirrors ``_get_stealthy_fetcher`` exactly (same lazy import, same error, same
    install message) so tests can monkeypatch ONE name to fake the whole browser.
    """
    try:
        from scrapling.fetchers import StealthySession
    except ImportError as e:
        raise ScraplingNotAvailable(
            f"scrapling not installed ({e}). "
            "Install with: pip install 'scraperx[stealth]' && scrapling install"
        ) from e
    return StealthySession


def _decode_body(page: Any) -> str:
    """page.body (bytes since scrapling 0.4) -> str. Never raises."""
    body_bytes = getattr(page, "body", b"") or b""
    if isinstance(body_bytes, bytes):
        try:
            return body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return body_bytes.decode("latin-1", errors="replace")
    return str(body_bytes)


def _build_stealth_kwargs(
    *,
    timeout: int,
    solve_cloudflare: bool = True,
    headless: bool = True,
    network_idle: bool = True,
    wait_ms: int = 2_000,
    page_action: Optional[Callable] = None,
    capture_xhr: Optional[str] = None,
    wait_selector: Optional[str] = None,
    extra_kwargs: dict[str, Any] | None = None,
    inject_selector_config: bool = False,
) -> dict[str, Any]:
    """THE single place the Scrapling kwarg dict is built. Do not duplicate it.

    ``fetch_stealth``, ``fetch_stealth_xhr`` and ``stealth_session`` all route
    through here, so their defaults and the timeout floor CANNOT drift apart.
    Before 1.10.0 the dict was written out twice, which is precisely how a
    divergence hides for months.

    ``inject_selector_config`` exists because of a verified asymmetry upstream:
    ``StealthyFetcher.fetch`` injects
    ``selector_config = {**cls._generate_parser_arguments(), **user_config}``
    (which carries ``huge_tree=True``) before constructing its session, but a
    hand-built ``StealthySession`` gets ``selector_config == {}``. Our proven
    DexScreener case is a 1.4 MB page — exactly the size class where lxml's
    huge_tree matters. So the SESSION path must inject it; the one-shot path
    must NOT (the fetcher already did, and doing it twice would be redundant).
    """
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

    if inject_selector_config:
        StealthyFetcher = _get_stealthy_fetcher()
        base = StealthyFetcher._generate_parser_arguments()
        kwargs["selector_config"] = {**base, **(kwargs.get("selector_config") or {})}

    return kwargs


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

    # inject_selector_config=False: StealthyFetcher.fetch injects selector_config
    # itself (huge_tree=True). The SESSION path has no such helper — see
    # _build_stealth_kwargs.
    kwargs = _build_stealth_kwargs(
        timeout=timeout,
        solve_cloudflare=solve_cloudflare,
        headless=headless,
        network_idle=network_idle,
        wait_ms=wait_ms,
        page_action=page_action,
        capture_xhr=capture_xhr,
        wait_selector=wait_selector,
        extra_kwargs=extra_kwargs,
    )

    logger.debug("scrapling_stealth fetching %s with kwargs=%s", url, kwargs)
    page = StealthyFetcher.fetch(url, **kwargs)

    # page.body is bytes (per scrapling docs since v0.4); decode for cascade contract.
    content = _decode_body(page)
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

    kwargs = _build_stealth_kwargs(
        timeout=timeout,
        solve_cloudflare=solve_cloudflare,
        headless=headless,
        network_idle=network_idle,
        wait_ms=wait_ms,
        page_action=page_action,
        capture_xhr=xhr_pattern,
        wait_selector=wait_selector,
        extra_kwargs=extra_kwargs,
    )

    logger.debug("scrapling_stealth_xhr fetching %s with kwargs=%s", url, kwargs)
    page = StealthyFetcher.fetch(url, **kwargs)

    content = _decode_body(page)
    status = getattr(page, "status", None)
    captured = list(getattr(page, "captured_xhr", []) or [])
    return content, status, captured


# ---------------------------------------------------------------------------
# Session surface (1.10.0) — N URLs on ONE browser
#
# WHY: `StealthyFetcher.fetch(url, **kwargs)` is literally
#   `with StealthySession(**kwargs) as engine: return engine.fetch(url)`
# (verified in scrapling/fetchers/stealth_chrome.py). So every one-shot call
# builds and tears down a whole browser. Hoisting that `with` out of per-call
# scope IS this entire feature — it is ADOPT, not BUILD.
#
# THE ONE RULE, and it is not stylistic: pass EVERYTHING at construction and
# call `session.fetch(url)` with NO kwargs. Upstream's per-fetch validator
# iterates only its own known field names (_validators.py:190) — any other key
# is never read and never raises. So `session.fetch(url, **flat_dict)` SILENTLY
# DROPS user_data_dir / headless / capture_xhr, and the batch runs cookie-less
# while looking perfectly healthy. That is the exact regression 1.9.0 existed to
# kill, re-introduced one layer up. `test_fetch_receives_url_only_and_no_kwargs`
# pins it.
# ---------------------------------------------------------------------------

# Errors that mean OUR code is wrong, not that the site blocked us. These ABORT
# the batch by re-raising, because a wall never aborts — see the class-5
# POISONED-EVIDENCE rule: a code defect must never be reportable as an anti-bot
# block. (2026-08-02: a consumer's stealth call had never executed once, while
# logging "abandoning" like a Cloudflare wall, in 10 worktrees.)
_DEFECT_TYPES = (TypeError, AttributeError, NameError, ImportError, KeyError, ValueError)

# A wedged page pool self-heals by close()+start() on the same session object.
# Bounded so a genuinely broken browser binary cannot become a slow infinite loop.
_MAX_RESTARTS = 2


@dataclass
class StealthPageResult:
    """One URL's outcome inside a batch. Mirrors the cascade's FetchResult shape."""

    url: str
    index: int
    content: str = ""
    http_status: int | None = None
    error: str | None = None
    #: "ok" | "fetch" (site/transport) | "skipped" (budget exhausted before we tried)
    error_kind: str = "ok"
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        """Empty-body-only success test — same rule the cascade uses (fetch.py).

        Deliberately does NOT interpret http_status: a Cloudflare interstitial
        served at 403 WITH a body counts as content here, exactly as it does in
        smart_fetch today. Changing that is a separate decision.
        """
        return self.error is None and bool(self.content)


class StealthSessionHandle:
    """What `stealth_session()` hands you: fetch URLs on ONE warm browser."""

    def __init__(self, session: Any, *, capture_xhr: Optional[str] = None) -> None:
        self._session = session
        self._capture_xhr = capture_xhr
        self._stats = {"fetches": 0, "failures": 0, "restarts": 0}

    @property
    def stats(self) -> dict[str, int]:
        """Counters for the run. Log these — nobody has measured a long session yet."""
        return dict(self._stats)

    def fetch(self, url: str) -> tuple[str, int | None]:
        """Fetch one URL. Raises exactly like `fetch_stealth` — same contract."""
        self._stats["fetches"] += 1
        try:
            page = self._session.fetch(url)  # NO kwargs — see module note above
        except Exception:
            self._stats["failures"] += 1
            raise
        return _decode_body(page), getattr(page, "status", None)

    def fetch_xhr(self, url: str) -> tuple[str, int | None, list[Any]]:
        """Fetch + return captured XHRs. Requires capture_xhr at session build.

        There is no per-URL pattern by design: upstream reads capture_xhr from
        the session config at fetch time, so one session = one pattern. A
        per-URL parameter would be silently ignored after the first URL.
        """
        if not self._capture_xhr:
            raise ValueError(
                "fetch_xhr() needs capture_xhr=<pattern> passed to stealth_session(). "
                "The pattern is session-scoped upstream; it cannot be set per URL."
            )
        content, status = self.fetch(url)
        page = getattr(self._session, "_last_page", None)
        captured = list(getattr(page, "captured_xhr", []) or []) if page else []
        return content, status, captured

    def _restart(self) -> None:
        """close() + start() the SAME object — legal, and cheap now.

        close() nulls playwright; start() guards on `if not self.playwright`, so
        the object rebuilds a fresh context. Cheap because the Cloudflare
        clearance lives on disk in the profile, not in the dead context.
        """
        self._session.close()
        self._session.start()
        self._stats["restarts"] += 1


@contextlib.contextmanager
def stealth_session(
    *,
    timeout: int = DEFAULT_STEALTH_TIMEOUT,
    solve_cloudflare: bool = True,
    headless: bool = True,
    network_idle: bool = True,
    wait_ms: int = 2_000,
    page_action: Optional[Callable] = None,
    capture_xhr: Optional[str] = None,
    wait_selector: Optional[str] = None,
    profile: str | None = None,
    retries: int = 1,
    extra_kwargs: dict[str, Any] | None = None,
):
    """ONE browser, many URLs. The primary session surface.

    Use this when you drive your own loop (early exit, interleaved logic, a tier
    ladder). Use `fetch_stealth_session()` when you just have a list of URLs.

        >>> with stealth_session(profile="~/.cache/scraperx/profiles/example.com") as s:
        ...     for url in urls:
        ...         content, status = s.fetch(url)

    Args:
        profile: Persistent browser-profile dir -> `user_data_dir`. Cookies and
                 Cloudflare clearance survive across fetches AND process restarts.
                 Created if absent; `~` expanded. One dir PER HOST.
        retries: Upstream per-fetch retry count. Defaults to **1**, not upstream's
                 3 — three retries burns three full navigation timeouts plus two
                 sleeps on every dead URL, which turns the amortization win into a
                 loss on any list with dead links.
        extra_kwargs: Verbatim escape hatch; caller keys win. `max_pages` > 1 is
                 REJECTED here (see below).

    Raises:
        ScraplingNotAvailable: scrapling missing. Propagates out of the `with`
                 entry — never recorded as a per-URL error.
        ValueError: max_pages > 1 requested (a verified lying knob upstream:
                 `StealthySession(max_pages=5).max_pages` is 1 while `_config`
                 says 5, because __init__ calls a bare `super().__init__()`).
                 Rejected loudly rather than accepted and ignored.

    Guarantees the browser closes on normal exit, on a caller exception, and on
    generator finalization. close() is idempotent upstream, so a double close is
    harmless.
    """
    extra = dict(extra_kwargs or {})
    if "max_pages" in extra:
        requested = extra.pop("max_pages")
        if int(requested) > 1:
            raise ValueError(
                f"max_pages={requested} is not honoured by StealthySession (verified: "
                "_config keeps your value but the instance attribute stays 1, so the "
                "pool is size 1 regardless). Fetches are serial anyway — fetch() "
                "blocks. Rejecting rather than silently ignoring it."
            )

    kwargs = _build_stealth_kwargs(
        timeout=timeout,
        solve_cloudflare=solve_cloudflare,
        headless=headless,
        network_idle=network_idle,
        wait_ms=wait_ms,
        page_action=page_action,
        capture_xhr=capture_xhr,
        wait_selector=wait_selector,
        extra_kwargs=extra,
        # A raw StealthySession gets selector_config={} — the fetcher's huge_tree
        # injection does not happen here. Our proven 1.4 MB DexScreener page is
        # exactly where that matters.
        inject_selector_config=True,
    )
    kwargs["retries"] = retries
    if profile and "user_data_dir" not in kwargs:
        # Same three ops as the cascade's stealth_profile (fetch.py): abspath,
        # expanduser, makedirs. An explicit extra_kwargs['user_data_dir'] wins.
        profile_dir = os.path.abspath(os.path.expanduser(profile))
        os.makedirs(profile_dir, exist_ok=True)
        kwargs["user_data_dir"] = profile_dir

    StealthySession = _get_stealthy_session()
    logger.debug("stealth_session opening with kwargs=%s", kwargs)
    session = StealthySession(**kwargs)
    session.start()
    handle = StealthSessionHandle(session, capture_xhr=capture_xhr)
    try:
        yield handle
    finally:
        logger.debug("stealth_session closing; stats=%s", handle.stats)
        session.close()


def fetch_stealth_session(
    urls,
    timeout: int = DEFAULT_STEALTH_TIMEOUT,
    *,
    sleep_between: float = 0.4,
    max_total_seconds: float | None = None,
    **session_opts: Any,
):
    """Fetch many URLs on ONE browser, yielding a `StealthPageResult` each.

        >>> for r in fetch_stealth_session(urls, profile="~/.cache/scraperx/p/example.com"):
        ...     if r.ok:
        ...         save(r.url, r.content)

    FAILURE SEMANTICS — the whole point of the batch shape:
      * a site/transport failure on URL 7 of 20 does NOT abort. It is yielded as
        `error_kind="fetch"` with a type-prefixed message, and URL 8 proceeds on
        the SAME warm session (a raising fetch does not poison it — the pool is
        reclaimed and the context, with its clearance, survives).
      * a CODE DEFECT (TypeError/ValueError/...) RE-RAISES and aborts. Three
        independent signals keep it distinguishable from a wall: it aborts (a
        wall never does), it is a traceback not a string, and `error_kind` is a
        machine-readable enum. The failure this kills is arity drift reported as
        20 identical "blocks".
      * `max_total_seconds` is checked BETWEEN URLs; the remainder is yielded as
        `error_kind="skipped"`. It CANNOT interrupt a wedged in-flight fetch —
        sync Playwright blocks and is not interruptible from the caller thread.

    EARLY EXIT: if you `break` out of this loop, close the generator —
    `contextlib.closing(...)` or `gen.close()` — or use `stealth_session()`
    directly. CPython refcounting usually finalizes it for you, but "usually" is
    not a guarantee, and a leaked generator means a leaked headless browser.

    Args:
        sleep_between: Pause between URLs, seconds. Applied N-1 times, never
                       before the first and never after the last.
        **session_opts: Forwarded verbatim to `stealth_session` (profile=,
                       capture_xhr=, retries=, extra_kwargs=, ...).
    """
    started = time.monotonic()
    restarts = 0

    with stealth_session(timeout=timeout, **session_opts) as handle:
        for i, url in enumerate(urls):
            if max_total_seconds is not None and (time.monotonic() - started) > max_total_seconds:
                yield StealthPageResult(
                    url=url,
                    index=i,
                    error=f"BudgetExceeded: max_total_seconds={max_total_seconds} elapsed before this URL",
                    error_kind="skipped",
                )
                continue

            if i and sleep_between:
                time.sleep(sleep_between)

            t0 = time.monotonic()
            try:
                content, status = handle.fetch(url)
            except _DEFECT_TYPES:
                # OUR bug. Abort loudly — results 0..i-1 are already delivered.
                raise
            except RuntimeError as e:
                if "Maximum page limit" in str(e) and restarts < _MAX_RESTARTS:
                    # Pool wedged: a page failed to close, so it was never removed
                    # from a size-1 pool and every later URL would die identically.
                    restarts += 1
                    logger.warning(
                        "stealth_session page pool wedged on %s — restarting browser "
                        "(%d/%d): %s", url, restarts, _MAX_RESTARTS, e,
                    )
                    handle._restart()
                    try:
                        content, status = handle.fetch(url)
                    except Exception as e2:
                        yield StealthPageResult(
                            url=url, index=i, error=f"{type(e2).__name__}: {e2}",
                            error_kind="fetch",
                            elapsed_ms=int((time.monotonic() - t0) * 1000),
                        )
                        continue
                else:
                    yield StealthPageResult(
                        url=url, index=i, error=f"{type(e).__name__}: {e}",
                        error_kind="fetch",
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                    )
                    continue
            except Exception as e:
                # Site / transport / timeout. Same error-string convention as the
                # cascade: the TYPE prefix is what makes a defect legible later.
                yield StealthPageResult(
                    url=url, index=i, error=f"{type(e).__name__}: {e}",
                    error_kind="fetch",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
                continue

            yield StealthPageResult(
                url=url, index=i, content=content, http_status=status,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
