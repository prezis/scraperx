# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.10.0] — 2026-08-02

### Added — N URLs on ONE browser

1.9.0 made the *cookie* survive across calls. It did nothing about *browser start*:
every stealth fetch still built and tore down a whole Chromium. This closes that.

- **`stealth_session(...)`** — context manager, the primary surface. Hand it a profile,
  drive your own loop:

  ```python
  with stealth_session(profile="~/.cache/scraperx/profiles/example.com") as s:
      for url in urls:
          content, status = s.fetch(url)
  ```
  It is the primary surface (not the list form) because the two heaviest real
  consumers — `reddit.py`'s tier ladder and cex-gate's classify loop — interleave
  their own logic between fetches and cannot be inverted into "hand me a URL list".

- **`fetch_stealth_session(urls, ...)`** — convenience generator over the above,
  yielding a `StealthPageResult` per URL. For when you genuinely just have a list.
- **`StealthPageResult`** — `url / index / content / http_status / error / error_kind /
  elapsed_ms`, with `.ok` mirroring the cascade's empty-body-only test. It does NOT
  interpret `http_status`, exactly as `smart_fetch` does not.
- **`StealthSessionHandle`** — `.fetch()`, `.fetch_xhr()`, `.stats` (fetches / failures /
  restarts). Log the stats: nobody has yet measured a long-lived session.

**This is ADOPT, not BUILD.** `StealthyFetcher.fetch(url, **kwargs)` is literally
`with StealthySession(**kwargs) as engine: return engine.fetch(url)` — verified by
reading the installed 0.4.12. Hoisting that `with` out of per-call scope *is* the
whole feature.

**The rule that shapes the implementation, and it is not stylistic:** everything is
passed at CONSTRUCTION and `session.fetch(url)` is called with NO kwargs. Upstream's
per-fetch validator iterates only its own known field names, so any other key is never
read and never raises — `session.fetch(url, **flat_dict)` would SILENTLY DROP
`user_data_dir`, `headless` and `capture_xhr`, and the batch would run cookie-less
while every result looked healthy. That is the 1.9.0 regression re-introduced one layer
up. `test_fetch_receives_url_only_and_no_kwargs` pins it, and was verified to fail
against a deliberately naive implementation.

### Failure semantics (the reason a batch API needs its own design)

- A site/transport failure on URL 7 of 20 does **not** abort. It is yielded with a
  type-prefixed message and `error_kind="fetch"`; URL 8 continues on the same warm
  session, because a raising fetch does not poison it — the page is reclaimed and the
  context (with its clearance) survives.
- A **code defect** (`TypeError`/`ValueError`/…) **re-raises and aborts.** Three
  independent signals keep it distinguishable from a wall: it aborts (a wall never
  does), it is a traceback rather than a string in a field, and `error_kind` is a
  machine-readable enum. This is the class-5 POISONED-EVIDENCE rule applied at design
  time — the failure it kills is arity drift reported as 20 identical "blocks".
- `max_total_seconds` is a **between-URL** gate; the remainder is yielded as
  `error_kind="skipped"`, never silently dropped. It cannot interrupt a wedged
  in-flight fetch (sync Playwright blocks and is not interruptible).
- **Pool-wedge self-heal:** a `RuntimeError: Maximum page limit` closes and restarts the
  same session object and retries that URL once, bounded at 2 restarts per batch.

### Changed

- **`_build_stealth_kwargs()` is now the single place the Scrapling kwarg dict is
  built.** `fetch_stealth`, `fetch_stealth_xhr` and `stealth_session` all route through
  it, so the timeout floor and curated defaults cannot drift. The dict had been written
  out twice; that is how a divergence hides for months. Body decoding likewise
  collapsed into `_decode_body()`. Behaviour-preserving — the existing 24 tests, which
  assert the exact kwargs both functions pass, stayed green through the refactor.
- The session path injects `selector_config` with `huge_tree=True`. Verified asymmetry:
  `StealthyFetcher.fetch` injects it before constructing its session, a hand-built
  `StealthySession` gets `{}`. Our proven DexScreener case is a 1.4 MB page — exactly
  where lxml's huge_tree matters.
- `retries` defaults to **1**, not upstream's 3: three retries burn three full
  navigation timeouts plus two sleeps on every dead URL, turning the amortization win
  into a loss on any list with dead links.

### Rejected on purpose

- **`max_pages` > 1 now raises `ValueError`.** Verified lying knob:
  `StealthySession(max_pages=5).max_pages` is `1` while `_config` says `5`, because
  `__init__` calls a bare `super().__init__()`. Fetches are serial regardless —
  `fetch()` blocks. Rejected loudly rather than accepted and ignored.
  `test_upstream_sync_max_pages_still_ignored` is a canary that goes red the day
  upstream fixes it.
