"""pdf_text_parser — generic column-band tokeniser for multi-column research PDFs.

Many research PDFs (Yardeni chartbooks, Topdown Charts, MSCI factsheets) lay
out tables in 2-or-3 vertical columns where the column geometry is consistent
across pages but no formal table structure is embedded. ``pdftotext -raw``
returns a soup; ``pdftotext -layout`` is closer but still mixes columns
when band gutters narrow.

This module ports a parser shape that proved itself in s31/s32 (Yardeni
sp546fundamentals + mscinetearnrev): a 3-state machine that walks lines,
slices by configurable column bands, applies a section-bounding regex, and
maps free-text labels through a sector-name alias table.

Source incident:
    s31/s32 wojak-wojtek OSINT session, 2026-04-30 — extracted sector P/E,
    earnings-revision, and fundamental tables from Yardeni PDFs after OCR
    consistently dropped columns 2-3. Inspired ``scripts/fetch_yardeni_*_text.py``.

Dependencies:
    - ``pdfplumber`` (preferred; column-aware extraction). Falls back to
      ``pdftotext -layout`` subprocess if pdfplumber is not installed.

Usage:
    from scraperx.pdf_text_parser import parse_pdf_with_columns

    rows = parse_pdf_with_columns(
        path="/tmp/yardeni_sp546fundamentals.pdf",
        section_re=r"Sector Earnings Revisions",
        footer_re=r"Source: Yardeni Research",
        sector_aliases={"Info Tech": "Information Technology", "Comm Svcs": "Communication Services"},
        column_bands=[(0, 250), (250, 500), (500, 750)],
    )
    for r in rows:
        print(r.label, r.values)
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ColumnRow",
    "ParseResult",
    "extract_text_per_page",
    "parse_pdf_with_columns",
    "tokenise_column_band",
]


@dataclass
class ColumnRow:
    """One row extracted from a column band.

    Attributes:
        label:    First non-numeric token block (typically sector / asset name).
        values:   Numeric tokens after the label, in document order.
        page:     1-based page number where this row was found.
        raw:     The raw line text (debugging aid).
    """

    label: str
    values: list[str] = field(default_factory=list)
    page: int = 0
    raw: str = ""


@dataclass
class ParseResult:
    """Outcome of one parse_pdf_with_columns call."""

    path: str
    rows: list[ColumnRow] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    pages_scanned: int = 0
    extraction_method: str = ""  # "pdfplumber" | "pdftotext"
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and bool(self.extraction_method)


# Token shapes. We keep these tight (anti-backtracking) — the s31 incident
# exposed a quadratic regex that spiked on a 60-page chartbook.
_NUMERIC_TOKEN_RE = re.compile(r"[+\-]?\d+(?:[.,]\d+)?%?")
_LABEL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9 &/\-\.]*")
_WHITESPACE_RUN_RE = re.compile(r" {2,}")  # column gutters = ≥2 spaces


# ---------------------------------------------------------------------------
# Text extraction (two backends)
# ---------------------------------------------------------------------------


def extract_text_per_page(path: str | Path) -> tuple[list[str], str]:
    """Extract per-page text from a PDF. Returns (pages, method_used).

    Tries pdfplumber first (column-aware), then falls back to ``pdftotext -layout``.
    Raises FileNotFoundError if neither backend can read the file.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF not found: {p}")

    # pdfplumber path
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        pdfplumber = None  # type: ignore

    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(p)) as pdf:
                pages = [(page.extract_text() or "") for page in pdf.pages]
            return pages, "pdfplumber"
        except Exception as e:  # noqa: BLE001
            logger.debug("pdfplumber failed on %s: %s — falling back to pdftotext", p, e)

    # pdftotext fallback
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(p), "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "Neither pdfplumber nor pdftotext is available. "
            "Install one: `pip install pdfplumber` or `apt install poppler-utils`."
        ) from e

    if result.returncode != 0:
        raise RuntimeError(f"pdftotext exit {result.returncode}: {result.stderr[:200]}")

    # pdftotext -layout uses a form-feed (\x0c) between pages
    pages = result.stdout.split("\x0c")
    return pages, "pdftotext"


# ---------------------------------------------------------------------------
# Column tokenisation
# ---------------------------------------------------------------------------


def tokenise_column_band(
    line: str,
    column_bands: list[tuple[int, int]] | None,
) -> list[str]:
    """Slice a line into column tokens.

    Two modes:
        - ``column_bands=None``: split on runs of ≥2 spaces (works when the
          PDF extractor preserved gutter whitespace).
        - ``column_bands=[(lo,hi), …]``: slice the raw line by character
          offset. Works when columns are aligned but inner whitespace varies.
    """
    if not line:
        return []
    if column_bands is None:
        return [tok.strip() for tok in _WHITESPACE_RUN_RE.split(line) if tok.strip()]
    out: list[str] = []
    for lo, hi in column_bands:
        chunk = line[lo:hi].strip()
        if chunk:
            out.append(chunk)
    return out


