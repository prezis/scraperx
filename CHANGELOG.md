# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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