- No async surface, no per-URL `xhr_pattern` (session-scoped upstream), no per-URL
  `proxy` (crashes on persistent sessions), no cache integration, no cascade
  integration. Each is a verified impossibility or crash, not a preference.

### Not verified (needs a real browser)

The wall-clock saving is **unmeasured** — the 30-90 s figure is our own docstring, not a
measurement, and 1.9.0 already recovered the clearance half, so only browser start
remains recoverable. Also unverified: whether Cloudflare re-challenges by session age
inside a long-lived session; whether two sessions on one profile dir collide on the
Chromium lock; and the pool-wedge trigger itself (the control flow is read, the trigger
was never forced). 21 new offline tests, 879 passing overall.

## [1.9.0] — 2026-08-02

### Added

- **`smart_fetch(..., stealth_profile=...)` — persistent, cookie-carrying stealth sessions.**
  The stealth leg can finally be handed a browser profile directory, so Cloudflare
  clearance and any established login **survive across calls and across process
  restarts**, instead of every fetch starting cold and re-paying the 30-90 s
  Turnstile round-trip.

  This was never a missing capability — it was a missing wire. Scrapling has
  always taken `StealthySession(user_data_dir=...)` (the persistent
  `launch_persistent_context` branch) and our own `fetch_stealth()` has always
  forwarded arbitrary kwargs. But `_fetch_scrapling_stealth` called
  `fetch_stealth(url, timeout=timeout)` — timeout was the only knob — and
  `smart_fetch` had no parameter to express a profile. **Zero call sites in the
  entire tree ever passed one.** The feature was installed, documented upstream,
  and unreachable.

  ```python
  smart_fetch(url, prefer="scrapling_stealth",
              stealth_profile="~/.cache/scraperx/profiles/example.com")
  ```

- **`smart_fetch(..., stealth_kwargs={...})`** — escape hatch forwarded verbatim to
  the stealth leg (`proxy`, `useragent`, `locale`, `timezone_id`, `block_ads`,
  `real_chrome`, `retries`, …). Authoritative key list:
  `scrapling.engines._browsers._types`. Merged after `stealth_profile`, so an
  explicit `user_data_dir` wins. ⚠ `proxy_rotator` disables persistence upstream —
  the two are mutually exclusive branches, not composable options.

- **`tests/test_stealth_session_wiring.py`** — 8 offline tests pinning the wire
  itself (shorthand → `user_data_dir`, directory creation, `~` expansion, verbatim
  kwarg forwarding, documented precedence, merge-not-replace, and two calls sharing
  one profile). Verified to bite: 7 of the 8 fail against the pre-fix `fetch.py`.

### Changed

- **`scrapling` upgraded 0.4.7 → 0.4.12** (was 5 releases / 3.5 months behind; nothing
  pinned us — `pyproject.toml` says `>=0.4.7`). Stale browser fingerprints are
  themselves a detection signal, and 0.4.9 is reported to fix browser navigation when
  `init_script` is combined with `user_data_dir` — precisely the persistent path this
  release wires up. *(That specific release note was read from the upstream changelog
  by a subagent and not independently re-verified here; the upgrade stands on the
  version gap alone.)* Full suite green on 0.4.12: 858 passed.

- `_fetch_scrapling_stealth()` gained a keyword-only `extra_kwargs`. Options are bound
  with `functools.partial` so the cascade dispatch loop still calls every leg with the
  same `(url, timeout)` signature — existing monkeypatch-by-name tests are untouched.

## [1.8.0] — 2026-08-02

> **Consolidating release — reconciles 4 months of unversioned work.**
>
> Version discipline lapsed after `1.7.0` (2026-04-26): **14 commits, 7 of them
> feature drops, all shipped under the same version string.** That is not a
> cosmetic problem — it is what made the 2026-08-02 outage possible. The
> pip-installed copy of `scraperx` was a strict SUBSET of this source (missing
> `scrapling_stealth`, `reddit`, `silent_video_ocr`, `docs_crawler`,
> `fingerprint_audit`, `method_telemetry`) **while reporting the same version
> 1.7.0**, so nothing — not pip, not a human, not an agent — could detect the
> drift. Six documented capabilities were silently unavailable for weeks, and
> the project wiki advertised them the whole time.
>
> Three bookkeeping surfaces had three different truths: `pyproject.toml` said
> 1.7.0, this CHANGELOG's last release said 1.4.3, and the code shipped six
> modules beyond both. `tests/test_version_consistency.py` now makes that
> divergence a **test failure** instead of a silent liability.

### Added — since 1.7.0 (each with the commit that shipped it)

