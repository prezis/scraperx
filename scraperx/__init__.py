"""ScraperX — multi-method X/Twitter scraper + YouTube transcriber + blockchain explorer."""
from .scraper import XScraper, Tweet, TweetNotFoundError
from .profile import XProfile, get_profile
from .thread import Thread, get_thread
from .search import search_tweets
from .token_extractor import TokenMention, extract_token_mentions
from .social_db import SocialDB
from .blockchain import (
    scrape_basescan_address,
    scrape_dexscreener_token,
    BasescanAddress,
    DexScreenerToken,
    PlaywrightNotAvailable,
)

__version__ = "1.2.0"

__all__ = [
    "XScraper", "Tweet", "TweetNotFoundError",
    "XProfile", "get_profile",
    "Thread", "get_thread",
    "search_tweets",
    "TokenMention", "extract_token_mentions",
    "SocialDB",
    "scrape_basescan_address", "scrape_dexscreener_token",
    "BasescanAddress", "DexScreenerToken",
    "PlaywrightNotAvailable",
]
