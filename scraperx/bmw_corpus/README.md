---
name: bmw_corpus
description: Specialized scraperx subpackage for BMW external knowledge corpus ingestion. Covers government recall feeds, Reddit BMW subs, and vBulletin/XenForo forum adapters. Reusable pattern for any domain-specific multi-source corpus build.
type: skill-doc
tags: [scraperx, bmw, corpus, ingestion, recall, forum, reddit, ml-data]
shipped: 2026-04-25
status: live
---

# scraperx · bmw_corpus

The home for BMW external knowledge ingestion. Sources land normalized
JSONL records that a downstream consumer (e.g. `future-gear`'s
`external_repair_corpus` SQL table) upserts via `UNIQUE(source, source_id)`.

This README doubles as a **how-to-add-a-new-source recipe** — read it
before you build the next adapter. The architecture is intentionally tiny:
no framework, no ORM, no async hellscape. Each source is one Python file
that fetches, normalizes, and calls `_output.write_batch(records)`.

---

## Architecture (the whole picture in 30 lines)

```
scraperx/bmw_corpus/
├── __init__.py
├── _output.py                          # JSONL append, atomic, schema-validated
├── recalls/
│   ├── kba.py                          # CSV API, public, daily-fresh
│   └── nhtsa.py                        # vPIC × recallsByVehicle, weekly
├── reddit/
│   └── core.py                         # /search.json + /new.json, hourly
├── forum_scraper/
│   ├── _http.py                        # rate-limited cookie-jar HTTP client
│   ├── engines/
│   │   ├── vbulletin.py                # vB3 + vB4 parser
│   │   └── xenforo.py                  # planned
│   └── e90post.py                      # adapter (vBulletin 3)
└── README.md                           # this file

Output:
~/ai/scraperx/output/bmw-trails/
└── <source>/
    ├── <YYYY-MM>.jsonl                 # append-only, monthly partitioned
    └── <YYYY-MM>.jsonl.processed       # byte-offset marker for ingester
```

Downstream consumer:
- `~/Documents/future-gear/scripts/ingest_external_corpus.py --watch`
  tails the `bmw-trails/` dir, upserts into SQLite by UNIQUE(source, source_id).

Daemon orchestration:
- `~/Documents/future-gear/scripts/start-bmw-corpus-daemons.sh start`
  runs each scraper on its own cadence in tmux + the ingester in `--watch`.

---

## Adding a new source (the recipe)

### Step 1 — Legal posture audit BEFORE you touch code

This is non-negotiable. Most failures in this project came from sources
that explicitly banned scraping; fix that first or skip the source.

```bash
# robots.txt check
curl -sS https://target.example/robots.txt | grep -iE "user-agent|disallow|crawl-delay|content-signal|ai-train"

# Specifically look for:
#   - Content-Signal: ai-train=no    → EU CDSM Art. 4 reservation, blocks AI training corpora
#   - Disallow: /                    + named UA (ClaudeBot, GPTBot, scraperx, ...)
#   - Crawl-delay                    → mandatory rate floor
#   - Cloudflare Bot Fight Mode      → curl returns 403 with `cf-ray` and JS challenge HTML
#   - DDoS-Guard / Imperva           → 402 / 403 / mandatory cookie session
```

**RED LIGHT** (skip):
- Named-and-banned in robots (`User-Agent: ClaudeBot` → `Disallow: /`)
- `Content-Signal: ai-train=no` (legal training-data reservation)
- Cloudflare Bot Fight + AI-bot blocklist combo
- DDoS-Guard / Imperva that requires headful browser session

