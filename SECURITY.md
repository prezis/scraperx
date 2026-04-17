# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.3.x   | ✅        |
| < 1.3   | ❌        |

## Reporting a vulnerability

If you discover a security issue, please **do NOT open a public issue**. Instead:

1. Email the maintainer directly (see GitHub profile for contact), OR
2. Use [GitHub's private vulnerability reporting](https://github.com/prezis/scraperx/security/advisories/new) (requires GitHub account)

Include:
- Affected version(s)
- Reproduction steps (minimal)
- Impact assessment
- Suggested mitigation (if any)

Expect a response within **72 hours**. We'll work with you on disclosure timing — typical is 30-90 days depending on severity.

## Security-sensitive code paths

ScraperX fetches remote URLs and parses untrusted input. Known attack surfaces:

- **SSRF** in `AvatarMatcher._fetch_image_bytes` — mitigated by `pbs.twimg.com` host allowlist + 2MB size cap + content-type check
- **SSRF** in `video_discovery.discover_videos` — scope: user provides page URL, so it's trusted input, but fetched content isn't. HTML parsing is regex/bs4 — no code execution.
- **Subprocess injection** via `yt-dlp` invocations in `VimeoScraper` + `YouTubeScraper` — URLs passed as argv arrays (no shell), arguments are constructed from parsed URL components (no raw user input).
- **JSON parsing** from FxTwitter/vxTwitter responses — standard `json.loads`, no eval.
- **SQLite** — parameterized queries throughout, no string-concat SQL.

## Out of scope

- Issues in upstream services (FxTwitter, vxTwitter, Twitter/X, YouTube, Vimeo)
- Rate limiting by external services (use your own backoff)
- Your own misuse of the library against targets you don't own

## Dependencies

Run `pip list` to audit installed versions. Optional extras:
- `imagehash`, `Pillow` — image processing (CVEs historically around image parsing)
- `beautifulsoup4` — HTML parsing
- `faster-whisper` — ML model loading

Pin dependencies in your own `requirements.txt` / `poetry.lock`.
