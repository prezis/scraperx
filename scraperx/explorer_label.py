"""explorer_label — extract <title>-tag labels from blockchain block-explorer pages.

Block explorers (Etherscan family + Solscan + Tronscan + explorer.solana.com) put
the human-curated wallet/contract label DIRECTLY in the HTML <title> tag, e.g.

    "ByBit: Hot Wallet 5 | Address 0xf89d... | Etherscan"
    "Coinbase 4 | Address 0x71660c4... | Etherscan"
    "Binance 2 | Account 5tzFkiKscXHK5Z... | Solscan"
    "Orbiter Finance: Bridge | Address 0x80c67432... | BscScan"

This is a deterministic, JS-free, vision-LLM-free way to attribute on-chain
addresses to known entities (CEXes, bridges, market makers). For static-rendered
explorers (Etherscan family, Tronscan) plain HTTP+regex works. Solscan is an SPA
that hydrates the title via JS, so it requires a Playwright fallback.

Pattern source: cex-gate session 2026-05-01 (verified ByBit, Coinbase, Orbiter,
Binance Solana). The HTML <title> is the single most reliable label signal —
beats screenshot-OCR, beats parsing the "Public Name Tag" widget (which moves
between Etherscan redesigns).

Public API:
    page_title(url)                  -> str          # raw <title> text
    extract_label(url)               -> LabelResult  # parsed {address,label,exchange,...}
    parse_title(title, address?)     -> LabelResult  # offline parser (no network)

CLI (see scraperx.__main__ wiring):
    scraperx page-title <url>
    scraperx label-extract <url>

Playwright is OPTIONAL — explorers that need JS hydration (solscan.io) raise
PlaywrightNotAvailable when it's missing; static explorers always work via stdlib.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = [
    "LabelResult",
    "page_title",
    "extract_label",
    "extract_chips",
    "parse_title",
    "detect_explorer",
    "ExplorerNotSupported",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 scraperx-explorer/1.0"
)

# Static-render Etherscan family — plain HTTP returns the label in <title>.
_STATIC_EXPLORERS: dict[str, str] = {
    "etherscan.io": "ethereum",
    "www.etherscan.io": "ethereum",
    "basescan.org": "base",
    "www.basescan.org": "base",
    "bscscan.com": "bsc",
    "www.bscscan.com": "bsc",
    "polygonscan.com": "polygon",
    "www.polygonscan.com": "polygon",
    "arbiscan.io": "arbitrum",
    "www.arbiscan.io": "arbitrum",
    "optimistic.etherscan.io": "optimism",
    "snowtrace.io": "avalanche",
    "www.snowtrace.io": "avalanche",
    "ftmscan.com": "fantom",
    "www.ftmscan.com": "fantom",
    "tronscan.org": "tron",
    "www.tronscan.org": "tron",
    "tronscan.io": "tron",
}

# JS-hydrated explorers — title only fills in after JS runs; need Playwright.
_DYNAMIC_EXPLORERS: dict[str, str] = {
    "solscan.io": "solana",
    "www.solscan.io": "solana",
    "explorer.solana.com": "solana",
    "solana.fm": "solana",
}

# Common known-entity prefixes — used to seed exchange_guess from the label.
# Order matters: longest prefix wins via the loop below.
_EXCHANGE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("binance", "binance"),
    ("bybit", "bybit"),
    ("coinbase", "coinbase"),
    ("kraken", "kraken"),
    ("okx", "okx"),
    ("kucoin", "kucoin"),
    ("htx", "htx"),
    ("huobi", "htx"),
    ("mexc", "mexc"),
    ("gate.io", "gate.io"),
    ("gate ", "gate.io"),
    ("crypto.com", "crypto.com"),
    ("bitfinex", "bitfinex"),
    ("bitget", "bitget"),
    ("upbit", "upbit"),
    ("bithumb", "bithumb"),
    ("orbiter", "orbiter"),
    ("wormhole", "wormhole"),
    ("layerzero", "layerzero"),
)

# Address regexes for parsing the address out of the URL when explicit address arg missing.
_EVM_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_SOL_ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")  # base58
_TRON_ADDR_RE = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")

# Title-extraction regex (case-insensitive, multi-line). Captures inner text.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# Chip-extraction regexes
# ---------------------------------------------------------------------------
#
# Pattern source: cex-gate session 2026-05-01 nest_ranker investigation
# (Etherscan chip metadata = primary per-address classification signal). The
# title alone gives ONE phrase; the chips give STRUCTURED facets ("Exchange",
# "Deposit Address", "#Gate", "Funder", "Burn") that survive Etherscan's title
# rewrites and are far easier to bucket than free-form titles.
#
# Etherscan-family page structure (verified Etherscan + BscScan, 2026-05-01):
#
#   <div id="ContentPlaceHolder1_divLabels" class="d-flex gap-1">
#       <span class="badge bg-white border ...">      ← entity-type chip
#           <div class="d-flex ...">
#               <span class="hash-tag ...">Exchange</span>
#           </div>
#       </span>
#       <a class="badge bg-white ..." href="/accounts/label/gate">  ← hashtag chip
#           <div class="d-flex ...">
#               <i class="far fa-hashtag"></i>
#               <span class="hash-tag ...">Gate</span>
#           </div>
#       </a>
#   </div>
#
# Rule: any chip whose wrapper is an <a href="/accounts/label/..."> gets a "#"
# prefix in the returned tuple (e.g. "#Gate"). Plain <span> chips don't.
# Etherscan and BscScan diverge on WHICH chips get the anchor — Etherscan tags
# only the "Gate"/"Burn"/"Donate"-style hashtag, BscScan also anchors "Binance"
# and "Exchange" — so the href is the only reliable hashtag signal, NOT the
# chip text.

# Locate the chip container (single capture: full inner HTML).
_DIV_LABELS_RE = re.compile(
    r'<div\s+id="ContentPlaceHolder1_divLabels"[^>]*>(.*?)</div>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
# Looser fallback when the closing-div sequence above doesn't match (some pages
# nest fewer wrappers): captures up to the next closing </div> at any depth-0.
_DIV_LABELS_LOOSE_RE = re.compile(
    r'<div\s+id="ContentPlaceHolder1_divLabels"[^>]*>(.*?)(?=<div\s+id=|</section|</body)',
    re.IGNORECASE | re.DOTALL,
)

# Each chip is either a <span class="badge ..."> or <a class="badge ..." href="/accounts/label/..."> ...
# We capture (full_open_tag, inner_html). The opening tag tells us if it's an
# anchor (and what its href is); the inner HTML carries the hash-tag span.
_CHIP_TAG_RE = re.compile(
    r'<(span|a)([^>]*\bclass="[^"]*\bbadge\s+bg-white\b[^"]*"[^>]*)>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)
# Pull the chip text from the inner <span class="hash-tag">TEXT</span>.
# Capture text up to the next "<" (no nested tags inside hash-tag span in
# practice). This is more robust than requiring "</span>" as the terminator,
# because the OUTER chip-tag regex (_CHIP_TAG_RE) is non-greedy and ends at the
# FIRST </span> it sees — which is the hash-tag's closing </span> when the
# wrapper span contains a nested hash-tag span. That truncation strips the
# closing </span> from the inner HTML we get here, so requiring it would fail.
_HASH_TAG_RE = re.compile(
    r'<span[^>]*\bclass="[^"]*\bhash-tag\b[^"]*"[^>]*>([^<]*)',
    re.IGNORECASE | re.DOTALL,
)
# Detect a hashtag-anchor chip via its href ("/accounts/label/<word>").
_HASHTAG_HREF_RE = re.compile(r'\bhref="(?:https?://[^/]+)?/accounts/label/[^"]+"', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExplorerNotSupported(ValueError):
    """Raised when the URL host isn't a recognized block-explorer domain."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class LabelResult:
    """Parsed result of a block-explorer title.

    Fields:
        url: the URL that was queried.
        title: the raw <title> tag text (None if not found).
        address: the on-chain address parsed from the URL (or None).
        label: the human label (everything before the first " | " in the title).
               None if the title looked like a generic explorer landing page.
        exchange_guess: lowercased canonical exchange name when the label starts
                        with a known exchange prefix; None otherwise.
        chain: normalized chain id ("ethereum"/"base"/"solana"/...).
        explorer: normalized explorer host (e.g. "etherscan.io").
        source: how the title was retrieved — "static" (stdlib HTTP),
                "playwright" (rendered), or "offline" (parse_title only).
        errors: any non-fatal errors encountered (mostly for debugging).
    """

    url: str
    title: Optional[str] = None
    address: Optional[str] = None
    label: Optional[str] = None
    exchange_guess: Optional[str] = None
    chain: Optional[str] = None
    explorer: Optional[str] = None
    source: str = ""
    errors: list[str] = field(default_factory=list)
    # NEW (cex-gate session 2026-05-01): structured per-address metadata chips
    # extracted from the address-header chip strip. Defaults to () so existing
    # consumers that don't read this field see unchanged behavior. Hashtag
    # chips preserve their "#" prefix (e.g. "#Gate", "#Burn"), entity-type
    # chips do not (e.g. "Exchange", "Deposit Address", "Funder"). Etherscan
    # family only — Solscan/Tronscan currently return ().
    chips: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.label is not None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_explorer(url: str) -> tuple[Optional[str], Optional[str], str]:
    """Identify (host, chain, kind) for a URL. kind is "static"/"dynamic"/"unknown"."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None, None, "unknown"
    if not host:
        return None, None, "unknown"
    if host in _STATIC_EXPLORERS:
        return host, _STATIC_EXPLORERS[host], "static"
    if host in _DYNAMIC_EXPLORERS:
        return host, _DYNAMIC_EXPLORERS[host], "dynamic"
    return host, None, "unknown"


def _extract_address_from_url(url: str, chain: Optional[str]) -> Optional[str]:
    """Best-effort: pull a chain-appropriate address out of the URL path."""
    if chain == "solana":
        # Solscan/explorer.solana.com use /account/<base58> or /address/<base58>.
        for m in _SOL_ADDR_RE.finditer(url):
            cand = m.group(0)
            # Filter out path words that happen to look base58-ish.
            if cand.lower() in {"account", "address", "tx", "token", "block"}:
                continue
            return cand
        return None
    if chain == "tron":
        m = _TRON_ADDR_RE.search(url)
        return m.group(0) if m else None
    # Default: EVM
    m = _EVM_ADDR_RE.search(url)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Title fetchers
# ---------------------------------------------------------------------------


def _fetch_static_html(url: str, timeout: int) -> str:
    """Fetch raw HTML with stdlib urllib. Raises on HTTP failure."""
    req = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — explorer host allowlist
        body = resp.read()
        # Decode robustly — explorer pages are utf-8 in practice.
        try:
            return body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return body.decode("utf-8", errors="replace")


def _fetch_static_title(url: str, timeout: int) -> str:
    """Fetch with stdlib urllib and pull <title> via regex. Raises on HTTP/parse fail."""
    html = _fetch_static_html(url, timeout)
    title = _title_from_html(html)
    if not title:
        raise RuntimeError(f"no <title> tag found in {url!r}")
    return title


def _fetch_static_title_and_html(url: str, timeout: int) -> tuple[str, str]:
    """Fetch HTML + extract <title> in one round-trip. Used by extract_label()
    so chips and title come from the same fetch."""
    html = _fetch_static_html(url, timeout)
    title = _title_from_html(html)
    if not title:
        raise RuntimeError(f"no <title> tag found in {url!r}")
    return title, html


def _title_from_html(html: str) -> Optional[str]:
    """Extract and clean the contents of the first <title>...</title> tag."""
    m = _TITLE_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    # Strip whitespace + decode common HTML entities (&amp; &#x2F; etc.).
    import html as _htmlmod

    cleaned = _htmlmod.unescape(raw).strip()
    # Collapse internal whitespace to single spaces (titles sometimes have newlines).
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _fetch_dynamic_title(url: str, timeout: int) -> str:
    """Render with headless Playwright and read document.title after hydration.

    Raises PlaywrightNotAvailable when playwright isn't installed or fails to launch.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        # Reuse the screenshot module's error class so callers can catch one type.
        from .screenshot import PlaywrightNotAvailable

        raise PlaywrightNotAvailable(
            "Playwright is required for SPA explorer titles (e.g. solscan.io). "
            "Install with: pip install playwright && playwright install chromium"
        ) from e

    from .screenshot import PlaywrightNotAvailable

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:
            raise PlaywrightNotAvailable(
                f"Failed to launch Chromium (run 'playwright install chromium'): {e}"
            ) from e
        try:
            page = browser.new_page(user_agent=DEFAULT_USER_AGENT)
            page.set_default_timeout(timeout * 1000)
            page.goto(url, wait_until="domcontentloaded")
            # Solscan hydrates the title shortly after DOMContentLoaded. Poll the
            # DOM until the title contains a "|" (the separator the SPA uses) OR
            # the timeout elapses. Empty/landing-page titles are still returned
            # as best-effort.
            try:
                page.wait_for_function(
                    "document.title && document.title.indexOf('|') !== -1",
                    timeout=timeout * 1000,
                )
            except Exception:
                # Fall back to whatever title we have at the timeout.
                pass
            title = page.title()
            return (title or "").strip()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Title parsing — pure, offline-testable
