"""Tests for scraperx.cookie_banner — sync dismiss + selector probe.

Network-free + browser-free. The Playwright Page is replaced with a stub that
implements the two methods we touch (wait_for_timeout, evaluate) so we can
verify the JS contract without launching Chromium.
"""

from __future__ import annotations

import pytest

from scraperx.cookie_banner import (
    DEFAULT_SELECTORS,
    BannerSelector,
    DismissResult,
    _interpret,
    _selectors_payload,
    dismiss_cookie_banner,
)


class _StubPage:
    """Minimal Playwright sync Page stub for tests.

    Args:
        hit:           CSS selector the page will "match" (or None for no banner).
        evaluate_raise: Exception type to raise from evaluate(), or None.
    """

    def __init__(self, hit: str | None, evaluate_raise: type[Exception] | None = None):
        self._hit = hit
        self._raise = evaluate_raise
        self.timeout_ms_called: int = 0

    def wait_for_timeout(self, ms: int) -> None:
        self.timeout_ms_called = ms

    def evaluate(self, _js: str, css: list[str]):
        if self._raise:
            raise self._raise("boom")
        if self._hit and self._hit in css:
            return {"selector": self._hit, "error": None}
        return None


# ---------------------------------------------------------------------------
# DEFAULT_SELECTORS / payload contract
# ---------------------------------------------------------------------------


def test_default_selectors_nonempty_and_unique():
    assert len(DEFAULT_SELECTORS) >= 12
    selectors_seen = set()
    for s in DEFAULT_SELECTORS:
        assert isinstance(s, BannerSelector)
        assert s.vendor and s.selector
        # Vendor-specific IDs must be unique; generic heuristics may repeat
        if s.selector.startswith("#"):
            assert s.selector not in selectors_seen, f"duplicate ID: {s.selector}"
            selectors_seen.add(s.selector)


def test_selectors_payload_round_trip():
    css, vendor_by_css = _selectors_payload(None)
    assert len(css) == len(DEFAULT_SELECTORS)
    # Mapping should round-trip: every default selector lives in vendor_by_css
    for s in DEFAULT_SELECTORS:
        assert vendor_by_css[s.selector] == s.vendor


def test_selectors_payload_override():
    custom = [BannerSelector("Acme", ".acme-accept")]
    css, vendor_by_css = _selectors_payload(custom)
    assert css == [".acme-accept"]
    assert vendor_by_css == {".acme-accept": "Acme"}


# ---------------------------------------------------------------------------
# _interpret — the JS-result decoder
# ---------------------------------------------------------------------------


def test_interpret_none_means_no_match():
    out = _interpret(None, {".x": "Acme"})
    assert isinstance(out, DismissResult)
    assert out.matched_selector == ""
    assert out.vendor == ""
    assert out.errors == []


def test_interpret_dict_with_error_records_error():
    out = _interpret({"selector": "#foo", "error": "elem detached"}, {"#foo": "Acme"})
    assert not out.ok
    assert out.errors == [("#foo", "elem detached")]


def test_interpret_clean_hit():
    out = _interpret(
        {"selector": "#onetrust-accept-btn-handler", "error": None},
        {"#onetrust-accept-btn-handler": "OneTrust"},
    )
    assert out.ok
    assert out.vendor == "OneTrust"


def test_interpret_garbage_payload_records_error():
    out = _interpret("not-a-dict", {})
    assert not out.ok
    assert any("non-dict" in msg for _, msg in out.errors)


# ---------------------------------------------------------------------------
# dismiss_cookie_banner — end-to-end on stub Page
# ---------------------------------------------------------------------------


def test_dismiss_hits_onetrust():
    page = _StubPage("#onetrust-accept-btn-handler")
    out = dismiss_cookie_banner(page, timeout_ms=10)  # type: ignore[arg-type]
    assert out.ok
    assert out.vendor == "OneTrust"
    # The wait_for_timeout argument was passed through
    assert page.timeout_ms_called == 10


def test_dismiss_no_banner_returns_clean_miss():
    page = _StubPage(None)
    out = dismiss_cookie_banner(page, timeout_ms=0)  # type: ignore[arg-type]
    assert not out.ok
    assert out.errors == []
    assert len(out.attempted) == len(DEFAULT_SELECTORS)


def test_dismiss_evaluate_failure_surfaces_as_error():
    page = _StubPage(None, evaluate_raise=RuntimeError)
    out = dismiss_cookie_banner(page, timeout_ms=0)  # type: ignore[arg-type]
    assert not out.ok
    assert out.errors and out.errors[0][0] == "evaluate"


def test_dismiss_with_custom_selector_only():
    page = _StubPage(".acme-accept")
    out = dismiss_cookie_banner(
        page,  # type: ignore[arg-type]
        selectors=[BannerSelector("Acme", ".acme-accept")],
        timeout_ms=0,
    )
    assert out.ok
    assert out.vendor == "Acme"
    assert out.attempted == [".acme-accept"]


def test_dismiss_custom_selectors_does_not_mutate_default():
    """Passing a list should not change DEFAULT_SELECTORS for later callers."""
    page = _StubPage(None)
    custom = [BannerSelector("X", ".x")]
    dismiss_cookie_banner(page, selectors=custom, timeout_ms=0)  # type: ignore[arg-type]
    assert len(DEFAULT_SELECTORS) >= 12
    # Default selector list still contains OneTrust at the front
    assert DEFAULT_SELECTORS[0].selector == "#onetrust-accept-btn-handler"


@pytest.mark.parametrize(
    "expected_vendor,expected_sel",
    [
        ("OneTrust",   "#onetrust-accept-btn-handler"),
        ("Cookiebot",  "#CybotCookiebotDialogBodyLevelButtonAccept"),
        ("TrustArc",   "#truste-consent-button"),
        ("Didomi",     "#didomi-notice-agree-button"),
    ],
)
def test_dismiss_recognises_each_known_vendor(expected_vendor, expected_sel):
    page = _StubPage(expected_sel)
    out = dismiss_cookie_banner(page, timeout_ms=0)  # type: ignore[arg-type]
    assert out.ok
    assert out.vendor == expected_vendor
    assert out.matched_selector == expected_sel
