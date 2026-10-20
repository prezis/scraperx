"""scraperx.page_markdown - clean, LLM-ready Markdown from a page, with a sanitiser in the loop.

A scraper that feeds raw page text to an agent is a prompt-injection channel: pages hide instructions from humans
(display:none, offscreen text) and show them to the model. Two layers run before the Markdown conversion:

1. scraperx's own pass (``_strip_extra_hidden``): elements with the ``hidden`` attribute and elements positioned
   offscreen / clipped / scaled to nothing. VERIFIED GAPS in Scrapling 0.5's sanitiser: it keeps ``<p hidden>``,
   ``left:-9999px`` and ``text-indent:-9999px`` text.
2. Scrapling's sanitiser (the same cleaning its MCP server does): scripts, styles, noscript, templates, comments,
   hidden inputs, display:none, visibility:hidden, opacity:0, font-size:0, aria-hidden, height:0+overflow.

NOT detectable statically, so NOT removed: text coloured like its background, tiny-contrast text, content behind a
closed ``<details>``. Markdown from an untrusted page stays untrusted data: never treat it as instructions.

    html_to_markdown(html, ...)   you already have HTML (any scraperx leg produced it)
    fetch_markdown(url, ...)      SSRF-guarded fetch (curl_cffi TLS impersonation, or the stealth browser) -> Markdown

Needs ``pip install 'scraperx[markdown]'`` (markdownify). ``html_to_markdown`` leans on Scrapling's ``Convertor``
helpers, internal upstream; ``tests/test_scrapling_caps.py`` pins their existence, so a rename fails loudly instead of
silently skipping the sanitiser.
"""

from __future__ import annotations

import re
from typing import Any

from .fetch import _check_ssrf
from .scrapling_stealth import ScraplingNotAvailable

__all__ = ["html_to_markdown", "fetch_markdown", "MarkdownUnavailable"]

# Matched against the style attribute with ALL whitespace removed and lowercased.
_HIDDEN_STYLE = re.compile(
    r"(?:left|right|top|bottom|text-indent|margin-left|margin-right|margin-top):-\d{3,}"
    r"|clip:rect\(0(?:px)?,0(?:px)?,0(?:px)?,0(?:px)?\)"
    r"|clip-path:inset\(100%\)"
    r"|transform:scale\(0\)"
)


class MarkdownUnavailable(RuntimeError):
    """markdownify (or scrapling) is not installed."""


def _strip_extra_hidden(html: str) -> str:
    """Drop elements that are invisible to a human but survive Scrapling's sanitiser. Never raises."""
    try:
        from lxml import html as lh

        root = lh.document_fromstring(html)
    except Exception:  # noqa: BLE001 - empty/garbage input: let the converter decide
        return html
    doomed = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        style = re.sub(r"\s+", "", (el.get("style") or "").lower())
        if el.get("hidden") is not None or (style and _HIDDEN_STYLE.search(style)):
            doomed.append(el)
    for el in doomed:
        if el.getparent() is not None:
            el.drop_tree()  # keeps the element's tail text, which belongs to the parent
    return lh.tostring(root, encoding="unicode")


def html_to_markdown(
    html: str, *, css_selector: str | None = None, main_content_only: bool = False, url: str = ""
) -> str:
    """HTML -> sanitised Markdown (see the module docstring for exactly what is and is not removed)."""
    try:
        from scrapling.core.shell import Convertor
        from scrapling.parser import Selector

        import markdownify  # noqa: F401  (Convertor imports it lazily; fail here with a clear message instead)
    except ImportError as e:
        raise MarkdownUnavailable(f"{e}. Install with: pip install 'scraperx[markdown]'") from e
    page: Any = Selector(_strip_extra_hidden(html), url=url)
    if main_content_only:
        body = page.css("body").first
        page = body if body is not None else page
    page = Convertor._sanitize_for_ai(Convertor._strip_noise_tags(page))
    pages = [page] if not css_selector else list(page.css(css_selector))
    return "\n\n".join(Convertor._convert_to_markdown(el.html_content) for el in pages)


def fetch_markdown(
    url: str,
    *,
    stealth: bool = False,
    css_selector: str | None = None,
    main_content_only: bool = True,
    timeout: int = 30,
    impersonate: str = "chrome",
    allow_private: bool = False,
    stealth_kwargs: dict[str, Any] | None = None,
) -> str:
    """Fetch ``url`` and return sanitised Markdown. ``stealth=True`` uses the patched browser (Cloudflare solver)."""
    why = _check_ssrf(url, allow_private=allow_private)
    if why:
        raise ValueError(f"blocked by SSRF guard: {why}")
    try:
        if stealth:
            from scrapling.fetchers import StealthyFetcher

            kw: dict[str, Any] = {"headless": True, "network_idle": True, "solve_cloudflare": True,
                                  "timeout": max(int(timeout) * 1000, 5_000)}
            kw.update(stealth_kwargs or {})
            page = StealthyFetcher.fetch(url, **kw)
        else:
            from scrapling.fetchers import Fetcher

            page = Fetcher.get(url, impersonate=impersonate, timeout=timeout, stealthy_headers=True)
    except ImportError as e:
        raise ScraplingNotAvailable(f"{e}. Install with: pip install 'scraperx[stealth]'") from e
    # One sanitiser path for every leg: our extra pass + Scrapling's, not Response.markdown() alone.
    return html_to_markdown(str(page.html_content), css_selector=css_selector, main_content_only=main_content_only, url=url)
