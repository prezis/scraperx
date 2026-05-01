"""Tests for scraperx.pdf_text_parser — column-band tokeniser + state machine.

No real PDF needed: ``extract_text_per_page`` is monkey-patched to return
canned per-page text, exercising the parser end-to-end.
"""

from __future__ import annotations

import pytest

from scraperx import pdf_text_parser as pdf_mod
from scraperx.pdf_text_parser import (
    ColumnRow,
    ParseResult,
    _classify_tokens,
    parse_pdf_with_columns,
    tokenise_column_band,
)


# ---------------------------------------------------------------------------
# tokenise_column_band
# ---------------------------------------------------------------------------


def test_tokenise_whitespace_run_split():
    line = "Information Technology   28.5    21.3   +14.2%"
    out = tokenise_column_band(line, None)
    assert out == ["Information Technology", "28.5", "21.3", "+14.2%"]


def test_tokenise_column_bands():
    line = "InfoTech            28.5     21.3      +14.2%"
    bands = [(0, 20), (20, 30), (30, 40), (40, 60)]
    out = tokenise_column_band(line, bands)
    assert out[0] == "InfoTech"
    assert "28.5" in out[1]


def test_tokenise_empty_line():
    assert tokenise_column_band("", None) == []
    assert tokenise_column_band("    ", None) == []


def test_tokenise_band_oob_safe():
    """``line[lo:hi]`` past EOL must return empty, not raise."""
    out = tokenise_column_band("short", [(100, 200)])
    assert out == []


# ---------------------------------------------------------------------------
# _classify_tokens
# ---------------------------------------------------------------------------


def test_classify_simple_sector_row():
    label, vals = _classify_tokens(["Information Technology", "28.5", "21.3", "+14.2%"])
    assert label == "Information Technology"
    assert vals == ["28.5", "21.3", "+14.2%"]


def test_classify_strips_leading_row_index():
    """A leading ``"1"`` row index must NOT eat the label."""
    label, vals = _classify_tokens(["1", "InfoTech", "28.5", "21.3"])
    assert label == "InfoTech"
    assert vals == ["28.5", "21.3"]


def test_classify_drops_trailing_footnote_marker():
    label, vals = _classify_tokens(["Health Care", "21.0", "*"])
    assert label == "Health Care"
    assert vals == ["21.0"]


def test_classify_label_only_row_returns_no_values():
    label, vals = _classify_tokens(["Banks", "Insurance"])
    assert label == "Banks Insurance"
    assert vals == []


def test_classify_empty_input():
    assert _classify_tokens([]) == ("", [])


# ---------------------------------------------------------------------------
# parse_pdf_with_columns end-to-end (text monkeypatched)
# ---------------------------------------------------------------------------


_FAKE_PDF_PAGES = [
    """\
SOME HEADER LINE
Sector Earnings Revisions

Information Technology   28.5    21.3   +14.2%
Health Care              21.0    19.5   +3.5%
Financials               14.2    12.0   +1.1%

Source: Yardeni Research, Inc.
disclaimer text follows
""",
]


def _patch_extract(monkeypatch, pages: list[str], method: str = "pdfplumber"):
    monkeypatch.setattr(pdf_mod, "extract_text_per_page", lambda _p: (pages, method))


def test_parse_extracts_three_rows(monkeypatch, tmp_path):
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    _patch_extract(monkeypatch, _FAKE_PDF_PAGES)

    result = parse_pdf_with_columns(
        path=pdf,
        section_re=r"Sector Earnings Revisions",
        footer_re=r"Source: Yardeni",
    )
    assert isinstance(result, ParseResult)
    assert result.ok
    assert len(result.rows) == 3
    labels = [r.label for r in result.rows]
    assert "Information Technology" in labels
    assert "Health Care" in labels
    assert "Financials" in labels
    # All rows must carry their numeric values
    assert all(len(r.values) >= 1 for r in result.rows)


def test_parse_section_close_drops_post_footer(monkeypatch, tmp_path):
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    _patch_extract(monkeypatch, _FAKE_PDF_PAGES)
    result = parse_pdf_with_columns(
        path=pdf,
        section_re=r"Sector Earnings Revisions",
        footer_re=r"Source: Yardeni",
    )
    # The "disclaimer text follows" line lives AFTER the footer; it must NOT
    # leak into rows.
    raws = [r.raw for r in result.rows]
    assert not any("disclaimer" in raw for raw in raws)


def test_parse_applies_sector_aliases(monkeypatch, tmp_path):
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    pages = [
        "Sector P/E\nInfoTech   25.0  20.0\nFinancials   12.0  11.0\nSource: X\n"
    ]
    _patch_extract(monkeypatch, pages)
    result = parse_pdf_with_columns(
        path=pdf,
        section_re=r"Sector P/E",
        footer_re=r"Source:",
        sector_aliases={"InfoTech": "Information Technology"},
    )
    labels = [r.label for r in result.rows]
    assert "Information Technology" in labels
    assert "Financials" in labels  # un-aliased label preserved verbatim


def test_parse_min_values_per_row_filter(monkeypatch, tmp_path):
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    pages = [
        "Section\nLabel only line\nLabel  10.0\nLabel  10.0  20.0\nFooter\n"
    ]
    _patch_extract(monkeypatch, pages)
    result = parse_pdf_with_columns(
        path=pdf,
        section_re=r"Section",
        footer_re=r"Footer",
        min_values_per_row=2,
    )
    # Only the row with ≥2 numeric values survives
    assert len(result.rows) == 1
    assert result.rows[0].values == ["10.0", "20.0"]


def test_parse_extraction_failure_recorded(monkeypatch, tmp_path):
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")

    def boom(_p):
        raise RuntimeError("pdftotext missing")

    monkeypatch.setattr(pdf_mod, "extract_text_per_page", boom)
    result = parse_pdf_with_columns(
        path=pdf,
        section_re=r"x",
        footer_re=r"y",
    )
    assert not result.ok
    assert result.errors
    assert "pdftotext missing" in result.errors[0]
