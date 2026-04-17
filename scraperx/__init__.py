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
from scraperx.avatar_matcher import AvatarMatcher, VerifiedAvatarRegistry
from scraperx.vimeo_scraper import VimeoScraper, VimeoResult, parse_vimeo_url
from scraperx.video_discovery import VideoRef, discover_videos, fetch_any_video_transcript

__version__ = "1.3.0"

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
    "AvatarMatcher", "VerifiedAvatarRegistry",
    "VimeoScraper", "VimeoResult", "parse_vimeo_url",
    "VideoRef", "discover_videos", "fetch_any_video_transcript",
]
