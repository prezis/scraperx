"""External-platform mention adapters for github_analyzer.

Each module in this package exposes a single `search(owner, repo, db=None, **kw)`
function that returns `list[ExternalMention]`. If a SocialDB is passed, results
are cached via `save_mentions_cache`/`get_mentions_cache` with per-adapter
default TTL (typically 4h).

Adapters (Tier A — dedicated API integrations):
    hn              Hacker News (Algolia)
    reddit          Reddit JSON search
    stackoverflow   StackExchange API
    devto           dev.to articles
    arxiv           arXiv search (XML — parsed via stdlib ElementTree)
    pwc             Papers With Code

Tier B (generic `local_web_search` wrappers) live in ../semantic.py.

All adapters share a common contract:
    - Never raise — return [] on any network / parse error, log at WARNING
    - Normalise platform-specific fields to the `ExternalMention` dataclass
    - `source=` is the canonical short key used in the SQLite cache + report
"""

from scraperx.github_analyzer.mentions.arxiv import search as arxiv_search
from scraperx.github_analyzer.mentions.devto import search as devto_search
from scraperx.github_analyzer.mentions.hn import search as hn_search
from scraperx.github_analyzer.mentions.pwc import search as pwc_search
from scraperx.github_analyzer.mentions.reddit import search as reddit_search
from scraperx.github_analyzer.mentions.stackoverflow import search as stackoverflow_search

# Registry — used by core.py to iterate all Tier-A sources in one pass.
ALL_SOURCES = {
    "hn": hn_search,
    "reddit": reddit_search,
    "stackoverflow": stackoverflow_search,
    "devto": devto_search,
    "arxiv": arxiv_search,
    "pwc": pwc_search,
}

__all__ = [
    "ALL_SOURCES",
    "arxiv_search",
    "devto_search",
    "hn_search",
    "pwc_search",
    "reddit_search",
    "stackoverflow_search",
]
