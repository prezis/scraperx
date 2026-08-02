"""docs_crawler's shell-render pass — the first consumer of `stealth_session`.

`crawl()` has flagged React-skeleton pages as "shells" and listed them in
`_SHELLS.md` under "Pages needing playwright render" since 2026-05-03, with
nothing to drain that queue. `render_shells()` is the drain, and it is the
NAMED FIRST ADOPTER of the 1.10.0 batch API — the anti-shelfware requirement
from the design pass: ship a consumer in the same release, or the function
joins `stealth_profile` in the "installed and unreachable" category that
`tests/test_stealth_session_wiring.py` exists to indict.

Offline: `fetch_stealth_session` is monkeypatched. No browser, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scraperx import docs_crawler as dc
from scraperx.docs_crawler import CrawlResult, PageResult, render_shells

LONG = "word " * 400  # comfortably over MIN_PROSE_CHARS (500)


def _mk(tmp_path: Path, pages) -> CrawlResult:
    out = tmp_path / "crawl"
    out.mkdir(parents=True, exist_ok=True)
    res = CrawlResult(root_url="https://docs.example.com/", output_dir=out)
    for p in pages:
        res.pages.append(p)
        (out / f"{p.slug}.txt").write_text("old", encoding="utf-8")
        res.fetched += 1
        if p.is_shell:
            res.shells += 1
    return res


def _shell(slug: str, chars: int = 80, status: int = 200) -> PageResult:
    return PageResult(
        url=f"https://docs.example.com/{slug}", slug=slug, http_status=status,
        html_bytes=1000, text_chars=chars, text_words=chars // 5, is_shell=True,
    )


class FakeResult:
    def __init__(self, url, index, content="", error=None, error_kind="ok"):
        self.url = url
        self.index = index
        self.content = content
        self.http_status = 200 if content else None
        self.error = error
        self.error_kind = error_kind

    @property
    def ok(self):
        return self.error is None and bool(self.content)


@pytest.fixture()
def fake_session(monkeypatch):
    """Replace the batch API with a recorder; drive per-URL outcomes from a map."""
    state = {"urls": [], "kwargs": {}, "closed": False, "bodies": {}, "errors": {}}

    def fake(urls, timeout=90, **kw):
        urls = list(urls)
        state["urls"] = urls
        state["kwargs"] = kw

        def gen():
            try:
                for i, u in enumerate(urls):
                    if u in state["errors"]:
                        yield FakeResult(u, i, error=state["errors"][u], error_kind="fetch")
                    else:
                        yield FakeResult(u, i, content=state["bodies"].get(u, f"<html><body>{LONG}</body></html>"))
            finally:
                state["closed"] = True

        return gen()

    monkeypatch.setattr(
        "scraperx.scrapling_stealth.fetch_stealth_session", fake, raising=True
    )
    return state


def test_shells_go_through_one_batch(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a"), _shell("b"), _shell("c")])
    recovered = render_shells(res)
    assert recovered == 3
    assert fake_session["urls"] == [p.url for p in res.pages]
    assert res.shells == 0, "recovered pages must stop counting as shells"


def test_non_shell_pages_are_not_refetched(fake_session, tmp_path):
    good = PageResult(
        url="https://docs.example.com/good", slug="good", http_status=200,
        html_bytes=9000, text_chars=5000, text_words=800, is_shell=False,
    )
    res = _mk(tmp_path, [_shell("a"), good])
    render_shells(res)
    assert fake_session["urls"] == ["https://docs.example.com/a"], (
        "a page that already has prose must not be re-fetched through a browser"
    )


def test_non_200_shells_are_skipped(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a"), _shell("dead", status=404)])
    render_shells(res)
    assert fake_session["urls"] == ["https://docs.example.com/a"]


def test_recovered_text_is_written_to_disk(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a")])
    render_shells(res)
    txt = (res.output_dir / "a.txt").read_text(encoding="utf-8")
    assert txt != "old"
    assert len(txt) >= dc.MIN_PROSE_CHARS
    assert res.pages[0].is_shell is False


def test_render_that_is_no_better_keeps_the_original(fake_session, tmp_path):
    """A 'fix' that silently replaces good data with worse is not a fix."""
    page = _shell("a", chars=300)
    res = _mk(tmp_path, [page])
    fake_session["bodies"][page.url] = "<html><body>tiny</body></html>"
    recovered = render_shells(res)
    assert recovered == 0
    assert (res.output_dir / "a.txt").read_text(encoding="utf-8") == "old"
    assert page.text_chars == 300, "original char count must be preserved"
    assert page.is_shell is True


def test_failed_page_records_error_and_others_continue(fake_session, tmp_path):
    a, b = _shell("a"), _shell("b")
    res = _mk(tmp_path, [a, b])
    fake_session["errors"][a.url] = "TimeoutError: Timeout 90000ms exceeded"
    recovered = render_shells(res)
    assert recovered == 1, "one bad page must not sink the batch"
    assert a.error.startswith("TimeoutError: ")
    assert a.is_shell is True
    assert b.is_shell is False


def test_generator_is_closed(fake_session, tmp_path):
    """contextlib.closing is required — a leaked generator is a leaked browser."""
    res = _mk(tmp_path, [_shell("a")])
    render_shells(res)
    assert fake_session["closed"] is True


def test_profile_defaults_to_one_dir_per_host(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a")])
    render_shells(res)
    assert fake_session["kwargs"]["profile"].endswith("profiles/docs.example.com")


def test_explicit_profile_wins(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a")])
    render_shells(res, profile="/tmp/custom-profile")
    assert fake_session["kwargs"]["profile"] == "/tmp/custom-profile"


def test_max_shells_caps_the_batch(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a"), _shell("b"), _shell("c")])
    render_shells(res, max_shells=2)
    assert len(fake_session["urls"]) == 2


def test_no_shells_is_a_noop(fake_session, tmp_path):
    good = PageResult(
        url="https://docs.example.com/good", slug="good", http_status=200,
        html_bytes=9000, text_chars=5000, text_words=800, is_shell=False,
    )
    res = _mk(tmp_path, [good])
    assert render_shells(res) == 0
    assert fake_session["urls"] == [], "must not open a browser for zero work"


def test_digest_is_rewritten_after_recovery(fake_session, tmp_path):
    res = _mk(tmp_path, [_shell("a")])
    render_shells(res)
    shells_md = (res.output_dir / "_SHELLS.md").read_text(encoding="utf-8")
    assert "https://docs.example.com/a" not in shells_md, (
        "a recovered page must leave the render queue, or the queue lies"
    )
