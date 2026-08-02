"""The cascade must be able to hand a PERSISTENT session to the stealth leg.

WHY THIS EXISTS (2026-08-02)
---------------------------
An audit asked a simple question — "can scraperx carry a cookie across two
fetches of the same walled host?" — and the answer was no, for a reason that had
nothing to do with capability:

  * Scrapling has always supported it: ``StealthySession(user_data_dir=...)``
    takes the persistent branch (``launch_persistent_context``).
  * Our wrapper has always forwarded it: ``fetch_stealth(..., extra_kwargs=...)``.
  * And **no call site anywhere ever passed one.** ``_fetch_scrapling_stealth``
    called ``fetch_stealth(url, timeout=timeout)`` — timeout was the only knob —
    and ``smart_fetch`` had no parameter to express it.

So every stealth fetch started from a cold, cookie-less temporary profile, paid
the full 30-90 s Turnstile round-trip again, and could never present an
authenticated cookie. The capability was installed and unreachable.

These tests pin the wire itself. They are offline: the stealth leg is replaced
by a recorder, so what is asserted is *what the cascade passes down*, which is
exactly the thing that was missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scraperx import fetch as fetch_mod
from scraperx.fetch import smart_fetch


@pytest.fixture()
def recorder(monkeypatch):
    """Replace the real stealth leg with a recorder; return the captured calls."""
    calls: list[dict] = []

    def fake_fetch_stealth(url, timeout=90, *, extra_kwargs=None, **_kw):
        calls.append({"url": url, "timeout": timeout, "extra_kwargs": extra_kwargs})
        return "<html>ok</html>", 200

    import scraperx.scrapling_stealth as ss_mod

    monkeypatch.setattr(ss_mod, "fetch_stealth", fake_fetch_stealth)
    # Force the cascade to use ONLY the stealth leg so nothing else answers first.
    monkeypatch.setattr(
        fetch_mod, "_fetch_jina", lambda *a: pytest.fail("jina must not run")
    )
    monkeypatch.setattr(
        fetch_mod, "_fetch_urllib", lambda *a: pytest.fail("urllib must not run")
    )
    monkeypatch.setattr(
        fetch_mod, "_fetch_playwright", lambda *a: pytest.fail("pw must not run")
    )
    return calls


def _run(tmp_path, **kwargs):
    return smart_fetch(
        "https://example.com",
        prefer="scrapling_stealth",
        strict=True,
        db_path=str(tmp_path / "t.db"),
        no_cache=True,
        **kwargs,
    )


def test_no_options_still_reaches_the_leg_with_none(recorder, tmp_path):
    """Default behaviour is unchanged: no options => extra_kwargs is None."""
    r = _run(tmp_path)
    assert r.ok
    assert len(recorder) == 1
    assert recorder[0]["extra_kwargs"] is None


def test_stealth_profile_becomes_user_data_dir(recorder, tmp_path):
    """The shorthand must arrive as the kwarg Scrapling actually reads."""
    prof = tmp_path / "profiles" / "example.com"
    r = _run(tmp_path, stealth_profile=str(prof))
    assert r.ok
    got = recorder[0]["extra_kwargs"]
    assert got is not None, "stealth_profile did not reach the leg at all"
    assert got["user_data_dir"] == str(prof.resolve())


def test_stealth_profile_directory_is_created(recorder, tmp_path):
    """A profile dir that does not exist yet must not blow up the fetch."""
    prof = tmp_path / "deep" / "nested" / "profile"
    assert not prof.exists()
    _run(tmp_path, stealth_profile=str(prof))
    assert prof.is_dir()


def test_stealth_profile_expands_user(recorder, tmp_path, monkeypatch):
    """`~` must be expanded — a literal '~' directory is a classic silent bug."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(tmp_path, stealth_profile="~/prof")
    got = recorder[0]["extra_kwargs"]["user_data_dir"]
    assert "~" not in got
    assert Path(got).is_dir()


def test_stealth_kwargs_are_forwarded_verbatim(recorder, tmp_path):
    """The escape hatch must survive intact — no filtering, no renaming."""
    _run(tmp_path, stealth_kwargs={"proxy": "socks5://x:1080", "locale": "pl-PL"})
    got = recorder[0]["extra_kwargs"]
    assert got["proxy"] == "socks5://x:1080"
    assert got["locale"] == "pl-PL"


def test_explicit_user_data_dir_beats_the_shorthand(recorder, tmp_path):
    """Documented precedence: stealth_kwargs wins over stealth_profile."""
    _run(
        tmp_path,
        stealth_profile=str(tmp_path / "shorthand"),
        stealth_kwargs={"user_data_dir": "/explicit/wins"},
    )
    assert recorder[0]["extra_kwargs"]["user_data_dir"] == "/explicit/wins"


def test_profile_and_kwargs_merge_rather_than_replace(recorder, tmp_path):
    """Both must arrive together — one must not silently drop the other."""
    prof = tmp_path / "p"
    _run(tmp_path, stealth_profile=str(prof), stealth_kwargs={"locale": "pl-PL"})
    got = recorder[0]["extra_kwargs"]
    assert got["user_data_dir"] == str(prof.resolve())
    assert got["locale"] == "pl-PL"


def test_two_fetches_share_one_profile_directory(recorder, tmp_path):
    """The point of the whole feature: the SAME profile across calls.

    Cookie survival itself is Scrapling's job and needs a real browser; what
    scraperx must guarantee is that both calls are pointed at one directory.
    """
    prof = tmp_path / "shared"
    _run(tmp_path, stealth_profile=str(prof))
    _run(tmp_path, stealth_profile=str(prof))
    assert len(recorder) == 2
    dirs = {c["extra_kwargs"]["user_data_dir"] for c in recorder}
    assert len(dirs) == 1, f"calls used different profiles: {dirs}"
