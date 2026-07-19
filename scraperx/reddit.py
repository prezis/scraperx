"""reddit — NO-LOGIN Reddit scraping with a tiered no-auth strategy.

Three tiers, tried in order (cheapest → most resilient):

1. **TIER 1 — old.reddit.com JSON** (primary, structured). Any Reddit listing
   or thread URL serves clean JSON when ``.json`` is appended, as long as the
   request carries a *browser* User-Agent (the default python-urllib UA gets
   a 429/403 wall). No OAuth, no account, no API key.

   - subreddit search:  ``/r/{sub}/search.json?q=...&restrict_sr=1&sort=new``
   - subreddit listing: ``/r/{sub}/{hot|new|top|rising}.json``
   - thread+comments:   ``{permalink}.json`` (returns ``[post, comment_tree]``)

2. **TIER 2 — redlib/libreddit public mirrors** (HTML, best-effort). When
   reddit.com itself blocks the caller IP, public privacy-frontend mirrors
   usually still serve the same listings as plain HTML. We probe a small list
   of known instances and regex-parse titles/permalinks (and comment bodies
   for threads). Structured fields (score, timestamps) degrade gracefully.

3. **TIER 3 — scrapling stealth browser** (last resort, requires the
   ``[stealth]`` extra). Two sub-legs: (3a) the TIER-1 ``.json`` URL through
   a patched headless Chromium — beats JS-challenge walls; (3b) the redlib
   mirrors through the same browser — beats the case where reddit.com
   IP-blocks this host outright (stealth can't fix an IP block against
   reddit.com itself, but mirrors proxy Reddit from THEIR IPs and their
   bot-walls ARE browser-passable; live-confirmed 2026-07-19).

Public API:

    from scraperx.reddit import RedditScraper, search_subreddit

    posts = search_subreddit("ethereum", "alchemy", limit=10)
    for p in posts:
        print(f"[{p.score:>5}] {p.title}  ({p.permalink})")

    thread = get_thread("https://www.reddit.com/r/ethereum/comments/abc123/x/")
    print(thread.post.title, len(thread.comments), "comments")

Rate limiting: polite by construction — a per-instance monotonic gate keeps
requests at ~1-2 req/s with jitter (Reddit soft-limits unauthed clients at
~10 req/min bursts; slow-and-steady never trips it).

SELF-LEARNING tier order (``adaptive_tiers=True``, default): every tier
attempt is recorded to the shared method-telemetry ledger
(``scraperx.method_telemetry`` → ``~/.scraperx/method-telemetry.jsonl``),
and each call ranks tiers by recent success-rate — so when old.reddit.com
starts blocking this host, TIER 1 gets demoted below the mirrors on
subsequent calls, and recovers automatically (exponential decay) once it
works again. Fail-open: telemetry can never break a scrape.

Siblings (surveyed 2026-07-19, ADOPT>EXTEND>BUILD):
  - ``scraperx.github_analyzer.mentions.reddit`` — narrow repo-mention search
    adapter only.
  - ``scraperx.bmw_corpus.reddit.core`` — domain-specific corpus crawler
    (curated subs → corpus rows, no threads/comments/tiering). Its empirical
    lesson is ADOPTED here: Reddit returns *transient* 403/429 for bursts
    ("per-IP burst limiter does not care about your average; it cares about
    your milliseconds") — so TIER 1 retries once with jittered backoff before
    demoting to mirrors.
This module is the general-purpose scraper: search + listings + full
thread/comment-tree flattening + tier fallback.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from scraperx.method_telemetry import preferred_order as _preferred_order
from scraperx.method_telemetry import record as _telemetry_record

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REDLIB_INSTANCES",
    "RedditComment",
    "RedditError",
    "RedditPost",
    "RedditScraper",
    "RedditThread",
    "TIER_DEFAULT_ORDER",
    "get_subreddit_posts",
    "get_thread",
    "search_subreddit",
]


OLD_REDDIT_BASE = "https://old.reddit.com"

# Public redlib/libreddit frontends (TIER 2). These churn over time — the list
# is a fallback pool, not a guarantee; per-instance failures are recorded and
# the next instance is tried. Update opportunistically when instances die.
# Source of truth: github.com/redlib-org/redlib-instances (instances.json).
# Live-probed 2026-07-19; note many now front an Anubis-style JS check for
# datacenter IPs — plain-HTTP TIER 2 may still pass from residential networks,
# otherwise TIER 3 (stealth browser) handles both walls.
DEFAULT_REDLIB_INSTANCES: tuple[str, ...] = (
    "https://safereddit.com",
    "https://redlib.r4fo.com",
    "https://red.artemislena.eu",
    "https://redlib.privacyredirect.com",
    "https://redlib.privadency.com",
)

# Reddit rejects the default python-urllib UA outright; a mainstream browser
# UA is REQUIRED for the free .json endpoints.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_VALID_LISTING_SORTS = ("hot", "new", "top", "rising")
_VALID_SEARCH_SORTS = ("relevance", "hot", "top", "new", "comments")
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_PERMALINK_RE = re.compile(r"(/r/[A-Za-z0-9_]+/comments/[a-z0-9]+(?:/[^/\s.?#]*)?)")

# Reddit hard-caps listing pages at 100 items per request.
_MAX_LIMIT = 100

# Transient "too fast" statuses worth ONE retry-with-backoff on TIER 1
# (empirical, from bmw_corpus.reddit.core 2026-04-27).
_RETRY_HTTP_CODES = frozenset({403, 429})

# ---------------------------------------------------------------------------
# Adaptive tier ordering (method_telemetry)
# ---------------------------------------------------------------------------

# Canonical (cheapest → most resilient) order; also the telemetry fallback.
TIER_DEFAULT_ORDER: tuple[str, ...] = ("old_json", "redlib", "stealth", "stealth_mirror")

# Human-readable labels used in aggregated all-tiers-failed error messages.
_TIER_LABELS: dict[str, str] = {
    "old_json": "tier1 old_json",
    "redlib": "tier2 redlib",
    "stealth": "tier3 stealth",
    "stealth_mirror": "tier3b stealth_mirror",
}

# What each tier actually talks to (recorded as target_domain).
_TIER_DOMAINS: dict[str, str] = {
    "old_json": "old.reddit.com",
    "redlib": "redlib-mirrors",
    "stealth": "old.reddit.com",
    "stealth_mirror": "redlib-mirrors",
}

# Our own tier errors embed "HTTP <code>" — recover it for telemetry.
_HTTP_STATUS_IN_ERROR_RE = re.compile(r"HTTP (\d{3})")


class RedditError(RuntimeError):
    """All tiers failed, or a single-tier call returned malformed data."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RedditPost:
    """One submission (t3). Fields degrade to defaults on TIER-2 HTML parses.

    Attributes:
        id:            Base-36 post id (``"abc123"``), no ``t3_`` prefix.
        title:         Submission title (HTML-unescaped).
        author:        Author username, "" when deleted/unavailable.
        subreddit:     Subreddit name without the ``r/`` prefix.
        permalink:     Site-relative permalink (``/r/sub/comments/id/slug/``).
        url:           Outbound link (== full permalink for self posts).
        selftext:      Self-post body markdown, "" for link posts.
        score:         Net upvotes at scrape time (0 when unknown — TIER 2).
        upvote_ratio:  0.0-1.0 consensus quality, 0.0 when unknown.
        num_comments:  Comment count at scrape time.
        created_utc:   Unix epoch seconds, 0.0 when unknown.
        created_iso:   ISO-8601 UTC derived from created_utc, "" when unknown.
        is_self:       True for text posts.
        over_18:       NSFW flag.
        flair:         Link flair text, "" when none.
        tier:          Which tier produced this post:
                       "old_json" | "redlib" | "stealth" | "stealth_mirror".
    """

    id: str
    title: str
    author: str = ""
    subreddit: str = ""
    permalink: str = ""
    url: str = ""
    selftext: str = ""
    score: int = 0
    upvote_ratio: float = 0.0
    num_comments: int = 0
    created_utc: float = 0.0
    created_iso: str = ""
    is_self: bool = False
    over_18: bool = False
    flair: str = ""
    tier: str = "old_json"

    @property
    def full_url(self) -> str:
        return f"https://www.reddit.com{self.permalink}" if self.permalink else self.url


