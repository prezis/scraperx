"""Tests for scraperx.github_analyzer.trending (T11).

HTML fixtures are synthetic but structurally match github.com/trending's
shape (as of 2026-04). Two parse paths are tested — bs4 and regex —
via monkey-patching the HAS_BS4 flag.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scraperx.github_analyzer import trending
from scraperx.github_analyzer.schemas import TrendingRepo
from scraperx.github_analyzer.trending import (
    fetch_trending,
    parse_trending_html,
)

# ---------------------------------------------------------------------------
# Fixture — compact realistic snippet matching github.com/trending structure


TRENDING_HTML = """
<html><body>
<main>
  <div class="Box">
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a data-hydro-click="..." href="/yt-dlp/yt-dlp" data-view-component="true" class="Link">
          <svg aria-hidden="true" class="octicon octicon-repo"></svg>
          <span data-view-component="true" class="text-normal">yt-dlp /</span>
          yt-dlp
        </a>
      </h2>
      <p class="col-9 color-fg-muted my-1 tmp-pr-4">
        A feature-rich command-line audio/video downloader
      </p>
      <div class="f6 color-fg-muted mt-2">
        <span class="tmp-mr-3 d-inline-block ml-0 tmp-ml-0">
          <span class="repo-language-color" style="background-color: #3572A5"></span>
          <span itemprop="programmingLanguage">Python</span>
        </span>
        <a href="/yt-dlp/yt-dlp/stargazers" class="tmp-mr-3 Link Link--muted d-inline-block">
          <svg aria-label="star" role="img" class="octicon octicon-star"></svg>
          85,432
        </a>
        <a href="/yt-dlp/yt-dlp/forks" class="tmp-mr-3 Link Link--muted d-inline-block">
          <svg class="octicon octicon-repo-forked"></svg>
          6,543
        </a>
        <span class="d-inline-block float-sm-right">
          <svg class="octicon octicon-star"></svg>
          212 stars today
        </span>
      </div>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/rust-lang/rust" class="Link">
          <span class="text-normal">rust-lang /</span>
          rust
        </a>
      </h2>
      <p class="col-9 color-fg-muted my-1 tmp-pr-4">
        Empowering everyone to build reliable and efficient software.
      </p>
      <div class="f6 color-fg-muted mt-2">
        <span>
          <span itemprop="programmingLanguage">Rust</span>
        </span>
        <a href="/rust-lang/rust/stargazers" class="Link Link--muted">
          102,345
        </a>
        <span class="d-inline-block float-sm-right">
          1,234 stars this week
        </span>
      </div>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/owner/no-stars-today-repo" class="Link">
          <span class="text-normal">owner /</span>
          no-stars-today-repo
        </a>
      </h2>
      <p class="col-9 color-fg-muted">
        A repo without "stars today" suffix
      </p>
      <div class="f6">
        <a href="/owner/no-stars-today-repo/stargazers">42</a>
      </div>
    </article>
  </div>
