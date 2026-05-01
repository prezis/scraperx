"""Tests for scraperx.explorer_label — block-explorer <title> tag label extraction.

Network-free: HTTP fetcher is monkeypatched. Pure parse_title tests cover the
fixture title strings collected from real explorer pages during the cex-gate
session 2026-05-01 (verified ByBit, Coinbase, Orbiter, Binance Solana).
"""

from __future__ import annotations

import pytest

from scraperx import explorer_label as el_mod
from scraperx.explorer_label import (
    ExplorerNotSupported,
    LabelResult,
    detect_explorer,
    extract_label,
    page_title,
    parse_title,
)


# ---------------------------------------------------------------------------
# Fixture titles — captured from real explorer pages 2026-05-01
# ---------------------------------------------------------------------------

# (url, raw_title, expected_label, expected_exchange_guess, expected_chain)
FIXTURES = [
    (
        "https://etherscan.io/address/0xf89d7b9c864f589bbF53a82105107622B35EaA40",
        "ByBit: Hot Wallet 5 | Address 0xf89d7b9c864f589bbF53a82105107622B35EaA40 | Etherscan",
        "ByBit: Hot Wallet 5",
        "bybit",
        "ethereum",
    ),
    (
        "https://etherscan.io/address/0x71660c4005BA85c37ccec55d0C4493E66Fe775d3",
        "Coinbase 4 | Address 0x71660c4005BA85c37ccec55d0C4493E66Fe775d3 | Etherscan",
        "Coinbase 4",
        "coinbase",
        "ethereum",
    ),
    (
        "https://bscscan.com/address/0x80c67432656d59144cefe41b748ecbe6c2e8b3e1",
        "Orbiter Finance: Bridge | Address 0x80c67432656d59144cefe41b748ecbe6c2e8b3e1 | BscScan",
        "Orbiter Finance: Bridge",
        "orbiter",
        "bsc",
    ),
    (
        "https://solscan.io/account/5tzFkiKscXHK5ZXCGbXbxzdEWZJ3LdZQjRJqJfXbGvU3",
        "Binance 2 | Account 5tzFkiKscXHK5ZXCGbXbxzdEWZJ3LdZQjRJqJfXbGvU3 | Solscan",
        "Binance 2",
        "binance",
        "solana",
    ),
    (
        "https://basescan.org/address/0x4200000000000000000000000000000000000006",
        "WETH Token | Address 0x4200000000000000000000000000000000000006 | BaseScan",
        "WETH Token",
        None,
        "base",
    ),
]


# ---------------------------------------------------------------------------
# parse_title — pure, offline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,title,expected_label,expected_guess,_chain", FIXTURES)
def test_parse_title_extracts_label(url, title, expected_label, expected_guess, _chain):
    res = parse_title(title)
    assert res.label == expected_label, f"label mismatch for {url}"
    assert res.exchange_guess == expected_guess, f"exchange_guess mismatch for {url}"
    assert res.title == title


def test_parse_title_empty_string():
    res = parse_title("")
    assert res.label is None
    assert res.exchange_guess is None
    assert res.ok is False


def test_parse_title_generic_first_segment_returns_no_label():
    """When the first segment is a generic context word, the explorer hasn't
    tagged this address — we honestly return label=None instead of guessing."""
    title = "Address 0xabc...def | Etherscan"
    res = parse_title(title)
    assert res.label is None
    assert res.ok is False


def test_parse_title_solscan_landing_page():
    res = parse_title("Solscan | Solana Explorer")
    assert res.label is None


def test_parse_title_carries_address_argument():
    res = parse_title("ByBit: Hot Wallet 5 | Address 0xf89d... | Etherscan", address="0xf89d")
    assert res.address == "0xf89d"
    assert res.label == "ByBit: Hot Wallet 5"


def test_parse_title_html_entities_decoded():
    # Real-world: some explorers HTML-encode "&" in labels.
    res = parse_title("Foo &amp; Bar | Address 0xabc | Etherscan")
    assert res.label == "Foo & Bar"


def test_parse_title_exchange_guess_case_insensitive():
    res = parse_title("BINANCE 14 | Address 0xabc | Etherscan")
    assert res.exchange_guess == "binance"


def test_parse_title_huobi_aliases_to_htx():
    res = parse_title("Huobi: Cold Wallet | Address 0xabc | Etherscan")
    assert res.exchange_guess == "htx"


# ---------------------------------------------------------------------------
# detect_explorer — host classification
# ---------------------------------------------------------------------------


def test_detect_explorer_etherscan_static():
    host, chain, kind = detect_explorer("https://etherscan.io/address/0xabc")
    assert host == "etherscan.io"
    assert chain == "ethereum"
    assert kind == "static"


def test_detect_explorer_solscan_dynamic():
    host, chain, kind = detect_explorer("https://solscan.io/account/abc")
    assert host == "solscan.io"
    assert chain == "solana"
    assert kind == "dynamic"


def test_detect_explorer_unknown_host():
    host, chain, kind = detect_explorer("https://example.com/")
    assert kind == "unknown"
    assert chain is None


