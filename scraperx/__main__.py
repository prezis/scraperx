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
import sys

from .scraper import XScraper, Tweet, TWEET_URL_RE
from .youtube_scraper import YouTubeScraper, YOUTUBE_URL_RE
from .profile import get_profile, parse_profile_url, PROFILE_URL_RE
from .search import search_tweets


def _is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(url))


def _is_tweet_url(url: str) -> bool:
    return bool(TWEET_URL_RE.search(url))


def _is_profile_url(url: str) -> bool:
    return bool(PROFILE_URL_RE.search(url))


def main():
    # Check if first arg is a subcommand
    if len(sys.argv) > 1:
        subcmd = sys.argv[1]
        if subcmd == "search":
            _main_search()
            return
        if subcmd == "basescan":
            _main_basescan()
            return
        if subcmd == "dexscreener":
            _main_dexscreener()
            return
    _main_url()


def _main_search():
    parser = argparse.ArgumentParser(
        prog="scraperx search",
        description="Search tweets via DuckDuckGo + FxTwitter"
    )
    parser.add_argument("_cmd", help=argparse.SUPPRESS)  # consume "search"
    parser.add_argument("query", nargs="+", help="Search query")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--time", "-t", choices=["d", "w", "m", "y"], help="Time filter")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--fast", action="store_true", help="Skip enrichment")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    _handle_search(args)


def _main_url():
    parser = argparse.ArgumentParser(
        description="Scrape X/Twitter tweets, profiles, threads, or YouTube transcripts"
    )
    parser.add_argument("url", nargs="?", help="Tweet URL, profile URL, or YouTube URL")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--thread", action="store_true", help="Fetch full thread")
    parser.add_argument("--cookies", help="Path to cookies file for yt-dlp")
    parser.add_argument("--whisper-model", default="base", help="Whisper model (base/medium/large)")
    parser.add_argument("--force-whisper", action="store_true", help="Skip auto-captions, use whisper")
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
        print(f"---")
        if len(result.transcript) > 2000:
            print(result.transcript[:2000])
            print(f"\n... [{len(result.transcript) - 2000} more chars]")
            print(f"Full transcript: {result.transcript_path}")
        else:
            print(result.transcript)


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


def _handle_tweet(args):
    scraper = XScraper(ytdlp_cookies=args.cookies)
    try:
        tweet = scraper.get_tweet(args.url)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = _tweet_to_dict(tweet)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"@{tweet.author_handle} ({tweet.author})")
        print(f"---")
        print(tweet.text)
        if tweet.article_title:
            print(f"\n[Article: {tweet.article_title}]")
        if tweet.media_urls:
            print(f"\nMedia: {len(tweet.media_urls)} file(s)")
            for u in tweet.media_urls:
                print(f"  {u}")
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
        print(f"===")
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
        print(f"---")
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
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not tweets:
        print("No tweets found.", file=sys.stderr)
        sys.exit(0)

    if args.json:
        out = []
        for t in tweets:
            out.append({
                "id": t.id,
                "text": t.text,
                "author": t.author,
                "author_handle": t.author_handle,
                "likes": t.likes,
                "retweets": t.retweets,
                "replies": t.replies,
                "views": t.views,
                "media_urls": t.media_urls,
                "source_method": t.source_method,
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"Found {len(tweets)} tweets for: {query}\n")
        for i, t in enumerate(tweets, 1):
            print(f"[{i}] @{t.author_handle} ({t.author})")
            text_preview = t.text[:200] + ("..." if len(t.text) > 200 else "")
            print(f"    {text_preview}")
            if t.likes or t.views:
                print(f"    {t.likes} likes | {t.retweets} RT | {t.views:,} views")
            print(f"    https://x.com/{t.author_handle}/status/{t.id}")
            print(f"    (via {t.source_method})")
            print()


def _main_basescan():
    from .blockchain import scrape_basescan_address, PlaywrightNotAvailable

    parser = argparse.ArgumentParser(
        prog="scraperx basescan",
        description="Scrape Basescan address info via headless browser"
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
    from .blockchain import scrape_dexscreener_token, PlaywrightNotAvailable

    parser = argparse.ArgumentParser(
        prog="scraperx dexscreener",
        description="Scrape DexScreener token info on Base chain via headless browser"
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


if __name__ == "__main__":
    main()
