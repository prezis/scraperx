"""quota_session — secure-by-default cached + rate-limited HTTP session.

Wraps `requests-cache` (response caching keyed on method+URL+body) and
`pyrate-limiter` (per-second / per-day token bucket) into ONE class with two
properties scraperx callers want by default:

1. **Auth-header hygiene**: ``Authorization`` and other configured auth headers
   are EXCLUDED from the cache key (`ignored_parameters`). Without this,
   rotating a Finnhub / Alchemy / OpenAI token writes a new cache row per token
   AND leaks the token in the SQLite cache file. We saw this happen in s31.

   **Trade-off (read this)**: Excluding auth headers from the cache key means
   ALL tokens hitting the same URL share ONE cache entry. If you revoke a token
   server-side, this client will continue serving the old (cached) body until
   ``cache_expire_s`` elapses. For read-only public-data APIs (Finnhub /
   Alchemy / CoinGecko) this is fine. For per-tenant data APIs where the
   response varies by token identity, set ``auth_headers=()`` so token IS in
   the key (accept token-leakage in the cache file in exchange for
   correctness).

2. **Dual-rate limiting**: takes a list of ``Rate(N, Duration.UNIT)`` so the
   common "60/sec AND 50_000/day" idiom is one constructor arg.

Source incident:
    s31/s32 wojak-wojtek OSINT session, 2026-04-30 — Finnhub free-tier
    (60 req/sec + 50_000/day quotas) integration. We wired requests-cache +
    pyrate-limiter inline; this module ports the wrapper.

Optional deps:
    pip install requests-cache pyrate-limiter

Usage:
    from pyrate_limiter import Rate, Duration
    from scraperx.quota_session import QuotaSession

    sess = QuotaSession(
        cache_path="~/.scraperx/finnhub-cache.sqlite",
        rates=[Rate(60, Duration.SECOND), Rate(50_000, Duration.DAY)],
        auth_headers=("X-Finnhub-Token", "Authorization"),
        bucket_name="finnhub-free",
    )
    resp = sess.get("https://finnhub.io/api/v1/stock/profile2", params={"symbol": "AAPL"})
    print(resp.json())
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_AUTH_HEADERS",
    "QuotaSession",
    "QuotaSessionConfig",
]


# The classic auth-header set. Lowercased on use because requests-cache
# normalises header names to lowercase before consulting ignored_parameters.
DEFAULT_AUTH_HEADERS: tuple[str, ...] = (
    "Authorization",
    "X-API-Key",
    "X-Auth-Token",
    "X-Finnhub-Token",
    "Apikey",
    "Api-Key",
)


@dataclass
class QuotaSessionConfig:
    """Configuration for a QuotaSession instance.

    Attributes:
        cache_path:    SQLite file path for requests-cache. ``~`` expanded.
        cache_expire_s: TTL on cached responses (seconds). Default 24h.
        auth_headers:  Header names whose VALUES must NOT contribute to the
                       cache key. Names are case-insensitive.
        bucket_name:   Identifier passed to pyrate-limiter's try_acquire.
                       Use the same name across processes hitting the same
                       upstream quota.
    """

    cache_path: str = "~/.scraperx/quota-cache.sqlite"
    cache_expire_s: int = 86_400
    auth_headers: tuple[str, ...] = DEFAULT_AUTH_HEADERS
    bucket_name: str = "default"


class QuotaSession:
    """Cached + rate-limited HTTP session with auth-header hygiene.

    The ``rates`` argument expects a list of ``pyrate_limiter.Rate`` objects.
    Both the per-second and per-day buckets are enforced together: a request
    blocks until BOTH buckets have a free token (that's why try_acquire is
    called with ``blocking=True``).
    """

    def __init__(
        self,
        cache_path: str = "~/.scraperx/quota-cache.sqlite",
        *,
        rates: Iterable[Any] | None = None,
        auth_headers: Iterable[str] = DEFAULT_AUTH_HEADERS,
        bucket_name: str = "default",
        cache_expire_s: int = 86_400,
    ):
        self.config = QuotaSessionConfig(
            cache_path=cache_path,
            cache_expire_s=cache_expire_s,
            auth_headers=tuple(auth_headers),
            bucket_name=bucket_name,
        )
        # Normalise header names to lowercase (requests-cache convention)
        self._ignored_params = tuple(h.lower() for h in self.config.auth_headers)
        self._session = self._build_session()
        self._limiter = self._build_limiter(list(rates) if rates else [])

    def _build_session(self) -> Any:
        """Build the requests-cache CachedSession."""
        try:
            from requests_cache import CachedSession  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "QuotaSession needs the optional `requests-cache` package. "
                "Install with `pip install requests-cache`."
            ) from e

        path = os.path.expanduser(self.config.cache_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return CachedSession(
            cache_name=path,
            backend="sqlite",
            expire_after=self.config.cache_expire_s,
            # `ignored_parameters` removes both query params AND headers from
            # the cache-key hash by name. Critical for auth-token hygiene.
            ignored_parameters=list(self._ignored_params),
            # Don't poison the cache with 5xx / 4xx
            allowable_codes=(200, 203, 300, 301, 308),
            # Match requests sent with same path but different query order
            match_headers=False,
        )

    def _build_limiter(self, rates: list[Any]) -> Any | None:
        """Build the pyrate-limiter Limiter, or return None if no rates given.

        Note (s37, 2026-05-02 wojak audit): pyrate-limiter's ``Limiter(...)``
        constructor takes a SINGLE positional arg (Rate or List[Rate]). Passing
        ``Limiter(*rates)`` silently drops all rates after the first. Always
        pass the list as a single arg.
        """
        if not rates:
            return None
        try:
            from pyrate_limiter import Limiter  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "QuotaSession needs the optional `pyrate-limiter` package. "
                "Install with `pip install pyrate-limiter>=4`."
            ) from e
        return Limiter(rates) if len(rates) > 1 else Limiter(rates[0])

    def _acquire(self) -> None:
        """Block until both rate buckets have a free token. No-op if no limiter.

        pyrate-limiter v3 / v4 differ:
            - v2: ``Limiter.try_acquire(name)`` returns bool, raises BucketFullException.
            - v3+: ``Limiter.try_acquire(name, weight=1)`` raises BucketFullException
              when over-quota; ``Limiter.ratelimit(name, delay=True)`` blocks.

        We attempt to call the blocking ``ratelimit`` first; if that doesn't
        exist (older pyrate-limiter), fall back to a poll-and-sleep loop on
        ``try_acquire`` so the contract "this method blocks until a token is
        available" holds across versions.
        """
        if self._limiter is None:
            return
        name = self.config.bucket_name

        # Path A — modern pyrate-limiter ``ratelimit`` (blocks if delay=True)
        ratelimit = getattr(self._limiter, "ratelimit", None)
        if callable(ratelimit):
            try:
                ratelimit(name, delay=True)
                return
            except TypeError:
                # Older signature without delay= kwarg — fall through to Path B.
                pass
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"rate-limiter ratelimit() failed: {e}") from e

        # Path B — try_acquire poll loop. Sleep between attempts so we don't
        # hot-spin while the bucket refills. ``time`` is module-level so tests
        # can monkeypatch ``scraperx.quota_session.time``.
        for _ in range(120):  # ~60s ceiling at 0.5s sleep
            try:
                ok = self._limiter.try_acquire(name)
            except Exception:  # noqa: BLE001 — older versions throw BucketFullException
                time.sleep(0.5)
                continue
            if ok or ok is None:
                # ok=True or older versions returning None on success
                return
            time.sleep(0.5)
        raise RuntimeError("rate-limiter acquire timed out after ~60s")

    def get(self, url: str, **kwargs: Any) -> Any:
        """Rate-limited cached GET. Returns a requests.Response."""
        self._acquire()
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """Rate-limited cached POST. Returns a requests.Response."""
        self._acquire()
        return self._session.post(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Generic rate-limited cached request."""
        self._acquire()
        return self._session.request(method, url, **kwargs)

    @property
    def ignored_parameters(self) -> tuple[str, ...]:
        """The tuple of header / query names excluded from the cache key."""
        return self._ignored_params


# ---------------------------------------------------------------------------
# Inline demo (no network)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    cfg = QuotaSessionConfig(
        cache_path="/tmp/qs-demo.sqlite",
        auth_headers=("Authorization", "X-Custom-Key"),
        bucket_name="demo",
    )
    print(f"cfg ignored (lowercased on use): {cfg.auth_headers}")
    # Touch lowercasing path without instantiating the optional deps
    lower = tuple(h.lower() for h in cfg.auth_headers)
    assert lower == ("authorization", "x-custom-key")
    print("auth-header lowercasing OK")
