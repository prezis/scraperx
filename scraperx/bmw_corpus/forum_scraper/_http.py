"""HTTP client for forum scrapers — rate-limited, cookie-jar, polite UA."""
from __future__ import annotations

import http.cookiejar
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair corpus for ML training; "
    "contact: przemyslaw.palyska@gmail.com)"
)
DEFAULT_TIMEOUT = 20.0


class RateLimitedClient:
    """Per-host rate-limited HTTP GET with cookie persistence + retry/backoff.

    Thread-safe (single lock per host). Use one client per host.
    """

    def __init__(
        self,
        *,
        host: str,
        rate_per_second: float = 0.5,
        user_agent: str = DEFAULT_UA,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self.user_agent = user_agent
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_call = 0.0

        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPRedirectHandler(),
        )
        self._opener.addheaders = [
            ("User-Agent", user_agent),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
            ("Accept-Encoding", "gzip, deflate"),
        ]

    def _wait(self):
        with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    def get_html(self, url: str, max_retries: int = 3) -> str:
        """GET URL, return decoded HTML. Raises on final failure."""
        last_err: Exception | None = None
        for attempt in range(max_retries):
            self._wait()
            try:
                with self._opener.open(url, timeout=self.timeout) as resp:
                    raw = resp.read()
                    encoding = resp.headers.get_content_charset() or "utf-8"
                    # Handle gzip if present (urllib doesn't auto-decompress)
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                        import gzip
                        raw = gzip.decompress(raw)
                    elif resp.headers.get("Content-Encoding", "").lower() == "deflate":
                        import zlib
                        raw = zlib.decompress(raw)
                    return raw.decode(encoding, errors="replace")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503):
                    backoff = (2 ** attempt) * 30
                    log.warning(
                        "%s: %d on %s — backing off %ds (attempt %d/%d)",
                        self.host, e.code, url, backoff, attempt + 1, max_retries,
                    )
                    time.sleep(backoff)
                    continue
                # 403/404/etc — non-retryable
                log.warning("%s: HTTP %d on %s", self.host, e.code, url)
                raise
            except urllib.error.URLError as e:
                last_err = e
                log.warning(
                    "%s: URLError on %s: %s (attempt %d/%d)",
                    self.host, url, e, attempt + 1, max_retries,
                )
                time.sleep(5 * (attempt + 1))
                continue
        if last_err:
            raise last_err
        raise RuntimeError(f"unreachable: {url}")
