"""e90post.com BMW forum scraper.

vBulletin 3, anonymous read OK, no Cloudflare, no AI-bot ban in robots.txt.

Subforums: F30, E90, E91, E92, E93, F32, F33, F36 + hybrid + service + DIY.
We enumerate the main BMW subforum index and walk thread listings.

Conservative: 0.5 req/s = 1 req per 2 seconds.

Usage:
  python -m scraperx.bmw_corpus.forum_scraper.e90post --subforum 2 --max-threads 5
  python -m scraperx.bmw_corpus.forum_scraper.e90post --max-pages 1 --max-threads 10
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from scraperx.bmw_corpus._output import write_batch
from scraperx.bmw_corpus.forum_scraper._http import RateLimitedClient
from scraperx.bmw_corpus.forum_scraper.engines.vbulletin import (
    ForumPost,
    ThreadRef,
    parse_subforum,
    parse_thread,
)

log = logging.getLogger(__name__)

BASE = "https://www.e90post.com"  # 2026-04-25: non-www returns empty body

# Curated subforum IDs from e90post (verified live during recon).
# `f` query param. Names from the front page.
SUBFORUMS = {
    2: "General E90 / E91 / E92 / E93 Discussion",
    37: "E90/E91 Sedan / Touring",
    47: "E92/E93 Coupe / Vert",
    87: "F30 Sedan / F31 Touring 2012-2019",
    93: "M3 / M4 (G80/G82/G83)",
    20: "335i Forum (E90/E91/E92/E93)",
    105: "DIY",
    99: "Drivetrain",
}

DEFAULT_RATE = 0.5  # req/s


def make_record(post: ForumPost, subforum_id: int, subforum_name: str, lang: str = "en") -> dict:
    return {
        "source": "e90post",
        "source_id": f"post:{post.post_id}",
        "source_url": post.post_url,
        "source_lang": lang,
        "content_type": "forum_post",
        "title": post.thread_title or None,
        "body_text": post.body_text,
        "raw_payload": {
            "post_id": post.post_id,
            "thread_id": post.thread_id,
            "thread_url": post.thread_url,
            "author": post.author,
            "posted_at_raw": post.posted_at,
            "position": post.position,
            "subforum_id": subforum_id,
            "subforum_name": subforum_name,
            "body_html_len": len(post.body_html),
        },
        "metadata_json": {
            "subforum_id": subforum_id,
            "subforum_name": subforum_name,
            "thread_id": post.thread_id,
            "post_position": post.position,
            "author": post.author,
        },
        "bmw_models": None,  # post-process pass via subforum_name + body NER
        "year_from": None,
        "year_to": None,
        "published_at": None,  # parse vB date string later
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def crawl_subforum(
    client: RateLimitedClient,
    subforum_id: int,
    subforum_name: str,
    max_pages: int = 1,
    max_threads: int = 5,
    max_thread_pages: int = 2,
) -> list[dict]:
    """Walk subforum index → up to max_threads threads → up to max_thread_pages of posts each."""
    records: list[dict] = []
    listing_url = f"{BASE}/forums/forumdisplay.php?f={subforum_id}"
    seen_threads = 0

    for page in range(1, max_pages + 1):
        page_url = listing_url if page == 1 else f"{listing_url}&page={page}"
        log.info("subforum %d page %d -> %s", subforum_id, page, page_url)
        try:
            html = client.get_html(page_url)
        except Exception as e:
            log.warning("subforum %d page %d failed: %s", subforum_id, page, e)
            break
        threads, _next = parse_subforum(html, listing_url)
        log.info("  %d threads found", len(threads))

        for tref in threads:
            if seen_threads >= max_threads:
                break
            seen_threads += 1
            thread_url = tref.url
            log.info("  thread %s -> %s", tref.thread_id, thread_url[:70])
            for tpage in range(1, max_thread_pages + 1):
                turl = thread_url if tpage == 1 else f"{thread_url}&page={tpage}"
                try:
                    thtml = client.get_html(turl)
                except Exception as e:
                    log.warning("  thread %s page %d failed: %s", tref.thread_id, tpage, e)
                    break
                posts, tnext = parse_thread(thtml, turl, thread_id=tref.thread_id)
                log.info("    page %d: %d posts", tpage, len(posts))
                for p in posts:
                    records.append(make_record(p, subforum_id, subforum_name))
                if not tnext or tpage >= max_thread_pages:
                    break
        if seen_threads >= max_threads:
            break
    return records


def main() -> int:
    p = argparse.ArgumentParser(description="e90post.com BMW forum scraper")
    p.add_argument("--subforum", type=int, default=None, help="Specific subforum ID (default: all curated)")
    p.add_argument("--max-pages", type=int, default=1, help="Subforum listing pages")
    p.add_argument("--max-threads", type=int, default=5, help="Threads per subforum")
    p.add_argument("--max-thread-pages", type=int, default=2, help="Pages per thread")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE, help="Requests per second")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = RateLimitedClient(host="e90post.com", rate_per_second=args.rate)

    targets: list[tuple[int, str]] = (
        [(args.subforum, SUBFORUMS.get(args.subforum, f"forum_{args.subforum}"))]
        if args.subforum
        else list(SUBFORUMS.items())
    )

    log.info(
        "e90post crawl: %d subforums × max %d pages × %d threads × %d thread-pages, rate=%.2f req/s",
        len(targets), args.max_pages, args.max_threads, args.max_thread_pages, args.rate,
    )

    all_records: list[dict] = []
    for sf_id, sf_name in targets:
        recs = crawl_subforum(
            client, sf_id, sf_name,
            max_pages=args.max_pages,
            max_threads=args.max_threads,
            max_thread_pages=args.max_thread_pages,
        )
        all_records.extend(recs)
        log.info("subforum %d (%s) yielded %d posts (running total %d)", sf_id, sf_name, len(recs), len(all_records))

    log.info("Total posts: %d", len(all_records))
    if args.dry_run:
        for r in all_records[:3]:
            log.info("  sample: %s — %s", r["source_id"], (r["title"] or "")[:60])
        return 0

    if all_records:
        path, n = write_batch(all_records)
        log.info("Wrote %d records to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
