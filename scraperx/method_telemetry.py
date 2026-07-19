"""Method telemetry — a self-learning ledger of WHICH scraping methods work.

Every tiered scraper in scraperx faces the same question: "which method
should I try first for this target?" The answer drifts over time (Reddit
blocks a datacenter IP, a redlib mirror dies, a JS-challenge appears), so
instead of hardcoding the order forever, scrapers RECORD each attempt here
and ASK for a ranking on the next call.

Ledger: newline-delimited JSON at ``~/.scraperx/method-telemetry.jsonl``
(same convention as ``github_analyzer/telemetry.py``'s verdicts.jsonl).
One event per line:

  {
    "timestamp":     "2026-07-19T12:34:56Z",
    "ts_epoch":      1784896496.123,
    "scraper":       "reddit",
    "method":        "old_json",
    "target_domain": "old.reddit.com",
    "success":       false,
    "latency_ms":    412.7,
    "http_status":   403          # or null when not an HTTP-shaped failure
  }

Usage
-----
    from scraperx.method_telemetry import record, preferred_order

    record("reddit", "old_json", "old.reddit.com", False,
           latency_ms=412.7, http_status=403)

    order = preferred_order("reddit", ("old_json", "redlib", "stealth"))
    # → ["redlib", "stealth", "old_json"] once old_json keeps failing

Ranking model
-------------
Per (scraper, method): exponentially time-decayed success rate
(half-life ``HALF_LIFE_S`` = 6 h) over the last ``LAST_N`` events.

  - < ``MIN_SAMPLES`` (5) raw samples → NEUTRAL prior 0.5 ("unknown").
  - ≥ 5 samples → decayed_successes / decayed_total  (0.0 … 1.0).

Sort is descending score, STABLE — so a fresh ledger (all methods unknown
at 0.5) returns exactly the caller's ``default_order``; a proven method
(→1.0) rises above untried ones (0.5); a failing method (→0.0) sinks below
untried ones. Decay means a method demoted by a transient block recovers
once fresh successes land.

Guarantees
----------
- **Dependency-free**: stdlib only.
- **Fail-open**: any telemetry error (unwritable dir, corrupt line, bad
  clock) is swallowed and logged at debug level — scraping NEVER breaks
  because of telemetry. ``preferred_order`` degrades to ``default_order``.
- **Test/CI isolation**: set ``SCRAPERX_METHOD_TELEMETRY_PATH`` to point
  the ledger elsewhere (tests use a tmpdir via this env var).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "TELEMETRY_DIR",
    "TELEMETRY_FILE",
    "method_stats",
    "preferred_order",
    "record",
]

TELEMETRY_DIR = Path.home() / ".scraperx"
TELEMETRY_FILE = TELEMETRY_DIR / "method-telemetry.jsonl"
ENV_PATH_VAR = "SCRAPERX_METHOD_TELEMETRY_PATH"

# Below this many raw samples a method's rate is not trusted → neutral prior.
MIN_SAMPLES = 5
# Exponential-decay half-life for event weight (seconds). 6 h: yesterday's
# block barely counts against a method that works today.
HALF_LIFE_S = 6 * 3600.0
# Neutral prior for unknown/under-sampled methods. 0.5 sits between a proven
# method (→1.0, promoted above untried) and a failing one (→0.0, demoted
# below untried); ties resolve to the caller's default order (stable sort).
NEUTRAL_PRIOR = 0.5
# Per-(scraper, method) sliding window — only the most recent N events count.
LAST_N = 200
# Only the tail of the ledger file is read (bounds cost as the file grows).
MAX_TAIL_BYTES = 512 * 1024


def _resolve_path(ledger_path: str | Path | None = None) -> Path:
    """Explicit arg > env var > module default (checked at CALL time)."""
    if ledger_path is not None:
        return Path(ledger_path)
    env = os.environ.get(ENV_PATH_VAR)
    return Path(env) if env else TELEMETRY_FILE


def record(
    scraper: str,
    method: str,
    target_domain: str,
    success: bool,
    latency_ms: float | None = None,
    http_status: int | None = None,
    *,
    ledger_path: str | Path | None = None,
) -> bool:
    """Append one method-attempt event to the JSONL ledger.

    Returns True on success, False on any error (errors are logged at debug
    level but never raised — telemetry must never break the scraper).
    """
    try:
        path = _resolve_path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "ts_epoch": round(time.time(), 3),
            "scraper": str(scraper),
            "method": str(method),
            "target_domain": str(target_domain or ""),
            "success": bool(success),
            "latency_ms": round(float(latency_ms), 1) if latency_ms is not None else None,
            "http_status": int(http_status) if http_status is not None else None,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # fail-open, always
        logger.debug("method-telemetry record(%s, %s) failed: %s", scraper, method, exc)
        return False


def _event_epoch(ev: dict) -> float | None:
    """Best-effort event time: ts_epoch, else ISO timestamp, else None."""
    ts = ev.get("ts_epoch")
    if isinstance(ts, (int, float)):
        return float(ts)
    raw = ev.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _read_events(path: Path, scraper: str, target_domain: str | None) -> list[dict]:
    """Tail-read the ledger, keeping parseable events for one scraper."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > MAX_TAIL_BYTES:
            fh.seek(size - MAX_TAIL_BYTES)
            fh.readline()  # discard the (likely partial) first line
        raw_lines = fh.read().decode("utf-8", errors="replace").splitlines()
    events: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # corrupt line — skip, never raise
        if not isinstance(ev, dict) or ev.get("scraper") != scraper:
            continue
        if target_domain is not None and ev.get("target_domain") != target_domain:
            continue
        events.append(ev)
    return events


