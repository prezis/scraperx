# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.2] — 2026-04-18

Telemetry: `--log-verdict` flag + agree/disagree corpus builder for calibrating v1.5.0.

### Added

- **`scraperx/github_analyzer/telemetry.py`** — `log_verdict(report, feedback=None)` appends one JSONL event to `~/.scraperx/verdicts.jsonl`. Fields: `timestamp`, `repo`, `url`, `overall`, `sub_scores` (all 4), `mentions_count`, `warnings_count`, `warnings[:5]`, `scraperx_version`, `feedback`. Returns `True/False` — never raises. Creates `~/.scraperx/` automatically.
- **`prompt_and_log_verdict(report)`** — interactive wrapper for CLI use. Logs the scoring event first (feedback-free), then prompts `Agree? [y/n/<reason>] (Enter to skip)` on stderr (safe for `--json` mode). User response coerced: `y/yes/agree/tak → "agree"`, `n/no/disagree/nie → "disagree"`, anything else stored as free-text.  Non-TTY stdin (pipes) is detected and silently skipped.
- **`scraperx github --log-verdict`** — new CLI flag. Fires `prompt_and_log_verdict` after output so it never delays the report rendering.
- **`_normalise_feedback(raw)`** — canonical alias coercion. Handles Polish (`tak`/`nie`) and common informal aliases (`ok`, `yep`, `nope`).
- **44 new tests** in `tests/test_github_telemetry.py` covering all feedback aliases, JSONL field correctness, multi-event append, warning cap, permission-error graceful return, non-TTY auto-skip, and timestamp ISO-8601-Z round-trip.

### Changed

- **`__version__` bumped to `1.4.2`** (1.4.1 was the metadata-enrichment commit; 1.4.2 adds telemetry).
- **`cli.py`** imports `prompt_and_log_verdict` from `telemetry`; `log_verdict` import removed (unused at CLI level — CLI always uses the interactive wrapper).

## [1.4.0] — 2026-04-18

Major feature release: deep GitHub repository trust analysis with scored verdicts, community mention aggregation across 6 dedicated platforms + 6 generic sites, GitHub Trending scraper, and graceful GPU-backed synthesis.

### Added — `scraperx.github_analyzer` module

- **`analyze_github_repo(url)` / `GithubAnalyzer`** — end-to-end pipeline: REST metadata → scoring → community mentions → optional web-search layer → LLM-synthesized 3-bullet verdict with inline citations + 0-100 overall score. Dependency-injected at every external call (GitHub token, SQLite cache, web-search fn, LLM fn) so the whole thing is unit-testable without a network.
- **`github_api.py`** — stdlib-only GitHub REST client. 8 endpoints: `get_repo`, `get_contributors`, `get_recent_commits`, `get_releases`, `get_top_forks`, `get_readme`, `get_workflows`, `get_advisories` (GHSA). Rate-limit header absorption + fail-fast pre-flight when the window is exhausted. Exceptions: `GithubAPIError`, `RepoNotFoundError`, `RateLimitExceededError(reset_at)`.
- **`scoring.py`** — 4 pure heuristics (0-100 int each): `bus_factor_score` (k-at-50% contribution share), `momentum_score` (commits + star delta over 90 days), `health_score` (archived / license / issue & fork ratios), `readme_quality_score` (length + heading + code + link + install keyword). Graceful on malformed input — never raises.
- **`mentions/`** — 6 Tier-A platform adapters: `hn` (Algolia HN Search), `reddit` (`/search.json`), `stackoverflow` (StackExchange API 2.3), `devto` (dev.to articles + client-side slug filter), `arxiv` (Atom XML, `xml.etree`), `pwc` (Papers With Code). Every adapter: common contract (never raise, return `[]` on any error, normalize to `ExternalMention`, cache hit/miss via SQLite). `ALL_SOURCES` registry for iteration.
- **`semantic.py`** — Tier-B generic web search. Takes an injected `web_search_fn` (matches `local_web_search` MCP signature), composes `(site:lobste.rs OR site:substack.com …) "owner/repo"` queries, filters hits to an allowlist of hosts (Lobsters, Substack, Medium, Product Hunt, Bluesky, LinkedIn). Graceful degradation when `web_search_fn` is None.
- **`trending.py`** — `fetch_trending(since, language, spoken_language_code)` scrapes github.com/trending (no public API). Dual parser: BeautifulSoup preferred, regex fallback when bs4 unavailable (same optional-bs4 pattern as `video_discovery.py`). Returns `list[TrendingRepo]`. Browser User-Agent required — GitHub blocks naked urllib.
- **`synthesis.py`** — populated report → `trust.overall` + `trust.rationale` + `verdict_markdown`. Dependency-injected `local_llm_fn` (qwen3:4b fast, qwen3.5:27b on `deep=True`). Robust JSON extraction via brace-counter (qwen sometimes wraps its output in prose or code fences). Heuristic fallback (sub-score weighted average) when the LLM is unreachable or returns unparseable output.
- **`schemas.py`** — 7 stdlib dataclasses: `GithubReport`, `RepoTrustScore`, `ContributorInfo`, `ForkInfo`, `ExternalMention`, `SecurityAdvisory` (GHSA), `TrendingRepo`. No Pydantic — matches scraperx core discipline. Full JSON serialization via `to_dict()`.

