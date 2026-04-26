"""SQLite storage for scraped social data (tweets, profiles, token mentions)."""

import hashlib
import json
import os
import sqlite3
import time

from scraperx.scraper import Tweet

try:
    from scraperx.profile import XProfile
except ImportError:
    XProfile = None

DEFAULT_DB_PATH = os.path.join(
    os.path.expanduser("~"),
    ".scraperx",
    "social.db",
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

-- Defense in depth: AvatarMatcher maintains its own connection, but any
-- consumer opening the shared DB sees these tables too.
CREATE TABLE IF NOT EXISTS avatar_hash (
    url TEXT PRIMARY KEY,
    phash TEXT,
    content_sha256 TEXT,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS verified_avatars (
    handle TEXT NOT NULL,
    phash TEXT NOT NULL,
    url TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (handle, recorded_at)
);

-- github_analyzer: cache for per-repo GitHub API payloads. One row per
-- (full_name, kind) pair so a single table covers repo / contributors /
-- commits / releases / readme / workflows with per-kind TTL.
CREATE TABLE IF NOT EXISTS github_repo_cache (
    full_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 86400,
    PRIMARY KEY (full_name, kind)
);
CREATE INDEX IF NOT EXISTS idx_github_repo_cache_full ON github_repo_cache(full_name);

-- github_analyzer: cache for /forks listings (serialized JSON). One row per
-- parent repo; list of forks goes in payload.
CREATE TABLE IF NOT EXISTS github_fork_cache (
    parent_full_name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 21600
);

-- github_analyzer: cache for external-platform mention searches. Keyed on
-- (source, query_hash). Source ∈ {hn,reddit,stackoverflow,devto,arxiv,pwc,
-- semantic_web,x,youtube}. Hash normalises the actual query string.
CREATE TABLE IF NOT EXISTS github_mentions_cache (
    source TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    query_text TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 14400,
    PRIMARY KEY (source, query_hash)
);
CREATE INDEX IF NOT EXISTS idx_github_mentions_source ON github_mentions_cache(source);

-- fetch.smart_fetch: per-URL cache for the universal Jina/urllib/Playwright
-- fetch cascade. Keyed on sha256(url) so callers don't have to normalize.
-- One row per fetched URL; mode_used records which cascade leg succeeded.
CREATE TABLE IF NOT EXISTS web_fetch_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    content TEXT NOT NULL,
    mode_used TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 86400,
    http_status INTEGER,
    elapsed_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_web_fetch_url ON web_fetch_cache(url);
CREATE INDEX IF NOT EXISTS idx_web_fetch_fetched_at ON web_fetch_cache(fetched_at);

-- tv_symbol_resolver: per-(ticker,exchange) probe cache. Includes a NEGATIVE
-- TTL — known-empty symbols (e.g. CBOE put/call ratios that simply have no
-- historical bars on tvDatafeed) get stored with status=empty_no_data so the
-- next call doesn't re-probe for hours.
-- Status legal values: resolved | empty_no_data | not_found
CREATE TABLE IF NOT EXISTS tv_symbol_cache (
    cache_key TEXT PRIMARY KEY,        -- f"{ticker.upper()}:{exchange.upper()}"
    ticker TEXT NOT NULL,
    exchange TEXT NOT NULL,
    status TEXT NOT NULL,
    last_checked REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 86400,
    error_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_tv_symbol_ticker ON tv_symbol_cache(ticker);
CREATE INDEX IF NOT EXISTS idx_tv_symbol_status ON tv_symbol_cache(status);
"""

# TTL defaults (seconds) — override per-call via save_repo_cache(..., ttl=N).
# Intentionally module-level constants so callers can import & compare.
GITHUB_TTL_REPO = 86400          # 24h — metadata, topics, license, stars
GITHUB_TTL_CONTRIBUTORS = 86400  # 24h — rarely changes
GITHUB_TTL_RELEASES = 86400      # 24h
GITHUB_TTL_README = 86400        # 24h
GITHUB_TTL_WORKFLOWS = 86400     # 24h — CI config
GITHUB_TTL_COMMITS = 21600       # 6h  — commits and issues shift faster
GITHUB_TTL_ISSUES = 21600        # 6h
GITHUB_TTL_FORKS = 21600         # 6h
GITHUB_TTL_MENTIONS = 14400      # 4h  — external platforms, most volatile
GITHUB_TTL_ADVISORIES = 21600    # 6h

# fetch.smart_fetch: 24h is a sane default for "did this URL change today?"
# research workloads; callers can override per-call.
WEB_FETCH_TTL = 86400            # 24h — generic web pages

# Map `kind` → default TTL used by save_repo_cache when ttl is None
_GITHUB_KIND_TTL = {
    "repo": GITHUB_TTL_REPO,
    "contributors": GITHUB_TTL_CONTRIBUTORS,
    "releases": GITHUB_TTL_RELEASES,
    "readme": GITHUB_TTL_README,
    "workflows": GITHUB_TTL_WORKFLOWS,
    "commits": GITHUB_TTL_COMMITS,
    "issues": GITHUB_TTL_ISSUES,
    "advisories": GITHUB_TTL_ADVISORIES,
}


class SocialDB:
    """Thin wrapper around SQLite for social scraping data."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        # Hardened WAL PRAGMA stack (1.4.3+) — closes the unbounded-WAL vector
        # for long-running daemons. See _sqlite_pragmas.py for rationale.
        from scraperx._sqlite_pragmas import apply_pragmas
        apply_pragmas(self._conn)
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

    def get_tweet(self, tweet_id: str) -> Tweet | None:
        """Return a Tweet or None."""
        row = self._conn.execute("SELECT * FROM tweets WHERE tweet_id = ?", (tweet_id,)).fetchone()
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
        row = self._conn.execute("SELECT * FROM profiles WHERE handle = ?", (handle,)).fetchone()
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
        token_address: str | None = None,
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

    def save_search_cache(self, query: str, tweet_ids: list[str], ttl: int = 3600) -> None:
        qh = self._query_hash(query)
        self._conn.execute(
            """INSERT OR REPLACE INTO search_cache
               (query_hash, query_text, result_tweet_ids, result_count,
                searched_at, ttl_seconds)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (qh, query, json.dumps(tweet_ids), len(tweet_ids), time.time(), ttl),
        )
        self._conn.commit()

    def get_search_cache(self, query: str) -> list[str] | None:
        """Return cached tweet_ids if fresh, else None."""
        qh = self._query_hash(query)
        row = self._conn.execute("SELECT * FROM search_cache WHERE query_hash = ?", (qh,)).fetchone()
        if row is None:
            return None
        if time.time() - row["searched_at"] > row["ttl_seconds"]:
            return None
        return json.loads(row["result_tweet_ids"])

    # ------------------------------------------------------------------
    # GitHub analyzer cache
    # ------------------------------------------------------------------
    # Shape contract for all github_* methods:
    #   - save_*: always (INSERT OR REPLACE) + commit
    #   - get_*:  returns None on miss OR when row age exceeds ttl_seconds
    #             (stored on the row, so TTL travels with the cached value)
    #   - payload is JSON-serializable (list | dict | primitives)

    def save_repo_cache(
        self,
        full_name: str,
        kind: str,
        payload,
        ttl: int | None = None,
    ) -> None:
        """Cache a per-repo payload keyed on (full_name, kind).

        kind ∈ {"repo","contributors","commits","releases","readme",
                 "workflows","issues","advisories"}.

        If ttl is None, the per-kind default in _GITHUB_KIND_TTL applies
        (falls back to 24h for unknown kinds).
        """
        effective_ttl = ttl if ttl is not None else _GITHUB_KIND_TTL.get(kind, GITHUB_TTL_REPO)
        self._conn.execute(
            """INSERT OR REPLACE INTO github_repo_cache
               (full_name, kind, payload, fetched_at, ttl_seconds)
               VALUES (?, ?, ?, ?, ?)""",
            (full_name, kind, json.dumps(payload), time.time(), effective_ttl),
        )
        self._conn.commit()

    def get_repo_cache(self, full_name: str, kind: str):
        """Return the cached payload (JSON-decoded) or None if missing/stale."""
        row = self._conn.execute(
            "SELECT payload, fetched_at, ttl_seconds FROM github_repo_cache "
            "WHERE full_name = ? AND kind = ?",
            (full_name, kind),
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["fetched_at"] > row["ttl_seconds"]:
            return None
        return json.loads(row["payload"])

    def save_fork_cache(
        self,
        parent_full_name: str,
        payload,
        ttl: int | None = None,
    ) -> None:
        """Cache the fork list for a parent repo."""
        self._conn.execute(
            """INSERT OR REPLACE INTO github_fork_cache
               (parent_full_name, payload, fetched_at, ttl_seconds)
               VALUES (?, ?, ?, ?)""",
            (
                parent_full_name,
                json.dumps(payload),
                time.time(),
                ttl if ttl is not None else GITHUB_TTL_FORKS,
            ),
        )
        self._conn.commit()

    def get_fork_cache(self, parent_full_name: str):
        row = self._conn.execute(
            "SELECT payload, fetched_at, ttl_seconds FROM github_fork_cache "
            "WHERE parent_full_name = ?",
            (parent_full_name,),
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["fetched_at"] > row["ttl_seconds"]:
            return None
        return json.loads(row["payload"])

    def save_mentions_cache(
        self,
        source: str,
        query: str,
        payload,
        ttl: int | None = None,
    ) -> None:
        """Cache an external-platform mention search result."""
        # Normalise query first so "Yt-Dlp" / "  yt-dlp  " collide through
        # _query_hash's inner strip+lower (which would otherwise only apply
        # to the composite's outside whitespace, not the embedded query).
        qh = self._query_hash(f"{source}::{query.strip().lower()}")
        self._conn.execute(
            """INSERT OR REPLACE INTO github_mentions_cache
               (source, query_hash, query_text, payload, fetched_at, ttl_seconds)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                source,
                qh,
                query,
                json.dumps(payload),
                time.time(),
                ttl if ttl is not None else GITHUB_TTL_MENTIONS,
            ),
        )
        self._conn.commit()

    def get_mentions_cache(self, source: str, query: str):
        # Normalise query first so "Yt-Dlp" / "  yt-dlp  " collide through
        # _query_hash's inner strip+lower (which would otherwise only apply
        # to the composite's outside whitespace, not the embedded query).
        qh = self._query_hash(f"{source}::{query.strip().lower()}")
        row = self._conn.execute(
            "SELECT payload, fetched_at, ttl_seconds FROM github_mentions_cache "
            "WHERE source = ? AND query_hash = ?",
            (source, qh),
        ).fetchone()
        if row is None:
            return None
        if time.time() - row["fetched_at"] > row["ttl_seconds"]:
            return None
        return json.loads(row["payload"])

    def purge_expired_github_cache(self) -> int:
        """Delete all expired rows across the 3 github caches. Returns total deletes.

        Safe to call periodically; cache reads already enforce TTL on the way
        out, but long-lived DBs accumulate stale rows that this sweeps.
        """
        now = time.time()
        total = 0
        for table in ("github_repo_cache", "github_fork_cache", "github_mentions_cache"):
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE (? - fetched_at) > ttl_seconds",
                (now,),
            )
            total += cur.rowcount or 0
        self._conn.commit()
        return total

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._conn.close()