# ---------------------------------------------------------------------------


def parse_title(title: str, *, address: Optional[str] = None) -> LabelResult:
    """Parse a block-explorer <title> tag into LabelResult fields.

    The title format across Etherscan family + Solscan is consistently:
        "<LABEL> | <CONTEXT> | <Explorer name>"
    For example:
        "ByBit: Hot Wallet 5 | Address 0xf89d... | Etherscan"
        "Coinbase 4 | Address 0x71660c4... | Etherscan"
        "Binance 2 | Account 5tzFki... | Solscan"

    We split on " | " and take the first segment as the candidate label. We also
    treat ": " in the first segment as a sub-namespace (e.g. "ByBit: Hot Wallet
    5" → exchange_guess="bybit", label="ByBit: Hot Wallet 5").

    When the first segment looks like a generic context word ("Address",
    "Account", "Token"), the explorer hasn't tagged this address — label is set
    to None, which is the honest result.
    """
    result = LabelResult(url="", title=title, address=address, source="offline")
    if not title:
        return result

    # Decode any HTML entities (&amp;, &#x2F;, ...) — explorers occasionally
    # encode "&" in labels and parse_title may be called with raw-HTML titles.
    import html as _htmlmod

    decoded = _htmlmod.unescape(title)
    parts = [p.strip() for p in decoded.split("|") if p.strip()]
    if not parts:
        return result

    candidate = parts[0]
    lower = candidate.lower()

    # Generic context words → no label assigned by the explorer.
    GENERIC_FIRST = {
        "address",
        "account",
        "token",
        "transaction",
        "block",
        "etherscan",
        "solscan",
        "basescan",
        "bscscan",
        "polygonscan",
        "arbiscan",
        "tronscan",
    }
    first_word = lower.split()[0] if lower.split() else ""
    if first_word in GENERIC_FIRST:
        # No human label present.
        return result

    result.label = candidate

    # Exchange-prefix sniff (case-insensitive, prefix match).
    for prefix, canonical in _EXCHANGE_PREFIXES:
        if lower.startswith(prefix):
            result.exchange_guess = canonical
            break

    return result


