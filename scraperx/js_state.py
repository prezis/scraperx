"""js_state — extract chart data + SPA hydration state from a live JS page.

Two complementary primitives:

1. ``extract_chart_data(page)`` — pulls all series data out of any
   `Highcharts.charts[]` instance currently rendered on the page. Skips
   chart-OCR entirely when the site uses Highcharts (which is the dominant
   JS chart library on financial-research sites — Yardeni, Topdown,
   Apollo Academy, Goldman charts, MSCI, etc.).

2. ``extract_spa_state(page)`` — captures hydration globals dropped by the
   three big SPA frameworks: ``__NEXT_DATA__`` (Next.js), ``__APOLLO_STATE__``
   (Apollo GraphQL), ``__INITIAL_STATE__`` (Redux / Vuex). When present, this
   gives you the full underlying data model BEFORE the user-facing render
   transforms it — often more accurate than DOM scraping.

Source incident:
    s31/s32 wojak-wojtek OSINT session, 2026-04-30 — Yardeni's
    sp546fundamentals chartbook is rendered with Highcharts; we OCR'd the PNG
    twice before someone noticed `window.Highcharts.charts[c].series[s].options.data`
    contains the EXACT numeric series. One eval call replaces 30 OCR slow-paths.

    For SPAs: thread.py already extracts ``__NEXT_DATA__`` from Twitter
    syndication HTML via regex (see ``_fetch_syndication_timeline_with_raw``).
    This module generalises that to a live Playwright page + Apollo + Redux.

Usage:
    from playwright.sync_api import sync_playwright
    from scraperx.js_state import extract_chart_data, extract_spa_state

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://example.com/chartpage")
        page.wait_for_load_state("networkidle")

        charts = extract_chart_data(page)
        for c in charts:
            print(c.title, [(s.name, len(s.data)) for s in c.series])

        spa = extract_spa_state(page)
        if spa.framework == "next":
            print(spa.data["props"]["pageProps"].keys())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page as AsyncPage
    from playwright.sync_api import Page as SyncPage

logger = logging.getLogger(__name__)

__all__ = [
    "ChartSeries",
    "ChartSnapshot",
    "SpaState",
    "extract_chart_data",
    "extract_chart_data_async",
    "extract_spa_state",
    "extract_spa_state_async",
]


# ---------------------------------------------------------------------------
# Highcharts extraction
# ---------------------------------------------------------------------------


@dataclass
class ChartSeries:
    """One data series in a Highcharts chart."""

    name: str
    data: list[Any]  # x/y pairs, scalar list, or full point dicts — preserved verbatim
    color: str | None = None
    type: str | None = None  # "line", "column", "spline", …


@dataclass
class ChartSnapshot:
    """One Highcharts chart instance found on the page."""

    title: str = ""
    subtitle: str = ""
    series: list[ChartSeries] = field(default_factory=list)
    x_axis_categories: list[str] = field(default_factory=list)


# Walks ``window.Highcharts.charts`` and returns a JSON-serialisable summary.
# Defensive: every property access is guarded so a half-rendered chart
# (e.g. one whose series has been destroyed) cannot raise inside the page.
_HIGHCHARTS_JS = """
() => {
    const out = [];
    const HC = window.Highcharts;
    if (!HC || !HC.charts) return out;
    for (const chart of HC.charts) {
        if (!chart || !chart.series) continue;  // destroyed slot
        const titleObj = chart.options && chart.options.title;
        const subObj = chart.options && chart.options.subtitle;
        const xAxis = chart.options && chart.options.xAxis;
        let xCats = [];
        if (xAxis) {
            const ax = Array.isArray(xAxis) ? xAxis[0] : xAxis;
            if (ax && Array.isArray(ax.categories)) xCats = ax.categories.slice();
        }
        const series = [];
        for (const s of chart.series) {
            if (!s || !s.options) continue;
            // s.options.data is the raw user data (preserves x/y semantics).
            // Use `== null` (not `!data`) so a legitimately empty series ([])
            // is preserved as [] instead of being overwritten with point-list.
            let data = s.options.data;
            if (data == null && s.data) {
                data = s.data.map(p => (p && typeof p === 'object') ? (p.y ?? null) : p);
            }
            if (data == null) data = [];
            series.push({
                name: s.name || s.options.name || '',
                data: data,
                color: s.options.color || null,
                type: s.options.type || null,
            });
        }
        out.push({
            title: (titleObj && titleObj.text) || '',
            subtitle: (subObj && subObj.text) || '',
            series: series,
            x_axis_categories: xCats,
        });
    }
    return out;
}
"""


def _parse_chart_payload(raw: Any) -> list[ChartSnapshot]:
    """Coerce the JS-side dict array into typed ChartSnapshot objects.

    Defensive against ``None``-typed series.data (Highcharts can hand back
    ``null`` rather than ``[]`` if a chart is mid-redraw). ``list(None)`` raises
    ``TypeError``, so we normalise via explicit ``is None`` checks.
    """
    if not isinstance(raw, list):
        return []
    out: list[ChartSnapshot] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        x_cats_raw = c.get("x_axis_categories")
        snap = ChartSnapshot(
            title=str(c.get("title") or ""),
            subtitle=str(c.get("subtitle") or ""),
            x_axis_categories=list(x_cats_raw) if isinstance(x_cats_raw, list) else [],
        )
        for s in c.get("series") or []:
            if not isinstance(s, dict):
                continue
            data_raw = s.get("data")
            data = list(data_raw) if isinstance(data_raw, list) else []
            snap.series.append(
                ChartSeries(
                    name=str(s.get("name") or ""),
                    data=data,
                    color=s.get("color"),
                    type=s.get("type"),
                )
            )
        out.append(snap)
    return out


def extract_chart_data(page: "SyncPage") -> list[ChartSnapshot]:
    """Sync: extract all Highcharts series currently on the page.

    Returns:
        list[ChartSnapshot] — empty if Highcharts not present or no charts
        rendered yet. Caller should ``page.wait_for_load_state('networkidle')``
        FIRST so charts have time to render.
    """
    try:
        raw = page.evaluate(_HIGHCHARTS_JS)
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_chart_data evaluate failed: %s", e)
        return []
    return _parse_chart_payload(raw)


async def extract_chart_data_async(page: "AsyncPage") -> list[ChartSnapshot]:
    """Async variant of :func:`extract_chart_data`."""
    try:
        raw = await page.evaluate(_HIGHCHARTS_JS)
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_chart_data_async evaluate failed: %s", e)
        return []
    return _parse_chart_payload(raw)


# ---------------------------------------------------------------------------
# SPA state extraction
# ---------------------------------------------------------------------------


@dataclass
class SpaState:
    """Hydration state grabbed from a live SPA page.

    Attributes:
        framework: "next" | "apollo" | "redux" | "" (none found).
        data:      The raw global object (typically a deep dict). Empty {} on miss.
        globals_present: Names of all known SPA globals that were detected,
                         even if a different one was selected.
    """

    framework: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    globals_present: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.framework) and bool(self.data)


# Returns a small descriptor of which globals are present + the chosen one.
# We let the caller decide priority via ``prefer`` arg on the Python side
# (default order: next → apollo → redux). The JS payload returns ALL of them.
_SPA_JS = """
() => {
    const out = {next: null, apollo: null, redux: null};
    if (window.__NEXT_DATA__) out.next = window.__NEXT_DATA__;
    if (window.__APOLLO_STATE__) out.apollo = window.__APOLLO_STATE__;
    if (window.__INITIAL_STATE__) out.redux = window.__INITIAL_STATE__;
    return out;
}
"""

_FRAMEWORK_PRIORITY: tuple[str, ...] = ("next", "apollo", "redux")


def _select_spa(raw: Any, prefer: tuple[str, ...]) -> SpaState:
    """Pick the highest-priority framework from the JS-side dict."""
    if not isinstance(raw, dict):
        return SpaState()
    present = [k for k in _FRAMEWORK_PRIORITY if isinstance(raw.get(k), dict) and raw[k]]
    for fw in prefer:
        candidate = raw.get(fw)
        if isinstance(candidate, dict) and candidate:
            return SpaState(framework=fw, data=candidate, globals_present=present)
    return SpaState(globals_present=present)


def extract_spa_state(
    page: "SyncPage",
    prefer: tuple[str, ...] = _FRAMEWORK_PRIORITY,
) -> SpaState:
    """Sync: capture Next/Apollo/Redux hydration state from a live page.

    Args:
        page:   Playwright sync Page.
        prefer: Tuple of framework keys in priority order. First non-empty
                global wins. Default ("next", "apollo", "redux").

    Returns:
        SpaState — check ``.ok`` to see if anything was captured.
    """
    try:
        raw = page.evaluate(_SPA_JS)
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_spa_state evaluate failed: %s", e)
        return SpaState()
    return _select_spa(raw, prefer)


async def extract_spa_state_async(
    page: "AsyncPage",
    prefer: tuple[str, ...] = _FRAMEWORK_PRIORITY,
) -> SpaState:
    """Async variant of :func:`extract_spa_state`."""
    try:
        raw = await page.evaluate(_SPA_JS)
    except Exception as e:  # noqa: BLE001
        logger.debug("extract_spa_state_async evaluate failed: %s", e)
        return SpaState()
    return _select_spa(raw, prefer)


# ---------------------------------------------------------------------------
# Inline demo (no real browser)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Highcharts shape demo
    sample_chart = [
        {
            "title": "S&P 500 fwd P/E",
            "subtitle": "Yardeni",
            "x_axis_categories": ["2020", "2021", "2022"],
            "series": [
                {"name": "fwd P/E", "data": [22.1, 21.4, 17.9], "color": "#1f77b4", "type": "line"},
            ],
        }
    ]
    snaps = _parse_chart_payload(sample_chart)
    assert len(snaps) == 1 and snaps[0].series[0].data == [22.1, 21.4, 17.9]
    print("Highcharts parse OK:", snaps[0].title, snaps[0].series[0].name)

    # SPA select demo
    raw = {"next": {"props": {"pageProps": {"foo": 1}}}, "apollo": None, "redux": None}
    state = _select_spa(raw, _FRAMEWORK_PRIORITY)
    assert state.ok and state.framework == "next"
    print("SPA select OK:", state.framework, list(state.data.keys()))
