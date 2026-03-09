"""SQLite storage for scraped social data (tweets, profiles, token mentions)."""

import hashlib
import json
import os
import sqlite3
import time
from typing import Optional

from scraperx.scraper import Tweet

try:
    from scraperx.profile import XProfile
except ImportError:
    XProfile = None

DEFAULT_DB_PATH = os.path.join(
    os.path.expanduser('~'),
    '.scraperx', 'social.db',
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id TEXT PRIMARY KEY,
    author_handle TEXT NOT NULL,
    author_name TEXT,
    text TEXT NOT NULL,
    likes INTEGER DEFAULT 0,
    retweets INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    media_urls TEXT,
    article_title TEXT,
    source_method TEXT,
    scraped_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    handle TEXT PRIMARY KEY,
    name TEXT,
    bio TEXT,
    followers INTEGER DEFAULT 0,
    following INTEGER DEFAULT 0,
    tweets_count INTEGER DEFAULT 0,
    likes_count INTEGER DEFAULT 0,
    joined TEXT,
    location TEXT,
    website TEXT,
    verified INTEGER DEFAULT 0,
    scraped_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id TEXT NOT NULL REFERENCES tweets(tweet_id),
    token_symbol TEXT NOT NULL,
    token_address TEXT,
    mention_type TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_token ON token_mentions(token_symbol);
CREATE INDEX IF NOT EXISTS idx_mentions_tweet ON token_mentions(tweet_id);

CREATE TABLE IF NOT EXISTS search_cache (
    query_hash TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    result_tweet_ids TEXT NOT NULL,
    result_count INTEGER,
    searched_at REAL NOT NULL,
    ttl_seconds INTEGER DEFAULT 3600
);
"""


class SocialDB:
    """Thin wrapper around SQLite for social scraping data."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Tweets
    # ------------------------------------------------------------------

    def save_tweet(self, tweet: Tweet) -> None:
        """INSERT OR REPLACE a Tweet dataclass."""
        self._conn.execute(
            """INSERT OR REPLACE INTO tweets
               (tweet_id, author_handle, author_name, text, likes, retweets,
                replies, views, media_urls, article_title, source_method, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tweet.id,
                tweet.author_handle,
                tweet.author,
                tweet.text,
                tweet.likes,
                tweet.retweets,
                tweet.replies,
                tweet.views,
                json.dumps(tweet.media_urls) if tweet.media_urls else None,
                tweet.article_title,
                tweet.source_method,
                time.time(),
            ),
        )
        self._conn.commit()

    def get_tweet(self, tweet_id: str) -> Optional[Tweet]:
        """Return a Tweet or None."""
        row = self._conn.execute(
            "SELECT * FROM tweets WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        if row is None:
            return None
        return Tweet(
            id=row["tweet_id"],
            text=row["text"],
            author=row["author_name"] or "",
            author_handle=row["author_handle"],
            likes=row["likes"],
            retweets=row["retweets"],
            replies=row["replies"],
            views=row["views"],
            media_urls=json.loads(row["media_urls"]) if row["media_urls"] else [],
            article_title=row["article_title"],
            source_method=row["source_method"] or "",
        )

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def save_profile(self, profile) -> None:
        """INSERT OR REPLACE an XProfile dataclass."""
        self._conn.execute(
            """INSERT OR REPLACE INTO profiles
               (handle, name, bio, followers, following, tweets_count,
                likes_count, joined, location, website, verified, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.handle,
                profile.name,
                profile.bio,
                profile.followers,
                profile.following,
                profile.tweets_count,
                profile.likes_count,
                profile.joined,
                profile.location,
                profile.website,
                int(profile.verified),
                time.time(),
            ),
        )
        self._conn.commit()

    def get_profile(self, handle: str, max_age_days: int = 7):
        """Return an XProfile or None if not found / stale."""
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE handle = ?", (handle,)
        ).fetchone()
        if row is None:
            return None
        age_seconds = time.time() - row["scraped_at"]
        if age_seconds > max_age_days * 86400:
            return None
        if XProfile is None:
            return None
        return XProfile(
            handle=row["handle"],
            name=row["name"] or "",
            bio=row["bio"] or "",
            followers=row["followers"],
            following=row["following"],
            tweets_count=row["tweets_count"],
            likes_count=row["likes_count"],
            joined=row["joined"] or "",
            location=row["location"] or "",
            website=row["website"],
            verified=bool(row["verified"]),
        )

    # ------------------------------------------------------------------
    # Token mentions
    # ------------------------------------------------------------------

    def save_token_mention(
        self,
        tweet_id: str,
        token_symbol: str,
        mention_type: str,
        token_address: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO token_mentions
               (tweet_id, token_symbol, token_address, mention_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tweet_id, token_symbol, token_address, mention_type, time.time()),
        )
        self._conn.commit()

    def get_token_buzz(self, token_symbol: str, hours: int = 24) -> dict:
        """Aggregate buzz stats for a token over the last *hours*."""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            """SELECT tm.tweet_id, t.author_handle, t.likes, t.retweets,
                      t.replies, t.views, t.text
               FROM token_mentions tm
               LEFT JOIN tweets t ON tm.tweet_id = t.tweet_id
               WHERE tm.token_symbol = ? AND tm.created_at >= ?""",
            (token_symbol, cutoff),
        ).fetchall()

        tweets = []
        authors = set()
        total_engagement = 0
        for r in rows:
            likes = r["likes"] or 0
            retweets = r["retweets"] or 0
            replies = r["replies"] or 0
            views = r["views"] or 0
            total_engagement += likes + retweets + replies + views
            if r["author_handle"]:
                authors.add(r["author_handle"])
            tweets.append(
                {
                    "tweet_id": r["tweet_id"],
                    "author": r["author_handle"],
                    "text": r["text"],
                    "engagement": likes + retweets + replies + views,
                }
            )

        return {
            "mention_count": len(rows),
            "unique_authors": len(authors),
            "total_engagement": total_engagement,
            "tweets": tweets,
        }

    # ------------------------------------------------------------------
    # Search cache
    # ------------------------------------------------------------------

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.sha256(query.strip().lower().encode()).hexdigest()

    def save_search_cache(
        self, query: str, tweet_ids: list[str], ttl: int = 3600
    ) -> None:
        qh = self._query_hash(query)
        self._conn.execute(
            """INSERT OR REPLACE INTO search_cache
               (query_hash, query_text, result_tweet_ids, result_count,
                searched_at, ttl_seconds)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (qh, query, json.dumps(tweet_ids), len(tweet_ids), time.time(), ttl),
        )
        self._conn.commit()

    def get_search_cache(self, query: str) -> Optional[list[str]]:
        """Return cached tweet_ids if fresh, else None."""
        qh = self._query_hash(query)
        row = self._conn.execute(
            "SELECT * FROM search_cache WHERE query_hash = ?", (qh,)
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["searched_at"] > row["ttl_seconds"]:
            return None
        return json.loads(row["result_tweet_ids"])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._conn.close()
