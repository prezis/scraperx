"""vBulletin 3 + 4 forum parser.

vB3 URL convention: forumdisplay.php?f=<id>, showthread.php?t=<id>, ?page=<N>
vB4 URL convention: /forum/forumdisplay.php/<id>-slug, /forum/showthread.php/<id>-slug, /pageN

HTML structure (both versions, common subset):
  - thread row in forumdisplay: <tr id="threadbits_forum_<f>" class="thread"><td>...</td>...</tr>
    OR newer: <li class="threadbit"> with <a href="showthread.php?t=N">
  - post in showthread: <table id="post<pid>" class="tborder">  (vB3)
    OR <li id="post_<pid>" class="postbitlegacy">              (vB4 friendly URLs)

Both versions have:
  - <a href="member.php?u=<uid>"> for author username
  - body in <div id="post_message_<pid>">

Tested against e90post.com (vB3) and bimmerforums.com (vB4 — but Cloudflare blocks).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)


@dataclass
class ThreadRef:
    thread_id: str
    title: str
    url: str
    reply_count: int | None = None
    view_count: int | None = None
    last_post_date: str | None = None  # ISO if parseable


@dataclass
class ForumPost:
    post_id: str
    thread_id: str
    thread_title: str
    thread_url: str
    post_url: str
    author: str | None
    body_html: str
    body_text: str
    posted_at: str | None  # ISO if parseable
    position: int  # 1-indexed within thread


def _resolve(base: str, link: str | None) -> str | None:
    if not link:
        return None
    return urljoin(base, link)


def _extract_thread_id(href: str) -> str | None:
    """vB3: ?t=<id>; vB4: /<id>-slug"""
    if not href:
        return None
    m = re.search(r"[?&]t=(\d+)", href)
    if m:
        return m.group(1)
    m = re.search(r"showthread\.php/(\d+)", href)
    if m:
        return m.group(1)
    return None


def parse_subforum(html: str, base_url: str) -> tuple[list[ThreadRef], str | None]:
    """Returns (threads, next_page_url)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[ThreadRef] = []

    # vB3: <a id="thread_title_NNNN" href="showthread.php?t=NNNN" class="title">
    for a in soup.select("a[id^='thread_title_'], a.title[href*='showthread']"):
        href = a.get("href") or ""
        tid = _extract_thread_id(href)
        if not tid:
            continue
        title = a.get_text(strip=True)
        out.append(
            ThreadRef(
                thread_id=tid,
                title=title,
                url=_resolve(base_url, href) or "",
            )
        )

    # vB4: <h3 class="threadtitle"><a href="...">title</a>
    for a in soup.select("h3.threadtitle a[href*='showthread']"):
        href = a.get("href") or ""
        tid = _extract_thread_id(href)
        if not tid or tid in {t.thread_id for t in out}:
            continue
        out.append(
            ThreadRef(
                thread_id=tid,
                title=a.get_text(strip=True),
                url=_resolve(base_url, href) or "",
            )
        )

    # next page link: <a rel="next"> or text "Next"
    next_url = None
    nxt = soup.find("a", rel="next")
    if nxt and nxt.get("href"):
        next_url = _resolve(base_url, nxt["href"])
    if not next_url:
        for a in soup.find_all("a", href=True):
            txt = (a.get_text() or "").strip().lower()
            if txt in ("next", ">"):
                next_url = _resolve(base_url, a["href"])
                break
    return out, next_url


def parse_thread(html: str, base_url: str, thread_id: str | None = None) -> tuple[list[ForumPost], str | None]:
    """Returns (posts, next_page_url)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[ForumPost] = []

    # Resolve thread title once
    title_el = (
        soup.select_one("h1.threadtitle, span.threadtitle, h2.title, title")
        or soup.find("title")
    )
    thread_title = title_el.get_text(strip=True) if title_el else ""

    # vB3 posts: <table id="post12345" class="tborder">
    # vB4 posts: <li id="post_12345" class="postbitlegacy">
    post_nodes: list[Tag] = []
    for sel in ("table[id^='post']", "li[id^='post_']", "div[id^='post_']"):
        nodes = soup.select(sel)
        if nodes:
            post_nodes = nodes
            break

    for i, node in enumerate(post_nodes, 1):
        node_id = node.get("id") or ""
        m = re.search(r"(\d+)", node_id)
        if not m:
            continue
        pid = m.group(1)

        # Body: <div id="post_message_NNNN"> (both vB3+vB4)
        body_node = node.select_one(f"#post_message_{pid}, div.postcontent, blockquote.postcontent")
        if body_node is None:
            # fallback: any blockquote inside post
            body_node = node.find("blockquote") or node
        body_html = str(body_node)
        body_text = body_node.get_text(separator=" ", strip=True)

        # Author
        author_el = (
            node.select_one("a.bigusername, a[href*='member.php?u=']")
            or node.select_one("a[href*='/members/']")
        )
        author = author_el.get_text(strip=True) if author_el else None

        # Posted-at: vB3 has <td class="thead">DateText</td> as first row in post table
        posted_at = None
        for date_el in node.select(".date, .postdate, time"):
            txt = date_el.get_text(strip=True)
            if txt and any(ch.isdigit() for ch in txt):
                posted_at = txt
                break

        if len(body_text) < 5:
            continue

        out.append(
            ForumPost(
                post_id=pid,
                thread_id=thread_id or "",
                thread_title=thread_title,
                thread_url=base_url,
                post_url=f"{base_url}#post{pid}",
                author=author,
                body_html=body_html,
                body_text=body_text,
                posted_at=posted_at,
                position=i,
            )
        )

    # next page
    next_url = None
    nxt = soup.find("a", rel="next")
    if nxt and nxt.get("href"):
        next_url = _resolve(base_url, nxt["href"])
    return out, next_url
