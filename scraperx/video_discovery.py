"""
Draft: scraperx/scraperx/video_discovery.py (NEW FILE).

Scans arbitrary webpages for embedded videos across 6 providers + HTML5.
Stdlib-only (BeautifulSoup optional — falls back to regex).

Integration notes:
- Integration agent must import VideoRef + discover_videos from __init__.py
- fetch_any_video_transcript() dispatches to YouTubeScraper or VimeoScraper
  based on provider. Wistia/JWPlayer/Brightcove — transcript NOT IMPLEMENTED
  in this first pass (return informational result).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Optional bs4 (fallback to regex if missing)
HAS_BS4 = False
try:
    from bs4 import BeautifulSoup  # noqa: F401

    HAS_BS4 = True
except ImportError:
    pass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Provider signatures — each returns (provider_key, video_id, canonical_url)
_YOUTUBE_IFRAME_RE = re.compile(
    r"(?:youtube\.com|youtube-nocookie\.com)/embed/([A-Za-z0-9_-]{6,})",
    re.IGNORECASE,
)
_YOUTUBE_WATCH_RE = re.compile(
    r"youtube\.com/watch\?[^\s\"']*v=([A-Za-z0-9_-]{6,})",
    re.IGNORECASE,
)
_YOUTU_BE_RE = re.compile(
    r"youtu\.be/([A-Za-z0-9_-]{6,})",
    re.IGNORECASE,
)
_VIMEO_IFRAME_RE = re.compile(
    r"player\.vimeo\.com/video/(\d+)(?:\?[^\"']*?h=([a-f0-9]+))?",
    re.IGNORECASE,
)
_VIMEO_PLAIN_RE = re.compile(
    r"(?:https?:)?//vimeo\.com/(\d+)",
    re.IGNORECASE,
)
_WISTIA_IFRAME_RE = re.compile(
    r"fast\.wistia\.(?:net|com)/embed/iframe/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_WISTIA_DIV_RE = re.compile(
    r"wistia_async_([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_JWPLAYER_IFRAME_RE = re.compile(
    r"(?:cdn\.jwplayer\.com|content\.jwplatform\.com)/players/([A-Za-z0-9]+)[-_]([A-Za-z0-9]+)\.html",
    re.IGNORECASE,
)
_BRIGHTCOVE_IFRAME_RE = re.compile(
    r"players\.brightcove\.net/(\d+)/([A-Za-z0-9_-]+)/index\.html\?[^\"']*?videoId=(\d+)",
    re.IGNORECASE,
)
_VIDEO_TAG_RE = re.compile(
    r"<video[^>]*\bsrc=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_SOURCE_TAG_RE = re.compile(
    r"<source[^>]*\bsrc=[\"']([^\"']+\.(?:mp4|m3u8|webm|ogg))[\"']",
    re.IGNORECASE,
)
_OG_VIDEO_RE = re.compile(
    r"<meta[^>]*property=[\"']og:video(?::secure_url|:url)?[\"'][^>]*content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_IFRAME_SRC_RE = re.compile(
    r"<iframe[^>]*\bsrc=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_JSON_LD_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class VideoRef:
    """Reference to a detected video embed on an arbitrary page."""

    provider: str  # youtube | vimeo | wistia | jwplayer | brightcove | html5
    id: str
    canonical_url: str
    embed_url: str
    page_url: str
    referer: str | None = None
    extra: dict = field(default_factory=dict, hash=False)

    def __hash__(self) -> int:
        return hash((self.provider, self.id))


def _is_safe_page_url(url: str) -> bool:
    """Reject SSRF vectors: file://, private IPs, loopback, link-local."""
    import ipaddress

    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return False
    host = p.hostname or ""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    except ValueError:
        low = host.lower()
        if low in {"localhost", "metadata.google.internal"} or low.endswith(".internal"):
            return False
    return True


def _fetch_html(url: str, timeout: int = 15) -> str:
    if not _is_safe_page_url(url):
        raise ValueError(f"page_url rejected by SSRF guard: scheme or host is private/loopback/reserved: {url}")
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"})
    with urlopen(req, timeout=timeout) as resp:
        # 10MB cap — bounds ReDoS risk on JSON-LD regex + general resource exhaustion
        body = resp.read(10 * 1024 * 1024)
        ct = resp.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in ct:
            encoding = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return body.decode(encoding, errors="replace")


def _normalize_url(url: str, base_page: str) -> str:
    """Handle //protocol-relative and relative URLs."""
    if url.startswith("//"):
        scheme = urlparse(base_page).scheme or "https"
        return f"{scheme}:{url}"
    return url


def _canonical_for_provider(provider: str, video_id: str, embed_url: str) -> str:
    if provider == "youtube":
        return f"https://www.youtube.com/watch?v={video_id}"
    if provider == "vimeo":
        return f"https://vimeo.com/{video_id}"
    if provider == "wistia":
        return f"https://fast.wistia.net/embed/iframe/{video_id}"
    if provider == "jwplayer":
        return f"https://cdn.jwplayer.com/players/{video_id}.html"
    if provider == "brightcove":
        return embed_url
    return embed_url


