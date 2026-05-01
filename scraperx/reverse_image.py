"""reverse_image — multi-engine reverse-image-search URL fan-out.

Builds the public web-search URL for each of 6 engines so a caller can either
(a) open them in a browser, or (b) drive them via Playwright with the
``image_url`` query already injected. Different engines hit different corpora:

    Engine     Corpus strength
    ------     ----------------------------------------------------------------
    Yandex     Russia / E-Europe + face crops; best general-purpose hit-rate
    Google     "Lens" — strong on ecommerce, generic web, products
    TinEye     Earliest known timestamps; good for "where did this leak first"
    SauceNAO   Anime / illustrations / fan-art (high precision, low coverage)
    Bing       Visual Search; good on Western news photography
    ascii2d    Bochi-flavoured Japanese illustration index

The function builds query URLs and (optionally) fetches each engine's results
page using ``smart_fetch`` so callers don't have to wire a Playwright stack.

Source incident:
    s31/s32 wojak-wojtek OSINT session, 2026-04-30 — image-attribution check
    on a profile-pic ran the same 6-engine fan-out by hand. This module ports
    the URL builders into a reusable primitive so a one-liner replaces the
    manual tab-spam.

Usage:
    from scraperx.reverse_image import reverse_image_search, ENGINES

    # 1) Just build the URLs (no fetch):
    urls = reverse_image_search("https://example.com/avatar.jpg", fetch=False)
    for hit in urls:
        print(f"{hit.engine}: {hit.search_url}")

    # 2) Fetch + capture each engine's HTML:
    hits = reverse_image_search("https://example.com/avatar.jpg", fetch=True)
    for hit in hits:
        if hit.body:
            print(f"{hit.engine}: {len(hit.body)} bytes")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ENGINES",
    "ENGINES",
    "EngineHit",
    "build_search_url",
    "reverse_image_search",
]


# Each entry: engine_name → URL template with ``{q}`` placeholder for the
# *URL-encoded image URL*. Templates verified 2026-05-01.
ENGINES: dict[str, str] = {
    "yandex":    "https://yandex.com/images/search?rpt=imageview&url={q}",
    "lens":      "https://lens.google.com/uploadbyurl?url={q}",
    "tineye":    "https://tineye.com/search?url={q}",
    "saucenao":  "https://saucenao.com/search.php?url={q}",
    "bing":      "https://www.bing.com/images/search?view=detailv2&iss=sbi&form=SBIVSP&sbisrc=UrlPaste&q=imgurl:{q}",
    "ascii2d":   "https://ascii2d.net/search/url/{q}",
}

DEFAULT_ENGINES: tuple[str, ...] = (
    "yandex", "lens", "tineye", "saucenao", "bing", "ascii2d",
)


@dataclass
class EngineHit:
    """Result of querying ONE engine.

    Attributes:
        engine:     Engine label (key from ENGINES).
        search_url: The URL where a human / scraper can view the results.
        body:       Optional HTML body (only populated when fetch=True succeeded).
        error:      Failure message, or empty.
    """

    engine: str
    search_url: str
    body: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.search_url) and not self.error


def _looks_like_private_host(image_url: str) -> bool:
    """Cheap private-host check (no DNS) — refuses obvious internal targets.

    Reverse-image engines must be able to fetch the image themselves. Pointing
    them at a private/loopback host either fails silently or, worse, lets a
    caller use this module to confirm internal-network reachability through
    a 3rd-party engine. Reject those URLs upfront.
    """
    from urllib.parse import urlparse
    host = (urlparse(image_url).hostname or "").lower()
    if not host:
        return True
    if host in ("localhost",) or host.endswith(".local"):
        return True
    # IPv4 private ranges + loopback
    if host.startswith(("10.", "127.", "169.254.", "192.168.", "0.")):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".", 2)[1])
            if 16 <= second <= 31:
                return True
        except (ValueError, IndexError):
            pass
    # IPv6 loopback / link-local
    if host in ("::1",) or host.startswith(("fc", "fd", "fe80")):
        return True
    return False


def build_search_url(engine: str, image_url: str) -> str:
    """Compose the reverse-image search URL for one engine.

    Args:
        engine:     Key from :data:`ENGINES`.
        image_url:  URL of the image to search. Must be HTTPS-reachable from
                    the search engine (most engines won't accept localhost or
                    behind-auth URLs).

    Raises:
        ValueError: Unknown engine key, malformed URL, or private/loopback host.
    """
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; choose from {sorted(ENGINES)}")
    if not image_url or not image_url.startswith(("http://", "https://")):
        raise ValueError(f"image_url must be http(s) URL, got {image_url!r}")
    if _looks_like_private_host(image_url):
        raise ValueError(f"refusing to send private/loopback host to a reverse-image engine: {image_url!r}")
    return ENGINES[engine].format(q=quote(image_url, safe=""))


def reverse_image_search(
    image_url: str,
    engines: Iterable[str] = DEFAULT_ENGINES,
    *,
    fetch: bool = False,
    timeout: int = 30,
) -> list[EngineHit]:
    """Build (and optionally fetch) reverse-image search URLs across N engines.

    Args:
        image_url: URL of the image to search.
        engines:   Iterable of engine keys (default = all 6).
        fetch:     If True, also fetch each engine's results HTML using
                   :func:`scraperx.fetch.smart_fetch`. Default False (just
                   builds the URLs — much faster, no network calls).
        timeout:   Per-engine fetch timeout in seconds (only applies if fetch=True).

    Returns:
        list[EngineHit] in the order engines were requested.
    """
    out: list[EngineHit] = []
    requested = list(engines)
    for eng in requested:
        try:
            url = build_search_url(eng, image_url)
        except ValueError as e:
            out.append(EngineHit(engine=eng, search_url="", error=str(e)))
            continue
        hit = EngineHit(engine=eng, search_url=url)
        if fetch:
            try:
                # Lazy import — keeps this module callable without a fetch dep
                from scraperx.fetch import smart_fetch

                fr = smart_fetch(url, timeout=timeout)
                if fr.ok:
                    hit.body = fr.content
                else:
                    hit.error = "; ".join(f"{m}:{e}" for m, e in fr.errors) or "fetch failed"
            except Exception as e:  # noqa: BLE001
                hit.error = f"{type(e).__name__}: {e}"
        out.append(hit)
    return out


# ---------------------------------------------------------------------------
# Inline demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    sample = "https://example.com/avatar.jpg"
    hits = reverse_image_search(sample, fetch=False)
    for h in hits:
        print(f"{h.engine:10s}  {h.search_url[:80]}…")
    assert all(h.ok for h in hits)
    assert len(hits) == len(DEFAULT_ENGINES)
    assert quote(sample, safe="") in hits[0].search_url
