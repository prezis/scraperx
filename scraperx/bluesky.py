"""bluesky — Bluesky (AT Protocol) without an account: post search, an account's posts, a thread, a profile.

Bluesky's AppView answers these over plain HTTPS with no login. Measured from this host 2026-10-28:

  public.api.bsky.app  app.bsky.feed.searchPosts   403 (search is refused here without auth)
  api.bsky.app         app.bsky.feed.searchPosts   200, first page only: the cursor page answers 403
                                                   (bluesky-social/bsky-docs issue #332)
  public.api.bsky.app  getAuthorFeed, getPostThread, getProfile, searchActors, resolveHandle   200

    from scraperx.bluesky import search_posts, get_author_posts, get_thread
    for p in search_posts("solana wallet", limit=25, sort="latest"):
        print(p.created_at, p.author_handle, p.likes, p.text[:80], p.url)
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = ["BlueskyError", "BlueskyPost", "BlueskyProfile", "get_author_posts", "get_profile", "get_thread",
           "search_posts"]

PUBLIC = "https://public.api.bsky.app/xrpc/"
SEARCH_APPVIEW = "https://api.bsky.app/xrpc/"
USER_AGENT = "scraperx (+https://github.com/prezis/scraperx)"
_FEED_FILTERS = ("posts_no_replies", "posts_with_replies", "posts_and_author_threads", "posts_with_media")
_SORTS = ("latest", "top")
_URL_RE = re.compile(r"bsky\.app/profile/([^/]+)/post/([a-z0-9]+)", re.IGNORECASE)
_MAX_PAGES = 20


class BlueskyError(RuntimeError):
    """Bluesky refused or the input is not a handle / post."""


@dataclass
class BlueskyPost:
    """One post.

    Attributes:
        uri / cid:     AT Protocol identifiers (at://did/app.bsky.feed.post/<rkey>).
        url:           https://bsky.app/profile/<handle>/post/<rkey>
        text:          The post text.
        created_at:    The author's timestamp (ISO-8601); indexed_at: when the AppView saw it.
        likes, reposts, replies, quotes: Counts at fetch time.
        links:         Links in the text (facets) and an external link card.
        images:        Full-size image URLs.
        reply_to:      URI of the post this one answers ("" for a top-level post).
        reposted_by:   Handle whose feed shows this post as a repost (author feeds only).
        depth:         Reply depth inside a thread (0 = the thread's post).
    """

    uri: str
    cid: str = ""
    url: str = ""
    author_handle: str = ""
    author_name: str = ""
    author_did: str = ""
    text: str = ""
    created_at: str = ""
    indexed_at: str = ""
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    reply_to: str = ""
    reposted_by: str = ""
    depth: int = 0


@dataclass
class BlueskyProfile:
    did: str
    handle: str
    display_name: str = ""
    description: str = ""
    followers: int = 0
    follows: int = 0
    posts: int = 0
    created_at: str = ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get(base: str, nsid: str, params: dict, timeout: int = 30) -> dict:
    """GET an XRPC method → JSON. Seam for tests."""
    url = f"{base}{nsid}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}),
                     timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
            detail = f": {body.get('error', '')} {body.get('message', '')}".rstrip()
        except Exception:
            pass
        raise BlueskyError(f"Bluesky {nsid} HTTP {e.code}{detail}") from e


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _embed_media(embed: dict) -> tuple[list[str], list[str]]:
    """(images, links) from an embed view: images, external link cards, and record-with-media."""
    images: list[str] = []
    links: list[str] = []
    if not isinstance(embed, dict):
        return images, links
    kind = embed.get("$type", "")
    if kind.startswith("app.bsky.embed.images"):
        images += [i.get("fullsize") or i.get("thumb") for i in embed.get("images") or [] if isinstance(i, dict)]
    elif kind.startswith("app.bsky.embed.external"):
        uri = (embed.get("external") or {}).get("uri")
        if uri:
            links.append(uri)
    elif kind.startswith("app.bsky.embed.recordWithMedia"):
        more_images, more_links = _embed_media(embed.get("media") or {})
        images += more_images
        links += more_links
    return [i for i in images if i], links


def _post(p: dict, *, reposted_by: str = "", depth: int = 0) -> BlueskyPost:
    author = p.get("author") or {}
    record = p.get("record") or {}
    uri = str(p.get("uri") or "")
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    handle = str(author.get("handle") or "")
    links = [
        feat.get("uri")
        for facet in record.get("facets") or []
        for feat in (facet.get("features") or [])
        if isinstance(feat, dict) and feat.get("uri")
    ]
    images, card_links = _embed_media(p.get("embed") or {})
    return BlueskyPost(
        uri=uri,
        cid=str(p.get("cid") or ""),
        url=f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else "",
        author_handle=handle,
        author_name=str(author.get("displayName") or ""),
        author_did=str(author.get("did") or ""),
        text=str(record.get("text") or ""),
        created_at=str(record.get("createdAt") or ""),
        indexed_at=str(p.get("indexedAt") or ""),
        likes=_int(p.get("likeCount")),
        reposts=_int(p.get("repostCount")),
        replies=_int(p.get("replyCount")),
        quotes=_int(p.get("quoteCount")),
        links=links + [u for u in card_links if u not in links],
        images=images,
        reply_to=str(((record.get("reply") or {}).get("parent") or {}).get("uri") or ""),
        reposted_by=reposted_by,
        depth=depth,
    )


def _actor(actor: str) -> str:
    a = (actor or "").strip().removeprefix("@")
    m = re.search(r"bsky\.app/profile/([^/?#]+)", a)
    if m:
        a = m.group(1)
    if not a or " " in a:
        raise BlueskyError(f"not a Bluesky handle or DID: {actor!r}")
    return a


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_posts(
    query: str,
    limit: int = 25,
    *,
    sort: str = "latest",
    since: str | None = None,
    until: str | None = None,
    lang: str | None = None,
    author: str | None = None,
    timeout: int = 30,
) -> list[BlueskyPost]:
    """Posts matching `query` (Bluesky search syntax), from the first result page only: without an account Bluesky
    refuses the next page (403), so at most 100 posts. `since` / `until` take ISO dates."""
    if not (query or "").strip():
        raise BlueskyError("query must be non-empty")
    if sort not in _SORTS:
        raise BlueskyError(f"sort must be one of {_SORTS}, got {sort!r}")
    data = _get(SEARCH_APPVIEW, "app.bsky.feed.searchPosts", {
        "q": query.strip(), "limit": max(1, min(int(limit), 100)), "sort": sort, "since": since, "until": until,
        "lang": lang, "author": _actor(author) if author else None,
    }, timeout)
    return [_post(p) for p in data.get("posts") or [] if isinstance(p, dict)]


def get_author_posts(
    actor: str,
    limit: int = 50,
    *,
    with_replies: bool = False,
    include_reposts: bool = True,
    timeout: int = 30,
    delay_s: float = 0.5,
) -> list[BlueskyPost]:
    """An account's newest posts (paged by cursor). Reposts carry `reposted_by`; `include_reposts=False` keeps the
    account's own posts only."""
    a = _actor(actor)
    want = max(1, int(limit))
    out: list[BlueskyPost] = []
    params: dict = {"actor": a, "limit": 100, "filter": "posts_with_replies" if with_replies else "posts_no_replies"}
    for page in range(_MAX_PAGES):
        if page and delay_s:
            time.sleep(delay_s + random.random() * delay_s)
        data = _get(PUBLIC, "app.bsky.feed.getAuthorFeed", params, timeout)
        items = [i for i in data.get("feed") or [] if isinstance(i, dict) and isinstance(i.get("post"), dict)]
        for it in items:
            reason = it.get("reason") or {}
            by = (reason.get("by") or {}).get("handle", "") if "reasonRepost" in str(reason.get("$type")) else ""
            if by and not include_reposts:
                continue
            out.append(_post(it["post"], reposted_by=by))
        cursor = data.get("cursor")
        if not items or not cursor or len(out) >= want:
            break
        params["cursor"] = cursor
    return out[:want]


def _resolve_uri(post: str, timeout: int) -> str:
    """A bsky.app post URL or an at:// URI → an at:// URI with a DID."""
    p = (post or "").strip()
    if p.startswith("at://"):
        return p
    m = _URL_RE.search(p)
    if not m:
        raise BlueskyError(f"not a Bluesky post URL or at:// URI: {post!r}")
    who, rkey = m.group(1), m.group(2)
    if not who.startswith("did:"):
        who = _get(PUBLIC, "com.atproto.identity.resolveHandle", {"handle": who}, timeout).get("did") or ""
        if not who:
            raise BlueskyError(f"could not resolve the handle in {post!r}")
    return f"at://{who}/app.bsky.feed.post/{rkey}"


def get_thread(post: str, *, depth: int = 6, timeout: int = 30) -> list[BlueskyPost]:
    """A post and its replies, flattened depth-first (`depth` 0 = the post itself)."""
    data = _get(PUBLIC, "app.bsky.feed.getPostThread", {"uri": _resolve_uri(post, timeout), "depth": depth}, timeout)
    out: list[BlueskyPost] = []

    def walk(node: dict, d: int) -> None:
        if not isinstance(node, dict) or not isinstance(node.get("post"), dict):
            return  # blocked / not-found reply stubs carry no post
        out.append(_post(node["post"], depth=d))
        for child in node.get("replies") or []:
            walk(child, d + 1)

    walk(data.get("thread") or {}, 0)
    if not out:
        raise BlueskyError(f"no thread for {post!r}")
    return out


def get_profile(actor: str, *, timeout: int = 30) -> BlueskyProfile:
    d = _get(PUBLIC, "app.bsky.actor.getProfile", {"actor": _actor(actor)}, timeout)
    return BlueskyProfile(
        did=str(d.get("did") or ""),
        handle=str(d.get("handle") or ""),
        display_name=str(d.get("displayName") or ""),
        description=str(d.get("description") or ""),
        followers=_int(d.get("followersCount")),
        follows=_int(d.get("followsCount")),
        posts=_int(d.get("postsCount")),
        created_at=str(d.get("createdAt") or ""),
    )
