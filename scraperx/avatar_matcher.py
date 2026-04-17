"""
AvatarMatcher + VerifiedAvatarRegistry — pHash-based avatar impersonation detection.

AvatarMatcher fetches avatar images (SSRF-safe: host allowlist, 2MB cap,
image/* content-type check), computes a perceptual hash (pHash 8x8 via
the `imagehash` library), and caches results in SQLite with a 30-day TTL.
When `imagehash` is not installed, gracefully degrades to content SHA256
exact-match comparison.

VerifiedAvatarRegistry maintains a rolling window of the last N known-good
avatar hashes per verified handle, tolerating legitimate avatar changes.
Its `check_impersonation()` returns `(is_match, best_hamming, matched_handle)`;
a cross-handle match (low distance against a DIFFERENT handle) is a strong
impersonation signal.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# --- Optional imagehash / PIL guard ---
IMAGEHASH_AVAILABLE = False
try:
    import io
    from PIL import Image
    import imagehash  # noqa: F401
    IMAGEHASH_AVAILABLE = True
except ImportError:
    logger.info("imagehash/PIL not installed — AvatarMatcher falls back to SHA256 + URL compare")

# --- Constants ---
DEFAULT_DB_PATH = os.path.expanduser("~/.scraperx/social.db")
AVATAR_HOST_ALLOWLIST = {"pbs.twimg.com"}
MAX_IMAGE_SIZE = 2 * 1024 * 1024  # 2MB cap
HASH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
MAX_HASHES_PER_HANDLE = 5
DEFAULT_HAMMING_THRESHOLD = 10  # ~same image re-uploaded on 64-bit pHash

# --- Schema ---
AVATAR_HASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS avatar_hash (
    url TEXT PRIMARY KEY,
    phash TEXT,
    content_sha256 TEXT,
    fetched_at INTEGER NOT NULL
)
"""