### Added — CLI

- **`scraperx github OWNER/REPO [--json] [--deep] [--no-mentions] [--no-cache]`** — produces markdown trust report (or JSON dump with `--json`). Accepts shorthand `owner/repo`, full URL, `.git` suffix, SSH form, or sub-path URLs. Invalid URL → exit 2 with stderr message.
- **`scraperx trending [--since daily|weekly|monthly] [--lang python] [--spoken en] [--limit 25] [--json]`** — lists github.com/trending. Defaults to daily + all languages (per Q2 handoff decision).

### Added — SQLite cache

- **3 new tables** in `social_db.py` (share the existing `~/.scraperx/social.db`): `github_repo_cache` (composite key `(full_name, kind)`, per-kind TTL: repo 24h, commits 6h, etc.), `github_fork_cache` (6h TTL), `github_mentions_cache` (4h TTL). Composite-kind design means one table covers repo / contributors / commits / releases / readme / workflows / issues / advisories without schema churn.
- **New SocialDB methods**: `save_repo_cache`/`get_repo_cache`, `save_fork_cache`/`get_fork_cache`, `save_mentions_cache`/`get_mentions_cache`, `purge_expired_github_cache`. Query-hash normalisation so `"Yt-Dlp"` and `"  yt-dlp  "` collide. Empty results NOT cached — lets transient errors retry next call.

### Added — top-level exports

- **`scraperx` package** re-exports: `GithubAnalyzer`, `GithubReport`, `InvalidRepoUrlError`, `analyze_github_repo`, `parse_github_repo_url`.

### Added — Tests

- **236 new tests** covering: URL parsing across 6 shapes, schema round-trip, SQLite cache (hit/miss/TTL/purge/case-insensitivity), GitHub REST (auth/404/403-rate-limit/URLError/invalid-JSON/pre-flight), scoring (34 parametrized heuristic cases), mention adapters (happy + error + cache per platform), semantic layer (graceful degradation + site filter + subdomain acceptance), trending (dual-parser + URL building), synthesis (JSON extraction + heuristic fallback + LLM happy + 5 error paths), CLI end-to-end (argv dispatch + flags + `__main__` routing), full-pipeline e2e integration (happy + partial-failure + 404-short-circuit + skip-mentions). Total suite: **441 passing, 0 ruff warnings**.

### Changed

- **`pyproject.toml`**: `description` extended to mention GitHub analyzer; `keywords` +5 entries.
- **`README.md`**: new top-level feature section (see below).

## [Unreleased-prior-to-1.4.0]

### Fixed
- **`VimeoScraper.get_metadata()` — fallback to player config when oEmbed 404s.** Vimeo's oEmbed endpoint has been unreliable since late 2025 (returns 404 on live queries even for public videos). `get_metadata` now tries oEmbed first and transparently falls back to `player.vimeo.com/video/{id}/config` for durable metadata (title, author, duration, thumbnail). Result dict now includes a `source` field (`"oembed"` | `"player_config"`). Only raises if BOTH endpoints fail.

## [1.3.0] — 2026-04-17

Major feature release: Vimeo, video discovery across 6 providers, perceptual-hash impersonation detection, and formal thread authenticity verification.

