"""scraperx.site_crawl against a real local site over curl_cffi HTTP (no browser). Needs scrapling>=0.5 (skips otherwise).

The site is on a RANDOM NON-DEFAULT PORT on purpose: that is exactly the case where upstream's allowed_domains check
(host:port) marks every link offsite, which silently ended the crawl after the start page before crawl() worked around it.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("scrapling.fetchers")
sp = pytest.importorskip("scrapling.spiders")
if not hasattr(sp.Spider, "autothrottle_enabled"):
    pytest.skip("crawl() needs a Spider with AutoThrottle + robots.txt", allow_module_level=True)

import scraperx.site_crawl as crawl_mod  # noqa: E402
from scraperx.site_crawl import crawl  # noqa: E402
from scraperx.scrapling_stealth import ScraplingNotAvailable  # noqa: E402

PAGES = {
    "/": '<html><body><h1>Home</h1><a href="/a">A</a><a href="/b">B</a><a href="/a#frag">A again</a>'
         '<a href="http://example.org/off">off-site</a><a href="mailto:x@y.z">m</a>'
         '<a href="http://metadata.internal/latest">meta</a></body></html>',
    "/a": '<html><body><h1>A</h1><a href="/">home</a><a href="/c">C</a></body></html>',
    "/b": "<html><body><h1>B</h1></body></html>",
    "/c": "<html><body><h1>C</h1></body></html>",
}


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/robots.txt":
            body, code = b"User-agent: *\nAllow: /\n", 200
        elif path in PAGES:
            body, code = PAGES[path].encode(), 200
        else:
            body, code = b"nope", 404
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if path != "/robots.txt" else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: D401
        pass


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _titles(out):
    return sorted(i["title"] for i in out.items)


def test_loopback_is_refused_by_default(site):
    out = crawl(site + "/", fields={"title": "h1::text"})
    assert out.items == [] and out.refused and "private/loopback" in out.refused[0]


def test_crawl_stays_on_site_dedupes_fragments_and_drops_offsite_and_mailto(site, monkeypatch):
    # The metadata link is on the SAME crawl; same_domain keeps it out, the guard would too.
    out = crawl(site + "/", fields={"title": "h1::text"}, allow_private=True, autothrottle=False, max_pages=10)
    assert _titles(out) == ["A", "B", "C", "Home"]
    assert out.stats["failed_requests_count"] == 0
    assert out.stats["offsite_requests_count"] == 0, "the port quirk must be neutralised by crawl()"


def test_max_pages_is_a_hard_cap_counted_at_schedule_time(site):
    out = crawl(site + "/", fields={"title": "h1::text"}, allow_private=True, autothrottle=False, max_pages=2)
    assert len(out.items) == 2 and out.scheduled == 2


def test_follow_false_fetches_only_the_start_page_and_list_fields_work(site):
    out = crawl(site + "/", fields={"title": "h1::text", "hrefs[]": "a::attr(href)"}, follow=False,
                allow_private=True, autothrottle=False)
    assert len(out.items) == 1 and out.items[0]["title"] == "Home"
    assert "/a" in out.items[0]["hrefs"] and len(out.items[0]["hrefs"]) == 6


def test_every_followed_link_goes_through_the_ssrf_guard_and_refusals_are_reported(site, monkeypatch):
    real = crawl_mod._check_ssrf
    monkeypatch.setattr(crawl_mod, "_check_ssrf",
                        lambda url, allow_private=False: "blocked by test guard" if "metadata.internal" in url else None)
    out = crawl(site + "/", fields={"title": "h1::text"}, same_domain=False, max_pages=10, autothrottle=False)
    assert any("metadata.internal" in r and "blocked by test guard" in r for r in out.refused), out.refused
    assert out.stats["failed_requests_count"] == 0, "the refused URL must never have been requested"
    assert real("http://127.0.0.1/", allow_private=False), "positive control: the real guard refuses loopback"


def test_works_from_inside_a_running_event_loop(site):
    async def inner():
        return crawl(site + "/b", fields={"title": "h1::text"}, follow=False, allow_private=True, autothrottle=False)

    out = asyncio.run(inner())
    assert out.items[0]["title"] == "B"


def test_refuses_to_run_an_impolite_crawl_on_old_scrapling(monkeypatch):
    class OldSpider:  # no autothrottle_enabled / robots_txt_obey
        pass

    class OldModule:
        Spider = OldSpider

    monkeypatch.setattr(crawl_mod, "_scrapling_spiders", lambda: OldModule)
    with pytest.raises(ScraplingNotAvailable, match="AutoThrottle"):
        crawl("https://example.com")


def test_bad_arguments_fail_loudly():
    with pytest.raises(ValueError):
        crawl("https://example.com", max_pages=0)
