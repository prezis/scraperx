"""tv_symbol_resolver — TradingView symbol/exchange resolution with negative caching.

Replaces the manual prefix-probing dance from wojak-wojtek s22 batches 1+3+5
(VIX9D was blocked under TVC: but resolved under CBOE:; PCC failed everywhere
and burned 8 retry probes per cron tick before being blacklisted by hand).

Public API:

    from scraperx import resolve_symbol, SymbolResolution

    res = resolve_symbol("VIX9D")
    if res.status == "resolved":
        # use res.exchange — e.g. "CBOE"
        symbol_string = f"{res.exchange}:{res.symbol}"

CLI:
    scraperx tv-resolve VIX9D
    scraperx tv-resolve ZN --asset-class futures
    scraperx tv-resolve VIX9D --json
    scraperx tv-resolve VIX9D --candidates CBOE,TVC,INDEX --strict

Cache strategy (per (ticker,exchange) pair, in ~/.scraperx/social.db):
    status=resolved        → 7 day TTL  (symbol exists, has data)
    status=empty_no_data   → 6 hour TTL (TV recognises it but n_bars=0)
    status=not_found       → 24 hour TTL (TV throws / unknown symbol)

Asset class → exchange priority (probe order):
    futures   CME, CBOT, COMEX, NYMEX, ICE
    vol       CBOE, TVC, INDEX
    fx        FX_IDC, OANDA, FOREXCOM
    equity    NASDAQ, NYSE, AMEX
    index     TVC, INDEX, CBOE, CRYPTOCAP
    crypto    BINANCE, COINBASE, KRAKEN, BYBIT, CRYPTOCAP

Auto-detection from ticker patterns: VIX*/SKEW/VVIX → vol, EURUSD/USDJPY → fx,
BTCUSDT/ETHUSDT → crypto, ZN/ZB/CL/GC → futures (CME-family heuristics), else
default to ("INDEX", "TVC", "NASDAQ", "NYSE") as a last-resort sweep.

Optional dep: ``pip install scraperx[tv-resolve]`` (pulls tvDatafeed). The
resolver is import-safe without tvDatafeed — calls just degrade to status=
not_found with a clear error_msg when the dep is missing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Literal, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "AssetClass",
    "EXCHANGE_PRIORITY",
    "ResolverError",
    "SymbolResolution",
    "TvDatafeedNotInstalled",
    "auto_detect_asset_class",
    "main_tv_resolve",
    "resolve_symbol",
]


# ---------------------------------------------------------------------------
# Types & errors
# ---------------------------------------------------------------------------


AssetClass = Literal["futures", "vol", "fx", "equity", "index", "crypto", "auto"]
ResolveStatus = Literal["resolved", "empty_no_data", "not_found", "cache_hit"]


class ResolverError(Exception):
    """Base class for all resolver errors."""


class TvDatafeedNotInstalled(ResolverError):
    """tvDatafeed is an optional dep — calls without it degrade gracefully."""


# Per asset-class, the exchanges to try in order. First hit wins.
# Ordering follows liquidity / symbol-most-likely-to-be-canonical.
EXCHANGE_PRIORITY: dict[str, tuple[str, ...]] = {
    "futures": ("CME", "CBOT", "COMEX", "NYMEX", "ICE"),
    "vol":     ("CBOE", "TVC", "INDEX"),
    "fx":      ("FX_IDC", "OANDA", "FOREXCOM"),
    "equity":  ("NASDAQ", "NYSE", "AMEX"),
    "index":   ("TVC", "INDEX", "CBOE", "CRYPTOCAP"),
    "crypto":  ("BINANCE", "COINBASE", "KRAKEN", "BYBIT", "CRYPTOCAP"),
}

# Last-resort sweep when asset_class cannot be inferred — broad coverage,
# quality bar lower (slower probes, more likely to false-positive).
_FALLBACK_SWEEP: tuple[str, ...] = (
    "INDEX", "TVC", "NASDAQ", "NYSE", "CBOE", "CME", "BINANCE", "FX_IDC",
)

# TTLs (seconds)
TTL_RESOLVED = 7 * 24 * 3600   # 7 days — exchange membership is stable
TTL_EMPTY = 6 * 3600           # 6 hours — exchange knows it but no data; recheck later
TTL_NOT_FOUND = 24 * 3600      # 24 hours — exchange doesn't recognise; rare to flip


@dataclass(frozen=True)
class SymbolResolution:
    """Result of a resolve_symbol() call.

    ``ok=True`` iff status == "resolved" or status == "cache_hit" with status
    metadata indicating the underlying probe succeeded.
    """

    ticker: str
    symbol: str          # canonical (TV-side) symbol; usually equals ticker
    exchange: str        # winning exchange prefix; "" if unresolved
    status: ResolveStatus
    tried: tuple[str, ...]  # exchanges tried in order, for debug
    elapsed_ms: int = 0
    was_cached: bool = False
    error_msg: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.exchange) and self.status in ("resolved", "cache_hit")

    @property
    def tv_symbol(self) -> str:
        """Format as ``EXCHANGE:SYMBOL`` (the canonical tvDatafeed string)."""
        return f"{self.exchange}:{self.symbol}" if self.exchange else self.symbol


# ---------------------------------------------------------------------------
# Auto-detection heuristics
# ---------------------------------------------------------------------------


# Vol indices: VIX, VVIX, VIX9D, VIX1D, VIX3M, SKEW, GVZ, OVX, MOVE
_VOL_RE = re.compile(r"^(VIX|VVIX|VIX[0-9]+[A-Z]?|SKEW|GVZ|OVX|MOVE|VOLM?)$", re.IGNORECASE)
# FX pairs: 6+ chars, alpha only, common pair shapes
_FX_RE = re.compile(r"^(EUR|USD|GBP|JPY|CHF|AUD|NZD|CAD|CNH|MXN|ZAR|TRY)[A-Z]{3}$", re.IGNORECASE)
# Crypto perp/spot pairs
_CRYPTO_RE = re.compile(r"^[A-Z0-9]{2,8}(USDT|USDC|USD|BTC|ETH|EUR)$", re.IGNORECASE)
# CME-family futures front-month tickers (plain and continuous)
_FUTURES_RE = re.compile(r"^(ZN|ZB|ZF|ZT|ZQ|CL|GC|SI|NG|HG|ES|NQ|YM|RTY|HE|LE|ZC|ZS|ZW|ZL|ZM|6E|6J|6B|6A|6C|DX)[!]?[0-9]?$", re.IGNORECASE)
# Common index tickers
_INDEX_RE = re.compile(r"^(SPX|NDX|DJI|RUT|VIX|TOTAL[0-9]?|BTC\.D)$", re.IGNORECASE)


def auto_detect_asset_class(ticker: str) -> AssetClass:
    """Best-effort guess of asset class from ticker patterns. Returns 'auto'
    when no heuristic matches — caller should fall back to the broad sweep.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return "auto"
    if _VOL_RE.match(t):
        return "vol"
    if _INDEX_RE.match(t):
        return "index"
    if _FX_RE.match(t):
        return "fx"
    if _CRYPTO_RE.match(t):
        return "crypto"
    if _FUTURES_RE.match(t):
        return "futures"
    return "auto"