def _score_methods(
    events: Iterable[dict],
    methods: Sequence[str],
    now: float,
) -> dict[str, float]:
    """Decayed success-rate per method; NEUTRAL_PRIOR under MIN_SAMPLES."""
    per_method: dict[str, list[dict]] = {m: [] for m in methods}
    for ev in events:
        m = ev.get("method")
        if m in per_method:
            per_method[m].append(ev)

    scores: dict[str, float] = {}
    for m in methods:
        evs = per_method[m][-LAST_N:]  # sliding window, newest last
        if len(evs) < MIN_SAMPLES:
            scores[m] = NEUTRAL_PRIOR
            continue
        w_total = 0.0
        w_success = 0.0
        for ev in evs:
            epoch = _event_epoch(ev)
            age = max(0.0, now - epoch) if epoch is not None else 0.0
            w = 0.5 ** (age / HALF_LIFE_S)
            w_total += w
            if ev.get("success"):
                w_success += w
        scores[m] = (w_success / w_total) if w_total > 0 else NEUTRAL_PRIOR
    return scores


def preferred_order(
    scraper: str,
    default_order: Sequence[str],
    *,
    target_domain: str | None = None,
    ledger_path: str | Path | None = None,
) -> list[str]:
    """Rank ``default_order`` methods by recent recorded success-rate.

    Args:
        scraper:       Ledger namespace, e.g. ``"reddit"``.
        default_order: The scraper's canonical method order — also the
                       fallback when the ledger is missing/empty/unreadable,
                       and the tiebreaker (stable sort) for equal scores.
        target_domain: Optionally rank using only events for one domain.
        ledger_path:   Explicit ledger override (tests); default resolves
                       env ``SCRAPERX_METHOD_TELEMETRY_PATH`` then
                       ``~/.scraperx/method-telemetry.jsonl``.

    Returns:
        A NEW list — every method of ``default_order`` exactly once,
        best-scoring first. Never raises.
    """
    default = list(default_order)
    try:
        path = _resolve_path(ledger_path)
        if not path.exists():
            return default
        events = _read_events(path, scraper, target_domain)
        if not events:
            return default
        scores = _score_methods(events, default, now=time.time())
        return sorted(default, key=lambda m: -scores[m])  # stable → default tiebreak
    except Exception as exc:  # fail-open, always
        logger.debug("method-telemetry preferred_order(%s) failed: %s", scraper, exc)
        return default


def method_stats(
    scraper: str,
    *,
    target_domain: str | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, dict]:
    """Introspection: per-method ``{n, successes, success_rate, avg_latency_ms}``.

    Raw (undecayed) numbers over the tail window — for humans and doctors,
    not for ranking. Returns {} on any error (fail-open).
    """
    try:
        path = _resolve_path(ledger_path)
        if not path.exists():
            return {}
        out: dict[str, dict] = {}
        for ev in _read_events(path, scraper, target_domain):
            m = str(ev.get("method") or "")
            if not m:
                continue
            s = out.setdefault(
                m, {"n": 0, "successes": 0, "success_rate": 0.0, "avg_latency_ms": None}
            )
            s["n"] += 1
            if ev.get("success"):
                s["successes"] += 1
            lat = ev.get("latency_ms")
            if isinstance(lat, (int, float)):
                prev = s["avg_latency_ms"]
                s["avg_latency_ms"] = lat if prev is None else prev + (lat - prev) / s["n"]
        for s in out.values():
            s["success_rate"] = round(s["successes"] / s["n"], 4) if s["n"] else 0.0
            if s["avg_latency_ms"] is not None:
                s["avg_latency_ms"] = round(s["avg_latency_ms"], 1)
        return out
    except Exception as exc:
        logger.debug("method-telemetry method_stats(%s) failed: %s", scraper, exc)
        return {}
