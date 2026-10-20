"""scraperx.site_crawl - Scrapling's Spider framework behind scraperx's guard rails.

Scrapling ships a Scrapy-like crawler (concurrency, per-domain throttling, AutoThrottle, robots.txt, pause/resume
checkpoints, blocked-request retry, development cache, ``crawl_strategy`` bfs/dfs, ready templates). Used raw it
fetches whatever a page links to, including ``http://127.0.0.1:...``. This module is the thin layer that makes it
safe to point at the open web from a machine that has secrets on it:

* SSRF guard (``fetch._check_ssrf``) on every start URL AND every followed link; refusals are reported, not hidden;
* stays on the start domains unless told otherwise (``same_domain=True``);
* a hard page budget (``max_pages``) counted at SCHEDULE time, so concurrency cannot overshoot it;
* polite by default: robots.txt obeyed, AutoThrottle on, 4 concurrent requests;
* runs from sync code AND from inside a running event loop (the spider owns an ``anyio.run``).

    >>> from scraperx.site_crawl import crawl
    >>> out = crawl("https://example.com", fields={"title": "h1::text", "links[]": "a::attr(href)"}, max_pages=10)
    >>> out.items[0]["title"], out.stats["requests_count"], out.refused

Field spec: ``{"name": "<css>"}`` -> first match (str or None); a name ending in ``[]`` -> list of all matches.
``markdown=True`` adds ``item["markdown"]`` (needs ``scraperx[markdown]``). ``stealth=True`` routes requests through a
StealthySession (patched Chromium, Cloudflare solver) instead of curl_cffi HTTP. ``spider_cls=`` accepts any Scrapling
Spider/CrawlSpider/SitemapSpider/ContentSpider subclass: the guard-rail attributes are applied, ``parse`` is yours.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urldefrag, urljoin, urlparse

from .fetch import _check_ssrf
from .scrapling_stealth import ScraplingNotAvailable

logger = logging.getLogger(__name__)

__all__ = ["crawl", "CrawlOutcome"]


@dataclass
class CrawlOutcome:
    """What a crawl produced. ``refused`` lists URLs the guard rails refused to fetch, with the reason."""

    items: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    paused: bool = False
    refused: list[str] = field(default_factory=list)
    scheduled: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.items) and not self.paused


def _scrapling_spiders() -> Any:
    try:
        import scrapling.spiders as sp  # needs scrapling[fetchers]

        return sp
    except ImportError as e:
        raise ScraplingNotAvailable(
            f"scrapling spiders not available ({e}). Install with: pip install 'scraperx[stealth]'"
        ) from e


def _host_ok(host: str, domains: set[str]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _stats_dict(stats: Any) -> dict[str, Any]:
    for name in ("to_dict", "as_dict", "dict"):
        fn = getattr(stats, name, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:  # noqa: BLE001
                break
    try:
        return {k: v for k, v in vars(stats).items() if not k.startswith("_")}
    except TypeError:
        return {"repr": repr(stats)}


def _run_blocking(fn: Any) -> Any:
    """Spider.start() calls anyio.run, which refuses to run inside a live loop: hop to a worker thread then."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result()


def _proxy_rotator(proxies: Sequence[Any]) -> Any:
    for path in ("scrapling.engines.toolbelt", "scrapling.engines.toolbelt.proxy_rotation", "scrapling.fetchers"):
        try:
            mod = __import__(path, fromlist=["ProxyRotator"])
            return mod.ProxyRotator(list(proxies))
        except (ImportError, AttributeError):
            continue
    raise ScraplingNotAvailable("ProxyRotator not found in the installed scrapling")


