"""cookie_banner — auto-dismiss cookie/consent banners on Playwright pages.

Most JS-heavy sites block content until a cookie consent popup is dismissed.
The popup taxonomy is dominated by ~12 vendors (OneTrust / Cookiebot / TrustArc /
Quantcast / Didomi / etc.) plus a long tail of Drupal / WordPress / GDPR plugin
patterns. Each one ships a known, stable accept-button selector.

Source incident:
    s31/s32 wojak-wojtek OSINT session, 2026-04-30 — fetching Yardeni / Topdown /
    PR-Newswire / Statista / Trading Economics screens repeatedly hit consent
    walls. We dispatched the same multi-selector fan-out via
    `mcp__playwright-local__browser_evaluate`. This module ports that loop into
    a reusable primitive so future scraperx callers don't re-implement it.

Usage:
    from playwright.sync_api import sync_playwright
    from scraperx.cookie_banner import dismiss_cookie_banner

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.yardeni.com/")
        result = dismiss_cookie_banner(page)
        if result.matched_selector:
            print(f"Dismissed via {result.vendor} ({result.matched_selector})")
        # …continue scraping…

Async variant: use ``dismiss_cookie_banner_async`` with ``playwright.async_api``.

Known limitations (deliberate):
    - ``document.querySelector`` does NOT pierce Shadow DOM. Some banner
      vendors (newer OneTrust v6, fresh Cookiebot) ship the accept button
      inside a closed shadow root; this primitive will not find them. When
      the probe returns ``ok=False`` AND the page still shows a banner,
      fall back to Playwright's ``page.locator(...).click()`` which DOES
      pierce shadow roots — at the cost of one selector per attempt.
    - Visibility check is ``offsetParent !== null``. Banners hidden via
      ``visibility: hidden`` or ``opacity: 0`` (rare) will be clicked
      anyway; almost never a problem in practice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from playwright.async_api import Page as AsyncPage
    from playwright.sync_api import Page as SyncPage

logger = logging.getLogger(__name__)

__all__ = [
    "BannerSelector",
    "DismissResult",
    "DEFAULT_SELECTORS",
    "dismiss_cookie_banner",
    "dismiss_cookie_banner_async",
]


@dataclass(frozen=True)
class BannerSelector:
    """One known cookie-banner accept button selector.

    Attributes:
        vendor:   Human-readable vendor / framework label.
        selector: CSS selector that matches the accept-all button.
    """

    vendor: str
    selector: str


# Ordered most-specific (vendor-id) → most-generic (text-match heuristic).
# Order matters: we click the FIRST match, so vendor-specific IDs win over
# generic ARIA / text fallbacks. New entries go ABOVE the generic block.
DEFAULT_SELECTORS: tuple[BannerSelector, ...] = (
    # — vendor-specific stable IDs —
    BannerSelector("OneTrust",   "#onetrust-accept-btn-handler"),
    BannerSelector("Cookiebot",  "#CybotCookiebotDialogBodyLevelButtonAccept"),
    BannerSelector("Cookiebot",  "#CybotCookiebotDialogBodyButtonAccept"),
    BannerSelector("TrustArc",   "#truste-consent-button"),
    BannerSelector("Quantcast",  ".qc-cmp2-summary-buttons button[mode='primary']"),
    BannerSelector("Didomi",     "#didomi-notice-agree-button"),
    BannerSelector("Osano",      ".osano-cm-accept-all"),
    BannerSelector("Usercentrics", "[data-testid='uc-accept-all-button']"),
    BannerSelector("Sourcepoint", ".sp_choice_type_11"),  # "Accept all"
    BannerSelector("Iubenda",    ".iubenda-cs-accept-btn"),
    # — framework / CMS plugins —
    BannerSelector("WP-GDPR",    "#wt-cli-accept-all-btn"),
    BannerSelector("WP-CCPA",    ".cli-user-preference-checkbox+a.cli_action_button"),
    BannerSelector("Drupal-EU",  "button.eu-cookie-compliance-default-button"),
    # — generic ARIA / data-attribute heuristics (lower confidence) —
    BannerSelector("ARIA",       "button[aria-label*='Accept' i]"),
    BannerSelector("data-attr",  "[data-cookieconsent='accept']"),
    BannerSelector("data-attr",  "[data-action='accept-all-cookies']"),
)


@dataclass
class DismissResult:
    """Outcome of a single dismiss attempt.

    Attributes:
        matched_selector: The selector that succeeded, or empty string.
        vendor:           Vendor label for the matched selector, or empty.
        attempted:        Selectors that were tried (in order).
        errors:           Per-selector failures collected along the way.
    """

    matched_selector: str = ""
    vendor: str = ""
    attempted: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff a banner was located AND its accept-button was clicked."""
        return bool(self.matched_selector)


