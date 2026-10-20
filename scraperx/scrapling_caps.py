"""scraperx.scrapling_caps - what Scrapling can do, what scraperx does with it, and a tripwire for upstream drift.

Why this exists: scraperx is meant to be the best scraper in the repo, and Scrapling (D4Vinci/Scrapling, BSD-3) ships
more scraping machinery than any one wrapper tracks: adaptive selectors, a spider framework, TLS impersonation, a
Cloudflare solver, network recording, a sanitising Markdown converter, an MCP server. Upstream moves fast and breaks
things (0.5 deleted ``DynamicFetcher`` and ``capture_xhr``). Three things keep scraperx honest about it:

1. ``CAPABILITIES``: every Scrapling feature we know, with a status (wired / passthrough / partial / gap /
   removed-upstream / not-adopted), the scraperx entry point that exposes it, and a note.
2. ``check_upstream()``: introspects the INSTALLED scrapling. ``missing`` = a symbol we map is gone;
   ``new`` = a public symbol upstream added that no one has looked at yet.
3. ``tests/test_scrapling_caps.py`` turns both into red tests, and checks ``docs/SCRAPLING.md`` is exactly the
   output of ``render_markdown()`` so the document cannot rot.

    python -m scraperx.scrapling_caps            # status + drift against the installed scrapling
    python -m scraperx.scrapling_caps --markdown # the table (this is docs/SCRAPLING.md)
    python -m scraperx.scrapling_caps --snapshot # print the public-symbol snapshot after you reviewed an upgrade
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from typing import Any

__all__ = ["Capability", "CAPABILITIES", "check_upstream", "render_markdown", "installed_version", "KNOWN_PUBLIC"]

WIRED, PASSTHROUGH, PARTIAL, GAP = "wired", "passthrough", "partial", "gap"
REMOVED, NOT_ADOPTED = "removed-upstream", "not-adopted"


@dataclass(frozen=True)
class Capability:
    key: str
    area: str  # fetch | parse | crawl | ai | devex
    feature: str  # what Scrapling offers, in one line
    status: str
    scraperx: str | None  # "module:attr" entry point, resolvable by the test; None = no entry point
    upstream: str | None  # "module:attr" in scrapling that must exist (None = a kwarg/behaviour, not a symbol)
    note: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    # ---- fetching ----------------------------------------------------------------------------------------
    Capability("stealth-browser", "fetch", "StealthyFetcher: patchright Chromium + browserforge fingerprints, one-shot fetch",
               WIRED, "scraperx.scrapling_stealth:fetch_stealth", "scrapling.fetchers:StealthyFetcher",
               "Last leg of smart_fetch's cascade. Needs `pip install 'scraperx[stealth]'` and a Chromium (`scrapling install`)."),
    Capability("browser-sessions", "fetch", "StealthySession / AsyncStealthySession: one warm browser, many URLs, persistent profile",
               WIRED, "scraperx.scrapling_stealth:stealth_session", "scrapling.fetchers:StealthySession",
               "stealth_session()/fetch_stealth_session(): profile dir keeps Cloudflare clearance across runs."),
    Capability("cloudflare-solver", "fetch", "solve_cloudflare=True: Turnstile / Interstitial auto-solve",
               WIRED, "scraperx.scrapling_stealth:fetch_stealth", None,
               "On by default in scraperx. 0.5 fixed delayed-widget detection and bounded the retry loops."),
    Capability("network-history", "fetch", "record_requests=True + session.network.search(): every request the page made, with bodies",
               WIRED, "scraperx.scrapling_stealth:fetch_stealth_xhr", None,
               "0.5 replaced capture_xhr with this. scraperx supports BOTH (feature-detected), keeping fetch_stealth_xhr's contract."),
    Capability("session-state", "fetch", "save_state()/load_state(): cookies + localStorage + IndexedDB as Playwright JSON (>=0.5)",
               WIRED, "scraperx.scrapling_stealth:StealthSessionHandle", None,
               "Handle methods save_state(path)/load_state(path). Needs a started shared context and no proxy_rotator."),
    Capability("http-tls-impersonation", "fetch", "Fetcher/FetcherSession: curl_cffi TLS + header impersonation, HTTP/3, stealthy headers",
               PARTIAL, "scraperx.page_markdown:fetch_markdown", "scrapling.fetchers:Fetcher",
               "Reachable through fetch_markdown() and crawl(impersonate=). NOT yet a smart_fetch cascade leg: that is the next gap to close."),
    Capability("proxy-rotation", "fetch", "ProxyRotator (cyclic or custom strategy) across all session types",
               PARTIAL, "scraperx.site_crawl:crawl", None,
               "crawl(proxies=[...]). One-shot fetches: extra_kwargs={'proxy': ...}. No rotation in smart_fetch."),
    Capability("browser-kwargs", "fetch", "pierce_shadow, block_ads, blocked_domains, disable_resources, dns_over_https, cdp_url, init_script, page_setup",
               PASSTHROUGH, "scraperx.scrapling_stealth:fetch_stealth", None,
               "Pass any of them in extra_kwargs={...}; caller keys win. Not individually wrapped on purpose."),
    Capability("browser-actions", "fetch", "page_action / wait_selector: automate clicks, scrolls, waits after navigation",
               WIRED, "scraperx.scrapling_stealth:fetch_stealth", None, "page_action= and wait_selector= are first-class parameters."),
    Capability("dynamic-fetcher", "fetch", "DynamicFetcher / DynamicSession (plain Playwright), 0.4.x only",
               REMOVED, None, None, "Deleted in 0.5 (use StealthyFetcher). scraperx never used it."),
    Capability("capture-xhr", "fetch", "capture_xhr=<pattern> kwarg and response.captured_xhr, 0.4.x only",
               REMOVED, None, None, "Deleted in 0.5: raises TypeError. Replaced by network-history, which fetch_stealth_xhr now uses."),
    # ---- parsing -----------------------------------------------------------------------------------------
    Capability("adaptive-selectors", "parse", "css(..., auto_save=True) then css(..., adaptive=True): relocate elements after a redesign",
               WIRED, "scraperx.adaptive_select:parse", "scrapling.parser:Selector",
               "NOTE: smart_fetch's `adaptive=` kwarg is cascade RANKING from telemetry, unrelated to this. Default threshold 40 percent similarity."),
    Capability("selector-engine", "parse", "CSS / XPath / regex / text search, find_similar, below_elements, generated selectors, DOM navigation",
               PASSTHROUGH, "scraperx.adaptive_select:parse", "scrapling.parser:Selectors",
               "parse() returns the real Selector, so every method is available. Parser-only install is enough."),
    Capability("markdown-sanitised", "parse", "Response.markdown(): scripts, styles and hidden/prompt-injection text stripped, then Markdown",
               WIRED, "scraperx.page_markdown:html_to_markdown", "scrapling.engines.toolbelt.custom:Response",
               "html_to_markdown() / fetch_markdown(). <nav> is kept (verified): only scripts/styles/hidden content are removed."),
    # ---- crawling ----------------------------------------------------------------------------------------
    Capability("spider-framework", "crawl", "Spider: concurrency, per-domain limits, multi-session, blocked-request retry, pause/resume checkpoints",
               WIRED, "scraperx.site_crawl:crawl", "scrapling.spiders:Spider",
               "crawl() adds the guard rails: SSRF check on every link, same-domain default, hard page budget. Needs a Spider with AutoThrottle + robots.txt (0.4.12 and 0.5 have it; crawl() refuses otherwise)."),
    Capability("polite-crawling", "crawl", "AutoThrottle, robots.txt (Crawl-delay, Request-rate), download_delay",
               WIRED, "scraperx.site_crawl:crawl", None, "On by default in crawl(): autothrottle=True, obey_robots=True."),
    Capability("crawl-templates", "crawl", "CrawlSpider, SitemapSpider, XMLFeedSpider, CSVFeedSpider, ShopifySpider, ContentSpider (rules, sitemaps, feeds)",
               PASSTHROUGH, "scraperx.site_crawl:crawl", "scrapling.spiders:SitemapSpider",
               "crawl(spider_cls=SitemapSpider, sitemap_urls=[...]): guard-rail attributes are applied, parse is the template's."),
    Capability("link-extractor", "crawl", "LinkExtractor: allow/deny patterns, domain filters, CSS/XPath scoping, canonicalisation",
               PASSTHROUGH, "scraperx.site_crawl:crawl", "scrapling.spiders:LinkExtractor", "Use inside a CrawlSpider passed as spider_cls."),
    Capability("site-to-markdown", "crawl", "SiteToMarkdownSpider / ContentSpider: a whole site into a Markdown corpus",
               GAP, None, "scrapling.spiders:SiteToMarkdownSpider",
               "Not wrapped or tested. crawl(markdown=True) gives per-page Markdown; the corpus-to-disk spider is the next gap."),
    Capability("streaming-and-dev-cache", "crawl", "spider.stream() with live stats; development_mode response cache",
               GAP, None, None, "Not exposed. crawl() returns after the run; use spider_cls + your own stream() driver meanwhile."),
    # ---- AI / developer experience -----------------------------------------------------------------------
    Capability("mcp-server", "ai", "scrapling[ai]: MCP server with HTTP/browser/stealth tools, CAPTCHA solving, prompt-injection stripping",
               NOT_ADOPTED, None, None, "Runs beside scraperx; scraperx does not re-host it. Decision: a second MCP surface is more attack surface than value."),
    Capability("agent-skill", "ai", "Official Agent Skill teaching coding agents the Scrapling API",
               NOT_ADOPTED, None, None, "The scraperx skill points at docs/SCRAPLING.md instead of duplicating it."),
    Capability("cli-and-shell", "devex", "`scrapling extract get|fetch`, interactive IPython shell, curl2fetcher",
               NOT_ADOPTED, None, None, "scraperx has its own CLI; the Scrapling shell is handy interactively (`pip install 'scrapling[shell]'`)."),
    Capability("scrapy-integration", "devex", "scrapling_response decorator: parse Scrapy responses with Scrapling's parser",
               NOT_ADOPTED, None, None, "No Scrapy in this stack."),
)

# Public classes/functions of the watched scrapling modules as of the last REVIEWED upgrade (0.5).
# Regenerate with `python -m scraperx.scrapling_caps --snapshot` ONLY after reading the upstream release notes.
KNOWN_PUBLIC: dict[str, list[str]] = {
    "scrapling.fetchers": [
        "AsyncFetcher",
        "AsyncStealthySession",
        "Fetcher",
        "FetcherSession",
        "StealthyFetcher",
        "StealthySession"
    ],
    "scrapling.spiders": [
        "CSVFeedSpider",
        "ContentSpider",
        "CrawlResult",
        "CrawlRule",
        "CrawlSpider",
        "CrawlerEngine",
        "LinkExtractor",
        "Request",
        "Response",
        "Scheduler",
        "SessionConfigurationError",
        "SessionManager",
        "ShopifySpider",
        "SiteToMarkdownSpider",
        "SitemapSpider",
        "Spider",
        "XMLFeedSpider"
    ],
    "scrapling.parser": [
        "Adaptor",
        "Adaptors",
        "AttributesHandler",
        "SQLiteStorageSystem",
        "Selector",
        "Selectors",
        "SelectorsGeneration",
        "StorageSystemMixin",
        "TextHandler",
        "TextHandlers",
        "clean_spaces",
        "flatten"
    ]
}

# Names that existed in 0.4.x and are gone in 0.5: not "missing", not "new".
_REMOVED_IN_05 = {"DynamicFetcher", "DynamicSession", "AsyncDynamicSession"}
_WATCHED = ("scrapling.fetchers", "scrapling.spiders", "scrapling.parser")


def installed_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("scrapling")
    except Exception:  # noqa: BLE001 - PackageNotFoundError and friends
        return None


def _resolve(path: str) -> Any:
    mod, _, attr = path.partition(":")
    obj: Any = importlib.import_module(mod)
    for part in attr.split(".") if attr else []:
        obj = getattr(obj, part)
    return obj


def _public(modname: str) -> set[str]:
    try:
        mod = importlib.import_module(modname)
    except ImportError:
        return set()
    out = set()
    for n in dir(mod):
        if n.startswith("_"):
            continue
        obj = getattr(mod, n)
        if (isinstance(obj, type) or callable(obj)) and getattr(obj, "__module__", "").startswith("scrapling"):
            out.add(n)
    return out


def snapshot() -> dict[str, list[str]]:
    return {m: sorted(_public(m)) for m in _WATCHED}


def _version_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    parts: list[int] = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_upstream() -> dict[str, Any]:
    """Compare the installed scrapling with this registry. Pure introspection, no network."""
    ver = installed_version()
    res: dict[str, Any] = {"version": ver, "installed": ver is not None, "scraperx_unresolved": [], "upstream_missing": [], "new_public": [], "absent_on_older_install": []}
    if ver is None:
        return res
    for cap in CAPABILITIES:
        if cap.scraperx:
            try:
                _resolve(cap.scraperx)
            except Exception as e:  # noqa: BLE001
                res["scraperx_unresolved"].append(f"{cap.key}: {cap.scraperx} ({type(e).__name__})")
        if cap.upstream and cap.status != REMOVED:
            try:
                _resolve(cap.upstream)
            except Exception as e:  # noqa: BLE001
                # The registry was reviewed against 0.5. An OLDER scrapling legitimately lacks newer symbols
                # (0.4.12 has no SiteToMarkdownSpider): report that, but only a >=0.5 install can "lose" one.
                bucket = "upstream_missing" if _version_tuple(ver) >= (0, 5) else "absent_on_older_install"
                res.setdefault(bucket, []).append(f"{cap.key}: {cap.upstream} ({type(e).__name__})")
    if _version_tuple(ver) >= (0, 5) and KNOWN_PUBLIC:
        for m in _WATCHED:
            now, known = _public(m), set(KNOWN_PUBLIC.get(m, []))
            res["new_public"] += [f"{m}:{n}" for n in sorted(now - known - _REMOVED_IN_05)]
            res["upstream_missing"] += [f"{m}:{n} (was public at review)" for n in sorted(known - now)]
    return res


def render_markdown() -> str:
    """The text of docs/SCRAPLING.md. A test asserts the file equals this, so edit CAPABILITIES, not the doc."""
    order = {"wired": 0, "passthrough": 1, "partial": 2, "gap": 3, "not-adopted": 4, "removed-upstream": 5}
    lines = [
        "# Scrapling in scraperx",
        "",
        "GENERATED by `python -m scraperx.scrapling_caps --markdown` from `scraperx/scrapling_caps.py`. Do not edit by hand:",
        "`tests/test_scrapling_caps.py` fails if this file differs from the generator.",
        "",
        "Scrapling (https://github.com/D4Vinci/Scrapling, BSD-3-Clause) is scraperx's engine for the hard web: anti-bot browsers,",
        "adaptive parsing, crawling. scraperx wraps it with guard rails (SSRF checks, polite defaults, a stable return contract)",
        "and does not fork it: upstream releases weekly, so we pin a floor, detect API breaks by feature, and test both sides.",
        "Statuses: **wired** = first-class scraperx API; **passthrough** = reachable through `extra_kwargs`/`spider_cls` or the returned",
        "object; **partial** = reachable but not integrated everywhere; **gap** = not exposed yet (a TODO, not a refusal);",
        "**not-adopted** = a decision, with the reason; **removed-upstream** = gone from the current Scrapling.",
        "",
        "| Capability | Area | What Scrapling offers | Status | scraperx entry point | Note |",
        "|---|---|---|---|---|---|",
    ]
    for c in sorted(CAPABILITIES, key=lambda c: (order[c.status], c.area, c.key)):
        entry = f"`{c.scraperx}`" if c.scraperx else "-"
        lines.append(f"| {c.key} | {c.area} | {c.feature} | {c.status} | {entry} | {c.note} |".replace("\n", " "))
    lines += ["", "## Upgrade procedure", "",
              "1. Read the upstream release notes and the migration guide for the new version.",
              "2. `pip install -U 'scrapling[fetchers]'` in a scratch venv; run `python -m scraperx.scrapling_caps` (lists `new_public` and `upstream_missing`).",
              "3. Run `pytest` and `python tests/live/scrapling_live_probe.py` (needs a Chromium: `scrapling install`).",
              "4. Update `CAPABILITIES`, then `python -m scraperx.scrapling_caps --snapshot` into `KNOWN_PUBLIC`, regenerate this file.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--markdown" in args:
        sys.stdout.write(render_markdown())
        return 0
    if "--snapshot" in args:
        print(json.dumps(snapshot(), indent=4))
        return 0
    res = check_upstream()
    counts: dict[str, int] = {}
    for c in CAPABILITIES:
        counts[c.status] = counts.get(c.status, 0) + 1
    print(f"scrapling installed: {res['version'] or 'NO'}")
    print("capabilities: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for k in ("scraperx_unresolved", "upstream_missing", "new_public", "absent_on_older_install"):
        print(f"{k}: {len(res[k])}")
        for x in res[k]:
            print("  -", x)
    return 1 if (res["scraperx_unresolved"] or res["upstream_missing"] or res["new_public"]) else 0  # absent_on_older_install is informational


if __name__ == "__main__":
    raise SystemExit(main())