@dataclass
class RedditComment:
    """One comment (t1), flattened out of the reply tree.

    Attributes:
        id:          Base-36 comment id, no ``t1_`` prefix.
        author:      Username, "" when deleted.
        body:        Comment markdown body.
        score:       Net upvotes (0 when unknown).
        depth:       0 = top-level, +1 per reply nesting level.
        parent_id:   Fullname of parent (``t3_...`` post or ``t1_...`` comment).
        created_utc: Unix epoch seconds, 0.0 when unknown.
        permalink:   Site-relative permalink when available.
    """

    id: str
    author: str
    body: str
    score: int = 0
    depth: int = 0
    parent_id: str = ""
    created_utc: float = 0.0
    permalink: str = ""


@dataclass
class RedditThread:
    """A post plus its flattened comment tree.

    Attributes:
        post:        The submission.
        comments:    Depth-first flattened comments (reading order).
        more_count:  Comments Reddit elided behind "load more" stubs — NOT
                     fetched (would need many extra requests); tells the
                     caller how incomplete the tree is.
        tier:        Which tier produced the thread.
    """

    post: RedditPost
    comments: list[RedditComment] = field(default_factory=list)
    more_count: int = 0
    tier: str = "old_json"


# ---------------------------------------------------------------------------
# Low-level HTTP (module-level so tests can monkeypatch one seam)
# ---------------------------------------------------------------------------