### Added

- **Tweet dataclass +21 fields**: `is_reply`, `in_reply_to_tweet_id`, `in_reply_to_handle`, `in_reply_to_author_id`, `is_quote`, `conversation_id`, `created_at`, `created_timestamp`, `lang`, `possibly_sensitive`, `source_client`, `is_note_tweet`, `is_community_note_marked`, `author_verified`, `author_verified_type`, `author_affiliation`, `author_followers`, `author_following`, `author_joined`, `author_protected`, `is_pinned`. All surfaced from data FxTwitter / vxTwitter / Twitter syndication already returned but scraperx previously dropped.
- **`authenticity.py`** — new module. `ThreadAuthenticity` dataclass + `check_thread_authenticity(thread)` function implementing the formal 4-property verification (`same_conversation`, `single_author` by numeric `author_id` not handle, `chronological`, `no_interpolation`) with graceful degradation when fields are missing. Advisory `has_branches` and `root_deleted` flags.
- **`avatar_matcher.py`** — new module. `AvatarMatcher` class with perceptual hash (pHash 8×8 via `imagehash`), SSRF-safe fetch (`pbs.twimg.com` host allowlist, 2MB size cap, `image/*` content-type check), SQLite cache with 30-day TTL. Graceful fallback to content SHA256 when `imagehash` not installed.
- **`VerifiedAvatarRegistry`** — rolling window of last 5 avatar hashes per handle, tolerates intentional avatar changes. `check_impersonation()` returns `(is_match, best_hamming_distance, matched_handle)`. Cross-handle match exposes impersonation signal (suspicious handle wearing verified account's avatar).
- **`vimeo_scraper.py`** — new module. `VimeoScraper` mirroring `YouTubeScraper` API: `get_metadata(url)` via Vimeo oEmbed, `get_transcript(url, force_whisper=, max_duration_minutes=, referer=)` via `player.vimeo.com/video/{id}/config` JSON — uses creator-uploaded `text_tracks` VTT when available, falls back to yt-dlp audio + `faster-whisper` / `whisper` CLI. Supports embed-domain-locked videos via `referer=` kwarg.
- **`video_discovery.py`** — new module. `discover_videos(page_url, html=None)` scans arbitrary webpages for embedded videos across 6 providers (YouTube, Vimeo, Wistia, JWPlayer, Brightcove, HTML5). Detects iframes, `og:video` meta, JSON-LD `VideoObject`, Wistia JS div-embeds. Optional BeautifulSoup; falls back to regex. Deduplicates by `(provider, id)`.
- **`fetch_any_video_transcript(url_or_page)`** — top-level dispatcher. Direct video URL → appropriate scraper; generic webpage → `discover_videos` + recurse.
- **CLI**: auto-detects `vimeo.com` URLs and routes to `VimeoScraper`. New `scraperx discover URL` subcommand prints detected video embeds.
- **Optional extras** in `pyproject.toml`: `[vision]` (imagehash, Pillow) and `[video-discovery]` (beautifulsoup4).

### Changed

- `impersonation.check_impersonation()` gained optional `avatar_matcher=None` kwarg. Backward compatible — default `None` preserves the legacy URL-string comparison.
- Exports in `scraperx/__init__.py`: 14 new names added to `__all__`.

### Fixed

- Version drift: `scraperx/__init__.py::__version__` and `pyproject.toml::version` now both report `1.3.0` (previously `1.2.0` vs `1.0.0` — likely a half-skipped bump).

### Tests

- 212 tests passing, zero regressions across all 3 feature additions. All new code paths are covered by existing integration + smoke tests; dedicated unit tests for the new modules are tracked for a follow-up release.

## [1.2.0] and earlier

Older history — see `git log` for pre-1.3.0 details. Highlights:

- **1.2.0** — stable fallback chain (FxTwitter → vxTwitter → yt-dlp → oEmbed), profile scraping, thread reconstruction (walk-up + syndication walk-down), YouTube transcription
- **1.1.x** — added profile + thread modules; token extraction
- **1.0.0** — initial public release: X/Twitter tweet scraping, YouTube transcription

[Unreleased]: https://github.com/prezis/scraperx/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/prezis/scraperx/compare/v1.2.0...v1.3.0
