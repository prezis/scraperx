"""scraperx.adaptive_select - Scrapling's headline parser feature: selectors that SURVIVE a redesign.

You save an element once (``auto_save=True``); when the site later renames classes or moves the markup, asking for
the same selector with ``adaptive=True`` relocates the element by similarity (tag, text, attributes, DOM path,
siblings) instead of returning nothing. Scraping jobs that run for months die of exactly this.

    >>> from scraperx.adaptive_select import parse
    >>> page = parse(html_v1, url="https://shop.example/list", adaptive=True)
    >>> page.css(".price", auto_save=True)            # first run: learn the element
    >>> page = parse(html_v2, url="https://shop.example/list", adaptive=True)
    >>> page.css(".price", adaptive=True)             # after a redesign: relocate it

Needs only the PARSER (``pip install scrapling``): no browser, no fetchers. Tell two selectors on one page apart with
``identifier=`` when they share a CSS string.

MEMORY ISOLATION (verified, scrapling 0.5): Scrapling keys its SQLite rows by ``(site, identifier)``, but "site" comes
from a public-suffix lookup, and for a host without a public TLD (``localhost``, an IP, ``.internal``, ``.example``,
``.local``) every site collapses into ONE key, so site B could be "healed" with site A's element. scraperx therefore
uses ONE DB FILE PER HOST by default (``~/.cache/scraperx/adaptive/<host>.db``). Passing ``db_path=`` opts out of that
and shares one file: fine for real domains, wrong for non-public hosts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .scrapling_stealth import ScraplingNotAvailable

__all__ = ["parse", "DEFAULT_DIR"]

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".cache", "scraperx", "adaptive")


def parse(html: str | bytes, *, url: str = "", adaptive: bool = True, db_path: str | None = None, **selector_kwargs: Any) -> Any:
    """HTML -> a Scrapling ``Selector``; with ``adaptive=True`` its memory lives in ``db_path``."""
    try:
        from scrapling.parser import Selector
    except ImportError as e:
        raise ScraplingNotAvailable(f"{e}. Install with: pip install scrapling") from e
    if not adaptive:
        return Selector(html, url=url, **selector_kwargs)
    if db_path:
        db = Path(db_path).expanduser()
    else:
        host = (urlparse(url).hostname or "local") if url else "local"
        db = Path(DEFAULT_DIR).expanduser() / (re.sub(r"[^a-z0-9.-]", "_", host.lower()) + ".db")
    db.parent.mkdir(parents=True, exist_ok=True)
    # The storage keys rows by the url's host: pass the full url so two sites never share a memory.
    storage_args = {"storage_file": str(db), "url": url or "scraperx.local"}
    if not urlparse(storage_args["url"]).scheme:
        storage_args["url"] = "https://" + storage_args["url"]
    return Selector(html, url=url, adaptive=True, storage_args=storage_args, **selector_kwargs)
