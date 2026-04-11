# ScraperX

Multi-method X/Twitter scraper + YouTube transcriber with automatic fallback.

No API keys required. No accounts needed. Just works.

## Install

```bash
pip install git+https://github.com/prezis/scraperx.git
```

Or clone and install locally:

```bash
git clone https://github.com/prezis/scraperx.git
cd scraperx
pip install -e .
```

## Quick Start

```bash
# Scrape a tweet
scraperx https://x.com/user/status/123456

# Get a profile
scraperx https://x.com/elonmusk

# Fetch full thread
scraperx https://x.com/user/status/123456 --thread

# Search tweets (zero credentials, via DuckDuckGo + FxTwitter)
scraperx search "Meteora DLMM strategy" --limit 10
scraperx search "from:elonmusk AI agents" --time w   # last week
scraperx search "prediction market" --json            # JSON output

# Transcribe YouTube video
scraperx https://youtube.com/watch?v=dQw4w9WgXcQ

# JSON output (pipe to jq, store, etc.)
scraperx https://x.com/user/status/123456 --json
```

Also works as a module:

```bash
python -m scraperx https://x.com/user/status/123456
```

## Python API

```python
from scraperx import XScraper, get_profile, get_thread, search_tweets, SocialDB

# Tweet
scraper = XScraper()
tweet = scraper.get_tweet("https://x.com/user/status/123")
print(tweet.text, tweet.likes, tweet.media_urls)

# Profile
profile = get_profile("elonmusk")
print(f"{profile.name}: {profile.followers:,} followers")

# Thread
thread = get_thread("https://x.com/user/status/123")
for t in thread.all_tweets:
    print(t.text)

# YouTube
from scraperx.youtube_scraper import YouTubeScraper
yt = YouTubeScraper()
result = yt.get_transcript("https://youtube.com/watch?v=...")
print(result.transcript[:500])

# Search tweets (DuckDuckGo discovery + FxTwitter enrichment)
results = search_tweets("Solana LP strategy", limit=5, time_filter="w")
for t in results:
    print(f"@{t.author_handle}: {t.text[:100]}")

# Token extraction
from scraperx import extract_token_mentions
mentions = extract_token_mentions("$SOL to the moon, $WIF looking good")
# [TokenMention(symbol='SOL', ...), TokenMention(symbol='WIF', ...)]

# Store & query
with SocialDB() as db:
    db.save_tweet(tweet)
    buzz = db.get_token_buzz("SOL", hours=24)
    print(f"{buzz['mention_count']} mentions by {buzz['unique_authors']} authors")
```

## Architecture

```
                         URL / Query Input
                              |
                      __main__.py (CLI router)
                   /     |        \         \        \
             Tweet?  Profile?  Thread?  YouTube?  Search?
               |        |        |         |         |
          scraper.py profile.py thread.py yt_scraper search.py
               |        |        |         |         |
       Fallback Chain FxTwitter Walk Up  captions  DDG discovery
       ┌──────────┐   User API  via IDs  →whisper  + FxTwitter
       │FxTwitter │                                 enrichment
       │vxTwitter │
       │yt-dlp    │
       │oembed    │
       └──────────┘
                \        |       /
                social_db.py (SQLite)
                       |
             token_extractor.py
```

## Fallback Chain

Every tweet fetch tries 4 methods in order. If one fails, it moves to the next:

| # | Method | Auth | Data Quality | Reliability |
|---|--------|------|-------------|-------------|
| 1 | FxTwitter API | None | Full (text, stats, media, articles) | High |
| 2 | vxTwitter API | None | Full (text, stats, media) | High |
| 3 | yt-dlp | Cookies (optional) | Medium (text, stats, video URL) | Medium |
| 4 | oembed | None | Minimal (text, author only) | Very High |

The chain ensures you always get at least the tweet text, even if third-party APIs go down. oembed is Twitter's own official endpoint.

