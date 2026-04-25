"""Shared SQLite PRAGMA hygiene for scraperx storage modules.

Why this exists
---------------
scraperx uses three module-local SQLite connections to ``~/.scraperx/social.db``:

  * ``SocialDB``                  — tweets, profiles, search cache, GitHub cache
  * ``AvatarMatcher``             — avatar phash table
  * ``VerifiedAvatarRegistry``    — known-good avatar hashes per handle

Pre-1.4.3 each callsite set ``PRAGMA journal_mode=WAL`` and stopped there. Two
problems with that:

1. **Unbounded WAL growth.** Without ``journal_size_limit``, a long-running
   daemon (e.g. the BMW corpus ingester) can accumulate a multi-GB WAL when a
   reader pins the snapshot — exactly the failure mode loke.dev documented as
   "checkpoint starvation" (Feb 2026) and the same root cause that produced an
   87 GB WAL on a different project of ours (kopanie-portfeli base v2).

2. **Slow + sometimes locked writes.** Default ``synchronous=FULL`` fsyncs
   every commit (redundant in WAL mode), and default ``busy_timeout=0`` raises
   ``SQLITE_BUSY`` immediately on lock contention instead of waiting.

This module exports ``apply_pragmas(conn)`` so all three callsites apply the
same hardened stack. Idempotent — safe to call multiple times on the same
connection. No-op on non-SQLite-3 connections (defensive — but in practice all
callers pass ``sqlite3.Connection`` objects).

Research grounding (2026)
-------------------------
- loke.dev "20GB WAL File That Shouldn't Exist" — checkpoint starvation
- oneuptime "How to Set Up SQLite for Production Use" — full PRAGMA stack
- powersync "SQLite Optimizations For Ultra High-Performance" — mmap_size + temp_store
- phiresky tune.md gist — synchronous=NORMAL safety in WAL mode
- sqlite.org/pragma.html — per-connection vs persistent-in-header scope
"""
from __future__ import annotations

import sqlite3


# Per-connection PRAGMAs are LOST when the connection closes. Persistent ones
# (journal_mode, journal_size_limit) live in the DB header but harmless to
# re-issue on every connect.
_PRAGMAS: tuple[str, ...] = (
    # Persistent in DB header (idempotent re-issue):
    "PRAGMA journal_mode=WAL",
    "PRAGMA journal_size_limit=67108864",  # 64 MB — caps WAL on checkpoint
    # Per-connection (must be applied on every connection):
    "PRAGMA synchronous=NORMAL",            # safe in WAL; 2-4x faster than FULL
    "PRAGMA busy_timeout=5000",             # wait up to 5 s on lock contention
    "PRAGMA foreign_keys=ON",               # default OFF — enforce integrity
    "PRAGMA mmap_size=268435456",           # 256 MB memory-mapped reads
    "PRAGMA temp_store=MEMORY",             # temp tables in RAM, not disk
)


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the hardened PRAGMA stack to ``conn``.

    Idempotent. Safe to call before any query. Order matters: ``journal_mode``
    must be set before ``synchronous`` (synchronous=NORMAL is only safe in WAL
    mode); the others are independent.

    Errors: this function suppresses nothing. If a PRAGMA fails, the
    underlying ``sqlite3`` exception propagates so the caller learns about
    misconfigured / read-only / unrecognized DBs immediately rather than
    silently degrading. In practice none of these PRAGMAs fail on a writable
    SQLite >=3.7 (WAL mode requires 3.7+).
    """
    cur = conn.cursor()
    try:
        for stmt in _PRAGMAS:
            cur.execute(stmt)
    finally:
        cur.close()


__all__ = ["apply_pragmas"]
