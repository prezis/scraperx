"""ScraperX — multi-method X/Twitter scraper + YouTube transcriber + blockchain explorer."""
from .scraper import XScraper, Tweet, TweetNotFoundError
from .profile import XProfile, get_profile
from .thread import Thread, get_thread
from .search import search_tweets
from .token_extractor import TokenMention, extract_token_mentions
from .social_db import SocialDB
from .screenshot import (
    screenshot_url,
    PlaywrightNotAvailable,
)
from scraperx.authenticity import ThreadAuthenticity, check_thread_authenticity

__version__ = "1.2.0"

__all__ = [
    "XScraper", "Tweet", "TweetNotFoundError",
    "XProfile", "get_profile",
    "Thread", "get_thread",
    "search_tweets",
    "TokenMention", "extract_token_mentions",
    "SocialDB",
    "screenshot_url",
    "PlaywrightNotAvailable",
    "ThreadAuthenticity", "check_thread_authenticity",
]