# ---------------------------------------------------------------------------
# Cache layer (mirrors fetch.py's singleton pattern)
# ---------------------------------------------------------------------------


DEFAULT_DB_PATH = os.path.expanduser("~/.scraperx/social.db")

_FETCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS tv_symbol_cache (
    cache_key TEXT PRIMARY KEY,
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

_DB_CONN_CACHE: dict[str, sqlite3.Connection] = {}
_DB_CONN_LOCK = threading.Lock()


def _open_db(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    cached = _DB_CONN_CACHE.get(path)
    if cached is not None:
        return cached
    with _DB_CONN_LOCK:
        cached = _DB_CONN_CACHE.get(path)
        if cached is not None:
            return cached
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            from scraperx._sqlite_pragmas import apply_pragmas
            apply_pragmas(conn)
        except ImportError:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_FETCH_SCHEMA)
        _DB_CONN_CACHE[path] = conn
        return conn


def _cache_key(ticker: str, exchange: str) -> str:
    return f"{ticker.upper()}:{exchange.upper()}"


def _cache_get(ticker: str, exchange: str, db_path: str | None = None) -> dict | None:
    conn = _open_db(db_path)
    row = conn.execute(
        """SELECT ticker, exchange, status, last_checked, ttl_seconds, error_msg
           FROM tv_symbol_cache WHERE cache_key = ?""",
        (_cache_key(ticker, exchange),),
    ).fetchone()
    if row is None:
        return None
    if (time.time() - row["last_checked"]) > row["ttl_seconds"]:
        return None
    return dict(row)


def _cache_put(
    ticker: str,
    exchange: str,
    status: ResolveStatus,
    *,
    ttl: int,
    error_msg: str = "",
    db_path: str | None = None,
) -> None:
    conn = _open_db(db_path)
    with _DB_CONN_LOCK:
        conn.execute(
            """INSERT OR REPLACE INTO tv_symbol_cache
               (cache_key, ticker, exchange, status, last_checked, ttl_seconds, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _cache_key(ticker, exchange),
                ticker.upper(),
                exchange.upper(),
                status,
                time.time(),
                ttl,
                error_msg,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Probe: tvDatafeed
# ---------------------------------------------------------------------------


def _probe_one(ticker: str, exchange: str, *, n_bars: int = 1) -> tuple[ResolveStatus, str]:
    """Try (ticker, exchange) on tvDatafeed. Returns (status, error_msg).

    status:
        resolved        — TV returned bars
        empty_no_data   — TV recognised exchange:ticker but n_bars=0
        not_found       — TV threw / unknown
    """
    try:
        from tvDatafeed import Interval, TvDatafeed
    except ImportError as e:
        raise TvDatafeedNotInstalled(
            "tvDatafeed not installed — pip install scraperx[tv-resolve]"
        ) from e

    try:
        tv = TvDatafeed()  # no login = limited but enough for index-style probes
        df = tv.get_hist(
            symbol=ticker.upper(),
            exchange=exchange.upper(),
            interval=Interval.in_daily,
            n_bars=n_bars,
        )
    except Exception as e:  # noqa: BLE001 — tvDatafeed raises a grab-bag of errors
        return "not_found", f"{type(e).__name__}: {e}"

    if df is None or df.empty:
        return "empty_no_data", "tvDatafeed returned empty frame"
    return "resolved", ""


# ---------------------------------------------------------------------------
# Public resolve_symbol
# ---------------------------------------------------------------------------


def _build_candidates(
    asset_class: str,
    custom_candidates: Iterable[str] | None,
) -> tuple[str, ...]:
    """Pick the exchange list to try, in order. Custom override wins."""
    if custom_candidates is not None:
        seen: list[str] = []
        for c in custom_candidates:
            c = c.strip().upper()
            if c and c not in seen:
                seen.append(c)
        return tuple(seen)
    if asset_class in EXCHANGE_PRIORITY:
        return EXCHANGE_PRIORITY[asset_class]
    return _FALLBACK_SWEEP


def resolve_symbol(
    ticker: str,
    *,
    asset_class: AssetClass | str = "auto",
    candidates: Iterable[str] | None = None,
    no_cache: bool = False,
    strict: bool = False,
    db_path: str | None = None,
    _probe_fn=None,  # injection point for tests; None → look up _probe_one at call time
) -> SymbolResolution:
    """Resolve a TradingView ticker to its exchange via probe cascade + cache.

    Args:
        ticker: Symbol to resolve, e.g. "VIX9D", "ZN", "EURUSD", "BTCUSDT".
        asset_class: One of the keys in EXCHANGE_PRIORITY, or "auto" to detect.
            When detection fails, a fallback sweep across the most common
            exchanges runs.
        candidates: Optional explicit override of the exchange order to try.
            Wins over asset_class. Useful to limit the probe surface in tests.
        no_cache: Skip cache reads AND writes.
        strict: When True, only try the FIRST exchange. No fallback.
        db_path: Override cache DB path (mostly for tests).
        _probe_fn: Network probe injection point (for tests).

    Returns:
        SymbolResolution. Check ``.ok`` to see if a winning exchange was found.

    Notes:
        - Negative results (empty_no_data, not_found) ARE cached with shorter
          TTLs so a re-run doesn't re-probe known-dead pairs.
        - Resolution is per-(ticker,exchange) — the same ticker can be
          resolved on a NEW exchange even if a previous one was cached as
          empty_no_data.
    """
    ticker = (ticker or "").strip()
    if not ticker:
        return SymbolResolution(
            ticker="", symbol="", exchange="", status="not_found",
            tried=(), error_msg="empty ticker",
        )

    # Resolve _probe_fn at call time so monkeypatching tvr._probe_one works
    # from the CLI test path (which can't pass _probe_fn directly).
    if _probe_fn is None:
        _probe_fn = _probe_one

    # Resolve asset_class
    if asset_class == "auto":
        asset_class = auto_detect_asset_class(ticker)
    cands = _build_candidates(asset_class, candidates)
    if strict:
        cands = cands[:1]

    if not cands:
        return SymbolResolution(
            ticker=ticker, symbol=ticker, exchange="", status="not_found",
            tried=(), error_msg="no candidate exchanges configured",
        )

    tried: list[str] = []
    t0 = time.monotonic()
    last_error = ""

    for ex in cands:
        tried.append(ex)

        # Cache lookup
        if not no_cache:
            row = _cache_get(ticker, ex, db_path=db_path)
            if row is not None:
                if row["status"] == "resolved":
                    elapsed = int((time.monotonic() - t0) * 1000)
                    return SymbolResolution(
                        ticker=ticker, symbol=ticker, exchange=ex,
                        status="cache_hit", tried=tuple(tried),
                        elapsed_ms=elapsed, was_cached=True,
                    )
                # negative cache: skip this exchange, move on
                last_error = row.get("error_msg") or row["status"]
                continue

        # Probe network
        try:
            status, err = _probe_fn(ticker, ex)
        except TvDatafeedNotInstalled as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SymbolResolution(
                ticker=ticker, symbol=ticker, exchange="", status="not_found",
                tried=tuple(tried), elapsed_ms=elapsed, error_msg=str(e),
            )

        if status == "resolved":
            if not no_cache:
                _cache_put(ticker, ex, "resolved", ttl=TTL_RESOLVED, db_path=db_path)
            elapsed = int((time.monotonic() - t0) * 1000)
            return SymbolResolution(
                ticker=ticker, symbol=ticker, exchange=ex,
                status="resolved", tried=tuple(tried), elapsed_ms=elapsed,
            )

        # Negative — cache and continue
        if not no_cache:
            ttl = TTL_EMPTY if status == "empty_no_data" else TTL_NOT_FOUND
            _cache_put(ticker, ex, status, ttl=ttl, error_msg=err, db_path=db_path)
        last_error = err

    elapsed = int((time.monotonic() - t0) * 1000)
    return SymbolResolution(
        ticker=ticker, symbol=ticker, exchange="", status="not_found",
        tried=tuple(tried), elapsed_ms=elapsed, error_msg=last_error,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_human(res: SymbolResolution) -> str:
    if res.ok:
        return (
            f"✓ {res.tv_symbol} (status={res.status}, "
            f"tried={','.join(res.tried)}, {res.elapsed_ms}ms)"
        )
    return (
        f"✗ {res.ticker} unresolved — tried={','.join(res.tried) or '(none)'}, "
        f"last_error={res.error_msg or 'n/a'}"
    )


def main_tv_resolve(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scraperx tv-resolve",
        description="Resolve a TradingView ticker to its exchange via probe cascade + cache.",
    )
    parser.add_argument("_cmd", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("ticker", help="Ticker to resolve, e.g. VIX9D, ZN, EURUSD")
    parser.add_argument(
        "--asset-class", default="auto",
        choices=("auto", "futures", "vol", "fx", "equity", "index", "crypto"),
        help="Force a specific asset class (skip auto-detect).",
    )
    parser.add_argument(
        "--candidates", default=None,
        help="Comma-separated exchange override (e.g. CBOE,TVC,INDEX). Wins over --asset-class.",
    )
    parser.add_argument("--strict", action="store_true", help="Try ONLY the first candidate.")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache reads/writes.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human format.")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args(argv)

    log_level = logging.WARNING
    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose == 1:
        log_level = logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    candidates = None
    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]

    res = resolve_symbol(
        args.ticker,
        asset_class=args.asset_class,
        candidates=candidates,
        no_cache=args.no_cache,
        strict=args.strict,
    )

    if args.json:
        print(json.dumps(asdict(res), indent=2, sort_keys=True))
    else:
        print(_format_human(res))

    return 0 if res.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_tv_resolve())
