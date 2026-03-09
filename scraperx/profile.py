"""
X/Twitter Profile Scraper - fetch user profiles via FxTwitter API.

Usage:
    from scraperx import get_profile
    profile = get_profile("elonmusk")
    print(profile.name, profile.followers, profile.bio)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from scraperx.scraper import _http_get_json

logger = logging.getLogger(__name__)

PROFILE_URL_RE = re.compile(
    r"(?:https?://)?(?:twitter|x)\.com/(?P<handle>[A-Za-z0-9_]+)/?$"
)


@dataclass
class XProfile:
    """Parsed X/Twitter profile data."""
    handle: str
    name: str = ""
    bio: str = ""
    followers: int = 0
    following: int = 0
    tweets_count: int = 0
    likes_count: int = 0
    joined: str = ""
    location: str = ""
    avatar_url: str = ""
    banner_url: str = ""
    website: Optional[str] = None
    verified: bool = False
    source_method: str = "fxtwitter"
    raw: dict = field(default_factory=dict, repr=False)


def parse_profile_url(url: str) -> str:
    """Extract handle from an X/Twitter profile URL.

    Raises ValueError if the URL doesn't match a profile pattern.
    """
    m = PROFILE_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a valid profile URL: {url}")
    return m.group("handle")


def get_profile(handle: str, timeout: int = 15) -> XProfile:
    """Fetch a user profile via FxTwitter API.

    Args:
        handle: Twitter/X handle (with or without leading @).
        timeout: HTTP request timeout in seconds.

    Returns:
        XProfile with user data.

    Raises:
        ValueError: If the API returns a non-200 code or handle is invalid.
    """
    handle = handle.lstrip("@")
    if not re.fullmatch(r'[A-Za-z0-9_]{1,50}', handle):
        raise ValueError(f"Invalid Twitter handle: {handle!r}")
    url = f"https://api.fxtwitter.com/{handle}"
    data = _http_get_json(url, timeout)

    if data.get("code") != 200:
        raise ValueError(
            f"FxTwitter returned code {data.get('code')}: {data.get('message')}"
        )

    u = data["user"]
    verification = u.get("verification", {})

    return XProfile(
        handle=u.get("screen_name", handle),
        name=u.get("name", ""),
        bio=u.get("description", ""),
        followers=u.get("followers", 0),
        following=u.get("following", 0),
        tweets_count=u.get("tweets", 0),
        likes_count=u.get("likes", 0),
        joined=u.get("joined", ""),
        location=u.get("location", ""),
        avatar_url=u.get("avatar_url", ""),
        banner_url=u.get("banner_url", ""),
        website=u.get("website"),
        verified=verification.get("verified", False) if isinstance(verification, dict) else False,
        source_method="fxtwitter",
        raw=data,
    )
