---
name: scraperx
description: Read and search X/Twitter, Reddit, Bluesky and public Telegram channels without logging in (no account of yours is used, so nothing can be banned), plus web pages behind bot walls, YouTube/Vimeo transcripts and GitHub repo checks, through the scraperx CLI. Use whenever the user asks to find, search, read or monitor posts or pages — "find posts about…", "what are people saying on X / Reddit about…", "search by hashtag or phrase", "posts from @user", "replies under this tweet", "Reddit comments about…", "what does this redditor post", "read this Telegram channel", "what is Bluesky saying about…", "znajdź posty o…", "co piszą na X / Reddicie", "szukaj po hashtagu", "sprawdź na reddicie" — or pastes an x.com, reddit.com or t.me link. Routes each question to the command measured to work and its fallback, instead of claiming it cannot be done without an account.
---

# scraperx — X, Reddit, Bluesky, Telegram and the walled web, without accounts

**Never log in.** No route below uses an X, Reddit, Bluesky or Telegram account of the user. Public relays (FxTwitter, Nitter
instances, redlib mirrors, the Arctic Shift archive, Bluesky's public AppView, Telegram's web preview) do the access. Do not suggest
account-backed tools (twscrape, Telethon, cookies) unless the user asks for that risk explicitly.

## 0. Run the routed command first

Go straight to the command in the table below; do not start with a diagnosis. Only if that command fails because
scraperx itself is missing (`command not found`, `ModuleNotFoundError: scraperx`), stop and give the user both the
install lines and the exact command you were going to run (do not install packages yourself unless the user asks):

```bash
pip install "scraperx[stealth] @ git+https://github.com/prezis/scraperx.git"
scrapling install          # the patched Chromium for walled sites and Reddit mirrors
```

`scraperx doctor` is for a broken install, not a first step. Add `--json` to any command below for
machine-readable output; stderr names the source that answered.

## 1. Which command for which question

| question | command | route (first → fallback) |
|---|---|---|
| X posts by words, `#tag`, `"phrase"`, `from:user`, `since:` | `scraperx search '#tag "phrase"' -n 20 [--time d\|w\|m\|y]` | Desearch (key) → Brave (key) → **Nitter search RSS** (no key) → DuckDuckGo |
| an X account's posts | `scraperx x user <handle> -n 40 [--no-reposts] [--replies]` | FxTwitter v2 → Nitter user RSS |
| replies under an X post | `scraperx x replies <post url> -n 50` | FxTwitter v2 conversation |
| trending on X | `scraperx x trends` | FxTwitter v2 |
| one X post, thread, profile | `scraperx <post url>` / `scraperx <post url> --thread` / `scraperx @handle` | FxTwitter → vxTwitter → yt-dlp → oEmbed |
| Reddit: search all / one subreddit | `scraperx reddit search <q> [--sub <s>] [--sort new] [-n 50]` | old.reddit JSON → redlib mirrors → stealth browser (on reddit, then on mirrors) |
| Reddit: subreddit listing, a thread | `scraperx reddit sub <s> --sort new`, `scraperx reddit thread <url>` | same ladder |
| Reddit: words inside COMMENTS | `scraperx reddit comments <text> --sub <s>` (or `--author <u>`) | Arctic Shift archive |
| Reddit: what a user posts and comments | `scraperx reddit user <name>` | Arctic Shift archive |
| Telegram public channel | `scraperx telegram posts <ch> -n 40`, `telegram search <ch> <q>`, `telegram info <ch>` | t.me/s/ web preview |
| Bluesky: posts by words, an account's posts, a thread, a profile | `scraperx bluesky search <q> [--sort latest\|top]`, `bluesky user <handle> [--no-reposts]`, `bluesky thread <post url>`, `bluesky profile <handle>` | public AppView (search: first page only, up to 100 posts) |
| YouTube / Vimeo transcript | `scraperx <video url>` | captions → whisper |
| GitHub repo trust check | `scraperx github <owner/repo>` | GitHub API + mentions |

Keys are optional: `SCRAPERX_DESEARCH_API_KEY` (Desearch X search, full posts) and `SCRAPERX_BRAVE_API_KEY` (Brave web
search) only change the first steps of X search. Everything else needs no key.

Python API, same routes: `scraperx.search_tweets`, `scraperx.x_timeline.get_user_tweets` / `get_replies`,
`scraperx.search_reddit` / `search_subreddit` / `get_reddit_thread`, `scraperx.reddit_archive.search_comments` /
`user_history`, `scraperx.get_telegram_posts`, `scraperx.search_bluesky` / `get_bluesky_posts`, `scraperx.smart_fetch` /
`fetch_markdown` / `fetch_stealth` for any page.

## 2. Read the answer correctly

- Every item carries its source (`source_method` / `tier`); quote it with the numbers.
- **Reddit archive scores are archive-time**: an item younger than ~36 h shows 0 or 1. `score_as_of` says when a number
  was read; never rank archive and live scores together without it.
- **Pinned posts** (`stickied`) sit on top of Reddit listings, even with `--sort new`.
- **Nitter hits carry no counts**; scraperx re-reads them through FxTwitter for likes and views (`nitter+fxtwitter`),
  a post it cannot re-read stays `nitter`.
- **X timelines mix in reposts**: `reposted_by` marks them; `--no-reposts` keeps the account's own posts.
- **Telegram counts are Telegram's rounded display values** ("1.61M"); the raw text is kept beside the number.
- Reddit through the stealth browser takes ~10 s per page of 25 posts; `-n 50` means two pages.

## 3. When a route fails

1. Read the error: scraperx names each instance or tier it tried and why (HTTP code, `block page: cloudflare_challenge`).
2. Nitter instances come and go. Pick healthy ones from https://status.d420.de/ and set
   `SCRAPERX_NITTER_INSTANCES=https://a,https://b`; no code change needed.
3. FxTwitter v2 answers intermittently; scraperx retries 5 times and falls back to Nitter for timelines.
4. Reddit's live tiers need the stealth browser (`scraperx[stealth]` + `scrapling install`) on hosts Reddit blocks.
   Some agent sandboxes refuse browser runs on bot-checked sites; then ask the user to run the command.
5. Do not route around a refusal by logging in, borrowing cookies or buying accounts.

Details, measurements and dates: the README of https://github.com/prezis/scraperx ("Reddit", "Telegram", "X without an
X account" sections). Keep this skill current with `scraperx skill install` after upgrading scraperx.