- **`scrapling_stealth`** (`5f08f64`, 2026-05-16) — Cloudflare/Turnstile bypass leg via
  Scrapling's `StealthyFetcher`. `fetch_stealth()` and `fetch_stealth_xhr()`.
  Proven 2026-05-06 on `intel.arkm.com`, 2026-05-31 on DexScreener Base trending
  (403 → 200, 94 token CAs).
- **`reddit`** (`83b65ea`, 2026-07-19) — `RedditScraper`, no-login tiered access
  (old.reddit.com + `.json` endpoints, `fetch_stealth` for walled pages).
  Earlier `8e767f9` (2026-04-27) added jittered inter-sub cooldown and
  retry-with-backoff on 403/429.
- **`method_telemetry`** (`83b65ea`, 2026-07-19) — self-learning cascade ledger;
  `preferred_order()` / `record()`. Currently wired into `reddit.py` only.
- **`fingerprint_audit`** (`5892b0e`, 2026-07-16) — self-verifies the stealth
  fingerprint, the layer Scrapling itself lacks. `diagnose_403()` separates
  `fingerprint` / `ip` / `behavior-or-account` / `not-403`.
- **`silent_video_ocr`** (`5b138b5` + `94cbb35`, 2026-05-17) — frame-OCR for silent
  screen-recordings; auto-fallback from the tweet-video path.
- **`docs_crawler`** (`bef68a0`, 2026-05-03) — exhaustive documentation-site crawler.
- **6 OSINT scraping primitives** ported from wojak-wojtek (`938489a`, 2026-05-01).
- **`explorer_label`** (`0bb896c`, 2026-05-01) — chip extraction from Etherscan-family pages.
- **`fetch_stealth_xhr` contract lock** (`8a3f1d6`, 2026-08-02) — 3 tests pinning the
  required `xhr_pattern` argument and the 3-tuple return. A consumer had drifted to
  `fetch_stealth_xhr(url, timeout=90)` unpacked into two names; the resulting
  `TypeError` was swallowed by a broad handler that logged a line indistinguishable
  from a real Cloudflare block, so the call had **never executed once** while
  producing evidence that it had been walled.

### Fixed

- **Install drift is now detectable.** `tests/test_version_consistency.py` asserts
  `pyproject.toml` version == `scraperx.__version__` == newest CHANGELOG release
  heading. Any future feature drop that forgets the bump fails CI instead of
  silently shipping a phantom version.

### Note on earlier detail

Entries below this line that predate 1.5.0 were written contemporaneously. The
1.5.0/1.6.0/1.7.0 headings were reconstructed from git history on 2026-08-02 —
they were bumped in `pyproject.toml` at the time but never recorded here.

## [1.7.0] — 2026-04-26

### Added

- **TradingView symbol/exchange resolver with negative cache** (`c42a61b`, Phase 2.2 P1).

## [1.6.0] — 2026-04-26

### Added

- **Topic-first GitHub repo discovery** (`73ce24b`, Phase 2.2 P3).

## [1.5.0] — 2026-04-26

### Added

- **`smart_fetch` thin-client** (`64af337`) — Jina → urllib → Playwright cascade.
- `.gitleaksignore` for Solana mint-address false positives (`9e732ae`).

## Appendix — expanded detail for 1.8.0 items

> Not a release. This is the contemporaneous prose that sat in the old
> `[Unreleased]` section until 2026-08-02; it documents the DexScreener stealth
> proof and `docs_crawler` in more depth than the 1.8.0 summary above. Kept
> verbatim rather than deleted — the recipes are load-bearing.

### Added

- **`scrapling_stealth` proven on DexScreener (2026-05-31)** — `fetch_stealth(solve_cloudflare=True)` cracks DexScreener's Cloudflare-walled trending screener (`/base?rankBy=trendingScoreH6` → `403` via Jina/curl → `200` via StealthyFetcher, 94 Base trending token CAs). Recipe in the module docstring + `~/ai/global-graph/tools/scraperx.md`: parse the rendered HTML with regex `"baseToken":{"symbol":"..","address":"0x.."}` (the embedded JSON has `"totalSupply":undefined`, so `json.loads` fails). Install: `pip install 'scrapling[fetchers]' && scrapling install`. The `api.dexscreener.com/latest/dex/*` JSON endpoints stay keyless/un-walled; GeckoTerminal `networks/base/trending_pools` is a clean structured cross-check.
- **`scraperx/docs_crawler.py`** — exhaustive documentation-site crawler. Born from the IC API docs incident (2026-05-03) where an LLM agent claimed "I read the docs" while having only read 5 of 82 pages. The module:
  - Discovers URLs via `sitemap.xml` (with namespace-agnostic fallback for non-conformant XML) or explicit URL list.
  - Fetches each page via curl with a Mozilla UA — Cloudflare-resistant.
  - Extracts prose with a *less aggressive* HTML parser that recovers Docusaurus / VitePress / mkdocs-material content (prior mistake: skipping `<nav>`/`<header>`/`<footer>` along with `<script>` nuked breadcrumbs, sidebars, and 95% of the prose on Docusaurus sites).
  - Flags pages with `<500` chars as "shell candidates" needing playwright render — written to `_SHELLS.md`.
  - Writes `_DIGEST.md` with per-page byte/word counts so the caller can verify coverage at a glance.
  - URL validation defends against curl flag injection (URLs starting with `-`, line breaks, non-http schemes).
  - Path-traversal defence: resolved write paths must stay inside the output directory.
