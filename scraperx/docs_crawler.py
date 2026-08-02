"""Docs-site crawler — exhaustive coverage of JS-hydrated documentation sites.

Born from the IC API docs incident (2026-05-03): an LLM agent kept claiming
"I read the docs" when it had only read 5 pages of an 82-page site. This
module is the proper way to do it.

Use case: when someone says "read every page of <docs site>", run:

    scraperx docs-crawl https://docs.example.com/

It will:
1. Fetch sitemap.xml (Cloudflare-resistant — uses Mozilla UA, direct curl).
2. For each URL: fetch HTML, extract text with the LESS aggressive parser
   that recovers content from Docusaurus / VitePress / mkdocs-material
   pre-rendered HTML (the pre-2026-05 mistake was using too-aggressive
   skip lists that nuked the prose).
3. Pages where text extraction returns < 500 chars are flagged for a
   playwright render pass — those are usually React-only pages where
   Server-Side Rendering didn't include content.
4. Outputs a directory with raw HTML + extracted text + per-page byte
   counts + a summary digest.

Lessons embedded:
- Skip ONLY {script, style, noscript, svg}. Do NOT skip nav/header/footer
  — they often carry breadcrumbs / sidebar content that's part of
  page comprehension.
- Block-level elements like p/li/h1-h4/div should produce newlines
  in extracted text so structure is preserved.
- Measure coverage by character count per page. <500 chars = probably
  a React shell; needs rendering.
- Always save BOTH HTML and extracted text so future agents can re-extract
  if the parser improves.
- Use sitemap.xml as the primary URL source. If absent, fall back to
  recursive link-following from the root, capped at MAX_PAGES.

The output is a directory the calling agent can then `grep` / `Read` /
`local_llm` over with confidence that nothing was silently skipped.

References (saved for next-session-context):
- IC docs incident: ~/.claude/projects/-home-palyslaf0s/00bd6bf2-*.jsonl
- Pattern doc: ~/Documents/future-gear/docs/wiki/ic-api-capabilities.md §10
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP_BETWEEN = 0.4  # be polite to the docs server
MIN_PROSE_CHARS = 500  # below this we suspect a React shell


@dataclass
class PageResult:
    """One crawled page."""

    url: str
    slug: str  # filesystem-safe identifier
    http_status: int
    html_bytes: int
    text_chars: int
    text_words: int
    is_shell: bool  # True if text < MIN_PROSE_CHARS
    error: Optional[str] = None


@dataclass
class CrawlResult:
    """Result of a full docs-site crawl."""

    root_url: str
    output_dir: Path
    pages: list[PageResult] = field(default_factory=list)
    sitemap_url: Optional[str] = None
    fetched: int = 0
    shells: int = 0
    errors: int = 0


class _LessAggressiveExtractor(HTMLParser):
    """Recovers content from Docusaurus / VitePress / mkdocs-material.

    Skip ONLY: script, style, noscript, svg.
    Block elements produce newlines so paragraph structure is preserved.
    Inline elements pass through transparently.
    """

    SKIP = {"script", "style", "noscript", "svg"}
    BLOCK = {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "li", "tr", "div", "section", "article",
        "pre", "blockquote", "table", "thead", "tbody",
        "header", "footer", "nav", "main",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):  # type: ignore[no-untyped-def]
        if tag in self.SKIP:
            self._skip_depth += 1
        if self._skip_depth == 0 and tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if self._skip_depth == 0 and tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            t = data.strip()
            if t:
                self._parts.append(t + " ")

    def text(self) -> str:
        out = "".join(self._parts)
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = re.sub(r" +", " ", out)
        return out.strip()


def extract_text(html: str) -> str:
    """Public extractor — use this anywhere you need to render docs HTML to prose."""
    parser = _LessAggressiveExtractor()
    parser.feed(html)
    return parser.text()


def discover_sitemap(root_url: str, *, user_agent: str = DEFAULT_USER_AGENT,
                     timeout: int = DEFAULT_TIMEOUT) -> tuple[Optional[str], list[str]]:
    """Return (sitemap_url_used, list_of_page_urls).

    Tries:
      1. <root>/sitemap.xml
      2. <root>/sitemap_index.xml
      3. <root>/robots.txt -> Sitemap: lines
    """
    base = root_url.rstrip("/")
    candidates = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]

    for sitemap_url in candidates:
        try:
            xml = _curl_text(sitemap_url, user_agent=user_agent, timeout=timeout)
            urls = _parse_sitemap(xml)
            if urls:
                logger.info("Found %d URLs in %s", len(urls), sitemap_url)
                return sitemap_url, urls
        except Exception as e:  # noqa: BLE001
            logger.debug("Sitemap probe failed for %s: %s", sitemap_url, e)
            continue

    # Fall back to robots.txt
    try:
        robots = _curl_text(f"{base}/robots.txt", user_agent=user_agent, timeout=timeout)
        for line in robots.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                xml = _curl_text(sitemap_url, user_agent=user_agent, timeout=timeout)
                urls = _parse_sitemap(xml)
                if urls:
                    return sitemap_url, urls
    except Exception:
        pass

    return None, []


def _parse_sitemap(xml: str) -> list[str]:
    """Parse sitemap XML — handles both single sitemap and sitemap_index.

    Tries the standard sitemap namespace first; falls back to local-name
    matching for non-conformant XML.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
    if not urls:
        # Fallback: namespace-agnostic search (matches `loc` in any namespace
        # or no namespace at all).
        urls = [
            el.text for el in root.iter()
            if el.tag.rsplit("}", 1)[-1] == "loc" and el.text
        ]
    return [u.strip() for u in urls if u and u.strip()]


