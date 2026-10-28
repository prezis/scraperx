#!/usr/bin/env python3
"""CLI for ScraperX — X/Twitter + YouTube scraper + blockchain explorer.

Usage:
    python -m scraperx https://x.com/user/status/123456
    python -m scraperx https://x.com/user/status/123456 --json
    python -m scraperx https://x.com/user/status/123456 --thread
    python -m scraperx https://x.com/elonmusk              # profile
    python -m scraperx https://youtube.com/watch?v=ID
    python -m scraperx basescan 0xADDRESS                  # Basescan address
    python -m scraperx dexscreener 0xADDRESS               # DexScreener token
    scraperx https://x.com/user/status/123456               # if pip installed
"""

import argparse
import json
import logging
import re
import sys

from scraperx.video_discovery import discover_videos
from scraperx.vimeo_scraper import VimeoScraper, _is_vimeo_url

from .profile import PROFILE_URL_RE, get_profile, parse_profile_url
from .scraper import TWEET_URL_RE, Tweet, XScraper
from .search import search_tweets
from .youtube_scraper import YOUTUBE_URL_RE, YouTubeScraper


def _is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(url))


def _is_tweet_url(url: str) -> bool:
    return bool(TWEET_URL_RE.search(url))


def _is_profile_url(url: str) -> bool:
    return bool(PROFILE_URL_RE.search(url))


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class _SafeTerminal:
    """Scraped text is untrusted: an ANSI / OSC escape inside a post, comment or transcript could rewrite the reader's
    terminal (title, clipboard, screen). Everything the CLI prints loses its C0 / C1 control characters except
    newline and tab. JSON output is unaffected: json.dumps already writes them as \\u escapes."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(_CONTROL_RE.sub("", text))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _guard_terminal() -> None:
    if not isinstance(sys.stdout, _SafeTerminal):
        sys.stdout = _SafeTerminal(sys.stdout)
    if not isinstance(sys.stderr, _SafeTerminal):
        sys.stderr = _SafeTerminal(sys.stderr)


def main():
    _guard_terminal()
    # Check if first arg is a subcommand
    if len(sys.argv) > 1:
        subcmd = sys.argv[1]
        if subcmd == "search":
            _main_search()
            return
        if subcmd == "reddit":
            _main_reddit()
            return
        if subcmd == "telegram":
            _main_telegram()
            return
        if subcmd == "x":
            _main_x()
            return
        if subcmd == "basescan":
            _main_basescan()
            return
        if subcmd == "dexscreener":
            _main_dexscreener()
            return
        if subcmd == "screenshot":
            _main_screenshot()
            return
        if subcmd == "discover":
            _main_discover()
            return
        if subcmd == "page-title":
            _main_page_title()
            return
        if subcmd == "label-extract":
            _main_label_extract()
            return
        if subcmd == "chips":
            _main_chips()
            return
        if subcmd == "github":
            from scraperx.github_analyzer.cli import main_github

            sys.exit(main_github())
        if subcmd == "trending":
            from scraperx.github_analyzer.cli import main_trending

            sys.exit(main_trending())
        if subcmd == "gh-discover":
            from scraperx.gh_discover import main_gh_discover

            sys.exit(main_gh_discover())
        if subcmd == "tv-resolve":
            from scraperx.tv_symbol_resolver import main_tv_resolve

            sys.exit(main_tv_resolve())
        if subcmd == "doctor":
            from scraperx.doctor import main as doctor_main

            sys.exit(doctor_main())
        if subcmd == "docs-crawl":
            from scraperx.docs_crawler import _main_docs_crawl

            sys.exit(_main_docs_crawl())
    _main_url()


def _main_search():
    parser = argparse.ArgumentParser(
        prog="scraperx search",
        description="Search posts on X (keywords, #tag, \"phrase\", from:user): Desearch (SCRAPERX_DESEARCH_API_KEY), "
        "Brave Search API (SCRAPERX_BRAVE_API_KEY), Nitter RSS (no key), DuckDuckGo (no key), in that order",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "search"
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--time", "-t", choices=["d", "w", "m", "y"], help="Time filter")
    parser.add_argument(
        "--backend",
        choices=["auto", "desearch", "brave", "nitter", "ddg"],
        default="auto",
        help="auto = Desearch, then Brave (each only when its key is set), then Nitter RSS (no key), then DuckDuckGo "
        "(default); desearch / brave / nitter / ddg = that one only",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--fast", action="store_true", help="Skip enrichment")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    _handle_search(args)


def _main_reddit():
    parser = argparse.ArgumentParser(
        prog="scraperx reddit",
        description="Reddit without an account: old.reddit.com JSON, then redlib mirrors, then the stealth browser "
        "(headless) on reddit.com and on the mirrors; the tier that served is printed to stderr",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "reddit"
    sub = parser.add_subparsers(dest="action", required=True)
    p_search = sub.add_parser("search", help="search all of Reddit, or one subreddit with --sub")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--sub", help="search inside this subreddit only")
    p_search.add_argument("--sort", default=None, choices=["relevance", "hot", "top", "new", "comments"],
                          help="default: relevance (all of Reddit) / new (--sub)")
    p_search.add_argument("--time", "-t", default="all", choices=["hour", "day", "week", "month", "year", "all"],
                          help="time filter, all of Reddit only (default: all)")
    p_sub = sub.add_parser("sub", help="a subreddit listing")
    p_sub.add_argument("name")
    p_sub.add_argument("--sort", default="hot", choices=["hot", "new", "top", "rising"])
    p_thread = sub.add_parser("thread", help="a thread with its comments")
    p_thread.add_argument("url")
    p_comments = sub.add_parser("comments", help="search COMMENTS (Arctic Shift archive; needs --sub or --author)")
    p_comments.add_argument("query", nargs="*", help="full text: words, \"phrase\", a OR b, -word")
    p_comments.add_argument("--sub", help="subreddit")
    p_comments.add_argument("--author", help="author")
    p_user = sub.add_parser("user", help="an author's newest posts and comments (Arctic Shift archive)")
    p_user.add_argument("name")
    for p in (p_search, p_sub):
        p.add_argument("--limit", "-n", type=int, default=25, help="max posts (default 25; more = one fetch per 25)")
    for p in (p_comments, p_user):
        p.add_argument("--limit", "-n", type=int, default=50, help="max items (default 50)")
    for p in (p_search, p_sub, p_thread, p_comments, p_user):
        p.add_argument("--json", action="store_true", help="output JSON")
        p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    _handle_reddit(args)


def _handle_reddit(args):
    from dataclasses import asdict

    from .reddit import RedditError, RedditScraper

    if args.action in ("comments", "user"):
        _handle_reddit_archive(args)
        return
    s = RedditScraper()
    try:
        if args.action == "thread":
            result = s.get_thread(args.url)
        elif args.action == "sub":
            result = s.get_subreddit_posts(args.name, sort=args.sort, limit=args.limit)
        elif args.sub:
            result = s.search_subreddit(args.sub, " ".join(args.query), args.limit, sort=args.sort or "new")
        else:
            result = s.search(" ".join(args.query), args.limit, sort=args.sort or "relevance", time_filter=args.time)
    except RedditError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    served = f"{s.last_tier_used} via {s.last_mirror_used}" if s.last_mirror_used else s.last_tier_used
    if args.json:
        payload = asdict(result) if args.action == "thread" else [asdict(p) for p in result]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.action == "thread":
        _print_reddit_post(result.post, comments=f"{len(result.comments)} loaded")
        if result.post.selftext:
            print(result.post.selftext + "\n")
        for c in result.comments:
            pad = "  " * c.depth
            print(f"{pad}u/{c.author} ({c.score})")
            for line in c.body.splitlines():
                print(f"{pad}  {line}")
    else:
        for p in result:
            _print_reddit_post(p)
    print(f"source: {served}", file=sys.stderr)


def _main_telegram():
    parser = argparse.ArgumentParser(
        prog="scraperx telegram",
        description="Public Telegram channels through the t.me/s/ web preview (no account, no API id)",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "telegram"
    sub = parser.add_subparsers(dest="action", required=True)
    p_posts = sub.add_parser("posts", help="newest messages of a channel")
    p_posts.add_argument("channel")
    p_posts.add_argument("--before", type=int, help="only messages older than this id")
    p_search = sub.add_parser("search", help="messages of one channel matching a query")
    p_search.add_argument("channel")
    p_search.add_argument("query", nargs="+")
    p_info = sub.add_parser("info", help="channel title, description, subscribers")
    p_info.add_argument("channel")
    for p in (p_posts, p_search):
        p.add_argument("--limit", "-n", type=int, default=20, help="max messages (default 20)")
    for p in (p_posts, p_search, p_info):
        p.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    from dataclasses import asdict

    from .telegram import TelegramError, get_channel_info, get_channel_posts, search_channel

    try:
        if args.action == "info":
            result = get_channel_info(args.channel)
        elif args.action == "search":
            result = search_channel(args.channel, " ".join(args.query), args.limit)
        else:
            result = get_channel_posts(args.channel, args.limit, before=args.before)
    except TelegramError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        payload = asdict(result) if args.action == "info" else [asdict(p) for p in result]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.action == "info":
        print(f"{result.title} (@{result.username}){' [verified]' if result.verified else ''}")
        print(f"  {result.subscribers_text or '?'} subscribers\n  {result.description}")
    else:
        for p in result:
            fwd = " [forwarded" + (f" from {p.forwarded_from}" if p.forwarded_from else "") + "]" if p.forwarded else ""
            media = f" [{len(p.photos)} photo, {len(p.videos)} video]" if p.photos or p.videos else ""
            print(f"{p.date[:16]}  {p.views_text or '?':>6} views  {p.url}{fwd}{media}")
            for line in p.text.splitlines():
                print(f"  {line}")
            print()


def _handle_reddit_archive(args):
    from dataclasses import asdict

    from .reddit import RedditError
    from .reddit_archive import iso, search_comments, user_history

    try:
        if args.action == "user":
            posts, comments = user_history(args.name, args.limit)
        else:
            posts, comments = [], search_comments(
                " ".join(args.query) or None, subreddit=args.sub, author=args.author, limit=args.limit
            )
    except RedditError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({"posts": [asdict(p) for p in posts], "comments": [asdict(c) for c in comments]},
                         indent=2, ensure_ascii=False))
    else:
        for p in posts:
            _print_reddit_post(p)
        for c in comments:
            print(f"[{c.score:>5}] {iso(c.created_utc)[:16]}  r/{c.subreddit}  u/{c.author}  in: {c.link_title[:70]}")
            for line in c.body.splitlines()[:8]:
                print(f"  {line}")
            print(f"  https://www.reddit.com{c.permalink}\n")
    print(f"source: arctic_shift archive ({len(posts)} posts, {len(comments)} comments; scores as of archive "
          "time, 0/1 for items younger than ~36 h)", file=sys.stderr)


def _print_reddit_post(p, comments: str | None = None) -> None:
    pin = " [pinned]" if p.stickied else ""
    count = comments or f"{p.num_comments:>4}c"
    print(f"[{p.score:>5}] {count}  {p.created_iso[:16] or '?'}  r/{p.subreddit}  u/{p.author or '?'}{pin}")
    print(f"  {p.title}")
    print(f"  {p.full_url}\n")


def _main_url():
    parser = argparse.ArgumentParser(description="Scrape X/Twitter tweets, profiles, threads, or YouTube transcripts")
    parser.add_argument("url", nargs="?", help="Tweet URL, profile URL, or YouTube URL")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--thread", action="store_true", help="Fetch full thread")
    parser.add_argument("--cookies", help="Path to cookies file for yt-dlp")
    parser.add_argument("--whisper-model", default="base", help="Whisper model (base/medium/large)")
    parser.add_argument("--force-whisper", action="store_true", help="Skip auto-captions, use whisper")
    parser.add_argument("--silent-video-frames", type=int, default=8,
                        help="N frames to OCR when video has no audio (tweet videos). 0 = disable")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if not args.url:
        parser.print_help()
        sys.exit(1)

    if _is_youtube_url(args.url):
        _handle_youtube(args)
    elif _is_vimeo_url(args.url):
        _handle_vimeo(args)
    elif _is_tweet_url(args.url):
        if args.thread:
            _handle_thread(args)
        else:
            _handle_tweet(args)
    elif _is_profile_url(args.url):
        _handle_profile(args)
    else:
        # Try as bare handle (e.g., "elonmusk" or "@elonmusk")
        handle = args.url.lstrip("@")
        if handle.isalnum() or "_" in handle:
            args.url = handle
            _handle_profile_by_handle(args)
        else:
            print(f"ERROR: Unrecognized URL format: {args.url}", file=sys.stderr)
            sys.exit(1)


def _handle_youtube(args):
    scraper = YouTubeScraper(whisper_model=args.whisper_model)
    try:
        result = scraper.get_transcript(args.url, force_whisper=args.force_whisper)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "video_id": result.video_id,
            "title": result.title,
            "channel": result.channel,
            "duration_seconds": result.duration_seconds,
            "transcript_method": result.transcript_method,
            "transcript_path": result.transcript_path,
            "transcript_length": len(result.transcript),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"{result.title} ({result.channel})")
        print(f"Duration: {result.duration_seconds // 60}min")
        print(f"Method: {result.transcript_method}")
        print("---")
        if len(result.transcript) > 2000:
            print(result.transcript[:2000])
            print(f"\n... [{len(result.transcript) - 2000} more chars]")
            print(f"Full transcript: {result.transcript_path}")
        else:
            print(result.transcript)


def _handle_vimeo(args):
    scraper = VimeoScraper()
    try:
        result = scraper.get_transcript(args.url, force_whisper=args.force_whisper)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "provider": result.provider,
            "video_id": result.video_id,
            "title": result.title,
            "author": result.author,
            "duration_seconds": result.duration_seconds,
            "canonical_url": result.canonical_url,
            "transcript": result.transcript,
            "transcript_method": result.transcript_method,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"Title: {result.title}")
        print(f"Author: {result.author}")
        print(f"Duration: {int(result.duration_seconds) // 60}min")
        print(f"Method: {result.transcript_method}")
        print("---")
        print(f"\nTranscript:\n{result.transcript}")


def _main_discover():
    parser = argparse.ArgumentParser(
        prog="scraperx discover", description="Detect embedded videos on an arbitrary webpage"
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "discover"
    parser.add_argument("url", help="Webpage URL to scan for embedded videos")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--timeout", type=int, default=15, help="Fetch timeout in seconds (default: 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    try:
        refs = discover_videos(args.url, timeout=args.timeout)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = [
            {
                "provider": r.provider,
                "id": r.id,
                "canonical_url": r.canonical_url,
                "embed_url": r.embed_url,
                "page_url": r.page_url,
                "referer": r.referer,
                "extra": r.extra,
            }
            for r in refs
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        if not refs:
            print(f"No videos detected on {args.url}")
            return
        print(f"Detected {len(refs)} video(s) on {args.url}:\n")
        for i, r in enumerate(refs, 1):
            print(f"[{i}] {r.provider}  id={r.id}")
            print(f"    canonical: {r.canonical_url}")
            print(f"    embed:     {r.embed_url}")
            if r.extra:
                print(f"    extra:     {r.extra}")
            print()


def _tweet_to_dict(tweet: Tweet) -> dict:
    """Serialize a Tweet to dict, recursively including quoted_tweet."""
    d = {
        "id": tweet.id,
        "text": tweet.text,
        "author": tweet.author,
        "author_handle": tweet.author_handle,
        "likes": tweet.likes,
        "retweets": tweet.retweets,
        "replies": tweet.replies,
        "views": tweet.views,
        "media_urls": tweet.media_urls,
        "article_title": tweet.article_title,
        "article_text": tweet.article_text,
        "source_method": tweet.source_method,
    }
    if tweet.quoted_tweet:
        d["quoted_tweet"] = _tweet_to_dict(tweet.quoted_tweet)
    return d


def _print_quoted_tweet(tweet: Tweet, indent: int = 2) -> None:
    """Print a quoted tweet with indentation, recursing for nested quotes."""
    prefix = " " * indent + "| "
    print(f"\n{' ' * indent}[Quote] @{tweet.author_handle} ({tweet.author}):")
    for line in tweet.text.splitlines():
        print(f"{prefix}{line}")
    if tweet.article_title:
        print(f"{prefix}")
        print(f"{prefix}[Article: {tweet.article_title}]")
        if tweet.article_text:
            for line in tweet.article_text.splitlines()[:10]:
                print(f"{prefix}  {line}")
            total_lines = len(tweet.article_text.splitlines())
            if total_lines > 10:
                print(f"{prefix}  ... [{total_lines - 10} more lines]")
    if tweet.media_urls:
        print(f"{prefix}Media: {len(tweet.media_urls)} file(s)")
    stats = []
    if tweet.likes:
        stats.append(f"{tweet.likes} likes")
    if tweet.retweets:
        stats.append(f"{tweet.retweets} RT")
    if tweet.views:
        stats.append(f"{tweet.views} views")
    if stats:
        print(f"{prefix}{' | '.join(stats)}")
    if tweet.quoted_tweet:
        _print_quoted_tweet(tweet.quoted_tweet, indent + 2)


def _maybe_transcribe_silent_video(media_urls: list[str], n_frames: int) -> dict | None:
    """If a tweet's media is a silent video (twimg.com/amplify_video, no audio
    track), OCR-transcribe the frames and return a dict. Returns None if no
    eligible video or transcription is disabled.

    Used by _handle_tweet to auto-fill the gap exposed on 2026-05-17 (gitlawb
    OpenGateway demo tweet). See ~/ai/global-graph/patterns/silent-video-frame-ocr-fallback.md.
    """
    if n_frames <= 0 or not media_urls:
        return None
    video_url = next(
        (u for u in media_urls if any(t in u for t in (".mp4", "video.twimg.com", "amplify_video"))),
        None,
    )
    if not video_url:
        return None
    try:
        from scraperx.silent_video_ocr import (
            transcribe_silent_video,
            SilentVideoNotAvailable,
        )
        result = transcribe_silent_video(video_url, n_frames=n_frames)
        # Only attach if it really was silent (otherwise leave to whisper paths)
        if result.has_audio:
            return None
        return result.to_dict()
    except SilentVideoNotAvailable:
        return {"error": "silent-video deps missing — install scraperx[silent-video]"}
    except Exception as e:
        return {"error": f"silent_video_ocr failed: {type(e).__name__}: {e}"}


def _handle_tweet(args):
    scraper = XScraper(ytdlp_cookies=args.cookies)
    try:
        tweet = scraper.get_tweet(args.url)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Auto-transcribe silent video media via frame OCR
    svo = _maybe_transcribe_silent_video(
        list(tweet.media_urls or []), getattr(args, "silent_video_frames", 8)
    )

    if args.json:
        out = _tweet_to_dict(tweet)
        if svo:
            out["silent_video_transcript"] = svo
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"@{tweet.author_handle} ({tweet.author})")
        print("---")
        print(tweet.text)
        if tweet.article_title:
            print(f"\n[Article: {tweet.article_title}]")
        if tweet.media_urls:
            print(f"\nMedia: {len(tweet.media_urls)} file(s)")
            for u in tweet.media_urls:
                print(f"  {u}")
        if svo:
            print(f"\n[Silent-video OCR ({svo.get('n_frames_sampled', 0)} frames @ "
                  f"{svo.get('duration_sec', 0):.1f}s)]")
            print(svo.get("summary", "(no summary)"))
        if tweet.quoted_tweet:
            _print_quoted_tweet(tweet.quoted_tweet)
        print(f"\n{tweet.likes} likes | {tweet.retweets} RT | {tweet.views} views")
        print(f"(via {tweet.source_method})")


def _handle_thread(args):
    from .thread import get_thread

    try:
        thread = get_thread(args.url)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        tweets_out = []
        for t in thread.all_tweets:
            tweets_out.append(_tweet_to_dict(t))
        out = {
            "total_tweets": thread.total_tweets,
            "tweets": tweets_out,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"Thread by @{thread.root_tweet.author_handle} ({thread.total_tweets} tweets)")
        print("===")
        for i, t in enumerate(thread.all_tweets, 1):
            print(f"\n[{i}/{thread.total_tweets}]")
            print(t.text)
            if t.media_urls:
                print(f"  Media: {len(t.media_urls)} file(s)")
            if t.quoted_tweet:
                _print_quoted_tweet(t.quoted_tweet)


def _handle_profile(args):
    try:
        handle = parse_profile_url(args.url)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    args.url = handle
    _handle_profile_by_handle(args)


def _handle_profile_by_handle(args):
    handle = args.url.lstrip("@")
    try:
        profile = get_profile(handle)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "handle": profile.handle,
            "name": profile.name,
            "bio": profile.bio,
            "followers": profile.followers,
            "following": profile.following,
            "tweets_count": profile.tweets_count,
            "likes_count": profile.likes_count,
            "joined": profile.joined,
            "location": profile.location,
            "website": profile.website,
            "verified": profile.verified,
            "source_method": profile.source_method,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        v = " [verified]" if profile.verified else ""
        print(f"@{profile.handle} ({profile.name}){v}")
        print("---")
        if profile.bio:
            print(profile.bio)
        print(f"\n{profile.followers:,} followers | {profile.following:,} following | {profile.tweets_count:,} tweets")
        if profile.location:
            print(f"Location: {profile.location}")
        if profile.website:
            print(f"Website: {profile.website}")
        if profile.joined:
            print(f"Joined: {profile.joined}")
        print(f"(via {profile.source_method})")


def _handle_search(args):
    query = " ".join(args.query)
    try:
        tweets = search_tweets(
            query,
            limit=args.limit,
            time_filter=getattr(args, "time", None),
            enrich=not args.fast,
            backend=getattr(args, "backend", "auto"),
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not tweets:
        print("No tweets found.", file=sys.stderr)
        sys.exit(0)

    _print_tweets(tweets, args.json, f"Found {len(tweets)} tweets for: {query}")


def _print_tweets(tweets, as_json: bool, header: str = "") -> None:
    if as_json:
        out = [
            {
                "id": t.id,
                "text": t.text,
                "author": t.author,
                "author_handle": t.author_handle,
                "created_at": t.created_at,
                "likes": t.likes,
                "retweets": t.retweets,
                "replies": t.replies,
                "views": t.views,
                "media_urls": t.media_urls,
                "is_reply": t.is_reply,
                "in_reply_to_tweet_id": t.in_reply_to_tweet_id,
                "reposted_by": t.reposted_by,
                "url": f"https://x.com/{t.author_handle}/status/{t.id}",
                "source_method": t.source_method,
            }
            for t in tweets
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if header:
        print(header + "\n")
    for i, t in enumerate(tweets, 1):
        rp = f"  [reposted by @{t.reposted_by}]" if t.reposted_by else ""
        print(f"[{i}] @{t.author_handle} ({t.author}){rp}  {t.created_at or ''}")
        text_preview = t.text[:200] + ("..." if len(t.text) > 200 else "")
        print(f"    {text_preview}")
        if t.likes or t.views:
            print(f"    {t.likes} likes | {t.retweets} RT | {t.views:,} views")
        print(f"    https://x.com/{t.author_handle}/status/{t.id}")
        print(f"    (via {t.source_method})")
        print()


def _main_x():
    parser = argparse.ArgumentParser(
        prog="scraperx x",
        description="X/Twitter without an X account: timelines and replies via FxTwitter v2, search via scraperx search",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "x"
    sub = parser.add_subparsers(dest="action", required=True)
    p_user = sub.add_parser("user", help="newest posts of an account")
    p_user.add_argument("handle")
    p_user.add_argument("--replies", action="store_true", help="include the account's replies")
    p_user.add_argument("--no-reposts", action="store_true", help="only the account's own posts")
    p_replies = sub.add_parser("replies", help="replies under a post")
    p_replies.add_argument("post", help="post URL or id")
    p_trends = sub.add_parser("trends", help="trending topics")
    for p in (p_user, p_replies, p_trends):
        p.add_argument("--limit", "-n", type=int, default=20, help="max items (default 20)")
        p.add_argument("--json", action="store_true", help="output JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    from .x_timeline import FxV2Error, get_replies, get_trends, get_user_tweets

    try:
        if args.action == "trends":
            trends = get_trends(args.limit)
            if args.json:
                print(json.dumps(trends, indent=2, ensure_ascii=False))
            else:
                for t in trends:
                    print(t.get("name") or t.get("query") or json.dumps(t, ensure_ascii=False))
            return
        if args.action == "user":
            tweets = get_user_tweets(
                args.handle, args.limit, with_replies=args.replies, include_reposts=not args.no_reposts
            )
        else:
            tweets = get_replies(args.post, args.limit)
    except FxV2Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    _print_tweets(tweets, args.json)


def _main_basescan():
    from .screenshot import PlaywrightNotAvailable, scrape_basescan_address

    parser = argparse.ArgumentParser(
        prog="scraperx basescan", description="Scrape Basescan address info via headless browser"
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "basescan"
    parser.add_argument("address", help="Ethereum address (0x...)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--timeout", type=int, default=30000, help="Timeout ms (default: 30000)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    try:
        result = scrape_basescan_address(args.address, timeout=args.timeout)
    except (PlaywrightNotAvailable, ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "address": result.address,
            "is_contract": result.is_contract,
            "eth_balance": result.eth_balance,
            "eth_value_usd": result.eth_value_usd,
            "token_holdings_count": result.token_holdings_count,
            "transaction_count": result.transaction_count,
            "contract_creator": result.contract_creator,
            "contract_name": result.contract_name,
            "source_method": result.source_method,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        kind = "Contract" if result.is_contract else "EOA (Externally Owned Account)"
        print(f"Address: {result.address}")
        print(f"Type: {kind}")
        if result.contract_name:
            print(f"Contract Name: {result.contract_name}")
        if result.eth_balance:
            usd_part = f" (${result.eth_value_usd})" if result.eth_value_usd else ""
            print(f"ETH Balance: {result.eth_balance} ETH{usd_part}")
        if result.transaction_count:
            print(f"Transactions: {result.transaction_count:,}")
        if result.token_holdings_count:
            print(f"Token Holdings: {result.token_holdings_count}")
        if result.contract_creator:
            print(f"Creator: {result.contract_creator}")
        print(f"(via {result.source_method})")


def _main_dexscreener():
    from .screenshot import PlaywrightNotAvailable, scrape_dexscreener_token

    parser = argparse.ArgumentParser(
        prog="scraperx dexscreener", description="Scrape DexScreener token info on Base chain via headless browser"
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "dexscreener"
    parser.add_argument("address", help="Token contract address (0x...)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--timeout", type=int, default=30000, help="Timeout ms (default: 30000)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    try:
        result = scrape_dexscreener_token(args.address, timeout=args.timeout)
    except (PlaywrightNotAvailable, ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "address": result.address,
            "name": result.name,
            "symbol": result.symbol,
            "price": result.price,
            "price_change_24h": result.price_change_24h,
            "liquidity": result.liquidity,
            "volume_24h": result.volume_24h,
            "market_cap": result.market_cap,
            "fdv": result.fdv,
            "pair_count": result.pair_count,
            "source_method": result.source_method,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        title = result.name
        if result.symbol:
            title += f" ({result.symbol})"
        if title:
            print(title)
        print(f"Address: {result.address}")
        if result.price:
            change = f" ({result.price_change_24h})" if result.price_change_24h else ""
            print(f"Price: {result.price}{change}")
        if result.liquidity:
            print(f"Liquidity: ${result.liquidity}")
        if result.volume_24h:
            print(f"Volume 24h: ${result.volume_24h}")
        if result.market_cap:
            print(f"Market Cap: ${result.market_cap}")
        if result.fdv:
            print(f"FDV: ${result.fdv}")
        if result.pair_count:
            print(f"Pairs: {result.pair_count}")
        print(f"(via {result.source_method})")


def _main_screenshot():
    from .screenshot import PlaywrightNotAvailable, screenshot_url

    parser = argparse.ArgumentParser(
        prog="scraperx screenshot",
        description="Take a headless background screenshot of any URL",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "screenshot"
    parser.add_argument("url", help="URL to screenshot")
    parser.add_argument("-o", "--output", default="screenshot.png", help="Output path (default: screenshot.png)")
    parser.add_argument("--full-page", action="store_true", help="Capture full scrollable page")
    parser.add_argument("--wait", default=None, help="CSS selector to wait for before screenshot")
    parser.add_argument("--timeout", type=int, default=30000, help="Timeout in ms (default: 30000)")
    parser.add_argument("--width", type=int, default=1280, help="Viewport width")
    parser.add_argument("--height", type=int, default=900, help="Viewport height")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        path = screenshot_url(
            args.url,
            output_path=args.output,
            full_page=args.full_page,
            wait_selector=args.wait,
            timeout=args.timeout,
            viewport=(args.width, args.height),
        )
        print(f"Screenshot saved: {path}")
    except PlaywrightNotAvailable as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _main_page_title():
    from .explorer_label import page_title

    parser = argparse.ArgumentParser(
        prog="scraperx page-title",
        description="Print the <title> tag text for a URL (block-explorers handled, others best-effort)",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "page-title"
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout in seconds (default: 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        print(page_title(args.url, timeout=args.timeout))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _main_label_extract():
    from .explorer_label import ExplorerNotSupported, extract_label

    parser = argparse.ArgumentParser(
        prog="scraperx label-extract",
        description="Extract the human label from a block-explorer URL (Etherscan family, Solscan, Tronscan, ...)",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "label-extract"
    parser.add_argument("url", help="Block-explorer URL (e.g. https://etherscan.io/address/0x...)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout in seconds (default: 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        result = extract_label(args.url, timeout=args.timeout)
    except ExplorerNotSupported as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "url": result.url,
            "title": result.title,
            "address": result.address,
            "label": result.label,
            "exchange_guess": result.exchange_guess,
            "chain": result.chain,
            "explorer": result.explorer,
            "source": result.source,
            "chips": list(result.chips),
            "errors": result.errors,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"URL:     {result.url}")
        print(f"Title:   {result.title or '-'}")
        print(f"Address: {result.address or '-'}")
        print(f"Label:   {result.label or '(no label assigned by explorer)'}")
        if result.exchange_guess:
            print(f"Guess:   {result.exchange_guess}")
        print(f"Chain:   {result.chain or '-'}")
        if result.chips:
            print(f"Chips:   {', '.join(result.chips)}")
        print(f"Source:  {result.source}")


def _main_chips():
    from .explorer_label import ExplorerNotSupported, extract_label

    parser = argparse.ArgumentParser(
        prog="scraperx chips",
        description="Print just the metadata chips from an Etherscan-family address page (one per line)",
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "chips"
    parser.add_argument("url", help="Block-explorer URL (e.g. https://etherscan.io/address/0x...)")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout in seconds (default: 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        result = extract_label(args.url, timeout=args.timeout)
    except ExplorerNotSupported as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    for chip in result.chips:
        print(chip)


if __name__ == "__main__":
    main()
