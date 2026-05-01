"""wayback — Wayback Machine CDX recovery with multi-generation URL family probing.

Two layered primitives:

1. ``query_cdx(domain, ...)`` — thin wrapper over the Internet Archive's
   ``/cdx/search/cdx`` JSON endpoint with a strict allow-list of fields and
   an SSRF-safe URL builder. Returns one ``CdxEntry`` per snapshot.

2. ``wayback_multi_generation_probe(domain, url_family_generations, year=None)``
   — given a list of URL-family path prefixes that the SAME content has
   migrated through over time, probe each generation and return the FIRST
   that has snapshots for the requested year. The classic OSINT recovery
   primitive: don't declare a year unrecoverable until you've tried every
   path-shape the host has ever used.

Source incident:
    s30.x ICI ETF flow recovery (wojak-wojtek), 2026-04-29 — same dataset
    moved from ``/info/`` (2018) → ``/doc-server/info%3A`` (2020) →
    ``/system/files/<YYYY-MM>/`` (2023). Probing only the latest URL family
    "proved" 2018 had no snapshots; probing all three found 47.

    s31/s32 generalised this into a one-shot helper.

Usage:
    from scraperx.wayback import wayback_multi_generation_probe

    families = [
        "ici.org/info/",
        "ici.org/doc-server/info%3A",
        "ici.org/system/files/",
    ]
    result = wayback_multi_generation_probe("ici.org", families, year=2018)
    if result.ok:
        print(f"Found {len(result.entries)} snapshots via family {result.matched_family!r}")
    else:
        print(f"Truly unrecoverable across {len(families)} generations.")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = [
    "CdxEntry",
    "MultiGenerationResult",
    "WaybackError",
    "query_cdx",
    "wayback_multi_generation_probe",
]


CDX_BASE = "https://web.archive.org/cdx/search/cdx"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 scraperx-wayback/1.0"
)
# CDX returns a header row first; fields we ALWAYS request (in this order).
_CDX_FIELDS: tuple[str, ...] = (
    "original",
    "timestamp",
    "statuscode",
    "mimetype",
    "digest",
    "length",
)


class WaybackError(RuntimeError):
    """CDX endpoint returned a non-200, malformed JSON, or empty response."""


@dataclass
class CdxEntry:
    """One Wayback snapshot returned by the CDX API.

    Attributes:
        original_url: The URL that was archived (verbatim).
        timestamp:    Wayback timestamp YYYYMMDDhhmmss (UTC).
        statuscode:   HTTP status at archive time, "-" if unknown.
        mimetype:     Content-Type at archive time, "-" if unknown.
        digest:       Wayback content digest (sha1 of body).
        length:       Body length in bytes ("-" if unknown).
    """

    original_url: str
    timestamp: str
    statuscode: str = "-"
    mimetype: str = "-"
    digest: str = "-"
    length: str = "-"

    @property
    def replay_url(self) -> str:
        """The URL where you can fetch the archived copy back."""
        return f"https://web.archive.org/web/{self.timestamp}/{self.original_url}"


@dataclass
class MultiGenerationResult:
    """Outcome of a wayback_multi_generation_probe call.

    Attributes:
        domain:           The domain being recovered (verbatim).
        matched_family:   First URL-family pattern that produced ≥1 entry, or "".
        entries:          Snapshots from the matched family. Empty on miss.
        per_family_counts: dict[family_pattern → snapshot_count] across ALL probed
                           families (useful for "everything was tried" reports).
        errors:           Per-family error tuples for families that 5xx'd.
    """

    domain: str
    matched_family: str = ""
    entries: list[CdxEntry] = field(default_factory=list)
    per_family_counts: dict[str, int] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.entries) and bool(self.matched_family)


# ---------------------------------------------------------------------------
# CDX query
# ---------------------------------------------------------------------------


def _year_to_ts(year: int | None, *, end: bool) -> str | None:
    """Convert a 4-digit year to a CDX timestamp.

    CDX expects ``YYYYMMDDhhmmss`` — passing a bare year like ``2018`` is parsed
    as the timestamp ``2018`` (which is technically year 0 + 2018ms — IA either
    rejects it or returns 0 hits). End-of-year uses Dec 31 23:59:59.
    """
    if year is None:
        return None
    return f"{year:04d}1231235959" if end else f"{year:04d}0101000000"


def _build_cdx_url(
    url_pattern: str,
    *,
    from_year: int | None,
    to_year: int | None,
    limit: int,
    match_type: str,
) -> str:
    """Compose the CDX query URL with an explicit field allow-list."""
    if match_type not in ("exact", "prefix", "host", "domain"):
        raise ValueError(f"match_type must be one of exact/prefix/host/domain, got {match_type!r}")
    parts = [
        f"url={quote(url_pattern, safe=':/?&=%')}",
        f"output=json",
        f"limit={int(limit)}",
        f"matchType={match_type}",
        f"fl={','.join(_CDX_FIELDS)}",
    ]
    from_ts = _year_to_ts(from_year, end=False)
    to_ts = _year_to_ts(to_year, end=True)
    if from_ts is not None:
        parts.append(f"from={from_ts}")
    if to_ts is not None:
        parts.append(f"to={to_ts}")
    return CDX_BASE + "?" + "&".join(parts)


def _parse_cdx_response(body: str) -> list[CdxEntry]:
    """Parse CDX JSON: header row + N data rows, each a list aligned to _CDX_FIELDS."""
    if not body.strip():
        return []
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as e:
        raise WaybackError(f"CDX returned invalid JSON: {e} (first 200 chars: {body[:200]})") from e
    if not isinstance(rows, list) or not rows:
        return []
    # First row is field names; verify it matches _CDX_FIELDS so a future schema
    # change doesn't silently mis-align our typed parse.
    header = rows[0]
    if not isinstance(header, list) or tuple(header) != _CDX_FIELDS:
        raise WaybackError(f"CDX response header drift: expected {_CDX_FIELDS}, got {header}")
    entries: list[CdxEntry] = []
    for row in rows[1:]:
        if not isinstance(row, list):
            continue
        # Pad short rows with "-" so we never silently drop a snapshot whose
        # only sin is a missing optional field (length / digest are sometimes
        # blank in older crawls).
        padded = list(row) + ["-"] * (len(_CDX_FIELDS) - len(row))
        entries.append(
            CdxEntry(
                original_url=str(padded[0]),
                timestamp=str(padded[1]),
                statuscode=str(padded[2]),
                mimetype=str(padded[3]),
                digest=str(padded[4]),
                length=str(padded[5]),
            )
        )
    return entries


def query_cdx(
    url_pattern: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    limit: int = 1000,
    match_type: str = "prefix",
    timeout: int = 30,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[CdxEntry]:
    """Query the Wayback CDX API with explicit field whitelist.

    Args:
        url_pattern: URL or path-prefix to query, e.g. ``"example.com/info/"``.
        from_year:   Lower bound year (inclusive). None = open-ended.
        to_year:     Upper bound year (inclusive). None = open-ended.
        limit:       Max snapshots to return. 1000 is the soft IA cap; bumping
                     higher will still work but costs IA more.
        match_type:  "exact" | "prefix" | "host" | "domain". Default "prefix".
        timeout:     Per-request timeout in seconds.
        user_agent:  Request UA. Default is our scraperx-wayback string.

    Raises:
        WaybackError: On non-200 status or malformed JSON.
    """
    url = _build_cdx_url(
        url_pattern,
        from_year=from_year,
        to_year=to_year,
        limit=limit,
        match_type=match_type,
    )
    # Defensive: confirm we built a request to the expected host (no injection)
    host = urlparse(url).hostname or ""
    if host != "web.archive.org":
        raise WaybackError(f"refusing to query non-IA host: {host!r}")

    req = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — host pinned above
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise WaybackError(f"CDX returned HTTP {status}")
            body = resp.read().decode("utf-8", errors="replace")
    except WaybackError:
        raise
    except Exception as e:  # noqa: BLE001
        raise WaybackError(f"CDX fetch failed: {type(e).__name__}: {e}") from e

    return _parse_cdx_response(body)


# ---------------------------------------------------------------------------
# Multi-generation probe
# ---------------------------------------------------------------------------


def wayback_multi_generation_probe(
    domain: str,
    url_family_generations: Iterable[str],
    *,
    year: int | None = None,
    limit_per_family: int = 200,
    polite_sleep_s: float = 0.5,
    timeout: int = 30,
) -> MultiGenerationResult:
    """Probe each URL-family generation; return the FIRST with snapshots.

    Designed to never give a false-negative "year unrecoverable" verdict.
    The returned MultiGenerationResult always carries ``per_family_counts``
    so the caller can assert "we tried everything".

    Args:
        domain:                  The host being recovered (e.g. ``"ici.org"``).
                                 Stored on the result for telemetry.
        url_family_generations:  Iterable of URL-prefix strings, ordered by
                                 your hypothesis (usually newest → oldest, but
                                 the function is order-agnostic — first match wins).
        year:                    Target year. If None, no temporal filtering;
                                 ALL snapshots returned for the matched family.
        limit_per_family:        Max snapshots to fetch per family.
        polite_sleep_s:          Sleep between probes. CDX rate-limits aggressively
                                 on the free tier; 0.5s is the IA-recommended floor.
        timeout:                 Per-CDX-request timeout in seconds.

    Returns:
        MultiGenerationResult — check ``.ok`` for the binary "found something".
    """
    families = list(url_family_generations)
    if not families:
        raise ValueError("url_family_generations must contain at least one prefix")

    out = MultiGenerationResult(domain=domain)
    for i, family in enumerate(families):
        if i > 0 and polite_sleep_s > 0:
            time.sleep(polite_sleep_s)
        try:
            entries = query_cdx(
                family,
                from_year=year,
                to_year=year,
                limit=limit_per_family,
                match_type="prefix",
                timeout=timeout,
            )
        except WaybackError as e:
            out.errors.append((family, str(e)))
            out.per_family_counts[family] = 0
            continue

        out.per_family_counts[family] = len(entries)
        if entries and not out.matched_family:
            # First family with hits wins; keep walking remaining families
            # only to populate per_family_counts for the report.
            out.matched_family = family
            out.entries = entries

    return out


# ---------------------------------------------------------------------------
# Inline demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Build-URL demo (offline)
    sample_url = _build_cdx_url(
        "ici.org/info/",
        from_year=2018,
        to_year=2018,
        limit=10,
        match_type="prefix",
    )
    assert "from=2018" in sample_url and "matchType=prefix" in sample_url
    print("CDX URL:", sample_url)

    # Parse demo (offline)
    sample_body = json.dumps(
        [
            list(_CDX_FIELDS),
            ["http://ici.org/info/2018-01-01.html", "20180102030405", "200", "text/html", "ABC", "1234"],
        ]
    )
    entries = _parse_cdx_response(sample_body)
    assert len(entries) == 1 and entries[0].timestamp == "20180102030405"
    print("Parsed entry:", entries[0].replay_url)
