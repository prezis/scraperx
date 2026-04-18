"""github.com/trending scraper — HTML-only (no API).

GitHub Trending has no public API. Parse the HTML. Prefer BeautifulSoup
(if `pip install scraperx[video-discovery]` provides it), fall back to
regex — same pattern as scraperx.video_discovery.

Returns: `list[TrendingRepo]` — stable shape across bs4/regex paths.

Cache key: `(since, language, spoken_language_code)` tuple → 6h TTL (forks
cache slot reused via `SocialDB.save_fork_cache` since it's the same 6h
window for the same kind of "moving list" data — or pass a raw ttl).

Signal: user can diff today's list vs last week's to spot sudden movers;
this is the "repo of the day" axis in the trust verdict.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from scraperx.github_analyzer.schemas import TrendingRepo

logger = logging.getLogger(__name__)

TRENDING_URL = "https://github.com/trending"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Optional bs4 (fallback to regex)
HAS_BS4 = False
try:
    from bs4 import BeautifulSoup  # noqa: F401

    HAS_BS4 = True
except ImportError:
    pass


# --- regex-only parser patterns -------------------------------------------------

# Each repo is one <article class="Box-row">...</article>.
# Slug is in <h2 class="h3 ..."><a ... href="/owner/repo" ...>
_ROW_SPLIT_RE = re.compile(r'<article class="Box-row">', re.IGNORECASE)
_SLUG_RE = re.compile(
    r'<h2[^>]*class="h3[^"]*"[^>]*>.*?<a[^>]*href="/([^/"]+)/([^/"]+)"',
    re.IGNORECASE | re.DOTALL,
)
_DESC_RE = re.compile(
    r'<p[^>]*class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>',
    re.IGNORECASE | re.DOTALL,
)
_LANG_RE = re.compile(
    r'<span[^>]*itemprop="programmingLanguage"[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)
_STARS_RE = re.compile(
    r'href="/[^/"]+/[^/"]+/stargazers"[^>]*>\s*(?:<svg[\s\S]*?</svg>)?\s*([\d,\.]+)',
    re.IGNORECASE,
)
_STARS_TODAY_RE = re.compile(
    r'([\d,]+)\s+stars?\s+(?:today|this week|this month)',
    re.IGNORECASE,
)


def _strip_html(text: str) -> str:
    """Strip HTML tags + normalize whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


def _int(s: str) -> int:
    """Parse '4,595' → 4595; '1.2k' → 1200 (GitHub uses commas, but be safe)."""
    s = s.strip().replace(",", "").replace(" ", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _parse_html_regex(html: str) -> list[TrendingRepo]:
    """Regex fallback — same shape as bs4 path."""
    out: list[TrendingRepo] = []
    # Split HTML into one chunk per row; skip the first (prelude).
    chunks = _ROW_SPLIT_RE.split(html)[1:]
    for chunk in chunks:
        slug = _SLUG_RE.search(chunk)
        if not slug:
            continue
        owner, repo = slug.group(1), slug.group(2)

        desc_match = _DESC_RE.search(chunk)
        description = _strip_html(desc_match.group(1)) if desc_match else ""

        lang_match = _LANG_RE.search(chunk)
        language = lang_match.group(1).strip() if lang_match else ""

        stars_match = _STARS_RE.search(chunk)
        stars = _int(stars_match.group(1)) if stars_match else 0

        today_match = _STARS_TODAY_RE.search(chunk)
        stars_today = _int(today_match.group(1)) if today_match else 0

        out.append(
            TrendingRepo(
                full_name=f"{owner}/{repo}",
                description=description,
                language=language,
                stars=stars,
                stars_today=stars_today,
                url=f"https://github.com/{owner}/{repo}",
            )
        )
    return out


def _parse_html_bs4(html: str) -> list[TrendingRepo]:
    """Preferred parser — more resilient to attribute reordering."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("article", class_="Box-row")
    out: list[TrendingRepo] = []
    for row in rows:
        heading = row.find("h2", class_="h3")
        link = heading.find("a") if heading else None
        href = link.get("href", "") if link else ""
        parts = [p for p in href.split("/") if p]
        if len(parts) < 2:
            continue
        owner, repo = parts[0], parts[1]

        desc_el = row.find("p", class_=lambda c: c and "col-9" in c)
        description = desc_el.get_text(strip=True) if desc_el else ""

        lang_el = row.find(attrs={"itemprop": "programmingLanguage"})
        language = lang_el.get_text(strip=True) if lang_el else ""

        stars = 0
        stars_anchor = row.find("a", href=f"/{owner}/{repo}/stargazers")
        if stars_anchor:
            stars = _int(stars_anchor.get_text(" ", strip=True))

        stars_today = 0
        text = row.get_text(" ", strip=True)
        today_match = _STARS_TODAY_RE.search(text)
        if today_match:
            stars_today = _int(today_match.group(1))

        out.append(
            TrendingRepo(
                full_name=f"{owner}/{repo}",
                description=description,
                language=language,
                stars=stars,
                stars_today=stars_today,
                url=f"https://github.com/{owner}/{repo}",
            )
        )
    return out


def parse_trending_html(html: str) -> list[TrendingRepo]:
    """Top-level parser — dispatches bs4 / regex based on availability.

    Returns [] on any error. Exported so tests can drive it directly with
    a fixture HTML file.
    """
    if not html:
        return []
    try:
        if HAS_BS4:
            return _parse_html_bs4(html)
        return _parse_html_regex(html)
    except Exception as e:
        logger.warning("parse_trending_html failed: %s", e)
        # Last-ditch: try the other parser
        try:
            return _parse_html_regex(html) if HAS_BS4 else []
        except Exception:
            return []


def fetch_trending(
    since: str = "daily",
    language: str = "",
    spoken_language_code: str = "",
    timeout: float = 10.0,
) -> list[TrendingRepo]:
    """Fetch + parse github.com/trending.

    Args:
        since: "daily" | "weekly" | "monthly" (GitHub's three windows)
        language: GitHub's language slug (e.g. "python", "rust", ""= all)
        spoken_language_code: "en" | "zh" | "" (empty = any)
        timeout: HTTP timeout in seconds.

    Returns:
        list[TrendingRepo]. Empty on network / parse failure.
    """
    since = since.lower() if since else "daily"
    if since not in {"daily", "weekly", "monthly"}:
        logger.warning("Invalid `since`=%r; defaulting to daily", since)
        since = "daily"

    params: dict[str, str] = {"since": since}
    if spoken_language_code:
        params["spoken_language_code"] = spoken_language_code

    # GitHub encodes language in the path (/trending/python), not querystring
    url = TRENDING_URL
    if language:
        url = f"{TRENDING_URL}/{urllib.parse.quote(language)}"
    url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    logger.debug("fetch_trending GET %s", url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("fetch_trending network error: %s", e)
        return []

    return parse_trending_html(html)