VERIFIED_AVATARS_SCHEMA = """
CREATE TABLE IF NOT EXISTS verified_avatars (
    handle TEXT NOT NULL,
    phash TEXT NOT NULL,
    url TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (handle, recorded_at)
)
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(AVATAR_HASH_SCHEMA)
    conn.execute(VERIFIED_AVATARS_SCHEMA)
    conn.commit()


def _url_is_allowed(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in AVATAR_HOST_ALLOWLIST
    except Exception:
        return False


def _fetch_image_bytes(url: str, timeout: int = 10) -> Optional[bytes]:
    """SSRF-safe fetch: allowlisted host, 2MB cap, image/* content-type check."""
    if not _url_is_allowed(url):
        logger.debug("avatar fetch blocked: host not in allowlist: %s", url)
        return None
    try:
        req = Request(url, headers={"User-Agent": "ScraperX-AvatarMatcher/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            if not ct.startswith("image/"):
                logger.debug("avatar fetch blocked: non-image content-type %s", ct)
                return None
            data = resp.read(MAX_IMAGE_SIZE + 1)
            if len(data) > MAX_IMAGE_SIZE:
                logger.debug("avatar fetch blocked: oversize (%d bytes)", len(data))
                return None
            return data
    except (URLError, HTTPError, OSError) as e:
        logger.debug("avatar fetch failed for %s: %s", url, e)
        return None


def _compute_phash(image_bytes: bytes) -> Optional[str]:
    """Returns 16-char hex string (8x8 pHash = 64 bits) or None if failed."""
    if not IMAGEHASH_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Use explicit pHash; 8x8 hash_size yields 64-bit hash
        h = imagehash.phash(img, hash_size=8)
        return str(h)  # 16-char hex
    except Exception as e:
        logger.debug("phash computation failed: %s", e)
        return None


def _hamming_hex(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two equal-length hex strings. Returns 64 on error."""
    try:
        a = int(hex_a, 16)
        b = int(hex_b, 16)
        return bin(a ^ b).count("1")
    except (ValueError, TypeError):
        return 64


class AvatarMatcher:
    """Perceptual-hash based avatar comparison with SQLite caching.

    Usage:
        matcher = AvatarMatcher()
        phash = matcher.fetch_and_hash(avatar_url)
        is_same = matcher.is_same(url_a, url_b)  # True if Hamming <= 10

    When imagehash is not installed, degrades gracefully:
      - phash column is populated with content SHA256 (hex)
      - compare() returns 0 on exact byte match, 64 otherwise (no gradient)
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        _init_schema(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def fetch_and_hash(self, url: str) -> Optional[str]:
        """Returns cached or freshly-computed phash (or content SHA256 fallback)."""
        if not url:
            return None

        # Cache lookup
        cur = self._conn.execute(
            "SELECT phash, content_sha256, fetched_at FROM avatar_hash WHERE url = ?",
            (url,),
        )
        row = cur.fetchone()
        now = int(time.time())
        if row and (now - row["fetched_at"]) < HASH_TTL_SECONDS:
            return row["phash"]

        # Fetch fresh
        data = _fetch_image_bytes(url)
        if not data:
            return None

        content_sha = hashlib.sha256(data).hexdigest()
        phash_hex = _compute_phash(data) if IMAGEHASH_AVAILABLE else content_sha

        self._conn.execute(
            """
            INSERT OR REPLACE INTO avatar_hash (url, phash, content_sha256, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (url, phash_hex, content_sha, now),
        )
        self._conn.commit()
        return phash_hex

    def compare(self, url_a: str, url_b: str) -> int:
        """Returns Hamming distance (0-64). 64 = unable / unrelated."""
        ha = self.fetch_and_hash(url_a)
        hb = self.fetch_and_hash(url_b)
        if not ha or not hb:
            return 64
        if not IMAGEHASH_AVAILABLE:
            # In fallback mode, both values are content SHA256 — exact match only
            return 0 if ha == hb else 64
        return _hamming_hex(ha, hb)

    def is_same(self, url_a: str, url_b: str, threshold: int = DEFAULT_HAMMING_THRESHOLD) -> bool:
        return self.compare(url_a, url_b) <= threshold


class VerifiedAvatarRegistry:
    """Rolling window of last N avatar hashes per verified handle.

    Use pattern:
      - When a known-legitimate avatar for @elon is observed (e.g., reply from the
        actual verified account), call registry.record_avatar("elon", avatar_url, matcher).
      - When checking a suspect reply claiming to be @elon, call
        registry.check_impersonation("elon", suspect_avatar_url, matcher).

    Rolling window (MAX_HASHES_PER_HANDLE=5) tolerates avatar changes —
    compares against any of the last 5 known-good hashes.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        _init_schema(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record_avatar(self, handle: str, url: str, matcher: "AvatarMatcher") -> None:
        """Hash the avatar and store under handle. Trims to rolling window."""
        if not handle or not url:
            return
        phash = matcher.fetch_and_hash(url)
        if not phash:
            return
        handle_lower = handle.lstrip("@").lower()
        now = int(time.time())

        # Avoid duplicate consecutive recordings of same hash
        cur = self._conn.execute(
            "SELECT phash FROM verified_avatars WHERE handle = ? ORDER BY recorded_at DESC LIMIT 1",
            (handle_lower,),
        )
        last = cur.fetchone()
        if last and last["phash"] == phash:
            return

        self._conn.execute(
            "INSERT OR REPLACE INTO verified_avatars (handle, phash, url, recorded_at) VALUES (?, ?, ?, ?)",
            (handle_lower, phash, url, now),
        )
        # Trim rolling window
        self._conn.execute(
            """
            DELETE FROM verified_avatars
            WHERE handle = ?
              AND recorded_at NOT IN (
                  SELECT recorded_at FROM verified_avatars
                  WHERE handle = ?
                  ORDER BY recorded_at DESC
                  LIMIT ?
              )
            """,
            (handle_lower, handle_lower, MAX_HASHES_PER_HANDLE),
        )
        self._conn.commit()

    def check_impersonation(
        self,
        claimed_handle: str,
        avatar_url: str,
        matcher: "AvatarMatcher",
        threshold: int = DEFAULT_HAMMING_THRESHOLD,
    ) -> tuple[bool, int, Optional[str]]:
        """Returns (is_match, best_hamming_distance, matched_handle).

        is_match=True means the avatar matches a known-good hash for THAT SAME handle.
        is_match=False + low distance means the avatar matches an OTHER verified
        handle — STRONG impersonation signal (matched_handle is not None and
        differs from claimed_handle).
        """
        suspect_phash = matcher.fetch_and_hash(avatar_url)
        if not suspect_phash:
            return False, 64, None

        handle_lower = claimed_handle.lstrip("@").lower()

        # First: check against claimed handle's own history
        cur = self._conn.execute(
            "SELECT phash, handle FROM verified_avatars WHERE handle = ? ORDER BY recorded_at DESC",
            (handle_lower,),
        )
        best_dist = 64
        for row in cur.fetchall():
            if IMAGEHASH_AVAILABLE:
                d = _hamming_hex(suspect_phash, row["phash"])
            else:
                d = 0 if suspect_phash == row["phash"] else 64
            if d < best_dist:
                best_dist = d
            if d == 0:
                return True, 0, handle_lower

        if best_dist <= threshold:
            return True, best_dist, handle_lower

        # Second: check against OTHER handles (cross-impersonation)
        cur = self._conn.execute(
            "SELECT phash, handle FROM verified_avatars WHERE handle != ?",
            (handle_lower,),
        )
        best_cross_dist = 64
        matched_other: Optional[str] = None
        for row in cur.fetchall():
            if IMAGEHASH_AVAILABLE:
                d = _hamming_hex(suspect_phash, row["phash"])
            else:
                d = 0 if suspect_phash == row["phash"] else 64
            if d < best_cross_dist:
                best_cross_dist = d
                matched_other = row["handle"]

        if best_cross_dist <= threshold and matched_other:
            # Cross-match → impersonation signal. Return the OTHER handle.
            return False, best_cross_dist, matched_other

        return False, min(best_dist, best_cross_dist), None
