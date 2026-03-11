"""ScraperX — multi-method X/Twitter scraper + YouTube transcriber."""
from .scraper import XScraper, Tweet
from .profile import XProfile, get_profile
from .thread import Thread, get_thread
from .search import search_tweets
from .token_extractor import TokenMention, extract_token_mentions
from .social_db import SocialDB

__version__ = "1.1.0"

__all__ = [
    "XScraper", "Tweet",
    "XProfile", "get_profile",
    "Thread", "get_thread",
    "search_tweets",
    "TokenMention", "extract_token_mentions",
    "SocialDB",
]
