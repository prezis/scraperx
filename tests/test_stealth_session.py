"""One browser, N URLs — the `stealth_session` / `fetch_stealth_session` surface.

Offline: `_get_stealthy_session` is monkeypatched to a recorder, mirroring the
accessor-seam pattern the existing stealth tests already use. No browser, no
network.

THE LOAD-BEARING TEST here is `test_fetch_receives_url_only_and_no_kwargs`.
Upstream's per-fetch validator iterates only its OWN known field names, so any
other key handed to `session.fetch(url, **kwargs)` is never read and never
raises — `user_data_dir`, `headless` and `capture_xhr` would vanish in silence
and the whole batch would run cookie-less while looking perfectly healthy. That
is exactly the regression 1.9.0 was written to kill (a capability wired
everywhere except at the call site), re-introduced one layer up. Everything
else in this file is ordinary; that one test is the reason the file exists.
"""

from __future__ import annotations

import contextlib

import pytest

from scraperx import scrapling_stealth as ss_mod
from scraperx.scrapling_stealth import (
    ScraplingNotAvailable,
    StealthPageResult,
    fetch_stealth_session,
    stealth_session,
)


class FakePage:
    def __init__(self, body=b"<html>ok</html>", status=200, xhr=None):
        self.body = body
        self.status = status
        self.captured_xhr = xhr or []