def crawl(
    start_urls: str | Sequence[str],
    *,
    fields: Mapping[str, str] | None = None,
    follow: bool = True,
    link_css: str = "a::attr(href)",
    max_pages: int = 25,
    same_domain: bool = True,
    allowed_domains: Sequence[str] | None = None,
    concurrency: int = 4,
    per_domain_concurrency: int = 2,
    delay: float = 0.0,
    autothrottle: bool = True,
    obey_robots: bool = True,
    stealth: bool = False,
    stealth_kwargs: Mapping[str, Any] | None = None,
    impersonate: str = "chrome",
    proxies: Sequence[Any] | None = None,
    crawldir: str | None = None,
    markdown: bool = False,
    allow_private: bool = False,
    spider_cls: type | None = None,
    name: str = "scraperx-crawl",
    **spider_attrs: Any,
) -> CrawlOutcome:
    """Crawl from ``start_urls`` and return items + stats. See the module docstring for the contract."""
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    sp = _scrapling_spiders()
    if not hasattr(sp.Spider, "autothrottle_enabled") or not hasattr(sp.Spider, "robots_txt_obey"):
        # Silently ignoring autothrottle/robots would make a "polite" crawl impolite. Refuse instead.
        raise ScraplingNotAvailable("crawl() needs scrapling>=0.5 (AutoThrottle + robots.txt support): pip install -U 'scraperx[crawl]'")
    urls = [start_urls] if isinstance(start_urls, str) else list(start_urls)

    refused: list[str] = []
    safe: list[str] = []
    for u in urls:
        why = _check_ssrf(u, allow_private=allow_private)
        if why:
            refused.append(f"{u}: {why}")
        else:
            safe.append(u)
    if not safe:
        return CrawlOutcome(refused=refused)
    safe = safe[:max_pages]

    domains = {d.lower() for d in (allowed_domains or [])}
    if not domains and same_domain:
        domains = {(urlparse(u).hostname or "").lower() for u in safe}
    domains.discard("")

    queued: set[str] = {urldefrag(u)[0] for u in safe}
    multi = {k[:-2]: v for k, v in (fields or {}).items() if k.endswith("[]")}
    single = {k: v for k, v in (fields or {}).items() if not k.endswith("[]")}

    async def parse(self: Any, response: Any):  # noqa: ANN202
        item: dict[str, Any] = {"url": str(response.url), "status": response.status}
        for key, sel in single.items():
            val = response.css(sel).get()
            item[key] = None if val is None else str(val)
        for key, sel in multi.items():
            item[key] = [str(v) for v in response.css(sel).getall()]
        if markdown:
            try:
                item["markdown"] = response.markdown(main_content_only=True)
            except Exception as e:  # noqa: BLE001 - missing markdownify must not kill the crawl
                item["markdown"] = None
                item["markdown_error"] = f"{type(e).__name__}: {e}"
        yield item
        if not follow:
            return
        for href in response.css(link_css).getall():
            if len(queued) >= max_pages:
                return
            target = urldefrag(urljoin(str(response.url), str(href)))[0]
            if not target or target in queued:
                continue
            p = urlparse(target)
            if p.scheme not in ("http", "https"):
                continue
            if domains and not _host_ok(p.hostname or "", domains):
                continue
            why = _check_ssrf(target, allow_private=allow_private)
            if why:
                refused.append(f"{target}: {why}")
                continue
            queued.add(target)
            yield response.follow(target)

    def configure_sessions(self: Any, manager: Any) -> None:
        from scrapling.fetchers import AsyncStealthySession, FetcherSession

        rotator = _proxy_rotator(proxies) if proxies else None
        if stealth:
            kw: dict[str, Any] = {"headless": True, "network_idle": True, "solve_cloudflare": True}
            kw.update(dict(stealth_kwargs or {}))
            if rotator is not None:
                kw["proxy_rotator"] = rotator
            manager.add("stealth", AsyncStealthySession(**kw))  # spiders are async: the sync class has no __aenter__
        else:
            kw = {"impersonate": impersonate}
            if rotator is not None:
                kw["proxy_rotator"] = rotator
            manager.add("default", FetcherSession(**kw))

    attrs: dict[str, Any] = {
        "name": name,
        "start_urls": safe,
        # VERIFIED upstream quirk (scrapling 0.5, engine._is_domain_allowed): the engine compares allowed_domains with
        # request.domain = host:PORT. A bare hostname therefore marks every link on a non-default port as OFFSITE
        # (stats.offsite_requests_count) and the crawl silently stops after the start page. Give the engine host AND
        # host:port; our own parse() filter stays hostname-based.
        "allowed_domains": set(domains) | {urlparse(u).netloc.lower() for u in safe if urlparse(u).port},
        "concurrent_requests": concurrency,
        "concurrent_requests_per_domain": per_domain_concurrency,
        "download_delay": delay,
        "robots_txt_obey": obey_robots,
        "autothrottle_enabled": autothrottle,
        "logging_level": logging.WARNING,
    }
    attrs.update(spider_attrs)
    base = spider_cls or sp.Spider
    if spider_cls is None:
        attrs["parse"] = parse
    if stealth or proxies or impersonate != "chrome":
        attrs["configure_sessions"] = configure_sessions

    spider = type("ScraperxCrawl", (base,), attrs)(crawldir=crawldir) if crawldir else type("ScraperxCrawl", (base,), attrs)()
    result = _run_blocking(spider.start)
    return CrawlOutcome(
        items=[dict(i) for i in result.items],
        stats=_stats_dict(result.stats),
        paused=bool(getattr(result, "paused", False)),
        refused=refused,
        scheduled=len(queued),
    )