## Modules

### Core Scraping

| Module | What it does |
|--------|-------------|
| `scraper.py` | Tweet scraping with 4-method fallback. `XScraper().get_tweet(url)` |
| `profile.py` | Profile data (bio, followers, verified). `get_profile("handle")` |
| `thread.py` | Full thread reconstruction. `get_thread(url)` |
| `search.py` | Tweet search via DuckDuckGo + FxTwitter. `search_tweets("query")` |
| `youtube_scraper.py` | Video transcription (auto-captions → faster-whisper GPU → whisper CLI fallback). `YouTubeScraper().get_transcript(url)` |

### Data & Storage

| Module | What it does |
|--------|-------------|
| `social_db.py` | SQLite storage with TTL caching. Tweets, profiles, mentions, search cache |
| `token_extractor.py` | Extracts $CASHTAG mentions and known Solana tokens from text |

### Optional

| Module | What it does |
|--------|-------------|
| `twscrape_backend.py` | Optional twscrape wrapper (requires Twitter accounts, `pip install twscrape`) |

## CLI Reference

```
scraperx [URL] [OPTIONS]

Positional:
  URL                   Tweet URL, profile URL, YouTube URL, or @handle

Options:
  --json                Output as JSON
  --thread              Fetch full thread (tweet URLs only)
  --cookies PATH        Cookies file for yt-dlp
  --whisper-model MODEL Whisper model: base, medium, large (default: base)
  --force-whisper       Skip auto-captions, use Whisper directly
  -v, --verbose         Debug logging


scraperx search QUERY [OPTIONS]

Positional:
  QUERY                 Search terms (supports from:user, quotes, DDG operators)

Options:
  -n, --limit N         Max results (default: 10)
  -t, --time {d,w,m,y}  Time filter: d=day, w=week, m=month, y=year
  --json                Output as JSON
  --fast                Return tweet IDs only (skip FxTwitter enrichment)
  -v, --verbose         Debug logging
```

Auto-detection routes the URL to the right handler:
- `x.com/user/status/ID` or `twitter.com/...` → tweet
- `x.com/handle` → profile
- `youtube.com/watch?v=ID` or `youtu.be/ID` → YouTube
- `@handle` or bare `handle` → profile
- `search QUERY` → DuckDuckGo tweet discovery + FxTwitter enrichment

## Data Storage

Social data is stored in `~/.scraperx/social.db` (SQLite):

| Table | TTL | Purpose |
|-------|-----|---------|
| `tweets` | Forever | Scraped tweet content and metadata |
| `profiles` | 7 days | User profiles (re-scraped when stale) |
| `token_mentions` | Forever | Extracted $CASHTAG and token name matches |
| `search_cache` | 1 hour | Cached search results |

## Media Quality

Videos: automatically selects the highest bitrate variant from API responses.
Photos: appends `:large` suffix for full resolution from `pbs.twimg.com`.

## Testing

```bash
# All tests (180 tests, ~4s, zero network calls)
pytest -v

# Just tweet scraper
pytest tests/test_scraper.py -v

# Just YouTube
pytest tests/test_youtube_scraper.py -v
```

All tests are fully mocked — no network calls, no external dependencies needed.

## Dependencies

**Required (stdlib only):**
- Python 3.10+
- No pip packages needed for core functionality

**Optional — GPU-accelerated transcription (recommended):**
- `pip install faster-whisper` — 4x faster than OpenAI whisper, auto-detects CUDA/Metal GPU
- Auto-detection: if GPU available → uses float16 on CUDA, int8 on Metal. No GPU → CPU fallback.

**Optional system tools:**
- `yt-dlp` — for yt-dlp fallback method and YouTube downloads
- `whisper` — for YouTube audio transcription (fallback when faster-whisper not installed)

**Optional pip packages:**
- `twscrape` — for Twitter account-based scraping (profiles, search, timelines)

## License

MIT