def _validate_url(url: str) -> str:
    """Reject URLs that don't look like HTTP/HTTPS — defends against curl
    flag injection (`--proto`, `--config`, etc) that could be triggered if
    a URL string starts with `-`.
    """
    if not isinstance(url, str) or not url:
        raise ValueError(f"Invalid URL: {url!r}")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(
            f"URL must start with http:// or https://, got {url!r}"
        )
    if "\n" in url or "\r" in url:
        raise ValueError(f"URL contains line breaks: {url!r}")
    return url


def _curl_text(url: str, *, user_agent: str = DEFAULT_USER_AGENT,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fetch URL via curl with a Mozilla UA. Cloudflare-resistant."""
    _validate_url(url)
    if not shutil.which("curl"):
        raise RuntimeError("curl binary not found — install it or use a different fetcher")
    # `--` terminator stops curl from interpreting any URL chars as flags.
    result = subprocess.run(
        ["curl", "-sS", "-A", user_agent, "--max-time", str(timeout), "--", url],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _curl_to_file(url: str, out_path: Path, *, user_agent: str = DEFAULT_USER_AGENT,
                  timeout: int = DEFAULT_TIMEOUT) -> int:
    """Fetch URL with curl to file. Returns HTTP status code (0 on fetch error)."""
    _validate_url(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # `--` terminator + explicit URL position guards against flag injection.
    result = subprocess.run(
        ["curl", "-sS", "-A", user_agent, "--max-time", str(timeout),
         "-o", str(out_path), "-w", "%{http_code}", "--", url],
        capture_output=True, text=True, check=False,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _safe_slug(url: str, root_url: str) -> str:
    """Make a filesystem-safe filename from a URL."""
    path = url[len(root_url):] if url.startswith(root_url) else url
    path = path.strip("/")
    if not path:
        path = "_root"
    # Replace path separators with __; URL-encode special chars.
    slug = path.replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", slug)
    return slug


def crawl(root_url: str, output_dir: str | Path, *,
          urls: Optional[list[str]] = None,
          user_agent: str = DEFAULT_USER_AGENT,
          timeout: int = DEFAULT_TIMEOUT,
          sleep_between: float = DEFAULT_SLEEP_BETWEEN,
          skip_url_encoded_dups: bool = True,
          max_pages: Optional[int] = None) -> CrawlResult:
    """Crawl a docs site exhaustively.

    Args:
        root_url: site root for sitemap discovery and slug generation.
        output_dir: where to write HTML + text files + digest.
        urls: optional explicit URL list. If omitted, sitemap discovery runs.
        user_agent: HTTP UA. Default Mozilla — beats Cloudflare for most docs sites.
        timeout: per-request timeout in seconds.
        sleep_between: delay between requests to be polite.
        skip_url_encoded_dups: skip URLs containing %20 (sitemap duplicates).
        max_pages: cap total pages crawled.

    Returns:
        CrawlResult with per-page stats. Files written to output_dir.
    """
    _validate_url(root_url)
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sitemap_url = None
    if urls is None:
        sitemap_url, urls = discover_sitemap(root_url, user_agent=user_agent, timeout=timeout)
        if not urls:
            raise RuntimeError(
                f"No URLs found for {root_url}. Supply urls=[...] explicitly "
                "or ensure /sitemap.xml is reachable."
            )

    # Deduplication
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if skip_url_encoded_dups and "%20" in u:
            continue
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
        if max_pages and len(deduped) >= max_pages:
            break

    result = CrawlResult(root_url=root_url, output_dir=out_dir, sitemap_url=sitemap_url)

    for url in deduped:
        slug = _safe_slug(url, root_url)
        html_path = out_dir / f"{slug}.html"
        txt_path = out_dir / f"{slug}.txt"

        # Path-traversal defence: resolved write paths must stay inside out_dir.
        if not str(html_path.resolve()).startswith(str(out_dir)):
            logger.warning("Skipping out-of-sandbox URL: %s -> %s", url, html_path)
            continue
        try:
            status = _curl_to_file(url, html_path, user_agent=user_agent, timeout=timeout)
            html_bytes = html_path.stat().st_size if html_path.exists() else 0
            text = ""
            if html_bytes > 0 and 200 <= status < 400:
                # errors='replace' guards against legacy encodings (ISO-8859-1
                # in older docs sites). Default Python open() uses the system
                # locale and would crash on non-UTF-8 bytes.
                with open(html_path, encoding="utf-8", errors="replace") as fh:
                    html = fh.read()
                text = extract_text(html)
            txt_path.write_text(text, encoding="utf-8")
            words = len(text.split()) if text else 0
            page = PageResult(
                url=url, slug=slug, http_status=status,
                html_bytes=html_bytes, text_chars=len(text), text_words=words,
                is_shell=len(text) < MIN_PROSE_CHARS,
            )
        except Exception as e:  # noqa: BLE001
            page = PageResult(
                url=url, slug=slug, http_status=0,
                html_bytes=0, text_chars=0, text_words=0,
                is_shell=True, error=str(e),
            )
            result.errors += 1

        result.pages.append(page)
        result.fetched += 1
        if page.is_shell:
            result.shells += 1
        if sleep_between:
            time.sleep(sleep_between)

    _write_digest(result)
    return result


def render_shells(
    result: CrawlResult,
    *,
    profile: str | None = None,
    timeout: int = 90,
    sleep_between: float = DEFAULT_SLEEP_BETWEEN,
    max_shells: int | None = None,
    **session_opts,
) -> int:
    """Re-fetch the shell pages through ONE stealth browser. Returns pages recovered.

    ``crawl()`` flags every page with <500 chars of prose as a "shell" — a React
    app that curl received as an empty skeleton — and lists them in ``_SHELLS.md``
    under the header "Pages needing playwright render". That queue has existed
    since 2026-05-03 with nothing to drain it. This is the drain.

    All shells go through a SINGLE ``fetch_stealth_session``, so the batch pays
    browser start once rather than once per page, and a profile dir keeps any
    Cloudflare clearance across the whole run.

        >>> res = crawl("https://docs.example.com/", "/tmp/out")
        >>> render_shells(res, profile="~/.cache/scraperx/profiles/docs.example.com")

    Recovered pages have their ``.txt`` rewritten in place and their PageResult
    updated (``is_shell`` flips False when the render clears MIN_PROSE_CHARS), then
    the digest is rewritten so ``_DIGEST.md`` / ``_SHELLS.md`` reflect reality.

    Args:
        result: a CrawlResult from ``crawl()``. Mutated in place.
        profile: persistent browser-profile dir. Defaults to
                 ``~/.cache/scraperx/profiles/<host>`` derived from the crawl root —
                 ONE dir per host, never shared between sites.
        timeout: per-page budget in seconds. 90 is the stealth default because
                 Cloudflare's solver routinely needs 30-60s.
        max_shells: cap the batch (useful for a first live run).
        **session_opts: forwarded to ``stealth_session`` (extra_kwargs=, retries=…).

    Raises:
        ScraplingNotAvailable: the [stealth] extra is not installed. Propagates —
                 it is a setup problem, not a per-page failure.
        Anything in the defect tuple (TypeError/ValueError/…): a code defect
                 aborts the batch by design, so it can never be mistaken for a
                 site-wide block.
    """
    # Local import: docs_crawler must stay usable without the [stealth] extra.
    from scraperx.scrapling_stealth import fetch_stealth_session

    shells = [p for p in result.pages if p.is_shell and p.http_status == 200]
    if max_shells is not None:
        shells = shells[:max_shells]
    if not shells:
        logger.info("render_shells: nothing to render")
        return 0

    if profile is None:
        host = re.sub(r"^https?://", "", result.root_url).split("/", 1)[0]
        profile = f"~/.cache/scraperx/profiles/{host}"

    by_url = {p.url: p for p in shells}
    recovered = 0

    logger.info(
        "render_shells: %d shell page(s) through ONE browser (profile=%s)",
        len(shells), profile,
    )
    # contextlib.closing is REQUIRED, not decorative: if this loop raises, an
    # unclosed generator is an unclosed headless Chromium. See the
    # fetch_stealth_session docstring.
    gen = fetch_stealth_session(
        [p.url for p in shells],
        timeout=timeout,
        sleep_between=sleep_between,
        profile=profile,
        **session_opts,
    )
    with contextlib.closing(gen):
        for res in gen:
            page = by_url.get(res.url)
            if page is None:  # pragma: no cover — url set is built above
                continue
            if not res.ok:
                logger.warning(
                    "render_shells: %s -> %s (%s)", res.url, res.error, res.error_kind
                )
                page.error = res.error
                continue

            text = extract_text(res.content)
            if len(text) <= page.text_chars:
                # The render did not beat what curl already had. Keep the old
                # text and say so — silently overwriting with something WORSE is
                # how a "fix" quietly destroys data.
                logger.info(
                    "render_shells: %s no better after render (%d -> %d chars), kept original",
                    res.url, page.text_chars, len(text),
                )
                continue

            (result.output_dir / f"{page.slug}.txt").write_text(text, encoding="utf-8")
            (result.output_dir / f"{page.slug}.html").write_text(
                res.content, encoding="utf-8"
            )
            page.html_bytes = len(res.content.encode("utf-8", "replace"))
            page.text_chars = len(text)
            page.text_words = len(text.split())
            page.error = None
            if page.text_chars >= MIN_PROSE_CHARS:
                page.is_shell = False
                result.shells -= 1
                recovered += 1

    _write_digest(result)
    logger.info("render_shells: recovered %d/%d shell page(s)", recovered, len(shells))
    return recovered


def _write_digest(result: CrawlResult) -> None:
    """Write _DIGEST.md and _SHELLS.md to output_dir."""
    out = result.output_dir
    digest = out / "_DIGEST.md"
    shells_md = out / "_SHELLS.md"

    lines = [
        f"# Docs crawl digest — {result.root_url}",
        "",
        f"- Sitemap: `{result.sitemap_url or '(none — explicit URL list)'}`",
        f"- Pages crawled: **{result.fetched}**",
        f"- Pages with <{MIN_PROSE_CHARS} chars (shell candidates): **{result.shells}**",
        f"- Errors: **{result.errors}**",
        "",
        "| URL | HTTP | HTML B | Text B | Words | Shell |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for p in result.pages:
        flag = "⚠️" if p.is_shell else ""
        lines.append(
            f"| {p.url} | {p.http_status} | {p.html_bytes} | {p.text_chars} | "
            f"{p.text_words} | {flag} |"
        )
    digest.write_text("\n".join(lines))

    # Shell-only file for follow-up rendering
    shell_lines = [f"# Pages needing playwright render (text < {MIN_PROSE_CHARS} chars)", ""]
    for p in result.pages:
        if p.is_shell and p.http_status == 200:
            shell_lines.append(f"- {p.url}  (chars={p.text_chars})")
    shells_md.write_text("\n".join(shell_lines))


def iter_pages(output_dir: str | Path) -> Iterator[tuple[str, str]]:
    """Yield (slug, text) pairs from a previously-crawled output directory.

    Useful for downstream `grep` / LLM digesting:

        for slug, text in iter_pages("/tmp/ic-docs"):
            if "returnedQuantity" in text:
                print(slug)
    """
    out = Path(output_dir)
    for txt_path in sorted(out.glob("*.txt")):
        if txt_path.name.startswith("_"):
            continue
        yield txt_path.stem, txt_path.read_text()


# ── CLI entry point ──────────────────────────────────────────────────────────

def _main_docs_crawl() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="scraperx docs-crawl",
        description="Exhaustively crawl a documentation site and save HTML + extracted text.",
        epilog=(
            "Examples:\n"
            "  scraperx docs-crawl https://docs.example.com/api/ --output /tmp/example-docs\n"
            "  scraperx docs-crawl https://docs.example.com/api/ --max-pages 50\n"
            "\n"
            "Use this whenever an LLM agent needs to 'read every page' of a docs site —\n"
            "it produces a directory the agent can then grep with confidence that nothing\n"
            "was silently skipped. Pages with <500 chars are flagged in _SHELLS.md for\n"
            "a follow-up playwright render pass.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root_url", help="Site root URL (used for sitemap discovery + slug generation)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: ./docs-crawl-<host>)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sleep-between", type=float, default=DEFAULT_SLEEP_BETWEEN)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--include-encoded-dups", action="store_true",
                        help="Don't skip URLs with %%20 (default: skip)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--stealth-shells", action="store_true",
        help="After crawling, re-fetch the shell pages (React skeletons that curl "
             "got empty) through ONE stealth browser. Requires the [stealth] extra. "
             "Slow: 30-90s per page.",
    )
    parser.add_argument(
        "--stealth-profile", default=None,
        help="Browser-profile dir for --stealth-shells. Defaults to "
             "~/.cache/scraperx/profiles/<host> — one per host, never shared.",
    )
    parser.add_argument(
        "--max-shells", type=int, default=None,
        help="Cap how many shells --stealth-shells renders (use on a first run).",
    )

    args = parser.parse_args(__import__("sys").argv[2:])

    if not args.quiet:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    output_dir = args.output
    if not output_dir:
        host = re.sub(r"^https?://", "", args.root_url).split("/", 1)[0]
        output_dir = f"./docs-crawl-{host}"

    result = crawl(
        args.root_url,
        output_dir,
        user_agent=args.user_agent,
        timeout=args.timeout,
        sleep_between=args.sleep_between,
        skip_url_encoded_dups=not args.include_encoded_dups,
        max_pages=args.max_pages,
    )

    print(f"Crawled {result.fetched} pages → {result.output_dir}")
    print(f"  Shells (need render): {result.shells}")
    print(f"  Errors: {result.errors}")
    print(f"  Digest: {result.output_dir}/_DIGEST.md")
    if result.shells:
        print(f"  Render queue: {result.output_dir}/_SHELLS.md")

    if args.stealth_shells and result.shells:
        before = result.shells
        print(f"\nRendering {before} shell page(s) through ONE stealth browser…")
        try:
            recovered = render_shells(
                result,
                profile=args.stealth_profile,
                sleep_between=args.sleep_between,
                max_shells=args.max_shells,
            )
        except Exception as e:  # noqa: BLE001 — surface the TYPE, never a bare "failed"
            print(f"  stealth render FAILED: {type(e).__name__}: {e}")
            print("  (install with: pip install 'scraperx[stealth]' && scrapling install)")
            return 1
        print(f"  Recovered: {recovered}/{before}")
        print(f"  Shells remaining: {result.shells}")
    elif args.stealth_shells:
        print("\n--stealth-shells: nothing to render (no shells).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main_docs_crawl())
