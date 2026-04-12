"""Tests for blockchain explorer scraper."""
from unittest.mock import patch, MagicMock, PropertyMock
import pytest

from scraperx.blockchain import (
    scrape_basescan_address,
    scrape_dexscreener_token,
    BasescanAddress,
    DexScreenerToken,
    PlaywrightNotAvailable,
    _validate_address,
    _parse_basescan_dom,
    _parse_dexscreener_dom,
    ADDRESS_RE,
)


# --- Address validation ---

class TestValidateAddress:
    def test_valid_address(self):
        addr = "0x1234567890abcdef1234567890abcdef12345678"
        assert _validate_address(addr) == addr

    def test_valid_checksummed(self):
        addr = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
        assert _validate_address(addr) == addr

    def test_strips_whitespace(self):
        addr = "  0x1234567890abcdef1234567890abcdef12345678  "
        assert _validate_address(addr) == addr.strip()

    def test_invalid_too_short(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            _validate_address("0x1234")

    def test_invalid_no_prefix(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            _validate_address("1234567890abcdef1234567890abcdef12345678")

    def test_invalid_non_hex(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            _validate_address("0xGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG")

    def test_empty(self):
        with pytest.raises(ValueError):
            _validate_address("")


class TestAddressRegex:
    def test_matches_valid(self):
        assert ADDRESS_RE.match("0x1234567890abcdef1234567890abcdef12345678")

    def test_rejects_short(self):
        assert not ADDRESS_RE.match("0x1234")


# --- Playwright not available ---

class TestPlaywrightNotAvailable:
    @patch("scraperx.blockchain._get_playwright", side_effect=PlaywrightNotAvailable("not installed"))
    def test_basescan_raises(self, mock_pw):
        with pytest.raises(PlaywrightNotAvailable, match="not installed"):
            scrape_basescan_address("0x1234567890abcdef1234567890abcdef12345678")

    @patch("scraperx.blockchain._get_playwright", side_effect=PlaywrightNotAvailable("not installed"))
    def test_dexscreener_raises(self, mock_pw):
        with pytest.raises(PlaywrightNotAvailable, match="not installed"):
            scrape_dexscreener_token("0x1234567890abcdef1234567890abcdef12345678")


# --- Basescan DOM parsing ---

def _make_mock_page(body_text="", elements=None):
    """Create a mock Playwright page with configurable DOM responses."""
    page = MagicMock()
    page.inner_text.return_value = body_text

    # Default: all query_selector calls return None
    def query_selector_side_effect(selector):
        if elements:
            for key, el in elements.items():
                if key in selector:
                    return el
        return None

    page.query_selector.side_effect = query_selector_side_effect
    page.query_selector_all.return_value = []
    return page


class TestParseBasescanDom:
    def test_eoa_address(self):
        """EOA: no contract tab, has balance and transactions."""
        balance_el = MagicMock()
        balance_el.inner_text.return_value = "0.5432 ETH\n($1,234.56)"

        txn_badge = MagicMock()
        txn_badge.inner_text.return_value = "42"

        page = _make_mock_page(
            body_text="Address 0x123... ETH Balance",
            elements={
                "divSummary": balance_el,
                "txtTxnCount": txn_badge,
            },
        )

        result = _parse_basescan_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert not result.is_contract
        assert result.eth_balance == "0.5432"
        assert result.eth_value_usd == "1234.56"
        assert result.transaction_count == 42

    def test_contract_address(self):
        """Contract: has contract tab, creator, and name."""
        contract_tab = MagicMock()

        balance_el = MagicMock()
        balance_el.inner_text.return_value = "0 ETH"

        creator_el = MagicMock()
        creator_el.inner_text.return_value = "0xabcdef1234567890abcdef1234567890abcdef12"

        name_el = MagicMock()
        name_el.inner_text.return_value = "MyToken"

        page = MagicMock()
        page.inner_text.return_value = "Contract Address contract code"
        page.query_selector_all.return_value = []

        # Map each selector call to the right element
        def qs(selector):
            if "li_contracts" in selector or "tab-contract" in selector:
                return contract_tab
            if "card-body" in selector or "FilterBalance" in selector:
                return balance_el
            if "trContract" in selector:
                return creator_el
            if "u-label--secondary" in selector or "contract-name" in selector:
                return name_el
            if "txs" in selector or "TxDataInfo" in selector:
                return None
            if "tokenbalance" in selector or "tokenholdings" in selector:
                return None
            if "transactions_count" in selector or "txtTxnCount" in selector:
                return None
            return None

        page.query_selector.side_effect = qs

        result = _parse_basescan_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.is_contract
        assert result.eth_balance == "0"
        assert result.contract_creator == "0xabcdef1234567890abcdef1234567890abcdef12"
        assert result.contract_name == "MyToken"

    def test_token_holdings(self):
        """Parse token holdings count."""
        token_el = MagicMock()
        token_el.inner_text.return_value = "15 tokens"

        page = _make_mock_page(
            body_text="Address",
            elements={"tokenbalance": token_el},
        )

        result = _parse_basescan_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.token_holdings_count == 15

    def test_empty_page(self):
        """Graceful handling of empty/minimal page."""
        page = _make_mock_page(body_text="Address")
        result = _parse_basescan_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.address == "0x1234567890abcdef1234567890abcdef12345678"
        assert not result.is_contract
        assert result.eth_balance == ""
        assert result.transaction_count == 0

    def test_txn_count_from_badge(self):
        """Transaction count from badge element."""
        badge = MagicMock()
        badge.inner_text.return_value = "1,234"

        page = _make_mock_page(
            body_text="Address",
            elements={"txtTxnCount": badge},
        )

        result = _parse_basescan_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.transaction_count == 1234


# --- DexScreener DOM parsing ---

class TestParseDexscreenerDom:
    def test_token_with_full_data(self):
        """Parse a token page with all data present."""
        header_el = MagicMock()
        header_el.inner_text.return_value = "MyToken (MTK)"

        pair_rows = [MagicMock(), MagicMock(), MagicMock()]

        page = _make_mock_page(
            body_text=(
                "MyToken (MTK)\n"
                "$0.00123\n"
                "24H: +15.5%\n"
                "Liquidity: $50,000\n"
                "Volume (24H): $12,345\n"
                "Market Cap: $1,500,000\n"
                "FDV: $2,000,000\n"
            ),
            elements={"token-name": header_el},
        )
        page.query_selector_all.return_value = pair_rows

        result = _parse_dexscreener_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.name == "MyToken"
        assert result.symbol == "MTK"
        assert result.price == "$0.00123"
        assert result.liquidity == "50,000"
        assert result.volume_24h == "12,345"
        assert result.market_cap == "1,500,000"
        assert result.fdv == "2,000,000"
        assert result.pair_count == 3

    def test_token_minimal_data(self):
        """Parse with only price available."""
        page = _make_mock_page(body_text="$0.05\nNo data available")

        result = _parse_dexscreener_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.price == "$0.05"
        assert result.liquidity == ""
        assert result.volume_24h == ""

    def test_empty_page(self):
        """Graceful handling when no data can be parsed."""
        page = _make_mock_page(body_text="Token not found")

        result = _parse_dexscreener_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.address == "0x1234567890abcdef1234567890abcdef12345678"
        assert result.price == ""
        assert result.source_method == "dexscreener-playwright"

    def test_token_with_slash_symbol(self):
        """Parse symbol from 'SYMBOL / WETH' format."""
        header_el = MagicMock()
        header_el.inner_text.return_value = "MTK / WETH"

        page = _make_mock_page(
            body_text="MTK / WETH $1.23",
            elements={"token-name": header_el},
        )

        result = _parse_dexscreener_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.symbol == "MTK"

    def test_price_change(self):
        """Parse 24h price change."""
        page = _make_mock_page(body_text="$1.00\n24h: -5.2%")

        result = _parse_dexscreener_dom(page, "0x1234567890abcdef1234567890abcdef12345678")
        assert result.price_change_24h == "-5.2%"


# --- Full scrape with mocked Playwright ---

def _mock_playwright_context(parse_fn):
    """Create a full mock for sync_playwright context manager."""
    mock_page = MagicMock()
    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser

    mock_sync_playwright = MagicMock()
    mock_sync_playwright.return_value.__enter__ = MagicMock(return_value=mock_pw)
    mock_sync_playwright.return_value.__exit__ = MagicMock(return_value=False)

    return mock_sync_playwright, mock_page


class TestScrapeBasescanIntegration:
    @patch("scraperx.blockchain._parse_basescan_dom")
    @patch("scraperx.blockchain._get_playwright")
    def test_full_scrape_flow(self, mock_get_pw, mock_parse):
        """Test full scrape_basescan_address with mocked Playwright."""
        mock_sync_pw, mock_page = _mock_playwright_context(mock_parse)
        mock_get_pw.return_value = mock_sync_pw

        expected = BasescanAddress(
            address="0x1234567890abcdef1234567890abcdef12345678",
            is_contract=True,
            eth_balance="1.5",
            transaction_count=100,
        )
        mock_parse.return_value = expected

        result = scrape_basescan_address("0x1234567890abcdef1234567890abcdef12345678")
        assert result.is_contract
        assert result.eth_balance == "1.5"
        assert result.transaction_count == 100

    def test_invalid_address_rejected(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            scrape_basescan_address("not-an-address")


class TestScrapeDexscreenerIntegration:
    @patch("scraperx.blockchain._parse_dexscreener_dom")
    @patch("scraperx.blockchain._get_playwright")
    def test_full_scrape_flow(self, mock_get_pw, mock_parse):
        """Test full scrape_dexscreener_token with mocked Playwright."""
        mock_sync_pw, mock_page = _mock_playwright_context(mock_parse)
        mock_get_pw.return_value = mock_sync_pw

        expected = DexScreenerToken(
            address="0x1234567890abcdef1234567890abcdef12345678",
            name="TestToken",
            price="$1.23",
            liquidity="50K",
        )
        mock_parse.return_value = expected

        result = scrape_dexscreener_token("0x1234567890abcdef1234567890abcdef12345678")
        assert result.name == "TestToken"
        assert result.price == "$1.23"

    def test_invalid_address_rejected(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            scrape_dexscreener_token("xyz")


# --- Dataclass defaults ---

class TestDataclassDefaults:
    def test_basescan_defaults(self):
        addr = BasescanAddress(address="0x" + "0" * 40)
        assert not addr.is_contract
        assert addr.eth_balance == ""
        assert addr.transaction_count == 0
        assert addr.source_method == "basescan-playwright"

    def test_dexscreener_defaults(self):
        token = DexScreenerToken(address="0x" + "0" * 40)
        assert token.name == ""
        assert token.price == ""
        assert token.pair_count == 0
        assert token.source_method == "dexscreener-playwright"