def _http_get(url: str, *, user_agent: str, timeout: int) -> tuple[int, str]:
    """GET a URL, return ``(status, body)``. Raises on transport errors."""
    host = urlparse(url).hostname or ""
    if not host:
        raise RedditError(f"refusing to fetch URL without a host: {url!r}")
    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        # urlopen RAISES on non-2xx — normalize to (status, body) so the
        # tier ladder / transient-403 retry can reason about the code.
        body = ""
        with contextlib.suppress(Exception):
            body = e.read().decode("utf-8", errors="replace")
        return int(e.code), body
    return int(status), body


# ---------------------------------------------------------------------------
# JSON parsing (TIER 1 + TIER 3 share these)
# ---------------------------------------------------------------------------


def _epoch_to_iso(epoch: Any) -> str:
    if epoch in (None, "", 0):
        return ""
    try:
        return (
            datetime.fromtimestamp(float(epoch), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return ""


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _post_from_t3(d: dict, *, tier: str = "old_json") -> RedditPost:
    """Build a RedditPost from a t3 ``data`` dict."""
    created = _safe_float(d.get("created_utc"))
    return RedditPost(
        id=str(d.get("id") or ""),
        title=unescape(str(d.get("title") or "")),
        author=str(d.get("author") or ""),
        subreddit=str(d.get("subreddit") or ""),
        permalink=str(d.get("permalink") or ""),
        url=str(d.get("url") or ""),
        selftext=str(d.get("selftext") or ""),
        score=_safe_int(d.get("score")),
        upvote_ratio=_safe_float(d.get("upvote_ratio")),
        num_comments=_safe_int(d.get("num_comments")),
        created_utc=created,
        created_iso=_epoch_to_iso(created),
        is_self=bool(d.get("is_self")),
        over_18=bool(d.get("over_18")),
        flair=str(d.get("link_flair_text") or ""),
        tier=tier,
    )


def _parse_listing(payload: Any, *, tier: str = "old_json") -> list[RedditPost]:
    """Parse a Reddit Listing envelope → posts. Non-t3 children are skipped."""
    if not isinstance(payload, dict):
        raise RedditError(f"expected Listing dict, got {type(payload).__name__}")
    children = (payload.get("data") or {}).get("children") or []
    posts: list[RedditPost] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            continue
        posts.append(_post_from_t3(child.get("data") or {}, tier=tier))
    return posts


def _walk_comments(children: list, depth: int = 0) -> tuple[list[RedditComment], int]:
    """Depth-first flatten of a comment-tree Listing's children.

    Returns ``(comments, more_count)`` where more_count sums the "load more"
    stubs (kind == "more") we deliberately do NOT chase.
    """
    out: list[RedditComment] = []
    more = 0
    for child in children or []:
        if not isinstance(child, dict):
            continue
        kind = child.get("kind")
        d = child.get("data") or {}
        if kind == "more":
            more += _safe_int(d.get("count")) or len(d.get("children") or [])
            continue
        if kind != "t1":
            continue
        out.append(
            RedditComment(
                id=str(d.get("id") or ""),
                author=str(d.get("author") or ""),
                body=str(d.get("body") or ""),
                score=_safe_int(d.get("score")),
                depth=depth,
                parent_id=str(d.get("parent_id") or ""),
                created_utc=_safe_float(d.get("created_utc")),
                permalink=str(d.get("permalink") or ""),
            )
        )
        replies = d.get("replies")
        if isinstance(replies, dict):
            sub_children = (replies.get("data") or {}).get("children") or []
            sub_out, sub_more = _walk_comments(sub_children, depth + 1)
            out.extend(sub_out)
            more += sub_more
    return out, more


def _parse_thread(payload: Any, *, tier: str = "old_json") -> RedditThread:
    """Parse the two-listing ``{permalink}.json`` response → RedditThread."""
    if not isinstance(payload, list) or len(payload) < 2:
        raise RedditError(
            f"expected [post_listing, comment_listing], got {type(payload).__name__}"
        )
    posts = _parse_listing(payload[0], tier=tier)
    if not posts:
        raise RedditError("thread JSON contained no t3 post in first listing")
    children = (payload[1].get("data") or {}).get("children") or []
    comments, more = _walk_comments(children)
    return RedditThread(post=posts[0], comments=comments, more_count=more, tier=tier)


def normalize_permalink(url_or_permalink: str) -> str:
    """Normalize any thread URL/permalink to a bare ``/r/sub/comments/id/slug`` path.

    Accepts full www/old/np/redlib URLs, bare permalinks, with/without
    trailing slash, ``.json`` suffix, or query strings.
    """
    s = (url_or_permalink or "").strip()
    m = _PERMALINK_RE.search(s)
    if not m:
        raise RedditError(f"not a recognizable Reddit thread URL/permalink: {url_or_permalink!r}")
    path = m.group(1).rstrip("/")
    if path.endswith(".json"):
        path = path[: -len(".json")]
    return path


# ---------------------------------------------------------------------------
# TIER 2 — redlib HTML parsing (best-effort)
# ---------------------------------------------------------------------------

_REDLIB_POST_LINK_RE = re.compile(
    r'<a\s+href="(/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_REDLIB_COMMENT_BODY_RE = re.compile(
    r'<div\s+class="comment_body[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
_REDLIB_TITLE_RE = re.compile(
    r'<h1[^>]*class="post_title"[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL
)


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def _parse_redlib_listing_html(html: str, *, tier: str = "redlib") -> list[RedditPost]:
    """Extract (permalink, title) post stubs from a redlib listing/search page."""
    posts: list[RedditPost] = []
    seen: set[str] = set()
    for m in _REDLIB_POST_LINK_RE.finditer(html):
        href, sub, post_id, inner = m.group(1), m.group(2), m.group(3), m.group(4)
        title = _strip_tags(inner)
        # Comment-count / timestamp anchors reuse the same permalink — keep the
        # first anchor with a real title per post id.
        if not title or post_id in seen:
            continue
        seen.add(post_id)
        posts.append(
            RedditPost(
                id=post_id,
                title=title,
                subreddit=sub,
                permalink=href.split("?")[0],
                url=f"https://www.reddit.com{href.split('?')[0]}",
                tier=tier,
            )
        )
    return posts


def _parse_redlib_thread_html(html: str, permalink: str, *, tier: str = "redlib") -> RedditThread:
    """Best-effort thread parse from a redlib comments page."""
    title_m = _REDLIB_TITLE_RE.search(html)
    title = _strip_tags(title_m.group(1)) if title_m else ""
    if not title:
        raise RedditError("redlib thread page had no recognizable post_title")
    post_id_m = re.search(r"/comments/([a-z0-9]+)", permalink)
    post = RedditPost(
        id=post_id_m.group(1) if post_id_m else "",
        title=title,
        permalink=permalink,
        url=f"https://www.reddit.com{permalink}",
        tier=tier,
    )
    comments = [
        RedditComment(id=f"html{i}", author="", body=_strip_tags(m.group(1)))
        for i, m in enumerate(_REDLIB_COMMENT_BODY_RE.finditer(html))
        if _strip_tags(m.group(1))
    ]
    return RedditThread(post=post, comments=comments, tier=tier)


# ---------------------------------------------------------------------------
# The scraper
# ---------------------------------------------------------------------------


class RedditScraper:
    """Tiered no-login Reddit scraper. See module docstring for the tiers.

    Args:
        user_agent:      Browser UA sent on every request (REQUIRED by Reddit).
        timeout:         Per-request timeout in seconds.
        min_interval_s:  Politeness floor between consecutive requests.
        jitter_s:        Extra random 0..jitter_s sleep on top of the floor
                         (combined default ≈ 1-2 req/s). Set both to 0 in tests.
        mirrors:         redlib/libreddit instances for TIER 2.
        enable_stealth:  Allow the TIER-3 scrapling leg (off ⇒ 2 tiers only).
        retry_backoff_s: Base sleep before the single TIER-1 retry on a
                         transient 403/429 (jittered to [x, 2x)). 0 disables.
        adaptive_tiers:  SELF-LEARNING tier order (default on). Every tier
                         attempt is recorded to the method-telemetry ledger
                         (``~/.scraperx/method-telemetry.jsonl``); subsequent
                         calls try tiers ranked by recent success-rate, so a
                         blocked old.reddit.com demotes TIER 1 below the
                         mirrors until it starts working again (exponential
                         decay). Off ⇒ fixed TIER_DEFAULT_ORDER, no ledger
                         reads or writes. Fail-open: telemetry errors never
                         break scraping.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 20,
        min_interval_s: float = 0.6,
        jitter_s: float = 0.7,
        mirrors: tuple[str, ...] = DEFAULT_REDLIB_INSTANCES,
        enable_stealth: bool = True,
        retry_backoff_s: float = 5.0,
        adaptive_tiers: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self.mirrors = tuple(mirrors)
        self.enable_stealth = enable_stealth
        self.retry_backoff_s = retry_backoff_s
        self.adaptive_tiers = adaptive_tiers
        self.last_tier_used: str | None = None
        self._last_request_ts = 0.0

    # -- adaptive tier ladder ------------------------------------------------

    def _tier_order(self) -> list[str]:
        """Telemetry-ranked tier order (canonical order when adaptive off)."""
        if not self.adaptive_tiers:
            return list(TIER_DEFAULT_ORDER)
        return _preferred_order("reddit", TIER_DEFAULT_ORDER)

    def _run_tier_ladder(self, legs: dict[str, Callable[[], Any]], desc: str) -> Any:
        """Try each tier leg in (possibly telemetry-adapted) order.

        Each attempt is recorded to method_telemetry (success, latency,
        best-effort HTTP status recovered from our own error messages).
        First leg to return wins; all-fail raises RedditError aggregating
        per-tier errors under their canonical labels.
        """
        errors: list[str] = []
        for name in self._tier_order():
            leg = legs.get(name)
            if leg is None:  # pragma: no cover — all callers wire all tiers
                continue
            t0 = time.monotonic()
            try:
                result = leg()
            except Exception as e:
                if self.adaptive_tiers:
                    status_m = _HTTP_STATUS_IN_ERROR_RE.search(str(e))
                    _telemetry_record(
                        "reddit",
                        name,
                        _TIER_DOMAINS[name],
                        False,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                        http_status=int(status_m.group(1)) if status_m else None,
                    )
                errors.append(f"{_TIER_LABELS[name]}: {e}")
                continue
            if self.adaptive_tiers:
                _telemetry_record(
                    "reddit",
                    name,
                    _TIER_DOMAINS[name],
                    True,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    http_status=200,
                )
            self.last_tier_used = name
            return result
        raise RedditError(f"{desc} failed all tiers: {' | '.join(errors)}")

    # -- politeness ---------------------------------------------------------

    def _polite_wait(self) -> None:
        """Sleep so consecutive requests stay ~1-2 req/s with jitter."""
        gap = self.min_interval_s + (random.random() * self.jitter_s if self.jitter_s else 0.0)
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_request_ts = time.monotonic()

    # -- tier legs ----------------------------------------------------------

    def _tier1_json(self, path: str, params: dict[str, Any]) -> Any:
        """old.reddit.com ``.json`` fetch → parsed JSON payload.

        Retries ONCE with jittered backoff on 403/429 — adopted from
        ``bmw_corpus.reddit.core``: Reddit's per-IP burst limiter returns
        transient 403s for "too fast", not just permanent blocks, so an
        immediate demotion to mirrors would be a false negative.
        """
        url = f"{OLD_REDDIT_BASE}{path}?{urlencode(params)}"
        status, body = 0, ""
        for attempt in (0, 1):
            self._polite_wait()
            status, body = _http_get(url, user_agent=self.user_agent, timeout=self.timeout)
            if status == 200:
                break
            if attempt == 0 and status in _RETRY_HTTP_CODES and self.retry_backoff_s > 0:
                lo = self.retry_backoff_s
                time.sleep(lo + random.random() * lo)  # jittered [lo, 2*lo)
                continue
            raise RedditError(f"old.reddit.com HTTP {status} for {path}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise RedditError(f"old.reddit.com returned non-JSON for {path}: {e}") from e

    def _tier2_html(self, path_with_query: str) -> str:
        """Probe redlib mirrors in order; return the first 200 HTML body."""
        errors: list[str] = []
        for instance in self.mirrors:
            url = f"{instance}{path_with_query}"
            self._polite_wait()
            try:
                status, body = _http_get(url, user_agent=self.user_agent, timeout=self.timeout)
            except Exception as e:
                errors.append(f"{instance}: {type(e).__name__}: {e}")
                continue
            if status == 200 and body.strip():
                return body
            errors.append(f"{instance}: HTTP {status}")
        raise RedditError(f"all {len(self.mirrors)} redlib mirrors failed: {'; '.join(errors)}")

    def _stealth_fn(self):
        if not self.enable_stealth:
            raise RedditError("stealth tier disabled (enable_stealth=False)")
        try:
            from scraperx.scrapling_stealth import fetch_stealth
        except Exception as e:
            raise RedditError(f"stealth tier unavailable: {type(e).__name__}: {e}") from e
        return fetch_stealth

    def _tier3_stealth_json(self, path: str, params: dict[str, Any]) -> Any:
        """Fetch the TIER-1 URL through the scrapling stealth browser.

        Helps when reddit.com fronts a JS challenge. Does NOT help against a
        pure IP-reputation 403 (same egress IP) — that case falls through to
        ``_tier3_stealth_mirror_html`` (mirrors proxy Reddit from THEIR IPs;
        their Anubis-style bot-walls ARE passable by a real browser).
        """
        fetch_stealth = self._stealth_fn()
        url = f"{OLD_REDDIT_BASE}{path}?{urlencode(params)}"
        content, status = fetch_stealth(url, self.timeout * 3)
        if status is not None and status != 200:
            raise RedditError(f"stealth fetch HTTP {status} for {path}")
        # A browser navigating to .json may wrap the payload in <pre>...</pre>.
        text = content.strip()
        if not text.startswith(("{", "[")):
            pre = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL | re.IGNORECASE)
            if pre:
                text = unescape(pre.group(1)).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RedditError(f"stealth fetch returned non-JSON for {path}: {e}") from e

    def _tier3_stealth_mirror_html(self, path_with_query: str) -> str:
        """Fetch a redlib mirror page through the stealth browser.

        The escape hatch when reddit.com IP-blocks this host outright: the
        mirror talks to Reddit from ITS OWN IP, and its Anubis-style
        "verifying your browser" wall (which kills plain-HTTP TIER 2) is
        passable by the patched Chromium. Live-confirmed 2026-07-19 against
        safereddit.com from a Reddit-403'd datacenter IP.
        """
        fetch_stealth = self._stealth_fn()
        errors: list[str] = []
        for instance in self.mirrors:
            url = f"{instance}{path_with_query}"
            try:
                content, status = fetch_stealth(url, self.timeout * 3)
            except Exception as e:
                errors.append(f"{instance}: {type(e).__name__}: {e}")
                continue
            if (status is None or status == 200) and content.strip():
                return content
            errors.append(f"{instance}: HTTP {status}")
        raise RedditError(
            f"stealth mirror fetch failed on all {len(self.mirrors)} instances: {'; '.join(errors)}"
        )

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _check_sub(sub: str) -> str:
        sub = (sub or "").strip().removeprefix("r/").removeprefix("/r/")
        if not _SUBREDDIT_RE.match(sub):
            raise RedditError(f"invalid subreddit name: {sub!r}")
        return sub

    @staticmethod
    def _clamp_limit(limit: int) -> int:
        return max(1, min(int(limit), _MAX_LIMIT))

    # -- public API ---------------------------------------------------------

    def search_subreddit(
        self,
        sub: str,
        query: str,
        limit: int = 25,
        *,
        sort: str = "new",
    ) -> list[RedditPost]:
        """Search inside one subreddit. Falls through tiers 1 → 2 → 3.

        Args:
            sub:   Subreddit name (with or without ``r/`` prefix).
            query: Search query (Reddit search syntax allowed).
            limit: Max posts (1-100).
            sort:  "relevance" | "hot" | "top" | "new" | "comments".
        """
        sub = self._check_sub(sub)
        if not (query or "").strip():
            raise RedditError("query must be non-empty")
        if sort not in _VALID_SEARCH_SORTS:
            raise RedditError(f"sort must be one of {_VALID_SEARCH_SORTS}, got {sort!r}")
        limit = self._clamp_limit(limit)
        path = f"/r/{sub}/search.json"
        params = {"q": query, "restrict_sr": 1, "sort": sort, "limit": limit, "raw_json": 1}
        q2 = urlencode({"q": query, "restrict_sr": "on", "sort": sort})

        def _leg_old_json() -> list[RedditPost]:
            return _parse_listing(self._tier1_json(path, params), tier="old_json")[:limit]

        def _leg_redlib() -> list[RedditPost]:
            posts = _parse_redlib_listing_html(self._tier2_html(f"/r/{sub}/search?{q2}"))
            if not posts:
                raise RedditError("parsed 0 posts")
            return posts[:limit]

        def _leg_stealth() -> list[RedditPost]:
            return _parse_listing(self._tier3_stealth_json(path, params), tier="stealth")[:limit]

        def _leg_stealth_mirror() -> list[RedditPost]:
            html = self._tier3_stealth_mirror_html(f"/r/{sub}/search?{q2}")
            posts = _parse_redlib_listing_html(html, tier="stealth_mirror")
            if not posts:
                raise RedditError("parsed 0 posts")
            return posts[:limit]

        return self._run_tier_ladder(
            {
                "old_json": _leg_old_json,
                "redlib": _leg_redlib,
                "stealth": _leg_stealth,
                "stealth_mirror": _leg_stealth_mirror,
            },
            f"search_subreddit(r/{sub}, {query!r})",
        )

    def get_thread(self, url_or_permalink: str, *, comment_limit: int = 100) -> RedditThread:
        """Fetch a thread (post + flattened comment tree). Tiers 1 → 2 → 3.

        Args:
            url_or_permalink: Full thread URL (www/old/np/mirror) or bare
                              ``/r/sub/comments/id/slug`` permalink.
            comment_limit:    Max comments Reddit should return (1-100 per page;
                              deeper trees surface in ``thread.more_count``).
        """
        permalink = normalize_permalink(url_or_permalink)
        path = f"{permalink}.json"
        params = {"raw_json": 1, "limit": self._clamp_limit(comment_limit)}

        def _leg_old_json() -> RedditThread:
            return _parse_thread(self._tier1_json(path, params), tier="old_json")

        def _leg_redlib() -> RedditThread:
            return _parse_redlib_thread_html(self._tier2_html(permalink), permalink)

        def _leg_stealth() -> RedditThread:
            return _parse_thread(self._tier3_stealth_json(path, params), tier="stealth")

        def _leg_stealth_mirror() -> RedditThread:
            html = self._tier3_stealth_mirror_html(permalink)
            return _parse_redlib_thread_html(html, permalink, tier="stealth_mirror")

        return self._run_tier_ladder(
            {
                "old_json": _leg_old_json,
                "redlib": _leg_redlib,
                "stealth": _leg_stealth,
                "stealth_mirror": _leg_stealth_mirror,
            },
            f"get_thread({url_or_permalink!r})",
        )

    def get_subreddit_posts(
        self,
        sub: str,
        sort: str = "hot",
        limit: int = 25,
    ) -> list[RedditPost]:
        """Fetch a subreddit listing (hot/new/top/rising). Tiers 1 → 2 → 3.

        Args:
            sub:   Subreddit name (with or without ``r/`` prefix).
            sort:  "hot" | "new" | "top" | "rising".
            limit: Max posts (1-100).
        """
        sub = self._check_sub(sub)
        if sort not in _VALID_LISTING_SORTS:
            raise RedditError(f"sort must be one of {_VALID_LISTING_SORTS}, got {sort!r}")
        limit = self._clamp_limit(limit)
        path = f"/r/{sub}/{sort}.json"
        params = {"limit": limit, "raw_json": 1}

        def _leg_old_json() -> list[RedditPost]:
            return _parse_listing(self._tier1_json(path, params), tier="old_json")[:limit]

        def _leg_redlib() -> list[RedditPost]:
            posts = _parse_redlib_listing_html(self._tier2_html(f"/r/{sub}/{sort}"))
            if not posts:
                raise RedditError("parsed 0 posts")
            return posts[:limit]

        def _leg_stealth() -> list[RedditPost]:
            return _parse_listing(self._tier3_stealth_json(path, params), tier="stealth")[:limit]

        def _leg_stealth_mirror() -> list[RedditPost]:
            html = self._tier3_stealth_mirror_html(f"/r/{sub}/{sort}")
            posts = _parse_redlib_listing_html(html, tier="stealth_mirror")
            if not posts:
                raise RedditError("parsed 0 posts")
            return posts[:limit]

        return self._run_tier_ladder(
            {
                "old_json": _leg_old_json,
                "redlib": _leg_redlib,
                "stealth": _leg_stealth,
                "stealth_mirror": _leg_stealth_mirror,
            },
            f"get_subreddit_posts(r/{sub}, {sort})",
        )


# ---------------------------------------------------------------------------
# Module-level convenience wrappers (shared default scraper)
# ---------------------------------------------------------------------------

_default_scraper: RedditScraper | None = None


def _default() -> RedditScraper:
    global _default_scraper
    if _default_scraper is None:
        _default_scraper = RedditScraper()
    return _default_scraper


def search_subreddit(sub: str, query: str, limit: int = 25, *, sort: str = "new") -> list[RedditPost]:
    """Search a subreddit without instantiating a scraper. See RedditScraper.search_subreddit."""
    return _default().search_subreddit(sub, query, limit, sort=sort)


def get_thread(url_or_permalink: str, *, comment_limit: int = 100) -> RedditThread:
    """Fetch a Reddit thread without instantiating a scraper. See RedditScraper.get_thread.

    Exported from the package as ``get_reddit_thread`` (``scraperx.get_thread``
    is the X/Twitter thread reconstructor).
    """
    return _default().get_thread(url_or_permalink, comment_limit=comment_limit)


def get_subreddit_posts(sub: str, sort: str = "hot", limit: int = 25) -> list[RedditPost]:
    """Fetch a subreddit listing without instantiating a scraper."""
    return _default().get_subreddit_posts(sub, sort, limit)


# ---------------------------------------------------------------------------
# Inline demo (offline)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    sample = {
        "kind": "Listing",
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "abc123",
                        "title": "Alchemy vs Infura in 2026",
                        "author": "dev",
                        "subreddit": "ethereum",
                        "permalink": "/r/ethereum/comments/abc123/alchemy_vs_infura/",
                        "score": 42,
                        "num_comments": 7,
                        "created_utc": 1770000000,
                        "is_self": True,
                    },
                }
            ]
        },
    }
    posts = _parse_listing(sample)
    assert posts[0].id == "abc123" and posts[0].score == 42
    print("Parsed:", posts[0].title, posts[0].full_url)
    print("Permalink normalize:", normalize_permalink(
        "https://old.reddit.com/r/ethereum/comments/abc123/alchemy_vs_infura/?ref=x"
    ))
