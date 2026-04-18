"""Tests for scraperx.github_analyzer.github_api (T3).

Never hits the real network — urlopen is mocked at the module level. The
target module is stdlib-only, so the mock surface is `urllib.request.urlopen`
imported inside github_api.
"""

from __future__ import annotations

import email.message
import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scraperx.github_analyzer.github_api import (
    GithubAPI,
    GithubAPIError,
    RateLimitExceededError,
    RepoNotFoundError,
)

# ---------------------------------------------------------------------------
# Mock helpers


def _mock_response(
    body,
    rate_remaining: str = "59",
    rate_reset: str = "1234567890",
    rate_limit: str = "60",
):
    """Build a MagicMock that quacks like an urlopen() context-manager response."""
    if isinstance(body, (dict, list)):
        raw = json.dumps(body).encode()
    elif isinstance(body, str):
        raw = body.encode()
    else:
        raw = body  # bytes

    headers = {
        "X-RateLimit-Remaining": rate_remaining,
        "X-RateLimit-Reset": rate_reset,
        "X-RateLimit-Limit": rate_limit,
    }

    resp = MagicMock()
    resp.read.return_value = raw
    resp.headers.getheader = lambda name, default=None: headers.get(name, default)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(code: int, msg: str = "err", headers: dict | None = None) -> urllib.error.HTTPError:
    """Build a real HTTPError with attached headers (used for rate-limit + 404)."""
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError(
        url="https://api.github.com/x",
        code=code,
        msg=msg,
        hdrs=hdrs,
        fp=None,
    )


# ---------------------------------------------------------------------------
# Authentication


def test_authenticated_false_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    api = GithubAPI()
    assert api.authenticated is False


def test_authenticated_true_with_explicit_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    api = GithubAPI(token="abc")
    assert api.authenticated is True
    assert api.token == "abc"


def test_authenticated_true_via_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    api = GithubAPI()
    assert api.authenticated is True
    assert api.token == "env-tok"


