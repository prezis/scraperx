# ScraperX

**Universal scraping + video intelligence, no API keys required.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/prezis/scraperx/actions/workflows/ci.yml/badge.svg)](https://github.com/prezis/scraperx/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.11.0-informational.svg)](CHANGELOG.md)

ScraperX fetches social-media posts, transcribes videos, and verifies authenticity — without API keys or account credentials. Built on stdlib, with optional extras for perceptual image hashing, web scraping helpers, and GPU-accelerated speech-to-text.

> **Status: beta.** Core functionality is stable — **923 tests, fully offline** (as of 1.9.0 the
> suite no longer talks to the internet; it used to, silently, and that had been failing CI since
> 2026-07-17). Newest in **1.11.0**: **the cascade LEARNS which leg works, per host** — on a
> walled site it stops re-trying the three legs that always 403 and goes straight to the one that
> works. 1.10.0 added `stealth_session()` (N URLs on ONE browser, ~50% faster, measured); 1.9.0
> made the *cookie* persist. 1.8.0 brought the Cloudflare-bypass leg, no-login Reddit, frame-OCR
> for silent video, a documentation crawler, and the 403 fingerprint self-audit. See the
> [CHANGELOG](CHANGELOG.md).

---

## What it does

- **X / Twitter** — tweets, threads, profiles, search. Fallback chain (FxTwitter → vxTwitter → yt-dlp → oEmbed) keeps data flowing when any single endpoint breaks.
- **YouTube transcription** — auto-captions, with fallback to `faster-whisper` (GPU) or `whisper` (CLI).
- **Vimeo transcription** (NEW in 1.3.0) — `oembed` + player config + creator-uploaded VTT tracks, falling back to yt-dlp + whisper.
- **Video discovery** (NEW) — scan any webpage for embedded videos across 6 providers (YouTube, Vimeo, Wistia, JWPlayer, Brightcove, HTML5).
- **Thread authenticity** (NEW) — formal 4-property check on a reconstructed thread: `same_conversation`, `single_author` (numeric ID), `chronological`, `no_interpolation`.
- **Impersonation detection** (NEW) — perceptual-hash avatar matcher (pHash 8×8) with SQLite cache + rolling-window registry. Catches scammers who re-upload a victim's avatar under a typosquat handle.
- **Scam content detection** — crypto-giveaway phrases, wallet addresses, shortener domains, emoji spam.
- **Token extraction** — `$CASHTAG` mentions + known Solana tokens.
- **GitHub deep analyzer** (NEW in 1.4.0) — paste any `owner/repo` URL, get a 0–100 trust verdict with 3-bullet rationale, community mention aggregation across HN / Reddit / StackOverflow / dev.to / arXiv / Papers With Code, notable forks, security advisories (GHSA), and sub-scores for bus factor / momentum / health / README quality. Optional LLM synthesis via local GPU.
- **GitHub trending** (NEW in 1.4.0) — `scraperx trending` lists github.com/trending for daily / weekly / monthly windows with language filters.
- **SQLite persistence** — tweets, profiles, mentions, avatar hashes, search cache, GitHub repo/fork/mention caches with per-kind TTL.

Why no API keys? The official APIs are expensive, rate-limited, and unstable. ScraperX leans on public endpoints (oEmbed, FxTwitter, vxTwitter, syndication, yt-dlp) with no auth wall.

---

## Install

```bash
pip install git+https://github.com/prezis/scraperx.git
```

Not yet on PyPI — install from GitHub.

Or clone + editable:

```bash
git clone https://github.com/prezis/scraperx.git
cd scraperx
pip install -e .
```

### Optional extras

| Extra | Installs | Enables |
|---|---|---|
| `[vision]` | `imagehash>=4.3`, `Pillow>=10.0` | Perceptual-hash avatar matching (falls back to SHA256 when absent) |
| `[video-discovery]` | `beautifulsoup4>=4.12` | More robust HTML parsing for `discover_videos` |
| `[whisper]` | `faster-whisper>=1.0` | GPU-accelerated transcription (4× faster than openai-whisper on CPU) |
| `[twscrape]` | `twscrape>=0.12` | Optional account-backed twscrape backend |
| `[stealth]` | `scrapling[fetchers]>=0.4.12` | Cloudflare-Turnstile / Interstitial / JS-challenge bypass via patchright Chromium + browserforge fingerprints. Adds the `scrapling_stealth` cascade leg **and** persistent sessions (`stealth_profile=`). After install, run `scrapling install` to fetch the patched Chromium binary. Keep this current — a stale browser fingerprint is itself a detection signal. |

Combined install:

```bash
pip install "scraperx[vision,video-discovery,whisper] @ git+https://github.com/prezis/scraperx.git"
```

System tools (optional): `yt-dlp` for audio download on YouTube/Vimeo whisper path; `whisper` CLI as fallback when `faster-whisper` not installed.

---

## Quick start

### CLI

```bash
scraperx https://x.com/user/status/123456789       # scrape a tweet
scraperx https://x.com/user/status/123 --thread    # full thread
scraperx @elonmusk                                 # profile
scraperx search "Meteora DLMM" --limit 10          # search (DDG + FxTwitter)
scraperx https://youtube.com/watch?v=dQw4w9WgXcQ   # YouTube transcript
scraperx https://vimeo.com/76979871                # Vimeo transcript
scraperx discover https://some-company.com/tour    # find embedded videos
```

### Python

```python
from scraperx import XScraper, VimeoScraper, discover_videos, check_thread_authenticity

scraper = XScraper()
tweet = scraper.get_tweet("https://x.com/user/status/1234567890")
print(f"{tweet.author_handle}: {tweet.text}")
print(f"  reply={tweet.is_reply}  quote={tweet.is_quote}")
print(f"  author verified={tweet.author_verified} ({tweet.author_verified_type})")
print(f"  joined={tweet.author_joined}  followers={tweet.author_followers}")

vimeo = VimeoScraper()
result = vimeo.get_transcript("https://vimeo.com/76979871")
print(result.transcript[:500])

refs = discover_videos("https://some-blog.example.com/post")
for v in refs:
    print(f"{v.provider}: {v.canonical_url}")
```

---

## Architecture

```
                              URL or @handle or query
                                      │
                                      ▼
                          ┌───────────────────────────┐
                          │   __main__.py CLI router  │
                          └───────────────────────────┘
            ┌────────┬─────────┬─────────┬─────────┬──────────┬──────────┐
            ▼        ▼         ▼         ▼         ▼          ▼          ▼
         Tweet   Profile    Thread   YouTube    Vimeo    Discover    Search
            │        │         │         │         │          │          │
       scraper.py profile thread.py yt_sc..  vimeo_sc..  disco...  search.py
            │        │         │         │         │          │          │
       Fallback Fx+synd   walk up  captions   oEmbed +  regex+bs4  DDG+Fx
        chain   timeline  (Fx) +   → whisper  config     scan        enrich
       ┌──────┐            walk                JSON
       │ Fx   │            down                 │
       │ vx   │          (synd+DDG)             ▼
       │yt-dlp│                             text_tracks
       │oembed│                             → whisper
       └──────┘
                  \     │      /          \     /         │
                   ▼    ▼     ▼            ▼   ▼          │
                  ┌────────────────────────────┐          │
                  │   impersonation.py         │          │
                  │  • handle typosquat        │          │
                  │  • scam content regex      │          │
                  │  • AvatarMatcher (pHash)   │          │
                  │  • VerifiedAvatarRegistry  │          │
                  └────────────────────────────┘          │
                                │                         │
                                ▼                         │
                       ┌──────────────────┐               │
                       │  authenticity.py │               │
                       │  4-property check│               │
                       └──────────────────┘               │
                                │                         │
                                ▼                         ▼
                        ┌──────────────────────────────────┐
                        │  social_db.py (SQLite)           │
                        │  tweets · profiles · mentions    │
                        │  avatar_hash · verified_avatars  │
                        └──────────────────────────────────┘
```

---

## Feature guide

### 1. Tweet scraping — 21 new fields in 1.3.0

```python
from scraperx import XScraper

scraper = XScraper()
t = scraper.get_tweet("https://x.com/user/status/123")

# Core (existed pre-1.3.0)
t.id, t.text, t.author_handle, t.likes, t.retweets, t.views, t.media_urls, t.quoted_tweet

# NEW — reply/quote/thread context
t.is_reply, t.in_reply_to_tweet_id, t.in_reply_to_handle, t.in_reply_to_author_id
t.is_quote, t.conversation_id

# NEW — temporal + locale
t.created_at, t.created_timestamp, t.lang, t.possibly_sensitive, t.source_client

# NEW — community/note flags
t.is_note_tweet, t.is_community_note_marked

# NEW — author trust signals
t.author_verified, t.author_verified_type  # "blue" | "business" | "government"
t.author_affiliation  # org-linked badge dict
t.author_followers, t.author_following
t.author_joined       # RFC 2822 — account age, strong scam signal
t.author_protected, t.is_pinned
```

All backward compatible — every new field has a safe default.

### 2. Thread reconstruction + authenticity

```python
from scraperx import get_thread, check_thread_authenticity

thread = get_thread("https://x.com/user/status/123456")
for t in thread.all_tweets:
    print(t.text)

auth = check_thread_authenticity(thread)
print(f"Authentic: {auth.is_authentic}")
print(f"  same conversation: {auth.same_conversation}")
print(f"  single author:     {auth.single_author}")
print(f"  chronological:     {auth.chronological}")
print(f"  no interpolation:  {auth.no_interpolation}")
if auth.reasons:
    for r in auth.reasons:
        print(f"  ↳ {r}")
```

**Formal authenticity properties:**
1. `same_conversation` — all tweets share the root's `conversation_id`
2. `single_author` — all tweets share the root's numeric `author_id` (handles are mutable; IDs are not)
3. `chronological` — `created_timestamp` non-decreasing along the reply chain
4. `no_interpolation` — every `in_reply_to_tweet_id` resolves within the thread set

**Advisory flags:** `has_branches` (author replied twice to the same parent — path, not tree), `root_deleted` (conversation_id set but root content missing).

**Graceful degradation** when the API omits a field: `missing_fields` tells you why, and the checker falls back (`author_handle` if numeric ID missing; tweet-ID ordering if timestamps missing).

### 3. Impersonation detection — perceptual avatar hashing

Scammers copy a verified account's avatar and re-upload it — different URL, same pixels. URL-string comparison is useless. `AvatarMatcher` uses pHash 8×8 (64-bit perceptual hash via DCT) with Hamming-distance thresholds.

```python
from scraperx import AvatarMatcher, VerifiedAvatarRegistry

matcher = AvatarMatcher()
registry = VerifiedAvatarRegistry()

# Seed the registry with known-good avatars
registry.record_avatar("elonmusk", "https://pbs.twimg.com/profile_images/...", matcher)

# A reply from @elonmuskk (typosquat) claiming to be Elon
is_match, hamming, matched = registry.check_impersonation(
    claimed_handle="elonmuskk",
    avatar_url="https://pbs.twimg.com/profile_images/NEW_URL.jpg",
    matcher=matcher,
)

if not is_match and matched and matched != "elonmuskk":
    print(f"IMPERSONATION: @elonmuskk sporting @{matched}'s avatar (hamming={hamming})")
```

**Hamming thresholds** (64-bit pHash):

| Distance | Interpretation |
|---|---|
| ≤ 6 bits | near-certain same image (re-upload + light JPEG) |
| 7–12 bits | same image modified (border/overlay/tint) — **flag** |
| 13–20 bits | ambiguous, needs tiebreaker |
| > 20 bits | different images |

Default threshold `10`. Caches hashes in SQLite with 30-day TTL. Rolling window of 5 hashes per handle tolerates intentional avatar changes.

**Safety:** host allowlist (`pbs.twimg.com`), 2MB size cap, `image/*` content-type check — no SSRF.

**Without `[vision]` extra:** degrades to content-SHA256 compare (byte-identical only). Fully opt-in.

### 4. YouTube + Vimeo transcription

```python
from scraperx import VimeoScraper
from scraperx.youtube_scraper import YouTubeScraper

# YouTube
yt = YouTubeScraper()
res = yt.get_transcript("https://youtube.com/watch?v=dQw4w9WgXcQ")
print(res.transcript[:500])

# Vimeo
vm = VimeoScraper()
res = vm.get_transcript("https://vimeo.com/76979871")
print(f"{res.title} / {res.author} / {res.duration_seconds}s")
print(f"method: {res.transcript_method}")   # text_tracks | whisper_faster | whisper_cli
print(res.transcript[:500])

# Embed-domain-locked Vimeo — pass the embedder URL as referer
res = vm.get_transcript(
    "https://player.vimeo.com/video/123456",
    referer="https://some-company.com/product-tour",
)
```

Transcription priority: creator-uploaded VTT → `faster-whisper` (GPU) → `whisper` CLI. Auto-detects GPU (float16 on CUDA, int8 on Metal, CPU fallback).

### 4b. Silent video transcription — frame OCR for audio-less videos

Videos with no audio track (screen recordings, silent TUI demos) can't be transcribed by Whisper — `transcribe_silent_video` fills the gap by sampling frames and running tesseract OCR.

```python
from scraperx import transcribe_silent_video

result = transcribe_silent_video("https://x.com/gitlawb/status/2055992174358274431", n_frames=6)
print(result.full_text)        # timestamped on-screen text
print(result.has_audio)        # False = confirms silent path was correct
```

Requires the `tesseract` system binary (`apt install tesseract-ocr` or `brew install tesseract`) and the `[silent-video]` extra (`pip install 'scraperx[silent-video]'`).

### 5. Video discovery — scan any webpage

```python
from scraperx import discover_videos, fetch_any_video_transcript

refs = discover_videos("https://some-company.example.com/product")
for v in refs:
    print(f"{v.provider}: {v.canonical_url}  (embed: {v.embed_url})")

# Top-level dispatcher — direct URL or webpage, auto-routes
result = fetch_any_video_transcript("https://some-blog.com/post-with-vimeo-embed")
```

**Detects 6 provider patterns:**
- YouTube / youtube-nocookie iframes
- Vimeo iframes (incl. unlisted-with-hash `?h=abc`)
- Wistia iframes AND JS div-embeds (`<div class="wistia_embed wistia_async_...">`)
- JWPlayer (`cdn.jwplayer.com/players/...`)
- Brightcove (`players.brightcove.net/{acc}/{player}/index.html?videoId={id}`)
- HTML5 `<video>` / `<source>` / `og:video` meta / JSON-LD `VideoObject`

Deduplicates by `(provider, id)`. Works without `beautifulsoup4` (regex fallback). Returns `VideoRef` objects with `page_url` + `referer` for embed-locked downstream calls.

### 7. GitHub deep analyzer — trust verdicts + community mentions (NEW in 1.4.0)

One command, one verdict. Paste a repo URL, get back:

- 0–100 overall trust score with a one-line rationale
- 4 sub-scores: bus factor, momentum, health, README quality
- Community mentions across 6 dedicated platforms (HN, Reddit, StackOverflow, dev.to, arXiv, Papers With Code) + 6 generic sites via the Tier-B semantic layer (Lobsters, Medium, Bluesky, Product Hunt, Substack, LinkedIn)
- Notable forks (catches "community took over" signals)
- Security advisories (GHSA)
- 3-bullet verdict with inline `[n]` citations to the mentions list

#### CLI

```bash
# Markdown report
scraperx github yt-dlp/yt-dlp

# Full URL, JSON output
scraperx github https://github.com/rust-lang/rust --json

# Deep mode — qwen3.5:27b synthesis instead of qwen3:4b (slower, higher quality)
scraperx github yt-dlp/yt-dlp --deep

# Skip community mentions for a quick metadata-only check
scraperx github yt-dlp/yt-dlp --no-mentions

# Disable SQLite cache for this run
scraperx github yt-dlp/yt-dlp --no-cache

# Also: trending
scraperx trending                         # daily, all languages
scraperx trending --since weekly --lang python --limit 10
scraperx trending --json
```

#### Python

```python
from scraperx import GithubAnalyzer, analyze_github_repo

# One-shot — heuristic verdict (no LLM, no cache)
report = analyze_github_repo("yt-dlp/yt-dlp")
print(f"Trust: {report.trust.overall}/100 — {report.trust.rationale}")

# With full wiring: cache + web-search + LLM synthesis
from scraperx import SocialDB

analyzer = GithubAnalyzer(
    github_token=None,           # or os.environ["GITHUB_TOKEN"] for 5000/h
    db=SocialDB(),               # SQLite cache, 4-24h TTL per kind
    web_search_fn=my_web_search, # Tier B — any local_web_search-compatible callable
    local_llm_fn=my_local_llm,   # qwen3:4b fast / qwen3.5:27b deep
)
report = analyzer.analyze_repo("https://github.com/rust-lang/rust", deep=True)

print(report.verdict_markdown)
for m in report.mentions:
    print(f"[{m.source}] {m.title} — {m.url}")
```

#### Authentication (Q1 handoff decision)

Unauth by default — 60 requests per hour, enough for personal use. Set `GITHUB_TOKEN` env var to upgrade to 5000/h; the analyzer picks it up automatically. No config file, no prompt.

#### How it avoids API-key lock-in

- HN: Algolia HN Search (free, unauthed)
- Reddit: `/search.json` (free, unauthed, UA required)
- StackOverflow: StackExchange API 2.3 (free, unauthed, 300/day)
- dev.to: public `/api/articles` (free)
- arXiv: Atom XML export (free)
- Papers With Code: public v1 API (free)
- Trending: HTML scrape of github.com/trending (no API exists)
- GitHub REST: works unauthed at 60/h

#### Cache discipline

`SocialDB` caches repo metadata for 24h, commits/issues for 6h, mentions for 4h. Empty results are NOT cached — transient network errors can retry next call. All new tables share the existing `~/.scraperx/social.db` file.

---

### 6. Profile, search, token extraction

```python
from scraperx import get_profile, search_tweets, extract_token_mentions, SocialDB

p = get_profile("elonmusk")
print(f"{p.name} ({p.handle}): {p.followers:,} followers, verified={p.verified}")

results = search_tweets("Solana LP strategy", limit=5, time_filter="w")
for t in results:
    print(f"@{t.author_handle}: {t.text[:120]}")

mentions = extract_token_mentions("$SOL to the moon, $WIF looking strong")
for m in mentions:
    print(m.symbol, m.kind)  # ("SOL", "cashtag"), ("WIF", "cashtag")

with SocialDB() as db:
    db.save_tweet(results[0])
    buzz = db.get_token_buzz("SOL", hours=24)
    print(f"{buzz['mention_count']} mentions / {buzz['unique_authors']} authors")
```

### 7a. smart_fetch cascade — universal URL fetcher with Cloudflare bypass

```python
from scraperx import smart_fetch
result = smart_fetch("https://intel.arkm.com/explorer/entity/fomo")
print(result.mode_used, len(result.content), result.http_status)
```

**Cascade order (cheapest → most resilient):**

| # | Leg | Best for | Cost |
|---|---|---|---|
| 1 | `jina` | research articles, docs, news (clean markdown) | free, ~1s |
| 2 | `urllib` | static pages, JSON endpoints, RSS | free, <1s |
| 3 | `playwright` | JS-heavy SPAs, sites that 403 plain HTTP | ~5-15s |
| 4 | `scrapling_stealth` | **Cloudflare Turnstile, JS challenges, fingerprint walls** | ~30-90s, requires `[stealth]` extra |

The cascade tries each leg in order, recording per-leg failures in
`result.errors`, and returns as soon as one succeeds. SQLite-cached for 24h
in `~/.scraperx/social.db`. SSRF-guarded (blocks RFC1918 / loopback / link-local
hosts); pass `allow_private=True` for tests.

`scrapling_stealth` is opt-in (heavy patchright Chromium dep). When the dep is
missing, the leg surfaces `ScraplingNotAvailable` and the cascade records it as
a failure rather than crashing — install with `pip install 'scraperx[stealth]'`
followed by `scrapling install`.

### 7. OSINT scraping primitives (1.7.0+)

Six reusable building-blocks ported from the s31/s32 wojak-wojtek session. Each one
replaces a recurring inline workaround with a typed, tested helper.

```python
from scraperx import (
    dismiss_cookie_banner,            # cookie_banner.py — 16 vendor selectors, fan-out probe
    extract_chart_data,               # js_state.py — pull Highcharts series WITHOUT OCR
    extract_spa_state,                # js_state.py — Next/Apollo/Redux hydration grab
    wayback_multi_generation_probe,   # wayback.py — multi-URL-family CDX recovery
    parse_pdf_with_columns,           # pdf_text_parser.py — column-band tokeniser
    reverse_image_search,             # reverse_image.py — 6-engine fan-out (Yandex/Lens/...)
    QuotaSession,                     # quota_session.py — requests-cache + pyrate-limiter
)
```

**Cookie banner — auto-dismiss before scraping:**

```python
from playwright.sync_api import sync_playwright
from scraperx import dismiss_cookie_banner

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.yardeni.com/")
    r = dismiss_cookie_banner(page)
    print(r.vendor, r.matched_selector)  # "OneTrust", "#onetrust-accept-btn-handler"
```

**Highcharts data extraction — bypass OCR:**

```python
from scraperx import extract_chart_data
charts = extract_chart_data(page)         # one ChartSnapshot per Highcharts instance
for c in charts:
    print(c.title, [(s.name, len(s.data)) for s in c.series])
```

**Wayback multi-generation recovery — never declare a year unrecoverable
until you've tried every URL family:**

```python
from scraperx import wayback_multi_generation_probe
result = wayback_multi_generation_probe(
    domain="ici.org",
    url_family_generations=[
        "ici.org/info/",
        "ici.org/doc-server/info%3A",
        "ici.org/system/files/",
    ],
    year=2018,
)
print(result.matched_family, len(result.entries))
print(result.per_family_counts)  # honest "we tried everything" report
```

**Multi-column PDF parsing — chartbooks, factsheets:**

```python
from scraperx import parse_pdf_with_columns
result = parse_pdf_with_columns(
    path="/tmp/yardeni_sp546fundamentals.pdf",
    section_re=r"Sector Earnings Revisions",
    footer_re=r"Source: Yardeni",
    sector_aliases={"Info Tech": "Information Technology"},
)
for r in result.rows:
    print(r.label, r.values)
```

**Reverse-image fan-out — different corpora hit different sources:**

```python
from scraperx import reverse_image_search
hits = reverse_image_search("https://example.com/avatar.jpg", fetch=False)
for h in hits:
    print(f"{h.engine:10s}  {h.search_url}")
```

**Cached + rate-limited HTTP session with auth-header hygiene:**

```python
from pyrate_limiter import Rate, Duration
from scraperx import QuotaSession
sess = QuotaSession(
    cache_path="~/.scraperx/finnhub-cache.sqlite",
    rates=[Rate(60, Duration.SECOND), Rate(50_000, Duration.DAY)],
    auth_headers=("X-Finnhub-Token", "Authorization"),
    bucket_name="finnhub-free",
)
resp = sess.get("https://finnhub.io/api/v1/stock/profile2", params={"symbol": "AAPL"})
```

Auth headers are excluded from the cache key (no token-leakage in the SQLite file)
— see the QuotaSession docstring for the cache-collision trade-off.

---

## Demo

What a session looks like.

### Scrape a tweet with full 1.3.0 context

```text
$ scraperx https://x.com/user/status/1234567890 --json
{
  "id": "1234567890",
  "author_handle": "user",
  "text": "Thread 🧵 on why on-chain auth matters...",
  "is_reply": false,
  "is_quote": false,
  "conversation_id": "1234567890",
  "created_at": "Thu Apr 17 09:12:00 +0000 2026",
  "author_verified": true,
  "author_verified_type": "business",
  "author_followers": 42000,
  "author_joined": "Wed Jan 03 12:00:00 +0000 2018",
  ...
}
```

### Reconstruct a thread and verify it

```text
$ scraperx https://x.com/user/status/1234567890 --thread
Thread (5 tweets by @user)
  [1/5] Thread 🧵 on why on-chain auth matters...
  [2/5] First: identity claims live in the address, not the handle.
  [3/5] Second: handles are mutable. Numeric IDs are not.
  [4/5] Third: this is what ThreadAuthenticity actually checks.
  [5/5] Source code: https://github.com/prezis/scraperx

Authenticity: OK
  ✓ same_conversation (all share conversation_id=1234567890)
  ✓ single_author    (all by author_id=987654321)
  ✓ chronological    (timestamps non-decreasing)
  ✓ no_interpolation (every reply resolves to a parent in the thread)
```

### Find embedded videos on a random webpage

```text
$ scraperx discover https://some-company.example.com/product-tour
Found 2 video(s):
  youtube  id=dQw4w9WgXcQ  https://www.youtube.com/watch?v=dQw4w9WgXcQ
  vimeo    id=76979871     https://vimeo.com/76979871
```

### Transcribe a Vimeo video (auto-captions or whisper)

```text
$ scraperx https://vimeo.com/76979871
Title: Sintel — The Durian Open Movie Project
Author: Blender Foundation
Duration: 888s
Method:   text_tracks   (creator-uploaded VTT used)

Transcript:
SINTEL: Wait! Hey wait... Please don't go...
...
```

---

## Comparison with alternatives

scraperx sits in a different niche than high-volume scrapers like `snscrape` or `yt-dlp`. It focuses on **per-URL enrichment** — authenticity signals, impersonation checks, and cross-provider video discovery — with a stdlib-only core and no API keys. Use the table below to pick the right tool for your job.

| Feature | scraperx | snscrape | tweepy | yt-dlp | twikit |
|---|:---:|:---:|:---:|:---:|:---:|
| Requires API keys | ❌ | ❌ | ✅ | ❌ | ❌ |
| Requires account credentials | ❌ | ❌ | ✅ | ❌ | ✅ |
| X/Twitter tweet scraping | ✅ | ⚠️ (broken post-API changes) | ✅ | ❌ | ✅ |
| X/Twitter thread reconstruction | ✅ | ❌ | ⚠️ (manual) | ❌ | ⚠️ (manual) |
| X/Twitter search | ✅ | ⚠️ | ✅ | ❌ | ✅ |
| X/Twitter profile | ✅ | ✅ | ✅ | ❌ | ✅ |
| YouTube transcription | ✅ | ❌ | ❌ | ⚠️ (subs only, no ASR) | ❌ |
| Vimeo transcription | ✅ | ❌ | ❌ | ⚠️ (subs if available) | ❌ |
| Generic video discovery (page → embeds) | ✅ | ❌ | ❌ | ⚠️ (direct URL only) | ❌ |
| Thread authenticity verification | ✅ | ❌ | ❌ | ❌ | ❌ |
| Impersonation detection (avatar pHash) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Scam content detection | ✅ | ❌ | ❌ | ❌ | ❌ |
| Python 3.10+ | ✅ | ✅ (3.8+) | ✅ | ✅ | ✅ |
| Active maintenance (2025-2026) | ✅ | ❌ (last commit 2023-11) | ✅ | ✅ | ✅ |
| Stars (Apr 2026) | 1 | 5.3k | 11.1k | 157k | 4.3k |
| License | MIT | GPL-3.0 | MIT | Unlicense | MIT |

**When to choose what:**
- **scraperx** — verify a specific URL or thread (authenticity, impersonation, embed discovery). Unique: perceptual-hash impersonation + thread authenticity scoring + cross-provider video discovery in one import.
- **snscrape** — historical archives. Note: effectively unmaintained since Nov 2023; Twitter support broke post-API changes.
- **tweepy** — when you already have official X API keys and need the full documented endpoint surface.
- **yt-dlp** — high-volume video downloading. Reference tool; scraperx uses it internally for audio extraction.
- **twikit** — logged-in X scraping (DMs, posting). scraperx deliberately avoids account-bound endpoints.

**Honest caveats:** scraperx is new and small (low single-digit stars as of April 2026) compared to `yt-dlp` (157k) or `tweepy` (11k). For Instagram, use `instaloader`. For high-volume X scraping with an account, use `twikit` or `twscrape`. scraperx isn't a replacement for those — it's the glue layer for authenticity + discovery on top of them.

---

## CLI reference

```
scraperx [URL|@handle] [OPTIONS]

Positional:
  URL|@handle         Tweet URL, profile URL, YouTube/Vimeo URL, or @handle

Options:
  --json              JSON output
  --thread            Fetch full thread (for tweet URLs)
  --cookies PATH      Cookies file for yt-dlp
  --whisper-model M   Whisper model: base | medium | large (default: base)
  --force-whisper     Skip auto-captions, go straight to Whisper
  -v, --verbose       Debug logging

Subcommands:

  scraperx search QUERY [OPTIONS]
    -n, --limit N         Max results (default: 10)
    -t, --time {d,w,m,y}  Day / week / month / year
    --json
    --fast                Tweet IDs only (skip FxTwitter enrichment)

  scraperx discover URL
    List embedded videos found on a webpage (6 providers).

  scraperx doctor [--json]
    System diagnostic — check Python, GPU, Ollama, optional deps,
    system tools (yt-dlp, ffmpeg). Prints install hints for missing extras.
```

**Example — check what optional features you have ready:**

```bash
$ scraperx doctor
scraperx doctor — system diagnostic

Python:   3.12.3
Platform: Linux x86_64

GPU acceleration:
  ✓ NVIDIA CUDA: NVIDIA GeForce RTX 5090, 32607 MiB, 570.211.01

Optional libraries:
  ✓ PIL
  ✓ faster_whisper (1.2.1)
  ✓ bs4
  ✗ imagehash        — pip install scraperx[vision]      # perceptual avatar hashing

Summary:
  ✓ Fast transcription ready (faster-whisper + GPU)
  ! Avatar matching falls back to SHA256 — install: pip install scraperx[vision]
  ...
```

---

## Testing

```bash
pytest -v
```

All tests are fully mocked — no network, no subprocess, no filesystem side effects. Runs in ~3 seconds. CI runs on Python 3.10, 3.11, 3.12.

---

## Data storage

`~/.scraperx/social.db` (SQLite):

| Table | TTL | Purpose |
|---|---|---|
| `tweets` | forever | scraped tweet content + metadata |
| `profiles` | 7 days | re-scraped when stale |
| `token_mentions` | forever | `$CASHTAG` + token matches |
| `search_cache` | 1 hour | cached search results |
| `avatar_hash` | 30 days | perceptual hashes for AvatarMatcher |
| `verified_avatars` | forever | rolling-window known-good hashes |

---

## Dependencies

**Required:** Python 3.10+. Stdlib only — no pip installs for core tweet/profile/thread/search scraping.

**Optional (install via extras):**
- `faster-whisper>=1.0` (`[whisper]`) — GPU-accelerated transcription
- `imagehash>=4.3` + `Pillow>=10.0` (`[vision]`) — perceptual avatar matching
- `beautifulsoup4>=4.12` (`[video-discovery]`) — more robust video discovery
- `twscrape>=0.12` (`[twscrape]`) — optional account-backed X scraping

**Optional system tools:**
- `yt-dlp` — audio download for Vimeo/YouTube whisper path, tweet video fetch
- `whisper` CLI — fallback when `faster-whisper` unavailable

---

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup + testing.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Current: **1.3.0** (2026-04-17).

## Security

Reports of security issues: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) — do what you want, attribution appreciated.

