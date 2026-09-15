"""The browser legs must not run on a thread that owns a running event loop.

WHY THIS FILE EXISTS (measured 2026-09-15, not theorised)
---------------------------------------------------------
``_fetch_playwright`` and ``_fetch_scrapling_stealth`` drive Playwright's
**sync** API. Playwright refuses that API on a thread with a running asyncio
loop and raises *immediately*::

    Error: It looks like you are using Playwright Sync API inside the asyncio
    loop. Please use the Async API instead.

So a caller shaped like ``async def f(): smart_fetch(url)`` -- a sync call
inside a coroutine -- loses BOTH browser legs, deterministically, in under
60 ms. That is not a wall, and no amount of retrying, ranking or stealth
fixes it.

Measured on this box before the fix, same URL, same leg:

  | arm                              | result                       |
  |----------------------------------|------------------------------|
  | sync context (control)           | ok=True, playwright, 0.57 s  |
  | inside ``asyncio.run``           | FAIL in 0.00 s               |
  | inside ``asyncio.run`` (stealth) | FAIL in 0.08 s               |

And in production: ``~/.scraperx/method-telemetry.jsonl`` held 9 205 attempts
against ``dexscreener.com`` with playwright 0/2292 and scrapling_stealth
0/2292 -- every browser attempt from ca-gate's ``upstream/dexscreener.py``
``_scraperx_fallback`` (an ``async def`` calling ``smart_fetch`` directly).
The 14-54 ms failure latencies in that ledger are this bug's fingerprint.

The contract locked here is therefore about WHERE a leg executes, not about
what it returns: **a browser leg never executes on a loop-owning thread.**
Network-free -- every leg is monkeypatched.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from scraperx import fetch as fetch_mod
from scraperx.fetch import smart_fetch

URL = "https://example.com/"


def _recording_leg(record: dict, content: str = "<html>ok</html>"):
    """A fake cascade leg that records the loop/thread it was executed on."""

    def _leg(url: str, timeout: int):
        try:
            asyncio.get_running_loop()
            record["saw_running_loop"] = True
        except RuntimeError:
            record["saw_running_loop"] = False
        record["thread"] = threading.current_thread().name
        record["calls"] = record.get("calls", 0) + 1
        return content, 200

    return _leg


def _in_loop(fn):
    """Run ``fn`` on a thread that owns a running event loop (the ca-gate shape)."""

    async def _main():
        return fn()

    return asyncio.run(_main())


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "attr"),
    [
        ("playwright", "_fetch_playwright"),
        ("scrapling_stealth", "_fetch_scrapling_stealth"),
    ],
)
def test_browser_leg_never_runs_on_a_loop_owning_thread(monkeypatch, mode, attr):
    rec: dict = {}
    monkeypatch.setattr(fetch_mod, attr, _recording_leg(rec))

    result = _in_loop(
        lambda: smart_fetch(
            URL, prefer=mode, strict=True, no_cache=True, adaptive=False
        )
    )

    assert rec.get("calls") == 1, "the leg must still be attempted"
    assert rec["saw_running_loop"] is False, (
        f"{mode} leg executed on a thread with a running event loop -- "
        "Playwright's sync API raises there, which is exactly the production "
        "defect this test exists to prevent"
    )
    assert rec["thread"] != threading.main_thread().name
    assert result.ok is True
    assert result.mode_used == mode


def test_offloaded_leg_failure_is_still_an_ordinary_leg_failure(monkeypatch):
    """A wall must keep looking like a wall -- offloading may not swallow errors.

    Otherwise the ledger can no longer tell "the host blocked us" from "our
    own call shape was impossible", and the cascade learns the wrong lesson.
    """

    def _walled(url: str, timeout: int):
        raise RuntimeError("HTTP 403 Cloudflare")

    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _walled)

    result = _in_loop(
        lambda: smart_fetch(
            URL, prefer="playwright", strict=True, no_cache=True, adaptive=False
        )
    )

    assert result.ok is False
    assert [m for m, _ in result.errors] == ["playwright"]
    assert "403" in result.errors[0][1]


def test_cascade_falls_through_to_the_next_leg_from_inside_a_loop(monkeypatch):
    """The whole point: a coroutine caller gets the SAME cascade a sync one does."""

    def _dead(url: str, timeout: int):
        raise RuntimeError("leg down")

    rec: dict = {}
    monkeypatch.setattr(fetch_mod, "_fetch_jina", _dead)
    monkeypatch.setattr(fetch_mod, "_fetch_urllib", _dead)
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _dead)
    monkeypatch.setattr(
        fetch_mod, "_fetch_scrapling_stealth", _recording_leg(rec, "<html>stealth</html>")
    )

    result = _in_loop(lambda: smart_fetch(URL, no_cache=True, adaptive=False))

    assert result.ok is True
    assert result.mode_used == "scrapling_stealth"
    assert rec["saw_running_loop"] is False


# ---------------------------------------------------------------------------
# No regression for everyone who was already fine
# ---------------------------------------------------------------------------


def test_sync_caller_still_runs_the_leg_inline(monkeypatch):
    """No loop → no thread. The 891-test offline suite's world must not move."""
    rec: dict = {}
    monkeypatch.setattr(fetch_mod, "_fetch_playwright", _recording_leg(rec))

    result = smart_fetch(
        URL, prefer="playwright", strict=True, no_cache=True, adaptive=False
    )

    assert result.ok is True
    assert rec["saw_running_loop"] is False
    assert rec["thread"] == threading.current_thread().name, (
        "a sync caller must not pay for a thread it does not need"
    )


@pytest.mark.parametrize(
    ("mode", "attr"),
    [("jina", "_fetch_jina"), ("urllib", "_fetch_urllib")],
)
def test_pure_http_legs_are_not_offloaded(monkeypatch, mode, attr):
    """Only the Playwright-backed legs need the thread; stdlib HTTP is loop-safe.

    Offloading those too would be scope creep that silently changes the
    threading model for every caller.
    """
    rec: dict = {}
    monkeypatch.setattr(fetch_mod, attr, _recording_leg(rec))

    result = _in_loop(
        lambda: smart_fetch(
            URL, prefer=mode, strict=True, no_cache=True, adaptive=False
        )
    )

    assert result.ok is True
    assert rec["saw_running_loop"] is True, (
        "the stdlib legs are expected to run inline, on the caller's thread"
    )


def test_stealth_options_survive_the_offload(monkeypatch, tmp_path):
    """``stealth_profile`` binds via partial(); the offload must not drop it."""
    seen: dict = {}

    def _leg(url: str, timeout: int, *, extra_kwargs=None):
        seen["extra_kwargs"] = extra_kwargs
        try:
            asyncio.get_running_loop()
            seen["saw_running_loop"] = True
        except RuntimeError:
            seen["saw_running_loop"] = False
        return "<html>ok</html>", 200

    monkeypatch.setattr(fetch_mod, "_fetch_scrapling_stealth", _leg)

    profile = tmp_path / "profiles" / "example.com"
    result = _in_loop(
        lambda: smart_fetch(
            URL,
            prefer="scrapling_stealth",
            strict=True,
            no_cache=True,
            adaptive=False,
            stealth_profile=str(profile),
        )
    )

    assert result.ok is True
    assert seen["saw_running_loop"] is False
    assert seen["extra_kwargs"]["user_data_dir"] == str(profile)