def test_explicit_token_wins_over_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    api = GithubAPI(token="arg-tok")
    assert api.token == "arg-tok"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_authorization_header_present_when_authed(mock_urlopen, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_urlopen.return_value = _mock_response({"id": 1})
    api = GithubAPI(token="xyz")
    api.get_repo("o", "r")
    sent_req = mock_urlopen.call_args[0][0]
    assert sent_req.headers.get("Authorization") == "Bearer xyz"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_authorization_header_absent_when_unauthed(mock_urlopen, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_urlopen.return_value = _mock_response({"id": 1})
    api = GithubAPI()
    api.get_repo("o", "r")
    sent_req = mock_urlopen.call_args[0][0]
    assert sent_req.headers.get("Authorization") is None


# ---------------------------------------------------------------------------
# Successful calls — URL, headers, parsing, rate-state absorption


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_repo_success(mock_urlopen, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_urlopen.return_value = _mock_response(
        {"id": 42, "name": "yt-dlp", "stargazers_count": 999},
        rate_remaining="55",
        rate_reset="2000000000",
        rate_limit="60",
    )
    api = GithubAPI()
    result = api.get_repo("yt-dlp", "yt-dlp")

    assert result == {"id": 42, "name": "yt-dlp", "stargazers_count": 999}

    # URL
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/yt-dlp/yt-dlp"

    # Standard headers
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-github-api-version"] == "2022-11-28"  # urllib lowercases
    assert req.headers["User-agent"] == "scraperx/github-analyzer"

    # Rate state absorbed
    assert api.rate_remaining == 55
    assert api.rate_reset == 2000000000
    assert api.rate_limit == 60


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_contributors_url_with_per_page(mock_urlopen):
    mock_urlopen.return_value = _mock_response([{"login": "alice", "contributions": 10}])
    api = GithubAPI(token="t")
    result = api.get_contributors("o", "r", per_page=5)
    assert result == [{"login": "alice", "contributions": 10}]

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/contributors?per_page=5"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_top_forks_has_sorted_querystring(mock_urlopen):
    """Params should be URL-encoded with sorted keys for determinism."""
    mock_urlopen.return_value = _mock_response([])
    api = GithubAPI(token="t")
    api.get_top_forks("o", "r", per_page=5)
    req = mock_urlopen.call_args[0][0]
    # Alphabetical: per_page, sort  →  per_page=5&sort=stargazers
    assert req.full_url == "https://api.github.com/repos/o/r/forks?per_page=5&sort=stargazers"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_advisories_default_per_page(mock_urlopen):
    mock_urlopen.return_value = _mock_response([])
    api = GithubAPI(token="t")
    api.get_advisories("o", "r")
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/security-advisories?per_page=10"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_readme_endpoint(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"content": "aGVsbG8=", "encoding": "base64"})
    api = GithubAPI(token="t")
    api.get_readme("o", "r")
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/readme"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_workflows_endpoint(mock_urlopen):
    mock_urlopen.return_value = _mock_response({"total_count": 2, "workflows": []})
    api = GithubAPI(token="t")
    api.get_workflows("o", "r")
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/actions/workflows"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_recent_commits_endpoint(mock_urlopen):
    mock_urlopen.return_value = _mock_response([{"sha": "abc"}])
    api = GithubAPI(token="t")
    api.get_recent_commits("o", "r", per_page=3)
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/commits?per_page=3"


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_get_releases_endpoint(mock_urlopen):
    mock_urlopen.return_value = _mock_response([])
    api = GithubAPI(token="t")
    api.get_releases("o", "r", per_page=7)
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/o/r/releases?per_page=7"


# ---------------------------------------------------------------------------
# Error paths


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_404_becomes_repo_not_found(mock_urlopen):
    mock_urlopen.side_effect = _http_error(404, "Not Found")
    api = GithubAPI(token="t")
    with pytest.raises(RepoNotFoundError):
        api.get_repo("nobody", "nope")


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_403_with_rate_zero_becomes_rate_limit_error(mock_urlopen):
    reset = int(time.time()) + 3600
    mock_urlopen.side_effect = _http_error(
        403,
        "rate limited",
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
            "X-RateLimit-Limit": "60",
        },
    )
    api = GithubAPI()
    with pytest.raises(RateLimitExceededError) as exc:
        api.get_repo("o", "r")
    assert exc.value.reset_at == float(reset)
    # State absorbed from error headers too
    assert api.rate_remaining == 0
    assert api.rate_reset == reset


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_500_becomes_generic_github_api_error(mock_urlopen):
    mock_urlopen.side_effect = _http_error(500, "internal server error")
    api = GithubAPI(token="t")
    with pytest.raises(GithubAPIError) as exc:
        api.get_repo("o", "r")
    # Not one of the more specific subclasses
    assert not isinstance(exc.value, (RepoNotFoundError, RateLimitExceededError))
    assert "500" in str(exc.value)


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_urlerror_becomes_github_api_error(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("DNS fail")
    api = GithubAPI(token="t")
    with pytest.raises(GithubAPIError) as exc:
        api.get_repo("o", "r")
    assert "DNS fail" in str(exc.value)


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_invalid_json_becomes_github_api_error(mock_urlopen):
    resp = MagicMock()
    resp.read.return_value = b"not json at all {{{"
    resp.headers.getheader = lambda name, default=None: None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    mock_urlopen.return_value = resp

    api = GithubAPI(token="t")
    with pytest.raises(GithubAPIError) as exc:
        api.get_repo("o", "r")
    assert "Invalid JSON" in str(exc.value)


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_empty_body_becomes_github_api_error(mock_urlopen):
    resp = MagicMock()
    resp.read.return_value = b""
    resp.headers.getheader = lambda name, default=None: None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    mock_urlopen.return_value = resp

    api = GithubAPI(token="t")
    with pytest.raises(GithubAPIError) as exc:
        api.get_repo("o", "r")
    assert "Empty" in str(exc.value)


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_scalar_json_root_rejected(mock_urlopen):
    """Response of bare number / null / string is not a valid GitHub shape."""
    mock_urlopen.return_value = _mock_response(b"42")  # literal integer
    api = GithubAPI(token="t")
    with pytest.raises(GithubAPIError):
        api.get_repo("o", "r")


# ---------------------------------------------------------------------------
# Rate-limit pre-flight


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_preflight_blocks_when_exhausted_future_reset(mock_urlopen):
    api = GithubAPI(token="t")
    api.rate_remaining = 0
    api.rate_reset = int(time.time()) + 500

    with pytest.raises(RateLimitExceededError):
        api.get_repo("o", "r")
    mock_urlopen.assert_not_called()


@patch("scraperx.github_analyzer.github_api.urllib.request.urlopen")
def test_preflight_allows_when_reset_passed(mock_urlopen):
    """Once reset is in the past, rate_exhausted flips False even if remaining still 0."""
    api = GithubAPI(token="t")
    api.rate_remaining = 0
    api.rate_reset = int(time.time()) - 10  # in the past
    assert api.rate_exhausted is False

    mock_urlopen.return_value = _mock_response({"id": 1})
    api.get_repo("o", "r")
    mock_urlopen.assert_called_once()


def test_initial_rate_state_is_unknown():
    api = GithubAPI(token="t")
    assert api.rate_remaining == -1
    assert api.rate_reset == 0
    assert api.rate_limit == -1
    assert api.rate_exhausted is False