def _scan_html_for_videos(html: str, page_url: str) -> list[VideoRef]:
    """Regex-based scan. Works without bs4 but less thorough."""
    refs: list[VideoRef] = []
    seen: set[tuple[str, str]] = set()

    def _add(provider: str, vid: str, embed: str, extra: dict | None = None) -> None:
        key = (provider, vid)
        if key in seen:
            return
        seen.add(key)
        canonical = _canonical_for_provider(provider, vid, embed)
        refs.append(
            VideoRef(
                provider=provider,
                id=vid,
                canonical_url=canonical,
                embed_url=_normalize_url(embed, page_url),
                page_url=page_url,
                referer=page_url,
                extra=(extra or {}),
            )
        )

    # Iframe sources
    for m in _IFRAME_SRC_RE.finditer(html):
        src = m.group(1)
        if ym := _YOUTUBE_IFRAME_RE.search(src):
            _add("youtube", ym.group(1), src)
            continue
        if vm := _VIMEO_IFRAME_RE.search(src):
            extra = {"hash": vm.group(2)} if vm.group(2) else {}
            _add("vimeo", vm.group(1), src, extra=extra)
            continue
        if wm := _WISTIA_IFRAME_RE.search(src):
            _add("wistia", wm.group(1), src)
            continue
        if jm := _JWPLAYER_IFRAME_RE.search(src):
            _add("jwplayer", f"{jm.group(1)}-{jm.group(2)}", src)
            continue
        if bm := _BRIGHTCOVE_IFRAME_RE.search(src):
            _add(
                "brightcove",
                bm.group(3),
                f"https://players.brightcove.net/{bm.group(1)}/{bm.group(2)}/index.html?videoId={bm.group(3)}",
                extra={"account_id": bm.group(1), "player_id": bm.group(2)},
            )
            continue

    # YouTube watch / youtu.be links (sometimes inline without iframe)
    for m in _YOUTUBE_WATCH_RE.finditer(html):
        _add("youtube", m.group(1), f"https://www.youtube.com/watch?v={m.group(1)}")
    for m in _YOUTU_BE_RE.finditer(html):
        _add("youtube", m.group(1), f"https://youtu.be/{m.group(1)}")

    # Vimeo plain links
    for m in _VIMEO_PLAIN_RE.finditer(html):
        _add("vimeo", m.group(1), f"https://vimeo.com/{m.group(1)}")

    # Wistia div-embeds (JS-injected, common in b2b marketing)
    for m in _WISTIA_DIV_RE.finditer(html):
        vid = m.group(1)
        _add("wistia", vid, f"https://fast.wistia.net/embed/iframe/{vid}")

    # HTML5 <video> / <source>
    for m in _VIDEO_TAG_RE.finditer(html):
        src = m.group(1)
        _add("html5", src, src)
    for m in _SOURCE_TAG_RE.finditer(html):
        src = m.group(1)
        _add("html5", src, src)

    # og:video:url / og:video:secure_url
    for m in _OG_VIDEO_RE.finditer(html):
        src = m.group(1)
        _add("html5", src, src, extra={"origin": "og"})

    # JSON-LD VideoObject
    for m in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if not isinstance(d, dict):
                continue
            if d.get("@type") == "VideoObject":
                content_url = d.get("contentUrl") or d.get("embedUrl")
                if content_url:
                    _add("html5", content_url, content_url, extra={"origin": "json-ld"})

    return refs


def discover_videos(page_url: str, html: str | None = None, timeout: int = 15) -> list[VideoRef]:
    """Scan an arbitrary webpage for embedded videos.

    Args:
        page_url: URL of the page to analyze
        html: Optional pre-fetched HTML (e.g., from Playwright-rendered DOM)
        timeout: HTTP fetch timeout

    Returns:
        List of VideoRef, deduplicated by (provider, id).

    Examples:
        >>> refs = discover_videos("https://some-product-page.example/tour")
        >>> for r in refs:
        ...     print(r.provider, r.id, r.canonical_url)
    """
    if html is None:
        try:
            html = _fetch_html(page_url, timeout=timeout)
        except (HTTPError, URLError, OSError) as e:
            logger.warning("discover_videos fetch failed for %s: %s", page_url, e)
            return []
    return _scan_html_for_videos(html, page_url)


def fetch_any_video_transcript(
    url_or_page: str,
    force_whisper: bool = False,
    max_duration_minutes: int = 120,
):
    """Top-level dispatcher: direct video URL OR webpage containing embeds.

    For direct URLs, routes to YouTubeScraper or VimeoScraper.
    For webpages, runs discover_videos() and recurses into first found video.

    Providers with no transcript implementation (wistia/jwplayer/brightcove/html5)
    return a VideoRef list instead of a transcript for now.

    Returns:
        Either a transcript result (YouTubeResult or VimeoResult) OR a list[VideoRef]
        when no direct transcription is possible.
    """
    # Direct URL detection
    if "youtube.com/watch" in url_or_page or "youtu.be/" in url_or_page or "youtube.com/embed" in url_or_page:
        from scraperx.youtube_scraper import YouTubeScraper

        scraper = YouTubeScraper()
        return scraper.get_transcript(url_or_page, force_whisper=force_whisper)

    if "vimeo.com" in url_or_page:
        from scraperx.vimeo_scraper import VimeoScraper

        scraper = VimeoScraper()
        return scraper.get_transcript(
            url_or_page, force_whisper=force_whisper, max_duration_minutes=max_duration_minutes
        )

    # Generic page — discover, then recurse on first video
    refs = discover_videos(url_or_page)
    if not refs:
        return []
    first = refs[0]
    if first.provider == "youtube":
        from scraperx.youtube_scraper import YouTubeScraper

        return YouTubeScraper().get_transcript(first.canonical_url, force_whisper=force_whisper)
    if first.provider == "vimeo":
        from scraperx.vimeo_scraper import VimeoScraper

        return VimeoScraper().get_transcript(
            first.canonical_url,
            force_whisper=force_whisper,
            max_duration_minutes=max_duration_minutes,
            referer=first.referer,
        )
    # Other providers — return refs list (transcript not implemented yet)
    return refs
