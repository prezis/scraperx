# Next Session — GitHub Deep Analyzer

**Purpose:** Resume implementing `scraperx.github_analyzer` with zero context load. This doc is the handoff. Read it, validate the open questions, then execute the task list.

---

## The problem we're solving

User pastes a GitHub repo URL and currently has to prompt 5+ times to find out:
- Is the repo actually good or abandoned?
- Is it breakthrough / discovery-worthy or reinventing the wheel?
- Are there better alternatives (sometimes in a fork of the same repo)?
- What does the community say — HN, Reddit, Twitter, blogs?
- Was it "repo of the day" recently (a fast signal of relevance)?

**Goal:** one CLI call → complete trust verdict with comparables + external mentions.

## Why one module, not 100 tools

User explicitly wants scraperx to be a Swiss Army knife with many talents, NOT a registry of 100 separate scrapers. Keeps mental overhead low — one library, many verbs.

## Landscape audit (done 2026-04-18)

| Tool | Stars | What it does | Our gap |
|---|---:|---|---|
| `github/github-mcp-server` | 28.9k | Official MCP — generic tool wrapper (issues, code search, commits) | **No quality scoring, no comparables, no external mentions** |
| `cyclotruc/gitingest` | 14.3k | Repo→LLM prompt dump | **No assessment** |
| `yamadashy/repomix` | 23.6k | Same class as gitingest | **No assessment** |
| `mufeedvh/code2prompt` | 7.3k | Same | **No assessment** |
| `Aider-AI/aider` | 43.5k | AI pair-programming CLI | Not a scraper |