- **`scraperx docs-crawl <root_url>`** — new CLI subcommand. Default output dir `./docs-crawl-<host>/`. Flags: `--max-pages`, `--user-agent`, `--timeout`, `--sleep-between`, `--include-encoded-dups`.
- **22 tests** in `tests/test_docs_crawler.py` covering extractor robustness (nav/header/footer preservation, script/style stripping, runaway-newline guard), sitemap namespace fallback, URL validation (flag-injection guard, line-break rejection), slug safety, end-to-end crawl with stub curl.

### Use case

When an agent says "read every page of docs.example.com":
```bash
scraperx docs-crawl https://docs.example.com/
# → ./docs-crawl-docs.example.com/_DIGEST.md   ← byte-counted page index
# → ./docs-crawl-docs.example.com/*.txt        ← extracted prose, ready to grep
# → ./docs-crawl-docs.example.com/_SHELLS.md   ← pages that need playwright
```

Then any LLM/agent can `grep -l "<topic>" ./docs-crawl-*/` with confidence that nothing was silently skipped.

### Why this matters

Pre-`docs_crawler`, agents would either (a) read 3-5 prose pages and claim coverage, (b) crawl with a too-aggressive HTML parser that returned 83 bytes per page, or (c) download swagger YAML and conflate "have access to" with "have read." This module makes "I read every page" verifiable.

## [1.4.3] — 2026-04-25

Bug-fix release: production-grade SQLite WAL hygiene across all storage callsites. Important for anyone running scraperx components as long-lived daemons (BMW corpus ingester, Reddit/KBA/forum scrapers, GitHub analyzer in batch mode) — closes the unbounded-WAL disaster vector.

### Added

- **`scraperx/_sqlite_pragmas.py`** — shared `apply_pragmas(conn)` helper that applies the production-grade WAL hygiene stack (`journal_mode=WAL`, `journal_size_limit=64MB`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`, `mmap_size=256MB`, `temp_store=MEMORY`). Idempotent. Per-connection PRAGMAs are LOST on close, so the helper MUST run on every new connection — that's why it's a function, not a one-time DB header write.
- **`tests/test_sqlite_pragmas.py`** — 7 tests covering the helper itself + end-to-end PRAGMA verification on `SocialDB`, `AvatarMatcher`, and `VerifiedAvatarRegistry`.

### Fixed

- **Closes the unbounded-WAL disaster vector** for long-running scraperx daemons (BMW corpus ingester, Reddit/KBA/forum scrapers running 24/7). Pre-1.4.3, `~/.scraperx/social.db` had `journal_size_limit=-1` (uncapped) — same root cause that produced an 87 GB WAL on a sister project. With `journal_size_limit=64MB` + `wal_autocheckpoint` defaults, the WAL is bounded by design.
- **`scraperx/social_db.py`** `SocialDB.__init__`: was setting only `PRAGMA journal_mode=WAL`. Now applies the full 7-PRAGMA stack via `apply_pragmas()`.
- **`scraperx/avatar_matcher.py`** `AvatarMatcher.__init__`: was setting only `PRAGMA journal_mode=WAL`. Now applies the full stack.
- **`scraperx/avatar_matcher.py`** `VerifiedAvatarRegistry.__init__`: was setting **NO PRAGMAs at all** — implicitly assumed another consumer (`SocialDB` / `AvatarMatcher`) had opened the shared `~/.scraperx/social.db` first. That assumption broke whenever a fresh process imported `VerifiedAvatarRegistry` standalone (e.g. the GitHub analyzer telemetry path). Now applies the full stack on every connect.

### Notes

The fix is fully additive — no schema migration, no behaviour change beyond performance + safety. Existing DB files keep working; the WAL bound only takes effect on the next checkpoint after upgrade.

Research grounding (2026): loke.dev "20GB WAL File That Shouldn't Exist", oneuptime "How to Set Up SQLite for Production Use", powersync "SQLite Optimizations For Ultra High-Performance", phiresky tune.md, sqlite.org/pragma.html.

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