def test_detect_explorer_bad_url():
    host, chain, kind = detect_explorer("not a url at all")
    assert kind == "unknown"


def test_detect_explorer_supports_full_etherscan_family():
    """Smoke check that the major Etherscan-family hosts all classify as static."""
    for host in [
        "etherscan.io",
        "basescan.org",
        "bscscan.com",
        "polygonscan.com",
        "arbiscan.io",
        "optimistic.etherscan.io",
        "snowtrace.io",
        "ftmscan.com",
        "tronscan.org",
    ]:
        _, _, kind = detect_explorer(f"https://{host}/address/0xabc")
        assert kind == "static", f"{host} should be static-render"


# ---------------------------------------------------------------------------
# Address extraction from URL path
# ---------------------------------------------------------------------------


def test_extract_address_from_evm_url():
    addr = el_mod._extract_address_from_url(
        "https://etherscan.io/address/0xf89d7b9c864f589bbF53a82105107622B35EaA40",
        "ethereum",
    )
    assert addr == "0xf89d7b9c864f589bbF53a82105107622B35EaA40"


def test_extract_address_from_solana_url():
    addr = el_mod._extract_address_from_url(
        "https://solscan.io/account/5tzFkiKscXHK5ZXCGbXbxzdEWZJ3LdZQjRJqJfXbGvU3",
        "solana",
    )
    assert addr == "5tzFkiKscXHK5ZXCGbXbxzdEWZJ3LdZQjRJqJfXbGvU3"


def test_extract_address_from_url_missing_returns_none():
    addr = el_mod._extract_address_from_url("https://etherscan.io/", "ethereum")
    assert addr is None


# ---------------------------------------------------------------------------
# Title regex extraction from raw HTML
# ---------------------------------------------------------------------------


def test_title_from_html_basic():
    html = "<html><head><title>Hello | World</title></head></html>"
    assert el_mod._title_from_html(html) == "Hello | World"


def test_title_from_html_attributes_on_title_tag():
    html = '<title data-foo="bar">Coinbase 4 | Address 0xabc | Etherscan</title>'
    assert el_mod._title_from_html(html) == "Coinbase 4 | Address 0xabc | Etherscan"


def test_title_from_html_collapses_whitespace():
    html = "<title>\n  ByBit: Hot Wallet 5\n  | Address\n</title>"
    assert el_mod._title_from_html(html) == "ByBit: Hot Wallet 5 | Address"


def test_title_from_html_missing():
    assert el_mod._title_from_html("<html><body>no title</body></html>") is None


# ---------------------------------------------------------------------------
# extract_label — high-level (HTTP layer monkeypatched)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,title,expected_label,expected_guess,expected_chain", FIXTURES)
def test_extract_label_static_explorers(monkeypatch, url, title, expected_label, expected_guess, expected_chain):
    """Patch both fetchers so the test never touches the network."""

    def fake_static(u, timeout):
        return title

    def fake_dynamic(u, timeout):
        return title

    monkeypatch.setattr(el_mod, "_fetch_static_title", fake_static)
    monkeypatch.setattr(el_mod, "_fetch_dynamic_title", fake_dynamic)

    res = extract_label(url, timeout=5)
    assert isinstance(res, LabelResult)
    assert res.title == title
    assert res.label == expected_label
    assert res.exchange_guess == expected_guess
    assert res.chain == expected_chain
    assert res.url == url
    assert res.source in {"static", "playwright"}
    if expected_chain == "solana":
        assert res.source == "playwright"
    else:
        assert res.source == "static"


def test_extract_label_unknown_host_raises(monkeypatch):
    with pytest.raises(ExplorerNotSupported):
        extract_label("https://example.com/foo")


def test_page_title_static_calls_static_fetcher(monkeypatch):
    called = {}

    def fake_static(u, timeout):
        called["url"] = u
        called["timeout"] = timeout
        return "fake title"

    monkeypatch.setattr(el_mod, "_fetch_static_title", fake_static)
    out = page_title("https://etherscan.io/address/0xabc", timeout=7)
    assert out == "fake title"
    assert called["url"].startswith("https://etherscan.io")
    assert called["timeout"] == 7


def test_page_title_dynamic_uses_playwright(monkeypatch):
    called = {}

    def fake_dynamic(u, timeout):
        called["dyn"] = True
        return "Binance 2 | Account abc | Solscan"

    def fake_static(u, timeout):  # should NOT be called for dynamic hosts
        called["static"] = True
        return "wrong"

    monkeypatch.setattr(el_mod, "_fetch_static_title", fake_static)
    monkeypatch.setattr(el_mod, "_fetch_dynamic_title", fake_dynamic)
    out = page_title("https://solscan.io/account/abc", timeout=5)
    assert out.startswith("Binance 2")
    assert called.get("dyn") is True
    assert "static" not in called


def test_label_result_ok_property():
    assert LabelResult(url="x").ok is False
    assert LabelResult(url="x", label="ByBit: Hot Wallet 5").ok is True