## Acknowledgments

Stands on the shoulders of:
- [FxTwitter](https://github.com/FixTweet/FxTwitter) and [vxTwitter](https://github.com/dylanpdx/BetterTwitFix) — the oauth-free tweet APIs that make this possible
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 1800+ video-site extractors
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 4× speedup over OpenAI Whisper
- [imagehash](https://github.com/JohannesBuchner/imagehash) — perceptual hashing

## Reddit scraping + self-learning method telemetry

### `scraperx.reddit` — tiered NO-LOGIN Reddit scraper

Search, listings, and full threads (flattened comment trees) with no OAuth,
no account, no API key. Three tiers, cheapest → most resilient:

1. **TIER 1 `old_json`** — `old.reddit.com/....json` (structured; browser UA
   required; one jittered retry on transient 403/429 bursts)
2. **TIER 2 `redlib`** — public redlib/libreddit mirror pool, HTML-parsed
   (fields degrade gracefully: titles/permalinks always, scores best-effort)
3. **TIER 3 `stealth` / `stealth_mirror`** — scrapling stealth Chromium
   against the `.json` URL, then against the mirrors (beats JS-challenge
   walls AND reddit.com IP-blocks — mirrors proxy Reddit from their own IPs)

```python
from scraperx.reddit import RedditScraper, search_subreddit, get_thread, get_subreddit_posts

posts = search_subreddit("ethereum", "alchemy", limit=10, sort="new")
thread = get_thread("https://www.reddit.com/r/ethereum/comments/abc123/x/")
hot = get_subreddit_posts("ethereum", sort="hot", limit=25)
# package-level: scraperx.get_reddit_thread (scraperx.get_thread is the X/Twitter one)
```

Politeness is built in (~1-2 req/s with jitter). Every result carries
`.tier` so you know which method produced it; `scraper.last_tier_used`
tells you what worked last.

### `scraperx.method_telemetry` — scrapers that LEARN which method works

Instead of a hardcoded tier order forever, every tier attempt is recorded to
`~/.scraperx/method-telemetry.jsonl` (same JSONL convention as the GitHub
analyzer's `verdicts.jsonl`), and each call re-ranks methods by **recent
success-rate** — exponentially time-decayed (6 h half-life) over a last-200
sliding window:

- **< 5 samples** for a method → neutral prior (0.5) → caller's default order
- a **proven** method (→1.0) rises above untried ones; a **failing** one
  (→0.0) sinks below them; decay means a transient block heals itself
- stdlib-only, **fail-open**: any telemetry error degrades to the default
  order and never breaks a scrape

```python
from scraperx.method_telemetry import record, preferred_order, method_stats

record("reddit", "old_json", "old.reddit.com", False, latency_ms=412.7, http_status=403)
preferred_order("reddit", ("old_json", "redlib", "stealth", "stealth_mirror"))
# → ["redlib", "stealth", "stealth_mirror", "old_json"] once old_json keeps failing
method_stats("reddit")   # raw per-method {n, successes, success_rate, avg_latency_ms}
```

`RedditScraper` wires this in by default (`adaptive_tiers=True`): a blocked
`old.reddit.com` demotes TIER 1 below the mirrors on subsequent calls, and
recovers automatically once it works again. Opt out per-instance with
`RedditScraper(adaptive_tiers=False)`; point the ledger elsewhere (tests/CI)
with `SCRAPERX_METHOD_TELEMETRY_PATH=/path/to/ledger.jsonl`. Any tiered
scraper can adopt the same two calls — the ledger is namespaced by `scraper`.

## Persistent stealth sessions — carrying cookies across calls (1.9.0+)

By default every stealth fetch starts from a **cold, temporary browser profile**: it
re-solves Turnstile (30-90 s), and it cannot present a cookie you obtained earlier.
Give it a profile directory and both problems go away.

```python
from scraperx import smart_fetch

PROFILE = "~/.cache/scraperx/profiles/example.com"   # one dir PER HOST

r1 = smart_fetch("https://example.com/a", prefer="scrapling_stealth",
                 stealth_profile=PROFILE)           # solves the challenge
r2 = smart_fetch("https://example.com/b", prefer="scrapling_stealth",
                 stealth_profile=PROFILE)           # reuses the clearance
```

The directory is created if absent and `~` is expanded. Clearance cookies — and any
login established in that profile — persist across calls **and across process
restarts**, because Scrapling launches a persistent browser context instead of a
throwaway one.

Everything else upstream exposes is reachable through `stealth_kwargs`:

```python
smart_fetch(url, prefer="scrapling_stealth",
            stealth_profile=PROFILE,
            stealth_kwargs={"proxy": "socks5://127.0.0.1:1080",
                            "locale": "pl-PL", "timezone_id": "Europe/Warsaw"})
```

Authoritative key list: `scrapling.engines._browsers._types`. `stealth_kwargs` is
merged **after** `stealth_profile`, so an explicit `user_data_dir` wins.

⚠ **`proxy_rotator` and persistence are mutually exclusive** — upstream takes a
different, non-persistent branch when a rotator is set. Pick one.

⚠ **A cookie is necessary, not sufficient.** Persistence beats the *challenge* layer.
It does not beat behavioural detection or an account ban — for that,
`fingerprint_audit.diagnose_403()` returns `behavior-or-account`, and that verdict is
**terminal**: a fresh token or fingerprint will not help.

### N URLs on ONE browser (1.10.0+)

`stealth_profile=` makes the *cookie* persist. It does not stop each call building and
tearing down a whole Chromium. For a batch, hold the session open:

```python
from scraperx import stealth_session, fetch_stealth_session

# You drive the loop — early exit, tier ladders, interleaved logic:
with stealth_session(profile="~/.cache/scraperx/profiles/example.com") as s:
    for url in urls:
        content, status = s.fetch(url)

# Or you just have a list:
for r in fetch_stealth_session(urls, profile="~/.cache/scraperx/profiles/example.com"):
    if r.ok:
        save(r.url, r.content)
    else:
        log.warning("%s -> %s (%s)", r.url, r.error, r.error_kind)
```

`StealthPageResult` carries `url / index / content / http_status / error / error_kind /
elapsed_ms`, with `.ok` using the same empty-body-only test as the cascade.

**Failure semantics**, because a batch fails differently from a single fetch:

| what happened | what you get |
|---|---|
| site/transport failure on URL 7 of 20 | that result gets `error_kind="fetch"`; URL 8 continues on the same warm session |
| **a code defect** (`TypeError` etc.) | **re-raises and aborts** — a wall never aborts, so you can always tell them apart |
| `max_total_seconds` exceeded | remaining URLs yielded as `error_kind="skipped"`, never silently dropped |
| browser page pool wedges | session restarts itself once per URL, bounded at 2 per batch |

⚠ **If you `break` out of `fetch_stealth_session`, close the generator** —
`contextlib.closing(...)` or `gen.close()` — or use `stealth_session()` directly.
CPython refcounting usually finalises it for you, but a leaked generator is a leaked
headless browser.

⚠ **`max_pages` > 1 raises `ValueError` on purpose.** It is a lying knob upstream:
`StealthySession(max_pages=5).max_pages` is `1` while its config says `5`. Fetches are
serial regardless — `fetch()` blocks. Rejected loudly rather than accepted and ignored.

**Measured** (2026-08-02, live, `example.com` ×3 per arm — unwalled on purpose, so the
number isolates browser-start amortization):

| | one-shot ×3 | ONE session ×3 |
|---|---|---|
| total | 2.6 s | **1.3 s** |
| browser start | paid 3× | 0.3 s, once |
| per fetch | 957 / 799 / 818 ms | 408 / 246 / **233 ms** |

Session wins ~50%; the marginal fetch is ~3.5× cheaper, and the win grows with N.

⚠ **After upgrading `scrapling`, re-install the browser** — the pin moves:

```bash
python3 -m patchright install chromium    # no root, ~114 MB into ~/.cache
```

Skipping it kills EVERY stealth call (`Executable doesn't exist at
…/chromium-<N>/chrome-linux64/chrome`) — including `fetch_stealth`, not just sessions.
A mocked test suite cannot see a missing binary: 891 offline tests were green here while
the feature was completely dead. Always follow an upgrade with one live fetch.

## Documentation crawling + fingerprint self-audit

Both modules shipped in the 1.7.0→1.8.0 window and were absent from this README
until 2026-08-02. Documented here so the front page matches what installs.

### `scraperx.docs_crawler` — "I read every page", made verifiable

Born from a 2026-05-03 incident: an agent claimed it had read a vendor's API docs
after reading 5 of 82 pages. This crawler makes coverage checkable instead of
asserted.

```bash
scraperx docs-crawl https://docs.example.com/
# → ./docs-crawl-docs.example.com/_DIGEST.md  ← per-page byte/word counts
# → ./docs-crawl-docs.example.com/*.txt       ← extracted prose, ready to grep
# → ./docs-crawl-docs.example.com/_SHELLS.md  ← pages <500 chars, need a real render
```

Discovers URLs via `sitemap.xml` (with a namespace-agnostic fallback for
non-conformant XML) or an explicit list; fetches with a browser UA; extracts prose
with a *deliberately less aggressive* parser that keeps Docusaurus / VitePress /
mkdocs-material content — stripping `<nav>`/`<header>`/`<footer>` alongside
`<script>` was an earlier mistake that nuked 95% of the prose on Docusaurus sites.
Hardened against curl flag-injection (URLs starting with `-`, embedded line breaks,
non-http schemes) and path traversal on write.

Flags: `--max-pages`, `--user-agent`, `--timeout`, `--sleep-between`,
`--include-encoded-dups`. 22 tests in `tests/test_docs_crawler.py`.

### `scraperx.fingerprint_audit` — the self-check Scrapling does not have

Answers the question every blocked scrape needs answered before anyone touches
code: **what kind of 403 is this?**

```python
from scraperx.fingerprint_audit import diagnose_403

verdict = diagnose_403(url, proxy=..., headers=..., cookies=...)
verdict.likely_cause  # "fingerprint" | "ip" | "behavior-or-account" | "not-403"
```

It accepts `cookies=` and `proxy=`, so an **authenticated** request can be
diagnosed, not just an anonymous one. The `behavior-or-account` verdict is
**terminal by design** — it means a fresh token or a new fingerprint will not
help, so the correct next action is to stop, not to escalate. Treating it as
"try harder" burns days.

Use it before concluding that a site is walled. A surprising share of "walled"
verdicts are a local defect: on 2026-08-02 a consumer's stealth call had never
executed once (missing required argument, swallowed `TypeError`) while logging a
line indistinguishable from a Cloudflare block.