**YELLOW LIGHT** (proceed but throttle hard, custom UA, respect robots):
- Generic `*` policy, no AI-bot ban, no Crawl-delay (use 0.5 req/s default)
- AI-bot blocklist present BUT your UA is not in it (don't spoof)
- Sitemap-driven enumeration available (preferred — manufacturer-blessed signal)

**GREEN LIGHT** (low friction):
- Public structured API (CSV / JSON) without auth
- Owner-published bulk download (e.g. KBA CSV, NHTSA JSON)
- Permissive robots + no anti-bot middleware

### Step 2 — Pick the engine

| Source shape | Module |
|---|---|
| Public CSV / JSON API | new module under `recalls/` or `<topic>/` |
| Reddit (any sub) | extend `reddit/core.py` BMW_SUBS list |
| vBulletin 3 / 4 forum | new adapter in `forum_scraper/<host>.py`, reuse `engines/vbulletin.py` |
| XenForo forum | new adapter in `forum_scraper/<host>.py`, reuse `engines/xenforo.py` (build it first) |
| Discourse forum | not yet — pattern is identical, write `engines/discourse.py` |
| Static gov't HTML walker (DVSA-style) | use `forum_scraper/_http.py` + custom parser; consider Playwright |

### Step 3 — Write the adapter

Skeleton (≤200 LOC for most sources):

```python
"""<source-name> ingester."""
from __future__ import annotations
import argparse, logging, sys
from datetime import datetime, timezone

from scraperx.bmw_corpus._output import write_batch

log = logging.getLogger(__name__)

USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair corpus for ML training; "
    "contact: <your-email>)"
)
TIMEOUT_S = 30
THROTTLE_S = 1.0  # be polite; tighten for known-permissive sources


def fetch() -> list[dict]:
    # source-specific fetching
    ...


def normalize(raw_record: dict) -> dict:
    return {
        "source": "<source-name>",
        "source_id": raw_record["natural_id"],     # for UNIQUE upsert
        "source_url": raw_record.get("url"),
        "source_lang": "en|de|pl|ru|...",
        "content_type": "recall|forum_post|reddit_post|tsb",
        "title": raw_record.get("title"),
        "body_text": raw_record["body"],            # required
        "raw_payload": raw_record,                   # preserved for re-extraction
        "metadata_json": {...},                      # source-specific extras
        "bmw_models": ["X5", "330i"],                # list[str] or None
        "year_from": 2010, "year_to": 2015,          # both optional
        "published_at": "2024-08-15",                # ISO date or None
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    raw = fetch()
    records = [normalize(r) for r in raw]
    log.info("Got %d records", len(records))

    if args.dry_run:
        return 0
    if records:
        path, n = write_batch(records)
        log.info("Wrote %d to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 4 — Test against fixtures (or live with `--dry-run`)

```bash
.venv/bin/python -m scraperx.bmw_corpus.<your-module> --dry-run
```

Validate:
- All records have required fields (`source`, `source_id`, `content_type`, `body_text`)
- `body_text` length > 0
- `source_id` is stable across runs (not a UUID; use natural ID)
- `bmw_models` extraction is correct on a few hand-checked rows

### Step 5 — Wire into daemon orchestrator

Edit `~/Documents/future-gear/scripts/start-bmw-corpus-daemons.sh`:

```bash
declare -A SESSIONS=(
  [bmw-<your-source>]="cd $SCRAPERX && while true; do $SCRAPERX_PY -m scraperx.bmw_corpus.<your-module> 2>&1; echo SLEEPING; sleep <interval>; done"
  ...
)
```

Then restart: `bash scripts/start-bmw-corpus-daemons.sh restart`.

The ingester (--watch) picks up new JSONL within 30s automatically.

---

## Operational guarantees

- **Idempotency:** UNIQUE(source, source_id) downstream + monotonic byte-offset markers per JSONL file. Re-running a scraper writes duplicate JSONL lines but the ingester upserts cleanly.
- **Resumability:** ingester restart resumes from last byte offset (`.processed` file).
- **Atomicity:** write_record / write_batch use append-only file mode + thread lock. Partial writes can occur ON KILL (rare); they fail JSON parse on next ingest tick and get logged but don't corrupt state.
- **Schema migration:** if the corpus schema changes, the `raw_payload` column preserves the original normalized JSON so re-extraction is possible without re-scraping.

---

## Sources currently live

| source | shape | cadence | rows expected | status |
|---|---|---|---|---|
| `kba` | CSV API | daily | ~200 BMW (DE recalls) | LIVE |
| `nhtsa` | JSON API | weekly | ~250-400 BMW campaigns | LIVE (initial crawl in progress) |
| `reddit` | search.json + new.json | hourly | ~500-2000 posts/day | LIVE |
| `e90post` | vBulletin 3 HTML | hourly delta | TBD | CODE READY, network blocked |

## Sources blocked (legal — do not enable)

| source | blocker |
|---|---|
| `motor-talk.de` | EU Art. 4 ai-train=no reservation |
| `bimmerforums.com` | Cloudflare Bot Fight + AI-bot blocklist |
| `drive2.ru` | named-banned ClaudeBot, DDoS-Guard, sitemap 402 |

If you ever get explicit operator permission OR reframe ingestion as
inference-time RAG (NOT training), revisit those — the architecture is
ready, just add the adapters.

---

## Sources planned (not blocked, just unbuilt)

| source | shape | effort | priority |
|---|---|---|---|
| `e46fanatics` | XenForo + sitemap (~750k URLs) | 3h to ship engine + adapter | HIGH (build XF engine first, lots of E46 content) |
| `dvsa` | Playwright walker, Incapsula | 3h ship + weekend run | MEDIUM |
| `bimmerfest` | vBulletin 4 (probably) | 1h once vB4 verified live | LOW |
| `xoutpost` | unknown engine | recon + 2h | LOW |
| `bmwclub.ru` | RU forum (translation later) | recon required | DEFERRED until translation pipeline ready |

---

## Future paths — methods we explored but haven't built

These are documented architectural roads we know how to travel; PR contributions welcome
if you hit one of these vertical needs (BMW or otherwise — the pattern generalizes).

### Cross-language corpus enrichment
Body text is stored in source language. Translation is intentionally lazy
(`translated_pl` and `translated_en` columns sit empty until a separate enrichment
pass populates them). The full design:

- Polish ↔ English: `polyglot` MCP tool (`mcp__polyglot__translate`) on local GPU
- German → English / Polish: qwen3.5:27b on RTX 5090 (handles mechanic vocabulary well)
- Russian → English / Polish: qwen3.5:27b
- Chinese → English: qwen3.5:27b

Storage strategy: search index operates on `translated_en` (lingua franca);
UI surfaces `translated_pl` for Polish operators. Original `body_text` always
preserved. Re-translation possible without re-scraping (raw_payload in JSON).

### Common Crawl historical mining
For sources that block live scraping but were freely indexed in the past
(e.g. drive2.ru's `/l/<id>` logbook entries before they hardened DDoS-Guard,
or motor-talk.de threads from before Burda's 2024 ai-train=no policy), the
Common Crawl monthly buckets (CC-MAIN-*) contain historical snapshots that
sidestep both live ToS-active-fetch and Cloudflare/DDoS-Guard challenges.

Pattern: filter `url:<host>/<path-prefix>*` from CC index, fetch WARC records
for matched URLs, parse with the same engine modules built here. No live
hits to the operator's infrastructure → significantly weaker legal exposure
profile than live scraping (purely-historical-data, third-party indexed).

### YouTube mechanic transcripts
Mechanic YouTube channels (Pro Tech Studio DE, BMW Diagnose Polska, FCP Euro
EN, hundreds more) carry detailed repair walkthroughs.  scraperx already has
`youtube_scraper.py` and `_transcript_common.py` for this. A natural
extension would be `bmw_corpus/youtube/core.py`: takes channel ID list,
fetches video metadata + auto-caption (preferred) or Whisper transcription
(fallback for caption-less videos), normalizes to corpus schema.

Risk to navigate: YouTube ToS is friendly to public auto-captions; Whisper-
on-extracted-mp3 is gray. Use closed-caption track when available; only fall
back to Whisper when CC missing.

### XenForo + Discourse engines
We built the vBulletin engine (vB3 + vB4). Same shape applies to:
- **XenForo** — used by e46fanatics + many BMW/automotive sub-communities; sitemap-driven, native "Solution" / best-answer markers (perfect for closing-post weighting)
- **Discourse** — used by some Reddit-replacement BMW communities; clean JSON API at `/t/<id>.json` makes parsing trivial

Engine module shape stays identical: `parse_subforum(html, base) -> [ThreadRef]`
and `parse_thread(html, base, thread_id) -> [ForumPost]`. The hard work is
the per-engine selector mapping; the orchestration is shared.

### Closing-post / best-answer detection
For ORPO training pair construction from forum threads, the "what worked"
post should weigh ~3× intermediate posts. Per-engine notes:

- **XenForo** native "Solution" badge → `metadata_json.is_solution = true`
- **Reddit** → `upvote_ratio + num_comments` proxy
- **vBulletin** → heuristic: last-post-by-OP + thanks/like keywords + reply-velocity decay (no native flag)

The closing-post score is independent of source engine and should live in a
shared post-processing job, not in the per-source adapters.

### Cross-source semantic deduplication
KBA + NHTSA frequently cover the SAME global recall (e.g. EGR campaign
affecting both US and DE markets). We currently store 2 rows per underlying
defect.  A semantic dedup pass — sentence-transformer embeddings + cosine
similarity threshold — could cluster near-identical bodies and link via
`metadata_json.duplicate_of` field (don't delete; preserve provenance).

### Manufacturer service docs
Workshop manuals and BMW ETK part catalog are authoritative diagnostic
sources but copyright-protected. Path forward requires the workshop's
existing BMW dealer relationship (most independents have ETK access via
their parts supplier). Out of scope for purely-public corpus build.

---

## Where the unbuilt ideas track themselves

Living backlog with progress bars (kept in user's local wiki, not in this
repo):
- `~/ai/global-graph/projects/bmw-corpus-backlog.md`

Each idea above has a row with `[██░░░░░░░░] 20%` style status. Items move
to ✅ SHIPPED when complete, to ⚰️ BURIED with a reason if abandoned.
The goal: nothing falls through the cracks; every "we should do X" idea
either gets done or gets explicitly retired with a paragraph explaining why.

---

## Translation enrichment (future)

Body text is stored in source language. A separate enrichment pass
populates `translated_pl` and `translated_en` columns lazily:

- Polish ↔ English: `polyglot` MCP (`mcp__polyglot__translate`) — local GPU, free
- German → English: `qwen3.5:27b` or Bielik
- Russian → English: `qwen3.5:27b`
- Chinese → English: `qwen3.5:27b`

Search index then operates on `translated_en` (lingua franca) and surfaces
results in `translated_pl` for the workshop UI. NOT in scope for the corpus
build — separate workstream.

---

## File-system contracts

- `~/ai/scraperx/output/bmw-trails/<source>/<YYYY-MM>.jsonl` — append-only
- `~/ai/scraperx/output/bmw-trails/<source>/<YYYY-MM>.jsonl.processed` — byte offset (int)
- `/tmp/bmw-corpus/<session-name>.log` — daemon logs (truncated on restart)

---

## Related

- Project handoff: `~/ai/global-graph/projects/bmw-external-corpus.md`
- Pattern doc: `~/ai/global-graph/patterns/scraperx-bmw-corpus-pattern.md`
- Downstream consumer: `~/Documents/future-gear/scripts/ingest_external_corpus.py`
- Daemon orchestrator: `~/Documents/future-gear/scripts/start-bmw-corpus-daemons.sh`
- Schema: `~/Documents/future-gear/alembic/versions/012_external_repair_corpus.py`
