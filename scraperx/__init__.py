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
from scraperx.silent_video_ocr import (
    FrameOCR,
    SilentVideoNotAvailable,
    SilentVideoResult,
    has_audio_stream,
    transcribe_silent_video,
)

from .cookie_banner import (
    BannerSelector,
    DEFAULT_SELECTORS,
    DismissResult,
    dismiss_cookie_banner,
    dismiss_cookie_banner_async,
)
from .explorer_label import (
    ExplorerNotSupported,
    LabelResult,
    detect_explorer,
    extract_label,
    page_title,
    parse_title,
)
from .fetch import FetchResult, smart_fetch
from .scrapling_stealth import (
    DEFAULT_STEALTH_TIMEOUT,
    ScraplingNotAvailable,
    fetch_stealth,
)
from .gh_discover import RepoCandidate, discover_repos
from .js_state import (
    ChartSeries,
    ChartSnapshot,
    SpaState,
    extract_chart_data,
    extract_chart_data_async,
    extract_spa_state,
    extract_spa_state_async,
)
from .pdf_text_parser import ColumnRow, ParseResult, parse_pdf_with_columns
from .quota_session import QuotaSession, QuotaSessionConfig
from .reverse_image import (
    DEFAULT_ENGINES,
    ENGINES,
    EngineHit,
    build_search_url,
    reverse_image_search,
)
from .tv_symbol_resolver import SymbolResolution, resolve_symbol
from .profile import XProfile, get_profile
from .scraper import Tweet, TweetNotFoundError, XScraper
from .wayback import (
    CdxEntry,
    MultiGenerationResult,
    WaybackError,
    query_cdx,
    wayback_multi_generation_probe,
)
from .screenshot import (
    PlaywrightNotAvailable,
    screenshot_url,
)
from .search import search_tweets
from .social_db import SocialDB
from .thread import Thread, get_thread
from .token_extractor import TokenMention, extract_token_mentions

__version__ = "1.7.0"

__all__ = [
    "AvatarMatcher",
    "BannerSelector",
    "CdxEntry",
    "ChartSeries",
    "ChartSnapshot",
    "ColumnRow",
    "DEFAULT_ENGINES",
    "DEFAULT_SELECTORS",
    "DismissResult",
    "ENGINES",
    "EngineHit",
    "ExplorerNotSupported",
    "FetchResult",
    "LabelResult",
    "GithubAnalyzer",
    "GithubReport",
    "InvalidRepoUrlError",
    "MultiGenerationResult",
    "ParseResult",
    "PlaywrightNotAvailable",
    "QuotaSession",
    "QuotaSessionConfig",
    "RepoCandidate",
    "SocialDB",
    "SpaState",
    "SymbolResolution",
    "Thread",
    "ThreadAuthenticity",
    "TokenMention",
    "Tweet",
    "TweetNotFoundError",
    "VerifiedAvatarRegistry",
    "VideoRef",
    "VimeoResult",
    "VimeoScraper",
    "DEFAULT_STEALTH_TIMEOUT",
    "ScraplingNotAvailable",
    "WaybackError",
    "XProfile",
    "XScraper",
    "analyze_github_repo",
    "build_search_url",
    "check_thread_authenticity",
    "detect_explorer",
    "discover_repos",
    "discover_videos",
    "dismiss_cookie_banner",
    "dismiss_cookie_banner_async",
    "extract_chart_data",
    "extract_chart_data_async",
    "extract_label",
    "extract_spa_state",
    "extract_spa_state_async",
    "extract_token_mentions",
    "fetch_any_video_transcript",
    "fetch_stealth",
    "get_profile",
    "get_thread",
    "page_title",
    "parse_github_repo_url",
    "parse_pdf_with_columns",
    "parse_title",
    "parse_vimeo_url",
    "query_cdx",
    "resolve_symbol",
    "reverse_image_search",
    "screenshot_url",
    "search_tweets",
    "smart_fetch",
    "wayback_multi_generation_probe",
]
