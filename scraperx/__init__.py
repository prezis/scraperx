"""ScraperX — multi-method X/Twitter scraper + YouTube transcriber + blockchain explorer."""

from scraperx.authenticity import ThreadAuthenticity, check_thread_authenticity
from scraperx.avatar_matcher import AvatarMatcher, VerifiedAvatarRegistry
from scraperx.github_analyzer import (
    GithubAnalyzer,
    GithubReport,
    InvalidRepoUrlError,
)
from scraperx.github_analyzer import (
    analyze_repo as analyze_github_repo,
)
from scraperx.github_analyzer import (
    parse_repo_url as parse_github_repo_url,
)
from scraperx.video_discovery import VideoRef, discover_videos, fetch_any_video_transcript
from scraperx.vimeo_scraper import VimeoResult, VimeoScraper, parse_vimeo_url

from .fetch import FetchResult, smart_fetch
from .gh_discover import RepoCandidate, discover_repos
from .profile import XProfile, get_profile
from .scraper import Tweet, TweetNotFoundError, XScraper
from .screenshot import (
    PlaywrightNotAvailable,
    screenshot_url,
)
from .search import search_tweets
from .social_db import SocialDB
from .thread import Thread, get_thread
from .token_extractor import TokenMention, extract_token_mentions

__version__ = "1.6.0"

__all__ = [
    "AvatarMatcher",
    "FetchResult",
    "GithubAnalyzer",
    "GithubReport",
    "InvalidRepoUrlError",
    "PlaywrightNotAvailable",
    "RepoCandidate",
    "SocialDB",
    "Thread",
    "ThreadAuthenticity",
    "TokenMention",
    "Tweet",
    "TweetNotFoundError",
    "VerifiedAvatarRegistry",
    "VideoRef",
    "VimeoResult",
    "VimeoScraper",
    "XProfile",
    "XScraper",
    "analyze_github_repo",
    "check_thread_authenticity",
    "discover_repos",
    "discover_videos",
    "extract_token_mentions",
    "fetch_any_video_transcript",
    "get_profile",
    "get_thread",
    "parse_github_repo_url",
    "parse_vimeo_url",
    "screenshot_url",
    "search_tweets",
    "smart_fetch",
]