# ---------------------------------------------------------------------------
# Chip parsing — pure, offline-testable
# ---------------------------------------------------------------------------


def extract_chips(html: str) -> tuple[str, ...]:
    """Extract Etherscan-family metadata chips from page HTML.

    Returns chips in the order they appear in #ContentPlaceHolder1_divLabels.
    Hashtag chips (anchored to /accounts/label/<word>) get a "#" prefix; plain
    entity-type chips do not.

    Examples (from real fixtures captured 2026-05-01):
        Coinbase Deposit page  -> ("Coinbase", "Deposit Address")
        Gate Deposit page      -> ("Exchange", "#Gate")
        Binance Hot Wallet 20  -> ("Binance", "Exchange")
        BscScan Binance 51     -> ("#Binance", "#Exchange", "Deposit Address")
        Burn (0x...dead)       -> ("#Burn",)
        Unlabeled EOA          -> ()

    Pattern source: cex-gate session 2026-05-01 nest_ranker investigation
    (Etherscan chip metadata = primary per-address classification signal).
    """
    if not html:
        return ()

    # Locate the chip container. Try the strict close-pair first; fall back to
    # the loose lookahead variant for pages with a different div nesting depth.
    m = _DIV_LABELS_RE.search(html)
    if not m:
        m = _DIV_LABELS_LOOSE_RE.search(html)
    if not m:
        return ()
    container = m.group(1)
    if not container.strip():
        return ()

    import html as _htmlmod

    chips: list[str] = []
    for chip_match in _CHIP_TAG_RE.finditer(container):
        tag = chip_match.group(1).lower()
        attrs = chip_match.group(2)
        inner = chip_match.group(3)
        # Pull the visible text from the nested .hash-tag span. Skip silently if
        # the chip wrapper has no inner hash-tag span (defensive — Etherscan
        # has occasionally A/B-tested wrappers without one).
        text_match = _HASH_TAG_RE.search(inner)
        if not text_match:
            continue
        raw_text = text_match.group(1)
        # Decode entities (&amp; etc.) and collapse whitespace.
        text = re.sub(r"\s+", " ", _htmlmod.unescape(raw_text)).strip()
        if not text:
            continue
        # Hashtag chip detection: anchor with href="/accounts/label/<word>".
        is_hashtag = tag == "a" and bool(_HASHTAG_HREF_RE.search(attrs))
        chips.append(f"#{text}" if is_hashtag else text)

    return tuple(chips)


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------