class FakeSession:
    """Records construction kwargs, every fetch call, and start/close counts."""

    instances: list["FakeSession"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fetch_calls: list[tuple[tuple, dict]] = []
        self.start_count = 0
        self.close_count = 0
        self.raise_on_index: dict[int, Exception] = {}
        FakeSession.instances.append(self)

    def start(self):
        self.start_count += 1

    def close(self):
        self.close_count += 1

    def fetch(self, *args, **kwargs):
        idx = len(self.fetch_calls)
        self.fetch_calls.append((args, kwargs))
        exc = self.raise_on_index.get(idx)
        if exc is not None:
            raise exc
        return FakePage()


@pytest.fixture()
def fake(monkeypatch):
    FakeSession.instances = []

    def factory(**kwargs):
        return FakeSession(**kwargs)

    monkeypatch.setattr(ss_mod, "_get_stealthy_session", lambda: factory)
    # inject_selector_config reaches for the real StealthyFetcher; stub it too so
    # the base (non-stealth) install stays green.
    monkeypatch.setattr(
        ss_mod,
        "_get_stealthy_fetcher",
        lambda: type("F", (), {"_generate_parser_arguments": staticmethod(lambda: {"huge_tree": True})}),
    )
    monkeypatch.setattr(ss_mod.time, "sleep", lambda *_a, **_k: None)
    return FakeSession


URLS = [f"https://example.com/{i}" for i in range(5)]


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_one_session_constructed_for_n_urls(fake):
    results = list(fetch_stealth_session(URLS))
    assert len(results) == 5
    assert len(fake.instances) == 1, "each URL built its own browser — the whole point is ONE"
    s = fake.instances[0]
    assert s.start_count == 1
    assert s.close_count == 1


def test_fetch_receives_url_only_and_no_kwargs(fake):
    """THE fail-against-naive test.

    Naive mistake: `session.fetch(url, **flat_kwargs)`. Upstream silently drops
    unknown keys — no exception, no log — so user_data_dir/headless/capture_xhr
    disappear and the batch runs cookie-less while every result looks fine.
    """
    list(fetch_stealth_session(URLS, profile="/tmp/scraperx-test-profile"))
    s = fake.instances[0]
    assert len(s.fetch_calls) == 5
    for args, kwargs in s.fetch_calls:
        assert len(args) == 1, f"fetch() got extra positional args: {args!r}"
        assert kwargs == {}, (
            f"fetch() was handed kwargs {kwargs!r} — upstream DROPS unknown keys "
            "silently. Everything must be passed at construction."
        )


def test_session_kwargs_carry_everything_including_selector_config(fake):
    list(fetch_stealth_session(URLS, profile="/tmp/scraperx-test-profile", retries=1))
    k = fake.instances[0].kwargs
    # the shared builder's curated defaults
    assert k["headless"] is True
    assert k["solve_cloudflare"] is True
    assert k["timeout"] == 90 * 1000
    # session-only keys
    assert k["retries"] == 1
    assert k["user_data_dir"].endswith("scraperx-test-profile")
    # the huge_tree gap a hand-built session would otherwise lose on 1.4 MB pages
    assert k["selector_config"]["huge_tree"] is True


def test_timeout_floor_matches_the_one_shot_path(fake):
    """Shared builder => the 5s floor cannot drift between the two surfaces."""
    list(fetch_stealth_session(["https://example.com/x"], timeout=1))
    assert fake.instances[0].kwargs["timeout"] == 5_000


# --------------------------------------------------------------------------
# Failure semantics
# --------------------------------------------------------------------------


def test_one_bad_url_does_not_abort_the_batch(fake):
    def factory(**kwargs):
        s = FakeSession(**kwargs)
        s.raise_on_index[2] = TimeoutError("Timeout 90000ms exceeded")
        return s

    import scraperx.scrapling_stealth as m

    m._get_stealthy_session = lambda: factory  # noqa: SLF001 — test seam
    results = list(fetch_stealth_session(URLS))

    assert len(results) == 5, "a single bad URL must not truncate the batch"
    assert results[2].error.startswith("TimeoutError: ")
    assert results[2].error_kind == "fetch"
    assert results[2].ok is False
    assert results[3].ok and results[4].ok, "later URLs must continue on the SAME session"


def test_code_defect_aborts_and_reraises(fake):
    """Arity drift must NEVER be reportable as 20 identical anti-bot blocks."""

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        s.raise_on_index[2] = TypeError("fetch() got an unexpected keyword argument")
        return s

    import scraperx.scrapling_stealth as m

    m._get_stealthy_session = lambda: factory  # noqa: SLF001

    seen = []
    with pytest.raises(TypeError):
        for r in fetch_stealth_session(URLS):
            seen.append(r)

    assert len(seen) == 2, "results before the defect must still reach the caller"
    assert all(r.ok for r in seen)


def test_skipped_when_budget_exhausted(fake, monkeypatch):
    """max_total_seconds is a BETWEEN-urls gate; the rest is reported, not dropped."""
    clock = iter([0.0] + [999.0] * 40)
    monkeypatch.setattr(ss_mod.time, "monotonic", lambda: next(clock))
    results = list(fetch_stealth_session(URLS, max_total_seconds=1.0))
    assert len(results) == 5, "skipped URLs must be REPORTED, never silently dropped"
    assert all(r.error_kind == "skipped" for r in results)
    assert results[0].error.startswith("BudgetExceeded: ")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_browser_closes_on_caller_exception(fake):
    with pytest.raises(RuntimeError, match="boom"):
        with stealth_session() as handle:
            handle.fetch("https://example.com/1")
            raise RuntimeError("boom")
    assert fake.instances[0].close_count == 1, "browser leaked on caller exception"


def test_browser_closes_on_generator_close(fake):
    gen = fetch_stealth_session(URLS)
    next(gen)
    next(gen)
    gen.close()
    assert fake.instances[0].close_count == 1, "browser leaked on early exit"


def test_contextlib_closing_idiom_closes(fake):
    with contextlib.closing(fetch_stealth_session(URLS)) as gen:
        for r in gen:
            if r.index == 1:
                break
    assert fake.instances[0].close_count == 1


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_max_pages_gt_one_is_rejected(fake):
    """A verified lying knob upstream — reject loudly, do not accept and ignore."""
    with pytest.raises(ValueError, match="max_pages"):
        with stealth_session(extra_kwargs={"max_pages": 5}):
            pass


def test_max_pages_one_is_accepted_and_not_forwarded(fake):
    with stealth_session(extra_kwargs={"max_pages": 1}) as handle:
        handle.fetch("https://example.com/1")
    assert "max_pages" not in fake.instances[0].kwargs


def test_scrapling_not_available_propagates(monkeypatch):
    def boom():
        raise ScraplingNotAvailable("scrapling not installed")

    monkeypatch.setattr(ss_mod, "_get_stealthy_session", boom)
    with pytest.raises(ScraplingNotAvailable):
        list(fetch_stealth_session(URLS))


def test_fetch_xhr_without_capture_xhr_raises_valueerror(fake):
    with stealth_session() as handle:
        with pytest.raises(ValueError, match="capture_xhr"):
            handle.fetch_xhr("https://example.com/1")


def test_explicit_user_data_dir_beats_profile(fake):
    with stealth_session(
        profile="/tmp/scraperx-shorthand",
        extra_kwargs={"user_data_dir": "/explicit/wins"},
    ):
        pass
    assert fake.instances[0].kwargs["user_data_dir"] == "/explicit/wins"


def test_profile_expands_user_and_creates_dir(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with stealth_session(profile="~/prof"):
        pass
    got = fake.instances[0].kwargs["user_data_dir"]
    assert "~" not in got
    assert (tmp_path / "prof").is_dir()


def test_sleep_between_applied_n_minus_one_times(fake, monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(ss_mod.time, "sleep", lambda s: calls.append(s))
    list(fetch_stealth_session(URLS, sleep_between=0.4))
    assert len(calls) == 4, "must not sleep before the first URL or after the last"


def test_stats_counts_fetches_and_failures(fake):
    def factory(**kwargs):
        s = FakeSession(**kwargs)
        s.raise_on_index[1] = TimeoutError("nope")
        return s

    import scraperx.scrapling_stealth as m

    m._get_stealthy_session = lambda: factory  # noqa: SLF001
    with stealth_session() as handle:
        handle.fetch("https://example.com/0")
        with pytest.raises(TimeoutError):
            handle.fetch("https://example.com/1")
        assert handle.stats["fetches"] == 2
        assert handle.stats["failures"] == 1


# --------------------------------------------------------------------------
# Pool wedge self-heal
# --------------------------------------------------------------------------


def test_pool_wedge_triggers_bounded_restart(fake):
    """A size-1 pool that never reclaims its page kills every remaining URL."""

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        s.raise_on_index[0] = RuntimeError("Maximum page limit (1) reached")
        return s

    import scraperx.scrapling_stealth as m

    m._get_stealthy_session = lambda: factory  # noqa: SLF001
    results = list(fetch_stealth_session(URLS[:3]))

    s = fake.instances[0]
    assert s.close_count >= 2, "wedge must close the browser"
    assert s.start_count >= 2, "...and start a fresh one"
    assert results[0].ok, "the wedged URL must be retried, not lost"
    assert all(r.ok for r in results)


def test_permanent_wedge_stops_after_bounded_restarts(fake):
    """A genuinely broken browser must not become a slow infinite loop."""

    def factory(**kwargs):
        s = FakeSession(**kwargs)
        for i in range(20):
            s.raise_on_index[i] = RuntimeError("Maximum page limit (1) reached")
        return s

    import scraperx.scrapling_stealth as m

    m._get_stealthy_session = lambda: factory  # noqa: SLF001
    results = list(fetch_stealth_session(URLS))

    assert len(results) == 5
    assert fake.instances[0].start_count <= 1 + ss_mod._MAX_RESTARTS
    assert any(r.error_kind == "fetch" for r in results)


# --------------------------------------------------------------------------
# Canary against the REAL upstream
# --------------------------------------------------------------------------


def test_upstream_sync_max_pages_still_ignored():
    """Goes RED the day upstream fixes it — telling us the guard can relax."""
    pytest.importorskip("scrapling")
    from scrapling.fetchers import StealthySession

    s = StealthySession(max_pages=5)
    assert s._config.max_pages == 5
    assert s.max_pages == 1, (
        "upstream now honours max_pages — revisit the ValueError guard in "
        "stealth_session(), it may no longer be needed."
    )