def _classify_tokens(tokens: list[str]) -> tuple[str, list[str]]:
    """Split tokens into (label, numeric_values).

    Strategy:
        1. Skip a leading PURE numeric token if present (row index like "1").
        2. Accumulate non-numeric tokens as the label until the first numeric.
        3. Treat all subsequent numeric tokens as values; drop non-numeric
           trailing tokens (footnote markers like "*", "(a)").

    Returning ("", []) means caller should drop the row.
    """
    if not tokens:
        return "", []

    work = list(tokens)
    # Step 1 — strip leading row index. ONLY if a label clearly follows.
    if work and _NUMERIC_TOKEN_RE.fullmatch(work[0]):
        if len(work) >= 2 and _LABEL_TOKEN_RE.fullmatch(work[1]) and not _NUMERIC_TOKEN_RE.fullmatch(work[1]):
            work = work[1:]

    label_parts: list[str] = []
    values: list[str] = []
    consumed_label = False
    for tok in work:
        if not consumed_label and _LABEL_TOKEN_RE.fullmatch(tok) and not _NUMERIC_TOKEN_RE.fullmatch(tok):
            label_parts.append(tok)
            continue
        consumed_label = True
        if _NUMERIC_TOKEN_RE.fullmatch(tok) or _NUMERIC_TOKEN_RE.match(tok):
            values.append(tok)
        # Non-numeric trailing tokens are dropped (footnote markers, etc.)
    return " ".join(label_parts).strip(), values


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


def parse_pdf_with_columns(
    path: str | Path,
    *,
    section_re: str,
    footer_re: str,
    sector_aliases: dict[str, str] | None = None,
    column_bands: list[tuple[int, int]] | None = None,
    min_values_per_row: int = 1,
) -> ParseResult:
    """Parse a multi-column research PDF into typed rows.

    State machine:
        - OUT  : skip lines until a section_re match opens a band.
        - IN   : tokenise each line; emit ColumnRow when ≥1 numeric value.
        - OUT' : footer_re match closes the band (back to OUT).

    Args:
        path:               Path to the PDF.
        section_re:         Regex (string) that, when found in a line, OPENS the
                            extraction band. Multiple sections allowed; each is
                            recorded on result.sections_found.
        footer_re:          Regex (string) that CLOSES the band. Typical pattern:
                            ``r"Source: <publisher>"`` or page-number markers.
        sector_aliases:     Optional ``{found_label → canonical_label}`` map
                            applied to the LABEL field after classification.
        column_bands:       Optional list of (start, end) char offsets per
                            column. None = split on whitespace runs.
        min_values_per_row: Skip rows with fewer numeric values than this.
                            Default 1 (any row with at least one number).

    Returns:
        ParseResult — check ``.ok`` for success.
    """
    section_pat = re.compile(section_re)
    footer_pat = re.compile(footer_re)
    aliases = sector_aliases or {}

    out = ParseResult(path=str(path))
    try:
        pages, method = extract_text_per_page(path)
    except Exception as e:  # noqa: BLE001
        out.errors.append(f"{type(e).__name__}: {e}")
        return out

    out.extraction_method = method
    out.pages_scanned = len(pages)

    in_band = False
    for page_no, page_text in enumerate(pages, start=1):
        for line in page_text.splitlines():
            if section_pat.search(line):
                in_band = True
                out.sections_found.append(line.strip())
                continue
            if in_band and footer_pat.search(line):
                in_band = False
                continue
            if not in_band:
                continue

            tokens = tokenise_column_band(line, column_bands)
            if not tokens:
                continue
            label, values = _classify_tokens(tokens)
            if not label or len(values) < min_values_per_row:
                continue
            label = aliases.get(label, label)
            out.rows.append(
                ColumnRow(label=label, values=values, page=page_no, raw=line.rstrip())
            )

    return out


# ---------------------------------------------------------------------------
# Inline demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    line = "Information Technology   28.5    21.3   +14.2%"
    toks = tokenise_column_band(line, None)
    label, vals = _classify_tokens(toks)
    print(f"label={label!r} values={vals}")
    assert label == "Information Technology"
    assert vals == ["28.5", "21.3", "+14.2%"]

    # Column-band slicing
    fixed = "InfoTech            28.5     21.3      +14.2%"
    bands = [(0, 20), (20, 30), (30, 40), (40, 60)]
    sliced = tokenise_column_band(fixed, bands)
    print(f"banded={sliced}")
    assert sliced[0] == "InfoTech"