# JS used in the page context — kept as a constant so tests can introspect it
# and so we can ship the EXACT same payload to both sync and async pages.
_PROBE_JS = """
(selectors) => {
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) {
            // visible + present — click and report which one matched
            try { el.click(); } catch (e) { return {selector: sel, error: e.toString()}; }
            return {selector: sel, error: null};
        }
    }
    return null;
}
"""


def _selectors_payload(selectors: list[BannerSelector] | None) -> tuple[list[str], dict[str, str]]:
    """Build (css_selector_list, selector→vendor lookup) for one dismiss call.

    Returns a fresh list each call so callers can mutate the input safely.
    """
    src = list(selectors) if selectors is not None else list(DEFAULT_SELECTORS)
    css = [s.selector for s in src]
    vendor_by_css = {s.selector: s.vendor for s in src}
    return css, vendor_by_css


def _interpret(probe_result: Any, vendor_by_css: dict[str, str]) -> DismissResult:
    """Turn the JS probe's return value into a typed DismissResult."""
    out = DismissResult(attempted=list(vendor_by_css.keys()))
    if probe_result is None:
        return out
    if not isinstance(probe_result, dict):
        out.errors.append(("probe", f"non-dict result: {probe_result!r}"))
        return out
    sel = probe_result.get("selector", "")
    err = probe_result.get("error")
    if err:
        out.errors.append((sel or "?", str(err)))
        return out
    out.matched_selector = sel
    out.vendor = vendor_by_css.get(sel, "")
    return out


def dismiss_cookie_banner(
    page: "SyncPage",
    selectors: list[BannerSelector] | None = None,
    *,
    timeout_ms: int = 2000,
) -> DismissResult:
    """Try to dismiss a cookie banner on a Playwright sync Page.

    Runs an in-page JS probe that walks ``selectors`` (default DEFAULT_SELECTORS)
    and clicks the FIRST visible match. Visibility = ``offsetParent !== null``,
    which excludes elements hidden via ``display:none`` or detached subtrees.

    Args:
        page:       Playwright sync Page object.
        selectors:  Override selector list. None ⇒ DEFAULT_SELECTORS.
        timeout_ms: Best-effort wait window; we still return immediately if
                    the probe finishes faster. Long banners can lag behind
                    DOMContentLoaded so we wait_for_timeout BEFORE probing.

    Returns:
        DismissResult — check ``.ok`` to see if anything was clicked.

    Notes:
        - Never raises on probe failure; errors land on ``result.errors``.
        - Idempotent: if the banner is already gone, returns ``ok=False``
          with empty errors. Caller decides whether that's a problem.
    """
    css, vendor_by_css = _selectors_payload(selectors)
    try:
        page.wait_for_timeout(timeout_ms)
        raw = page.evaluate(_PROBE_JS, css)
    except Exception as e:  # noqa: BLE001 — surface as error, don't propagate
        out = DismissResult(attempted=css)
        out.errors.append(("evaluate", f"{type(e).__name__}: {e}"))
        return out
    return _interpret(raw, vendor_by_css)


async def dismiss_cookie_banner_async(
    page: "AsyncPage",
    selectors: list[BannerSelector] | None = None,
    *,
    timeout_ms: int = 2000,
) -> DismissResult:
    """Async variant of :func:`dismiss_cookie_banner` for ``playwright.async_api``."""
    css, vendor_by_css = _selectors_payload(selectors)
    try:
        await page.wait_for_timeout(timeout_ms)
        raw = await page.evaluate(_PROBE_JS, css)
    except Exception as e:  # noqa: BLE001
        out = DismissResult(attempted=css)
        out.errors.append(("evaluate", f"{type(e).__name__}: {e}"))
        return out
    return _interpret(raw, vendor_by_css)


# ---------------------------------------------------------------------------
# Inline smoke-test (no real browser needed; uses a tiny stub page object)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover — exercised by tests/
    class _StubPage:
        """Minimal Playwright Page stub used to demo the probe contract."""
        def __init__(self, hit_selector: str | None):
            self._hit = hit_selector
        def wait_for_timeout(self, _ms: int) -> None: pass
        def evaluate(self, _js: str, css: list[str]) -> dict | None:
            if self._hit and self._hit in css:
                return {"selector": self._hit, "error": None}
            return None

    # Hit case
    page = _StubPage("#onetrust-accept-btn-handler")
    r = dismiss_cookie_banner(page)  # type: ignore[arg-type]
    print(f"hit={r.ok} vendor={r.vendor!r} selector={r.matched_selector!r}")
    assert r.ok and r.vendor == "OneTrust"

    # Miss case
    page = _StubPage(None)
    r = dismiss_cookie_banner(page)  # type: ignore[arg-type]
    print(f"miss ok={r.ok} attempted={len(r.attempted)} selectors")
    assert not r.ok and len(r.attempted) == len(DEFAULT_SELECTORS)
