"""Tests for scraperx.wayback — CDX URL builder + multi-generation probe.

Network-free: query_cdx is monkey-patched to return canned entries, so the
multi-generation probe can be exercised without hitting the IA.
"""

from __future__ import annotations

import json

import pytest

from scraperx import wayback as wayback_mod
from scraperx.wayback import (
    _CDX_FIELDS,
    CdxEntry,
    MultiGenerationResult,
    WaybackError,
    _build_cdx_url,
    _parse_cdx_response,
    _year_to_ts,
    wayback_multi_generation_probe,
)


# ---------------------------------------------------------------------------
# Year → CDX timestamp
# ---------------------------------------------------------------------------


def test_year_to_ts_start_of_year():
    assert _year_to_ts(2018, end=False) == "20180101000000"


def test_year_to_ts_end_of_year():
    assert _year_to_ts(2018, end=True) == "20181231235959"


def test_year_to_ts_none_passes_through():
    assert _year_to_ts(None, end=False) is None


def test_year_to_ts_zero_padding():
    """4-digit year is required by the CDX API; year 9 must serialise as 0009."""
    assert _year_to_ts(9, end=False) == "00090101000000"


# ---------------------------------------------------------------------------
# _build_cdx_url
# ---------------------------------------------------------------------------


def test_build_cdx_url_minimal():
    url = _build_cdx_url("ici.org/info/", from_year=None, to_year=None, limit=10, match_type="prefix")
    assert "matchType=prefix" in url
    assert "limit=10" in url
    assert "from=" not in url and "to=" not in url
    assert "fl=" + ",".join(_CDX_FIELDS) in url


def test_build_cdx_url_with_year_uses_yyyymmdd_not_bare_year():
    url = _build_cdx_url("ici.org/info/", from_year=2018, to_year=2018, limit=10, match_type="prefix")
    # The bug we're guarding against: passing bare 2018 returns 0 hits from CDX.
    assert "from=20180101000000" in url
    assert "to=20181231235959" in url
    assert "from=2018&" not in url


def test_build_cdx_url_rejects_bad_match_type():
    with pytest.raises(ValueError):
        _build_cdx_url("x.com/", from_year=None, to_year=None, limit=1, match_type="kebab")


# ---------------------------------------------------------------------------
# _build_cdx_url — collapse param (IA CDX deduplication)
# ---------------------------------------------------------------------------


def test_build_cdx_url_collapse_omitted_by_default():
    """When collapse is None, the URL has no ``collapse=`` clause at all."""
    url = _build_cdx_url("ici.org/info/", from_year=None, to_year=None, limit=10, match_type="prefix")
    assert "collapse=" not in url


def test_build_cdx_url_collapse_bare_field():
    """``collapse=urlkey`` — first snapshot per unique URL within the window."""
    url = _build_cdx_url(
        "ici.org/info/", from_year=None, to_year=None, limit=10, match_type="prefix",
        collapse="urlkey",
    )
    assert "collapse=urlkey" in url


def test_build_cdx_url_collapse_field_with_n():
    """``collapse=timestamp:8`` — one row per YYYYMMDD bucket."""
    url = _build_cdx_url(
        "ici.org/info/", from_year=2018, to_year=2018, limit=10, match_type="prefix",
        collapse="timestamp:8",
    )
    # Colon must be preserved (URL-safe in CDX query string)
    assert "collapse=timestamp%3A8" in url or "collapse=timestamp:8" in url


def test_build_cdx_url_collapse_rejects_unknown_field():
    with pytest.raises(ValueError, match="collapse"):
        _build_cdx_url(
            "x.com/", from_year=None, to_year=None, limit=1, match_type="prefix",
            collapse="bogusfield",
        )


def test_build_cdx_url_collapse_rejects_unknown_field_with_n():
    with pytest.raises(ValueError, match="collapse field"):
        _build_cdx_url(
            "x.com/", from_year=None, to_year=None, limit=1, match_type="prefix",
            collapse="bogus:8",
        )


def test_build_cdx_url_collapse_rejects_non_int_n():
    with pytest.raises(ValueError, match="collapse N"):
        _build_cdx_url(
            "x.com/", from_year=None, to_year=None, limit=1, match_type="prefix",
            collapse="timestamp:abc",
        )


def test_build_cdx_url_collapse_rejects_zero_n():
    with pytest.raises(ValueError, match="collapse N"):
        _build_cdx_url(
            "x.com/", from_year=None, to_year=None, limit=1, match_type="prefix",
            collapse="timestamp:0",
        )