def page_title(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Return the raw <title> tag text for any URL.

    For static pages: stdlib HTTP. For known SPA explorers: Playwright. For
    arbitrary URLs (not a known explorer): tries static first; raises if the
    HTML has no <title>.
    """
    host, _chain, kind = detect_explorer(url)
    if kind == "dynamic":
        return _fetch_dynamic_title(url, timeout)
    # static or unknown — try plain HTTP first.
    return _fetch_static_title(url, timeout)


def extract_label(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> LabelResult:
    """Fetch the title for a block-explorer URL and parse it into LabelResult.

    Raises ExplorerNotSupported when the host is not a recognized explorer.
    Raises PlaywrightNotAvailable for dynamic explorers when Playwright is
    missing/broken — caller can catch and degrade gracefully.
    """
    host, chain, kind = detect_explorer(url)
    if kind == "unknown":
        raise ExplorerNotSupported(
            f"host {host!r} is not a recognized block explorer. "
            f"Supported: {sorted(set(list(_STATIC_EXPLORERS) + list(_DYNAMIC_EXPLORERS)))}"
        )

    address = _extract_address_from_url(url, chain)

    chips: tuple[str, ...] = ()
    if kind == "dynamic":
        title = _fetch_dynamic_title(url, timeout)
        source = "playwright"
        # Solscan/Tronscan: chip extraction not implemented (different page
        # structure, future work). Leave chips=() per spec.
    else:
        # Etherscan family: fetch HTML once, derive both title AND chips.
        title, html = _fetch_static_title_and_html(url, timeout)
        source = "static"
        try:
            chips = extract_chips(html)
        except Exception as e:  # defensive — never let chip parsing break label-extract
            logger.debug("extract_chips failed for %s: %s", url, e)
            chips = ()

    parsed = parse_title(title, address=address)
    parsed.url = url
    parsed.chain = chain
    parsed.explorer = host
    parsed.source = source
    parsed.chips = chips
    return parsed
