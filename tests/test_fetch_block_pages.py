"""smart_fetch success criteria (2026-10-28): HTTP >= 400 and vendor block pages are failures, a browser 404 ends the
cascade, and the jina leg runs only with a key. Network-free: legs are monkeypatched and the SSRF guard (which needs
DNS) is stubbed, so the file also runs under `unshare -n`."""

from __future__ import annotations

import pytest

from scraperx import fetch as fetch_mod
from scraperx.fetch import detect_block_page, smart_fetch


@pytest.fixture(autouse=True)
def _no_dns_no_key(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_check_ssrf", lambda url, allow_private=False: None)
    for name in ("JINA_API_KEY", "SCRAPERX_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _leg(content, status=200, calls=None, name=""):
    def leg(_url, _timeout):
        if calls is not None:
            calls.append(name)
        return content, status
    return leg


ARTICLE = "<html><head><title>Rug pulls explained</title></head><body>" + "<p>real text</p>" * 50 + "</body></html>"

BLOCK_PAGES = {
    "imperva": "<html><body>Request unsuccessful. Incapsula incident ID: 123000000000000000-1</body></html>",
    "cloudflare_challenge": "<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={}</script></body></html>",
    "datadome": "<html><body><iframe src=\"https://geo.captcha-delivery.com/captcha/?initialCid=x\"></iframe></body></html>",
    "perimeterx": "<html><body><div id=\"px-captcha\"></div>Press &amp; Hold</body></html>",
    "akamai": "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission. Reference&#32;&#35;18.1"
              "<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18.1</BODY></HTML>",
    "anubis": "<html><head><title>Making sure you&#39;re not a bot!</title></head>"
              "<script src=\"/.within.website/x/cmd/anubis/static/js/main.mjs\"></script></html>",
    "jina_upstream_error": "Title: \n\nURL Source: https://example.com/x\n\nWarning: Target URL returned error 403: Forbidden\n",
}


@pytest.mark.parametrize("vendor", sorted(BLOCK_PAGES))
def test_block_pages_are_named(vendor):
    assert detect_block_page(BLOCK_PAGES[vendor]) == vendor


@pytest.mark.parametrize(
    "page",
    [
        ARTICLE,
        # normal pages carry the same vendors' sensor scripts; those are not block pages
        ARTICLE.replace("</head>", '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head>'),
        ARTICLE.replace("</head>", '<script src="https://js.datadome.co/tags.js"></script></head>'),
        ARTICLE.replace("</head>", '<script src="/_Incapsula_Resource?SWJIYLWA=719d34"></script></head>'),
        ARTICLE.replace("</head>", '<script src="https://client.perimeterx.net/PXabc/main.min.js"></script></head>'),
        # an article that talks about challenge pages in its text
        ARTICLE.replace("real text", "Cloudflare shows 'Just a moment...' while it checks the browser"),
        "",
    ],
)
def test_normal_pages_are_not_block_pages(page):
    assert detect_block_page(page) is None


def test_block_page_with_200_falls_through_and_is_not_cached(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _leg(BLOCK_PAGES["imperva"], 200))
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _leg(ARTICLE, 200))
    r = smart_fetch("https://example.com/a", prefer="urllib", adaptive=False, db_path=db)
    assert r.ok and r.mode_used == "playwright" and r.content == ARTICLE
    assert ("urllib", "block page: imperva") in r.errors
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _leg(BLOCK_PAGES["imperva"], 200))
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _leg(BLOCK_PAGES["imperva"], 200))
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _leg(BLOCK_PAGES["imperva"], 200))
    r2 = smart_fetch("https://example.com/b", prefer="urllib", adaptive=False, db_path=db)
    assert not r2.ok and fetch_mod._cache_get("https://example.com/b", db_path=db) is None


def test_browser_403_is_a_failure_and_the_next_leg_runs(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _leg("<html>403 Forbidden</html>", 403, calls, "playwright"))
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _leg(ARTICLE, 200, calls, "stealth"))
    r = smart_fetch("https://example.com/c", prefer="playwright", adaptive=False, db_path=str(tmp_path / "t.db"))
    assert calls == ["playwright", "stealth"] and r.mode_used == "scrapling_stealth"
    assert ("playwright", "HTTP 403") in r.errors


def test_browser_404_ends_the_cascade(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _leg("<html>Not Found</html>", 404, calls, "playwright"))
    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _leg(ARTICLE, 200, calls, "stealth"))
    r = smart_fetch("https://example.com/d", prefer="playwright", adaptive=False, db_path=str(tmp_path / "t.db"))
    assert calls == ["playwright"] and not r.ok and r.http_status == 404


def test_jina_without_a_key_is_skipped_and_not_recorded(tmp_path, monkeypatch):
    recorded: list[tuple] = []
    monkeypatch.setattr(fetch_mod, "_telemetry_record", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _leg(ARTICLE, 200))
    r = smart_fetch("https://example.com/e", adaptive=False, db_path=str(tmp_path / "t.db"))
    assert r.mode_used == "urllib"
    assert r.errors and r.errors[0][0] == "jina" and r.errors[0][1].startswith("skipped: no Jina key")
    assert [a[1] for a in recorded] == ["urllib"]  # the skipped leg says nothing about the site


def test_jina_with_a_key_sends_bearer(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"# Title\n\nbody"

    def fake_urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setenv("JINA_API_KEY", "jina_test_key")
    monkeypatch.setattr(fetch_mod, "urlopen", fake_urlopen)
    content, status = fetch_mod._fetch_jina("https://example.com/f", 10)
    assert content.startswith("# Title") and status is None
    assert seen["auth"] == "Bearer jina_test_key" and seen["url"] == "https://r.jina.ai/https://example.com/f"
