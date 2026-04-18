"""Shared HTTP / cache helpers for mention adapters.

Kept private (underscore prefix) — adapters import these; callers shouldn't.
Centralises the urllib boilerplate so we don't repeat 6x across hn/reddit/
stackoverflow/devto/arxiv/pwc.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_UA = "scraperx/github-analyzer (+https://github.com/prezis)"
DEFAULT_TIMEOUT = 10.0


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """GET `url` with optional querystring + JSON body. Returns parsed object.

    Raises `urllib.error.URLError` or `json.JSONDecodeError` — callers are
    expected to wrap in try/except and return []. We don't swallow here so
    test helpers can assert on specific failures.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    h = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    if headers:
        h.update(headers)

    req = urllib.request.Request(url, headers=h)
    logger.debug("mentions GET %s", url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw)


def http_get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Same as http_get_json but returns decoded text (for arXiv XML etc.)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    h = {"User-Agent": DEFAULT_UA}
    if headers:
        h.update(headers)

    req = urllib.request.Request(url, headers=h)
    logger.debug("mentions GET %s (text)", url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def safe_int(x, default: int = 0) -> int:
    """Coerce to int; return default on None / non-numeric."""
    if x is None:
        return default
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def safe_float(x, default: float | None = None):
    """Coerce to float; return default on None / non-numeric.

    Exists because some platforms (Reddit via CDN-cached responses, per API
    quirks) occasionally return numeric fields as strings. Keeps type
    contract honest in metadata dicts.
    """
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def safe_str(x, default: str = "") -> str:
    """Coerce to stripped string; return default on None."""
    if x is None:
        return default
    try:
        return str(x).strip()
    except Exception:
        return default


def cache_or_fetch(db, source: str, query: str, fetch_fn, ttl: int | None = None):
    """Cache-wrapper pattern: try cache first, fall through to fetch_fn().

    `fetch_fn()` returns a list of serializable dicts (ExternalMention.to_dict
    shape). We cache the dict form — adapters re-hydrate to ExternalMention
    before returning.

    If `db` is None, skip caching entirely — just call fetch_fn.
    """
    if db is None:
        return fetch_fn()

    cached = db.get_mentions_cache(source, query)
    if cached is not None:
        return cached

    fresh = fetch_fn()
    if fresh:  # never cache empty results — lets transient errors retry
        db.save_mentions_cache(source, query, fresh, ttl=ttl)
    return fresh
