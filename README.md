# ScraperX

**Universal scraping + video intelligence, no API keys required.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/prezis/scraperx/actions/workflows/ci.yml/badge.svg)](https://github.com/prezis/scraperx/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.3.0-informational.svg)](CHANGELOG.md)

ScraperX fetches social-media posts, transcribes videos, and verifies authenticity — without API keys or account credentials. Built on stdlib, with optional extras for perceptual image hashing, web scraping helpers, and GPU-accelerated speech-to-text.

> **Status: beta.** Core functionality is stable (212 mocked tests); new v1.3.0 features (Vimeo, video discovery, thread authenticity, avatar pHash) are freshly-released — feedback welcome.

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
- **SQLite persistence** — tweets, profiles, mentions, avatar hashes, search cache.

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
