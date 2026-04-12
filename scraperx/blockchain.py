"""
Blockchain Explorer Scraper — headless Playwright scraping for Basescan & DexScreener.

Parses DOM snapshots from blockchain explorers to extract on-chain data
without requiring any API keys.

Usage:
    from scraperx.blockchain import scrape_basescan_address, scrape_dexscreener_token
    info = scrape_basescan_address("0x1234...")
    token = scrape_dexscreener_token("0x1234...")

Requires: playwright (optional dependency)
    pip install playwright && playwright install chromium
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Ethereum address pattern (0x + 40 hex chars)
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class PlaywrightNotAvailable(RuntimeError):
    """Raised when Playwright is not installed or browsers are missing."""
    pass


@dataclass
class BasescanAddress:
    """Parsed Basescan address data."""
    address: str
    is_contract: bool = False
    eth_balance: str = ""
    eth_value_usd: str = ""
    token_holdings_count: int = 0
    transaction_count: int = 0
    contract_creator: str = ""
    contract_name: str = ""
    source_method: str = "basescan-playwright"


@dataclass
class DexScreenerToken:
    """Parsed DexScreener token data."""
    address: str
    name: str = ""
    symbol: str = ""
    price: str = ""
    price_change_24h: str = ""
    liquidity: str = ""
    volume_24h: str = ""
    market_cap: str = ""
    fdv: str = ""
    pair_count: int = 0
    source_method: str = "dexscreener-playwright"


def _validate_address(address: str) -> str:
    """Validate and normalize an Ethereum address."""
    address = address.strip()
    if not ADDRESS_RE.match(address):
        raise ValueError(f"Invalid Ethereum address: {address!r}")
    return address


def _get_playwright():
    """Import and return playwright sync API. Raises PlaywrightNotAvailable on failure."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise PlaywrightNotAvailable(
            "Playwright is not installed. Install with: "
            "pip install playwright && playwright install chromium"
        )


def _launch_browser(playwright):
    """Launch a headless Chromium browser with stealth-like settings."""
    try:
        browser = playwright.chromium.launch(headless=True)
    except Exception as e:
        raise PlaywrightNotAvailable(
            f"Failed to launch Chromium (run 'playwright install chromium'): {e}"
        )
    return browser


def _parse_number_text(text: str) -> str:
    """Clean up number text from DOM (strip whitespace, normalize)."""
    if not text:
        return ""
    return text.strip().replace("\xa0", " ")


def scrape_basescan_address(address: str, *, timeout: int = 30_000) -> BasescanAddress:
    """Scrape Basescan for address information using headless Playwright.

    Args:
        address: Ethereum address (0x...).
        timeout: Page load timeout in milliseconds.

    Returns:
        BasescanAddress with parsed on-chain data.

    Raises:
        ValueError: If address format is invalid.
        PlaywrightNotAvailable: If Playwright is not installed.
        RuntimeError: If scraping fails.
    """
    address = _validate_address(address)
    sync_playwright = _get_playwright()

    url = f"https://basescan.org/address/{address}"
    logger.info("Scraping Basescan: %s", url)

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page.set_default_timeout(timeout)
            page.goto(url, wait_until="domcontentloaded")

            # Wait for the main content to render
            page.wait_for_selector("#ContentPlaceHolder1_divSummary", timeout=timeout)

            return _parse_basescan_dom(page, address)
        finally:
            browser.close()


def _parse_basescan_dom(page, address: str) -> BasescanAddress:
    """Extract data from a loaded Basescan address page DOM."""
    result = BasescanAddress(address=address)

    # Detect contract vs EOA: contract pages have a "Contract" tab
    contract_tab = page.query_selector("#ContentPlaceHolder1_li_contracts, a#tab-contract")
    if contract_tab is None:
        # Also check for contract-related text in the page
        page_text = page.inner_text("body")
        result.is_contract = "contract" in page_text.lower() and (
            "contract creator" in page_text.lower()
            or "contract code" in page_text.lower()
        )
    else:
        result.is_contract = True

    # ETH Balance — look for the balance section
    balance_el = page.query_selector(
        "#ContentPlaceHolder1_divSummary .card-body #ContentPlaceHolder1_divFilterBalanceByDateBal, "
        "#ContentPlaceHolder1_divSummary [data-bs-toggle] .text-muted + div, "
        "#ContentPlaceHolder1_divSummary .card-body"
    )
    if balance_el:
        balance_text = balance_el.inner_text()
        # Look for ETH balance pattern like "0.1234 ETH" or "0.1234 Ether"
        eth_match = re.search(r"([\d,]+\.?\d*)\s*(?:ETH|Ether)", balance_text)
        if eth_match:
            result.eth_balance = eth_match.group(1).replace(",", "")

        # USD value
        usd_match = re.search(r"\$\s*([\d,]+\.?\d*)", balance_text)
        if usd_match:
            result.eth_value_usd = usd_match.group(1).replace(",", "")

    # Transaction count
    txn_el = page.query_selector(
        "#ContentPlaceHolder1_divSummary a[href*='txs'], "
        "#ContentPlaceHolder1_divTxDataInfo"
    )
    if txn_el:
        txn_text = txn_el.inner_text()
        txn_match = re.search(r"([\d,]+)\s*transactions?", txn_text, re.IGNORECASE)
        if txn_match:
            result.transaction_count = int(txn_match.group(1).replace(",", ""))
    # Fallback: check the transactions tab badge
    if result.transaction_count == 0:
        txn_badge = page.query_selector("#transactions_count, #ContentPlaceHolder1_txtTxnCount")
        if txn_badge:
            txt = txn_badge.inner_text().strip()
            txn_num = re.search(r"([\d,]+)", txt)
            if txn_num:
                result.transaction_count = int(txn_num.group(1).replace(",", ""))

    # Token holdings count
    token_el = page.query_selector(
        "#ContentPlaceHolder1_tokenbalance, "
        "#dropdownMenuBalance, "
        "a[href*='tokenholdings']"
    )
    if token_el:
        token_text = token_el.inner_text()
        # Pattern: "N tokens" or just a number
        token_match = re.search(r"(\d+)\s*token", token_text, re.IGNORECASE)
        if token_match:
            result.token_holdings_count = int(token_match.group(1))

    # Contract creator (only for contracts)
    if result.is_contract:
        creator_el = page.query_selector(
            "#ContentPlaceHolder1_trContract .hash-tag, "
            "a[href*='address/0x'][data-bs-toggle='tooltip']"
        )
        if creator_el:
            creator_text = creator_el.inner_text().strip()
            if ADDRESS_RE.match(creator_text):
                result.contract_creator = creator_text

        # Contract name
        name_el = page.query_selector(
            "#ContentPlaceHolder1_divSummary .u-label--secondary, "
            ".contract-name"
        )
        if name_el:
            result.contract_name = name_el.inner_text().strip()

    logger.info(
        "Basescan parsed: contract=%s balance=%s txns=%d",
        result.is_contract, result.eth_balance, result.transaction_count,
    )
    return result


