"""Tests for scraperx.explorer_label.extract_chips — Etherscan chip metadata parsing.

Pattern source: cex-gate session 2026-05-01 nest_ranker investigation
(Etherscan chip metadata = primary per-address classification signal).

Fixtures live in tests/fixtures/explorer_chips/ and are trimmed-but-real chip
HTML captured 2026-05-01 via Playwright MCP (Cloudflare 403-bypassed). They
contain only public on-chain label data — safe to commit.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from scraperx import explorer_label as el_mod
from scraperx.explorer_label import LabelResult, extract_chips, extract_label

FIXTURES = Path(__file__).parent / "fixtures" / "explorer_chips"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# extract_chips — pure parser, real-fixture HTML
# ---------------------------------------------------------------------------


def test_chips_coinbase_deposit_includes_deposit_address_not_exchange():
    """Coinbase Deposit fixture: chips are entity ('Coinbase') + ('Deposit Address').
    Must NOT include 'Exchange' — Etherscan does not chip Coinbase deposit
    addresses with 'Exchange'."""
    chips = extract_chips(_read("coinbase_deposit.html"))
    assert "Deposit Address" in chips
    assert "Exchange" not in chips
    assert "Coinbase" in chips


def test_chips_gate_deposit_includes_exchange_and_hashtag_gate():
    """Gate Deposit fixture: chips include 'Exchange' (entity-type) AND '#Gate'
    (hashtag chip — Etherscan anchors it to /accounts/label/gate, so we prefix '#')."""
    chips = extract_chips(_read("gate_deposit.html"))
    assert "Exchange" in chips
    assert "#Gate" in chips
    # Order preserved: Exchange first, then #Gate.
    assert chips == ("Exchange", "#Gate")


def test_chips_funder_fixture_includes_funder():
    chips = extract_chips(_read("funder.html"))
    assert "Funder" in chips
    assert chips == ("Funder",)


def test_chips_binance_hot_wallet_includes_exchange():
    """Binance Hot Wallet 20 (Etherscan): chips are 'Binance' + 'Exchange'.
    'Hot Wallet' lives in the title, not in the chip strip."""
    chips = extract_chips(_read("binance_hot_wallet.html"))
    assert "Exchange" in chips
    assert "Binance" in chips
    assert "Hot Wallet" not in chips
    assert "#Binance" not in chips  # Etherscan does NOT anchor Binance


def test_chips_unlabeled_eoa_returns_empty_tuple():
    chips = extract_chips(_read("unlabeled_eoa.html"))
    assert chips == ()
    assert isinstance(chips, tuple)


def test_chips_bscscan_anchors_more_chips_than_etherscan():
    """BscScan tags BOTH 'Binance' and 'Exchange' as hashtag-anchors —
    different from Etherscan. The href is the only reliable hashtag signal."""
    chips = extract_chips(_read("bscscan_binance.html"))
    assert "#Binance" in chips
    assert "#Exchange" in chips
    assert "Deposit Address" in chips  # plain span — no '#' prefix
    # Order: #Binance, #Exchange, Deposit Address
    assert chips == ("#Binance", "#Exchange", "Deposit Address")


def test_chips_empty_html_returns_empty_tuple():
    assert extract_chips("") == ()
    assert extract_chips("<html><body>no chips here</body></html>") == ()


def test_chips_returns_tuple_not_list():
    """Type contract: chips field is a tuple (immutable, hashable, dataclass-safe)."""
    chips = extract_chips(_read("gate_deposit.html"))
    assert isinstance(chips, tuple)


# ---------------------------------------------------------------------------
# LabelResult.chips field — back-compat + dataclass integration
# ---------------------------------------------------------------------------


def test_label_result_chips_default_is_empty_tuple():
    """Backward compat: existing callers that construct LabelResult without
    chips see chips == () — no AttributeError, no surprise."""
    res = LabelResult(url="x")
    assert res.chips == ()
    assert isinstance(res.chips, tuple)


def test_label_result_chips_populated_via_extract_label(monkeypatch):
    """End-to-end: extract_label() on an Etherscan URL populates .chips from
    the same HTML that supplied .title. HTTP layer monkeypatched."""
    html = _read("gate_deposit.html")

    def fake_static_title_and_html(url, timeout):
        return "Gate Deposit: 0x1c4b... | Address: 0x... | Etherscan", html

    monkeypatch.setattr(el_mod, "_fetch_static_title_and_html", fake_static_title_and_html)
    res = extract_label("https://etherscan.io/address/0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c", timeout=5)
    assert res.chips == ("Exchange", "#Gate")
    assert res.label == "Gate Deposit: 0x1c4b..."  # title still parsed


def test_extract_label_back_compat_callers_can_ignore_chips(monkeypatch):
    """Existing consumers that don't read .chips still work end-to-end:
    label, exchange_guess, chain all populate as before."""

    def fake_static_title_and_html(url, timeout):
        return "Coinbase 4 | Address 0x71660c4 | Etherscan", _read("coinbase_deposit.html")

    monkeypatch.setattr(el_mod, "_fetch_static_title_and_html", fake_static_title_and_html)
    res = extract_label("https://etherscan.io/address/0x71660c4005BA85c37ccec55d0C4493E66Fe775d3", timeout=5)
    # Assertions identical to pre-chip behavior:
    assert res.label == "Coinbase 4"
    assert res.exchange_guess == "coinbase"
    assert res.chain == "ethereum"
    assert res.source == "static"
    # Bonus: chips present but caller not forced to read them.
    assert isinstance(res.chips, tuple)


# ---------------------------------------------------------------------------
# CLI — `scraperx chips <url>`
# ---------------------------------------------------------------------------


def test_cli_chips_emits_one_per_line(monkeypatch, capsys):
    """`scraperx chips <url>` prints chips one per line, no JSON, no chrome."""
    from scraperx.__main__ import main

    def fake_static_title_and_html(url, timeout):
        return "Gate Deposit: 0x1c4b... | Address: 0x... | Etherscan", _read("gate_deposit.html")

    monkeypatch.setattr(el_mod, "_fetch_static_title_and_html", fake_static_title_and_html)
    monkeypatch.setattr(
        sys,
        "argv",
        ["scraperx", "chips", "https://etherscan.io/address/0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c"],
    )
    main()
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert lines == ["Exchange", "#Gate"]


def test_cli_chips_unlabeled_eoa_prints_nothing(monkeypatch, capsys):
    from scraperx.__main__ import main

    def fake_static_title_and_html(url, timeout):
        return "Address: 0xabcdef01... | Etherscan", _read("unlabeled_eoa.html")

    monkeypatch.setattr(el_mod, "_fetch_static_title_and_html", fake_static_title_and_html)
    monkeypatch.setattr(
        sys,
        "argv",
        ["scraperx", "chips", "https://etherscan.io/address/0xabcdef0123456789abcdef0123456789abcdef01"],
    )
    main()
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