</main>
</body></html>
"""


# ---------------------------------------------------------------------------
# parse_trending_html — bs4 path


def test_parse_bs4_happy_path():
    """With bs4 available, the three fixture rows all parse cleanly."""
    if not trending.HAS_BS4:
        pytest.skip("bs4 not installed — skipping bs4 path")
    repos = parse_trending_html(TRENDING_HTML)
    assert len(repos) == 3

    by_name = {r.full_name: r for r in repos}
    assert "yt-dlp/yt-dlp" in by_name

    r = by_name["yt-dlp/yt-dlp"]
    assert r.language == "Python"
    assert r.stars == 85432
    assert r.stars_today == 212
    assert r.url == "https://github.com/yt-dlp/yt-dlp"
    assert "downloader" in r.description

    r2 = by_name["rust-lang/rust"]
    assert r2.language == "Rust"
    assert r2.stars == 102345
    assert r2.stars_today == 1234  # "this week" variant

    r3 = by_name["owner/no-stars-today-repo"]
    assert r3.stars == 42
    assert r3.stars_today == 0


def test_parse_empty_html():
    assert parse_trending_html("") == []


def test_parse_html_with_no_rows():
    assert parse_trending_html("<html><body><main></main></body></html>") == []


# ---------------------------------------------------------------------------
# parse_trending_html — regex path (force HAS_BS4=False)


def test_parse_regex_path(monkeypatch):
    """Force the regex parser — should return the same shape."""
    monkeypatch.setattr(trending, "HAS_BS4", False)
    repos = parse_trending_html(TRENDING_HTML)
    # Regex is less precise — allow it to miss at most one of three, but
    # assert it finds at least the big fish.
    names = {r.full_name for r in repos}
    assert "yt-dlp/yt-dlp" in names
    assert "rust-lang/rust" in names


def test_parse_regex_extracts_language_and_stars(monkeypatch):
    monkeypatch.setattr(trending, "HAS_BS4", False)
    repos = parse_trending_html(TRENDING_HTML)
    by_name = {r.full_name: r for r in repos}
    r = by_name["yt-dlp/yt-dlp"]
    assert r.language == "Python"
    assert r.stars == 85432
    assert r.stars_today == 212


def test_returns_trendingrepo_instances():
    repos = parse_trending_html(TRENDING_HTML)
    assert all(isinstance(r, TrendingRepo) for r in repos)


# ---------------------------------------------------------------------------
# Parser robustness


def test_parse_garbage_html_returns_empty():
    assert parse_trending_html("<not actually html{{{") == []


def test_parse_html_without_articles():
    html = "<html><body><div>No trending today</div></body></html>"
    assert parse_trending_html(html) == []


# ---------------------------------------------------------------------------
# fetch_trending — HTTP integration


URLOPEN_PATH = "scraperx.github_analyzer.trending.urllib.request.urlopen"


def _mock_resp(html: str):
    resp = MagicMock()
    resp.read.return_value = html.encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


@patch(URLOPEN_PATH)
def test_fetch_trending_happy_path(mock_urlopen):
    mock_urlopen.return_value = _mock_resp(TRENDING_HTML)
    repos = fetch_trending()
    assert len(repos) >= 2  # bs4 gets 3, regex at least 2
    names = {r.full_name for r in repos}
    assert "yt-dlp/yt-dlp" in names


@patch(URLOPEN_PATH)
def test_fetch_trending_builds_correct_url(mock_urlopen):
    mock_urlopen.return_value = _mock_resp("<html></html>")
    fetch_trending(since="weekly", language="python", spoken_language_code="en")
    req = mock_urlopen.call_args[0][0]
    assert "/trending/python" in req.full_url
    assert "since=weekly" in req.full_url
    assert "spoken_language_code=en" in req.full_url


@patch(URLOPEN_PATH)
def test_fetch_trending_defaults_to_daily(mock_urlopen):
    mock_urlopen.return_value = _mock_resp("<html></html>")
    fetch_trending()
    req = mock_urlopen.call_args[0][0]
    assert "since=daily" in req.full_url
    # No language in path
    assert req.full_url.split("?")[0].endswith("/trending")


@patch(URLOPEN_PATH)
def test_fetch_trending_invalid_since_falls_back(mock_urlopen):
    mock_urlopen.return_value = _mock_resp("<html></html>")
    fetch_trending(since="hourly")  # Not a valid option
    req = mock_urlopen.call_args[0][0]
    assert "since=daily" in req.full_url


@patch(URLOPEN_PATH)
def test_fetch_trending_network_error_returns_empty(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("DNS")
    assert fetch_trending() == []


@patch(URLOPEN_PATH)
def test_fetch_trending_timeout_returns_empty(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError("slow")
    assert fetch_trending() == []


@patch(URLOPEN_PATH)
def test_fetch_trending_sends_user_agent_header(mock_urlopen):
    """GitHub blocks naked python-urllib UA — must set a browser-like one."""
    mock_urlopen.return_value = _mock_resp("<html></html>")
    fetch_trending()
    req = mock_urlopen.call_args[0][0]
    ua = req.headers.get("User-agent", "")
    assert "Mozilla" in ua or "scraperx" in ua.lower()


# ---------------------------------------------------------------------------
# Number parsing


def test_int_parser_handles_commas():
    from scraperx.github_analyzer.trending import _int

    assert _int("4,595") == 4595
    assert _int("1,234,567") == 1234567
    assert _int("42") == 42
    assert _int("") == 0
    assert _int("not a number") == 0