def scrape_dexscreener_token(address: str, *, timeout: int = 30_000) -> DexScreenerToken:
    """Scrape DexScreener for token information on Base chain.

    Args:
        address: Token contract address (0x...).
        timeout: Page load timeout in milliseconds.

    Returns:
        DexScreenerToken with parsed market data.

    Raises:
        ValueError: If address format is invalid.
        PlaywrightNotAvailable: If Playwright is not installed.
        RuntimeError: If scraping fails.
    """
    address = _validate_address(address)
    sync_playwright = _get_playwright()

    url = f"https://dexscreener.com/base/{address}"
    logger.info("Scraping DexScreener: %s", url)

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page.set_default_timeout(timeout)
            page.goto(url, wait_until="domcontentloaded")

            # DexScreener is an SPA — wait for price element to render
            try:
                page.wait_for_selector(
                    ".ds-dex-table-row-col-pair-price, "
                    "[class*='price'], "
                    "a[href*='/base/']",
                    timeout=timeout,
                )
            except Exception:
                logger.warning("DexScreener: price element not found, parsing available DOM")

            return _parse_dexscreener_dom(page, address)
        finally:
            browser.close()


def _parse_dexscreener_dom(page, address: str) -> DexScreenerToken:
    """Extract data from a loaded DexScreener token page DOM."""
    result = DexScreenerToken(address=address)

    body_text = page.inner_text("body")

    # Token name and symbol — usually in the header area
    header_el = page.query_selector(
        "h1, [class*='pair-name'], [class*='token-name'], "
        ".ds-dex-table-row-col-token"
    )
    if header_el:
        header_text = header_el.inner_text().strip()
        # Pattern: "TokenName (SYMBOL)" or "SYMBOL / WETH"
        name_match = re.search(r"^(.+?)\s*[(/]", header_text)
        if name_match:
            result.name = name_match.group(1).strip()
        symbol_match = re.search(r"\((\w+)\)", header_text)
        if symbol_match:
            result.symbol = symbol_match.group(1)
        elif "/" in header_text:
            result.symbol = header_text.split("/")[0].strip()

    # Price
    price_match = re.search(r"\$\s*([\d,]+\.?\d*(?:e[+-]?\d+)?)", body_text)
    if price_match:
        result.price = "$" + price_match.group(1)

    # Liquidity
    liq_match = re.search(r"(?:Liquidity|LIQ)[:\s]*\$?\s*([\d,.]+[KMBkmb]?)", body_text)
    if liq_match:
        result.liquidity = liq_match.group(1)

    # 24h Volume
    vol_match = re.search(r"(?:Volume|VOL)\s*(?:\(24[Hh]\))?[:\s]*\$?\s*([\d,.]+[KMBkmb]?)", body_text)
    if vol_match:
        result.volume_24h = vol_match.group(1)

    # Market cap
    mcap_match = re.search(r"(?:Market\s*Cap|MKT\s*CAP|MCAP)[:\s]*\$?\s*([\d,.]+[KMBkmb]?)", body_text)
    if mcap_match:
        result.market_cap = mcap_match.group(1)

    # FDV
    fdv_match = re.search(r"(?:FDV)[:\s]*\$?\s*([\d,.]+[KMBkmb]?)", body_text)
    if fdv_match:
        result.fdv = fdv_match.group(1)

    # Price change 24h
    change_match = re.search(r"24[Hh][:\s]*([+-]?[\d,.]+%)", body_text)
    if change_match:
        result.price_change_24h = change_match.group(1)

    # Count pairs listed
    pair_rows = page.query_selector_all(
        ".ds-dex-table-row, [class*='pair-row'], tr[class*='pair']"
    )
    result.pair_count = len(pair_rows) if pair_rows else 0

    logger.info(
        "DexScreener parsed: price=%s liq=%s vol=%s",
        result.price, result.liquidity, result.volume_24h,
    )
    return result