# ---------------------------------------------------------------------------
# _parse_cdx_response
# ---------------------------------------------------------------------------


def test_parse_cdx_response_well_formed():
    body = json.dumps([
        list(_CDX_FIELDS),
        ["http://ici.org/info/x.html", "20180102030405", "200", "text/html", "ABC", "1234"],
        ["http://ici.org/info/y.html", "20180202030405", "200", "text/html", "DEF", "5678"],
    ])
    entries = _parse_cdx_response(body)
    assert len(entries) == 2
    assert isinstance(entries[0], CdxEntry)
    assert entries[0].timestamp == "20180102030405"
    assert entries[0].replay_url.startswith("https://web.archive.org/web/20180102030405/")


def test_parse_cdx_response_empty_body():
    assert _parse_cdx_response("") == []
    # Just the header row → no entries
    body = json.dumps([list(_CDX_FIELDS)])
    assert _parse_cdx_response(body) == []


def test_parse_cdx_response_pads_short_rows():
    """Older crawls sometimes drop 'length' / 'digest'; we must NOT silently skip those."""
    body = json.dumps([
        list(_CDX_FIELDS),
        ["http://ici.org/info/x.html", "20180102030405", "200"],  # truncated row
    ])
    entries = _parse_cdx_response(body)
    assert len(entries) == 1
    assert entries[0].length == "-"
    assert entries[0].digest == "-"


def test_parse_cdx_response_invalid_json_raises():
    with pytest.raises(WaybackError):
        _parse_cdx_response("{not json")


def test_parse_cdx_response_header_drift_raises():
    body = json.dumps([
        ["bogus", "schema"],
        ["a", "b"],
    ])
    with pytest.raises(WaybackError):
        _parse_cdx_response(body)


# ---------------------------------------------------------------------------
# wayback_multi_generation_probe — monkey-patched query_cdx
# ---------------------------------------------------------------------------


def _fake_entries(n: int, family: str) -> list[CdxEntry]:
    return [
        CdxEntry(
            original_url=f"http://{family}{i}.html",
            timestamp=f"2018010{i:02d}030405",
            statuscode="200",
        )
        for i in range(n)
    ]


def test_multi_gen_probe_first_family_wins(monkeypatch):
    calls: list[str] = []

    def fake_query(family, **_kw):
        calls.append(family)
        return _fake_entries(3, family) if "system/files" in family else []

    monkeypatch.setattr(wayback_mod, "query_cdx", fake_query)

    fams = ["ici.org/info/", "ici.org/doc-server/info%3A", "ici.org/system/files/"]
    out = wayback_multi_generation_probe("ici.org", fams, year=2018, polite_sleep_s=0)

    # All families probed (so the report is honest about coverage)
    assert calls == fams
    # First family with hits wins; later families MAY also have hits but are
    # only used to populate per_family_counts.
    assert out.matched_family == "ici.org/system/files/"
    assert len(out.entries) == 3
    assert out.per_family_counts == {fams[0]: 0, fams[1]: 0, fams[2]: 3}


def test_multi_gen_probe_all_empty(monkeypatch):
    monkeypatch.setattr(wayback_mod, "query_cdx", lambda *a, **kw: [])
    out = wayback_multi_generation_probe(
        "x.com",
        ["x.com/v1/", "x.com/v2/"],
        year=2018,
        polite_sleep_s=0,
    )
    assert not out.ok
    assert out.entries == []
    assert out.per_family_counts == {"x.com/v1/": 0, "x.com/v2/": 0}


def test_multi_gen_probe_records_per_family_errors(monkeypatch):
    def bad(family, **_kw):
        if "v1" in family:
            raise WaybackError("CDX 503")
        return _fake_entries(2, family)

    monkeypatch.setattr(wayback_mod, "query_cdx", bad)
    out = wayback_multi_generation_probe(
        "x.com",
        ["x.com/v1/", "x.com/v2/"],
        year=2018,
        polite_sleep_s=0,
    )
    # v2 still wins because v1 failed
    assert out.matched_family == "x.com/v2/"
    assert len(out.entries) == 2
    assert out.errors and out.errors[0][0] == "x.com/v1/"


def test_multi_gen_probe_rejects_empty_family_list():
    with pytest.raises(ValueError):
        wayback_multi_generation_probe("x.com", [], polite_sleep_s=0)