**Verdict: genuine gap. BUILD (don't adopt).** None of these answer "is this repo worth using, is there a better alternative, and what does the community say?"

## Existing infrastructure to leverage

- **`scraperx` core** — stdlib HTTP, `social_db.py` SQLite cache, `search_tweets()` already works
- **`local-ai-mcp.tools.web_research`** — `local_web_search`, `local_web_fetch`, `local_web_deep` (SearXNG + Jina Reader + qwen3.5:27b synthesis, all local, FREE). No need to write per-platform scrapers for Substack/Bluesky/Medium etc. — use local_web_search instead.
- **`gh` CLI** — installed, unauthenticated 60/hr, authenticated 5000/hr with PAT
- **GitHub REST API** — verified live: `/repos/:owner/:repo`, `/forks`, `/contributors`, `/commits`
- **GitHub Trending** — HTML only, but verified `github.com/trending` returns 200
- **HN Algolia API** — free, verified: 94 hits for "yt-dlp", top-scored 1244↑
- **Reddit JSON** — free, verified works unauth

## Source rubric + voting (done via local GPU qwen3.5:27b, ultrathink)

Scored on 4 axes (signal density, accessibility, coverage, freshness) 1-5 each = /20:

### Tier A — dedicated integration (direct API)

| # | Source | Score | API |
|---|---|---:|---|
| 1 | Reddit | 19/20 | `reddit.com/search.json` (JSON unauth) |
| 2 | Hacker News | 18/20 | `hn.algolia.com/api/v1/search` (free) |
| 3 | Stack Overflow | 18/20 | StackExchange API (free, unauth 300/day) |
| 4 | GitHub Discussions | 18/20 | GitHub GraphQL on repo |
| 5 | GitHub Trending | 17/20 | HTML scrape `github.com/trending` |
| 6 | arXiv | 17/20 | `export.arxiv.org/api/query` (free XML) |
| 7 | Papers With Code | 17/20 | `paperswithcode.com/api/v1` (free) |
| 8 | DEV.to | 17/20 | `dev.to/api/articles?search=...` (free) |
| 9 | X/Twitter | 16/20 | `scraperx.search_tweets()` (existing) |
| 10 | Substack | 16/20 | individual blogs + `local_web_search` |
| 11 | Semantic Scholar | 16/20 | `api.semanticscholar.org` (free) |
| 12 | YouTube | 16/20 | `scraperx.youtube_scraper` (existing) |

### Tier B — generic `local_web_search` query

Lobsters (15), Google Scholar (15), Product Hunt (14), Medium (13), LinkedIn (13), Bluesky (12), Discord (12).

Don't write dedicated scrapers for these — call `local_web_search("site:lobste.rs github.com/owner/repo")` and let SearXNG handle it.

### Tier C — skip

Mastodon (10) — federated, fragmented, signal-to-noise too low.

## Architecture decision

```
scraperx/
├── github_analyzer/
│   ├── __init__.py          # public API
│   ├── core.py              # GithubAnalyzer class + analyze_repo()
│   ├── github_api.py        # REST adapters (repos, forks, commits, contributors)
│   ├── trending.py          # github.com/trending HTML scraper
│   ├── mentions/            # Tier A dedicated integrations
│   │   ├── hn.py            # HN Algolia
│   │   ├── reddit.py        # Reddit JSON
│   │   ├── stackoverflow.py # StackExchange API
│   │   ├── devto.py         # dev.to API
│   │   ├── arxiv.py         # arXiv search
│   │   └── pwc.py           # Papers With Code
│   ├── semantic.py          # Tier B wrapper — calls local_web_search
│   ├── scoring.py           # scoring heuristics (bus factor, momentum)
│   ├── synthesis.py         # qwen3.5:27b verdict via local-ai-mcp
│   └── schemas.py           # GithubReport + RepoTrustScore dataclasses (stdlib, NOT Pydantic)
```

**Single entry point:**
```python
from scraperx.github_analyzer import analyze_repo
report = analyze_repo("https://github.com/owner/repo")
```

**CLI:**
```bash
scraperx github OWNER/REPO
scraperx github OWNER/REPO --json
scraperx github OWNER/REPO --deep        # full external-mentions aggregation
scraperx trending --since daily --lang python
```

**MCP tool exposure** (via local-ai-mcp):
```
local_github_analyze(repo_url: str) -> str    # markdown report
local_github_trending(since: str, lang: str) -> str
```

## Task list (ordered, ~15h total)

### Phase 1 — Scaffolding (2h)

1. **T1 — Module skeleton + schemas** (1h)
   - Create `scraperx/github_analyzer/` dir
   - `schemas.py` — `GithubReport`, `ExternalMention`, `ForkInfo`, `ContributorInfo` dataclasses (stdlib only)
   - `__init__.py` exports (add to `scraperx/__init__.py` too)
   - Stub `core.py::GithubAnalyzer`
   - **NOTE:** use `@dataclass` NOT Pydantic. scraperx is stdlib-only core.

2. **T2 — SQLite caching layer** (1h)
   - Extend `social_db.py` with tables: `github_repo_cache`, `github_fork_cache`, `github_mentions_cache`
   - TTL strategy: repo metadata 24h, commits/issues 6h, external mentions 4h
   - Methods: `save_repo_cache`, `get_repo_cache` (with TTL check)

### Phase 2 — Core GitHub adapter (3h)

3. **T3 — `github_api.py` REST adapter** (2h)
   - Unauth/auth detection (via `GITHUB_TOKEN` env)
   - Rate-limit header parsing + graceful backoff
   - Fetch methods: `get_repo()`, `get_contributors()`, `get_recent_commits()`, `get_releases()`, `get_top_forks()`, `get_readme()`, `get_workflows()` (detect CI)
   - Return raw JSON; scoring is separate

4. **T4 — `scoring.py` heuristics** (1h)
   - `bus_factor_score()` — from contributor commit distribution
   - `momentum_score()` — from 90d star delta, commit cadence
   - `health_score()` — from issue close rate, PR merge rate
   - `readme_quality_score()` — from length, section count, code-example presence

### Phase 3 — External mentions (3h)

5. **T5 — `mentions/hn.py`** (0.5h) — HN Algolia search by repo URL
6. **T6 — `mentions/reddit.py`** (0.5h) — Reddit search by repo URL
7. **T7 — `mentions/stackoverflow.py`** (0.5h) — StackExchange API tag search
8. **T8 — `mentions/devto.py`** (0.5h) — dev.to articles search
9. **T9 — `mentions/arxiv.py` + `pwc.py`** (1h) — arXiv + Papers With Code for algorithmic topics
   - Use topics/keywords from repo to find related papers

### Phase 4 — Semantic layer (Tier B) (1.5h)

10. **T10 — `semantic.py`** (1.5h)
    - Wraps `local_web_search` (call via subprocess or MCP)
    - Build queries like `site:lobste.rs OR site:substack.com "github.com/owner/repo"`
    - Parse + normalize results into same `ExternalMention` shape as Tier A
    - Graceful degradation if local-ai-mcp unavailable → return empty with note

### Phase 5 — Trending + Discovery (1.5h)

11. **T11 — `trending.py`** (1.5h)
    - Scrape `github.com/trending?since=daily|weekly|monthly`
    - Parse with regex or BS4 (optional)
    - Return list of `TrendingRepo(name, description, stars, stars_today, language)`
    - CLI subcommand: `scraperx trending --since daily --lang python`

### Phase 6 — Synthesis + CLI (2h)

12. **T12 — `synthesis.py`** (1h)
    - Collect all data into GithubReport
    - Call local-ai-mcp for qwen3.5:27b verdict
    - Prompt template: "given metrics + mentions + comparables, produce 3-bullet verdict + score 1-100"
    - Inline citations [1][2] linking to mentions
    - Graceful degradation: if qwen unavailable, return raw data + warn

13. **T13 — CLI integration** (1h)
    - `__main__.py` dispatch for `github` + `trending` subcommands
    - Human + `--json` output modes
    - Update README.md

### Phase 7 — Testing + Infrastructure (2h)

14. **T14 — Unit tests** (1.5h)
    - Mock urlopen responses per source
    - Fixtures for GitHub API responses, HN hits, Reddit JSON
    - Cache hit/miss tests
    - Rate-limit backoff test

15. **T15 — MCP tool exposure** (0.5h)
    - In `~/ai/local-ai-mcp/server.py` — add `local_github_analyze` and `local_github_trending`
    - Register in tools/ list
    - Bump scraperx pin in `local-ai-mcp/requirements.txt`

### Phase 8 — Docs (1h)

16. **T16 — Documentation** (1h)
    - `CHANGELOG.md` — 1.4.0 entry
    - `README.md` — new feature section with examples
    - `global-graph/tools/scraperx.md` — update routing table + docs for 1.4.0
    - `global-graph/patterns/gpu-first-enforcement.md` — add github-analyzer row
    - Version bump both `pyproject.toml` and `__init__.py` → 1.4.0

## Open questions — need user input before next session

1. **Auth strategy** — Do we:
   - (a) Run unauth-only in MVP (60/hr — enough for personal use, limits bulk analysis)
   - (b) Prompt for `GITHUB_TOKEN` on first run and persist in `~/.scraperx/config.json`
   - (c) Document env var setup in README and assume user handles it

2. **Trending filter defaults** — When running `scraperx trending` without flags:
   - (a) Daily + all languages (GitHub's default)
   - (b) Weekly + user's primary language (detected from recent git activity)
   - (c) Ask each time

3. **Synthesis model** — For the verdict:
   - (a) Always qwen3.5:27b (slow first call — model swap, high quality)
   - (b) qwen3:4b for fast first pass + qwen3.5:27b on-demand via `--deep` flag
   - (c) Configurable via env var

4. **Scope creep guard** — Do we also analyze:
   - (a) Sibling GitHub projects by same owner (detect mono-repos, side-projects)
   - (b) GitHub Actions usage stats (does repo use the GH features it advertises?)
   - (c) Security advisories on the repo via GHSA

5. **Module naming** — `scraperx.github_analyzer` (long, explicit) or `scraperx.gh` (short, ambiguous with gh CLI) or `scraperx.github` (reserved-word-ish)?

## Session handoff

**What this session did:**
- Audited existing tools (no direct competitor)
- Tested API reachability (HN, Reddit, GitHub Trending all ✅)
- Local GPU rubric-judged 20 discussion platforms
- Produced 16-task sequenced plan
- Leveraged existing `local_web_search` to avoid 7 platform-specific scrapers

**What next session needs:**
- User answers to the 5 open questions above (2 minutes)
- Start at T1, execute sequentially, commit per phase
- ~15h estimated — can compress to ~10h with parallel GPU drafting

**Files not to touch this session:**
- Don't modify any existing scraperx module
- Don't change `pyproject.toml` until T16 (version bump last)
- Don't commit until T16 is done (one atomic 1.4.0 release)

---

**Generated:** 2026-04-18 via local GPU rubric-judge + voting  
**Related:** [[patterns/context-engineering-playbook]] · [[tools/local-ai-mcp]] · [[tools/scraperx]]
