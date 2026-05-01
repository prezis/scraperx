"""Tests for scraperx.js_state — Highcharts + SPA hydration grab.

Network-free + browser-free; the parsers are exercised directly with sample
JS-shaped payloads, and the public functions are tested via stub Page objects.
"""

from __future__ import annotations

import pytest

from scraperx.js_state import (
    ChartSeries,
    ChartSnapshot,
    SpaState,
    _FRAMEWORK_PRIORITY,
    _parse_chart_payload,
    _select_spa,
    extract_chart_data,
    extract_spa_state,
)


class _StubPage:
    """Minimal sync Page stub used for both extract_chart_data + extract_spa_state."""

    def __init__(self, payload, evaluate_raise: type[Exception] | None = None):
        self._payload = payload
        self._raise = evaluate_raise

    def evaluate(self, _js: str):
        if self._raise:
            raise self._raise("boom")
        return self._payload


# ---------------------------------------------------------------------------
# _parse_chart_payload
# ---------------------------------------------------------------------------


def test_parse_chart_payload_well_formed():
    raw = [
        {
            "title": "S&P 500",
            "subtitle": "fwd P/E",
            "x_axis_categories": ["2020", "2021", "2022"],
            "series": [
                {"name": "fwd P/E", "data": [22.1, 21.4, 17.9], "color": "#1f77b4", "type": "line"},
                {"name": "trailing P/E", "data": [25.0, 24.0, 19.0], "color": None, "type": None},
            ],
        }
    ]
    snaps = _parse_chart_payload(raw)
    assert len(snaps) == 1
    s0 = snaps[0]
    assert isinstance(s0, ChartSnapshot)
    assert s0.title == "S&P 500"
    assert s0.x_axis_categories == ["2020", "2021", "2022"]
    assert len(s0.series) == 2
    assert s0.series[0].data == [22.1, 21.4, 17.9]
    assert s0.series[0].color == "#1f77b4"


def test_parse_chart_payload_handles_none_data():
    """Highcharts can return ``null`` instead of ``[]`` mid-redraw — list(None) raises."""
    raw = [{"title": "X", "subtitle": "", "x_axis_categories": None, "series": [{"name": "s", "data": None}]}]
    snaps = _parse_chart_payload(raw)
    assert snaps[0].series[0].data == []
    assert snaps[0].x_axis_categories == []


def test_parse_chart_payload_skips_non_dicts():
    raw = ["junk", 42, None, {"title": "ok", "series": []}]
    snaps = _parse_chart_payload(raw)
    assert len(snaps) == 1
    assert snaps[0].title == "ok"


def test_parse_chart_payload_empty_input():
    assert _parse_chart_payload([]) == []
    assert _parse_chart_payload(None) == []
    assert _parse_chart_payload("garbage") == []


def test_parse_chart_payload_preserves_empty_series_data():
    """Empty data list ([]) must NOT be replaced with a synthesised list."""
    raw = [{"title": "", "series": [{"name": "empty", "data": []}]}]
    snaps = _parse_chart_payload(raw)
    assert snaps[0].series[0].data == []


# ---------------------------------------------------------------------------
# _select_spa
# ---------------------------------------------------------------------------


def test_select_spa_picks_next_first():
    raw = {
        "next": {"props": {"pageProps": {"foo": 1}}},
        "apollo": {"some": "apollo data"},
        "redux": {"r": 1},
    }
    state = _select_spa(raw, _FRAMEWORK_PRIORITY)
    assert state.framework == "next"
    assert "props" in state.data
    assert set(state.globals_present) == {"next", "apollo", "redux"}


def test_select_spa_falls_through_to_apollo():
    raw = {"next": None, "apollo": {"a": 1}, "redux": None}
    state = _select_spa(raw, _FRAMEWORK_PRIORITY)
    assert state.framework == "apollo"
    assert state.data == {"a": 1}
    assert state.globals_present == ["apollo"]


def test_select_spa_custom_priority():
    raw = {"next": {"n": 1}, "apollo": {"a": 1}, "redux": {"r": 1}}
    state = _select_spa(raw, ("redux", "next", "apollo"))
    assert state.framework == "redux"


def test_select_spa_all_missing():
    state = _select_spa({"next": None, "apollo": None, "redux": None}, _FRAMEWORK_PRIORITY)
    assert not state.ok
    assert state.framework == ""
    assert state.globals_present == []


def test_select_spa_handles_garbage():
    assert not _select_spa("garbage", _FRAMEWORK_PRIORITY).ok
    assert not _select_spa(None, _FRAMEWORK_PRIORITY).ok


# ---------------------------------------------------------------------------
# Public API on stub pages
# ---------------------------------------------------------------------------


def test_extract_chart_data_e2e_stub():
    payload = [{"title": "x", "series": [{"name": "y", "data": [1, 2]}]}]
    page = _StubPage(payload)
    snaps = extract_chart_data(page)  # type: ignore[arg-type]
    assert len(snaps) == 1 and snaps[0].title == "x"


def test_extract_chart_data_swallows_evaluate_errors():
    page = _StubPage(None, evaluate_raise=RuntimeError)
    assert extract_chart_data(page) == []  # type: ignore[arg-type]


def test_extract_spa_state_e2e_stub():
    payload = {"next": {"props": {"pageProps": {"x": 1}}}, "apollo": None, "redux": None}
    page = _StubPage(payload)
    state = extract_spa_state(page)  # type: ignore[arg-type]
    assert state.ok and state.framework == "next"


def test_extract_spa_state_swallows_evaluate_errors():
    page = _StubPage(None, evaluate_raise=RuntimeError)
    state = extract_spa_state(page)  # type: ignore[arg-type]
    assert not state.ok
