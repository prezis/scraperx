"""Tests for scraperx.reverse_image — multi-engine URL fan-out + SSRF guard."""

from __future__ import annotations

from urllib.parse import quote

import pytest

from scraperx.reverse_image import (
    DEFAULT_ENGINES,
    ENGINES,
    EngineHit,
    _looks_like_private_host,
    build_search_url,
    reverse_image_search,
)


# ---------------------------------------------------------------------------
# Templates / engine list
# ---------------------------------------------------------------------------


def test_default_engines_match_engine_keys():
    """Every engine in DEFAULT_ENGINES must have a corresponding template."""
    for eng in DEFAULT_ENGINES:
        assert eng in ENGINES, f"DEFAULT_ENGINES references unknown engine: {eng}"


def test_engine_templates_carry_q_placeholder():
    for eng, tpl in ENGINES.items():
        assert "{q}" in tpl, f"Template for {eng} missing {{q}}"


def test_engine_templates_use_real_ampersands_not_html_entities():
    """Guard regression: a copy-paste from HTML can leave &amp; in templates."""
    for eng, tpl in ENGINES.items():
        assert "&amp;" not in tpl, f"{eng} template contains HTML-entity &amp;"


# ---------------------------------------------------------------------------
# build_search_url
# ---------------------------------------------------------------------------


def test_build_search_url_yandex():
    out = build_search_url("yandex", "https://example.com/avatar.jpg")
    assert out.startswith("https://yandex.com/images/search?rpt=imageview&url=")
    assert quote("https://example.com/avatar.jpg", safe="") in out


def test_build_search_url_unknown_engine():
    with pytest.raises(ValueError):
        build_search_url("notreal", "https://example.com/x.jpg")


def test_build_search_url_rejects_non_http():
    with pytest.raises(ValueError):
        build_search_url("yandex", "ftp://example.com/x.jpg")


def test_build_search_url_rejects_loopback():
    with pytest.raises(ValueError):
        build_search_url("yandex", "http://127.0.0.1/x.jpg")


def test_build_search_url_rejects_localhost():
    with pytest.raises(ValueError):
        build_search_url("yandex", "http://localhost:8080/x.jpg")


@pytest.mark.parametrize(
    "host",
    ["10.0.0.5", "192.168.1.10", "172.16.5.5", "172.31.255.255", "169.254.169.254"],
)
def test_looks_like_private_host_ipv4(host):
    assert _looks_like_private_host(f"https://{host}/x.jpg")


@pytest.mark.parametrize(
    "host",
    ["172.15.0.1", "172.32.0.1", "8.8.8.8", "example.com", "1.1.1.1"],
)
def test_looks_like_private_host_public(host):
    assert not _looks_like_private_host(f"https://{host}/x.jpg")


# ---------------------------------------------------------------------------
# reverse_image_search (no fetch — fast)
# ---------------------------------------------------------------------------


def test_reverse_image_search_default_engines():
    hits = reverse_image_search("https://example.com/avatar.jpg", fetch=False)
    assert len(hits) == len(DEFAULT_ENGINES)
    by_engine = {h.engine: h for h in hits}
    assert all(by_engine[e].ok for e in DEFAULT_ENGINES)
    # Engines preserve the requested order
    assert [h.engine for h in hits] == list(DEFAULT_ENGINES)


def test_reverse_image_search_engine_subset():
    hits = reverse_image_search(
        "https://example.com/avatar.jpg",
        engines=["yandex", "tineye"],
        fetch=False,
    )
    assert len(hits) == 2
    assert {h.engine for h in hits} == {"yandex", "tineye"}


def test_reverse_image_search_unknown_engine_records_error():
    hits = reverse_image_search(
        "https://example.com/x.jpg",
        engines=["yandex", "fake-engine"],
        fetch=False,
    )
    assert len(hits) == 2
    fake = next(h for h in hits if h.engine == "fake-engine")
    assert not fake.ok
    assert fake.error
    assert fake.search_url == ""


def test_reverse_image_search_image_url_encoded():
    weird = "https://example.com/path with space?q=1"
    hits = reverse_image_search(weird, engines=["yandex"], fetch=False)
    # Spaces and ? must be percent-encoded in the embedded URL
    encoded = quote(weird, safe="")
    assert encoded in hits[0].search_url
    assert " " not in hits[0].search_url


def test_reverse_image_search_blocks_private_host():
    hits = reverse_image_search("http://127.0.0.1/x.jpg", engines=["yandex"], fetch=False)
    assert len(hits) == 1
    assert not hits[0].ok
    assert "private" in hits[0].error.lower()


# ---------------------------------------------------------------------------
# fetch=True (smart_fetch monkeypatched)
# ---------------------------------------------------------------------------


def test_reverse_image_search_fetch_uses_smart_fetch(monkeypatch):
    from scraperx import fetch as fetch_mod

    captured: list[str] = []

    def fake_smart_fetch(url, **kwargs):
        captured.append(url)
        return fetch_mod.FetchResult(url=url, content="<html>ok</html>", mode_used="urllib")

    monkeypatch.setattr(fetch_mod, "smart_fetch", fake_smart_fetch)

    hits = reverse_image_search(
        "https://example.com/avatar.jpg",
        engines=["yandex"],
        fetch=True,
    )
    assert len(hits) == 1
    assert hits[0].body == "<html>ok</html>"
    assert len(captured) == 1
