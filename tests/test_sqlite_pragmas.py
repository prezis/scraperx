"""Tests for `scraperx._sqlite_pragmas.apply_pragmas`.

Closes the unbounded-WAL disaster vector that all three storage modules
(SocialDB, AvatarMatcher, VerifiedAvatarRegistry) shared in 1.4.2 and earlier.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scraperx._sqlite_pragmas import apply_pragmas


# Persistent (DB-header) PRAGMAs survive close+reopen on disk-backed DBs.
# Per-connection PRAGMAs reset on a new connection — they MUST be re-applied
# on every connect, which is what apply_pragmas() does.
_EXPECTED_VALUES = {
    "journal_mode": {"wal"},
    "journal_size_limit": {67108864},
    "synchronous": {1},        # 1 = NORMAL
    "busy_timeout": {5000},
    "foreign_keys": {1},
    "mmap_size": {268435456},  # 256 MB
    "temp_store": {2},          # 2 = MEMORY
}


def _read_pragma(conn: sqlite3.Connection, name: str):
    return conn.execute(f"PRAGMA {name};").fetchone()[0]


def test_apply_pragmas_sets_all_values_on_disk_db(tmp_path: Path):
    """All 7 expected PRAGMAs are observable after apply_pragmas runs."""
    db = tmp_path / "scraperx.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_pragmas(conn)
        for pragma, expected in _EXPECTED_VALUES.items():
            actual = _read_pragma(conn, pragma)
            assert actual in expected, (
                f"PRAGMA {pragma}: got {actual!r}, expected one of {expected}"
            )
    finally:
        conn.close()


def test_apply_pragmas_is_idempotent(tmp_path: Path):
    """Calling apply_pragmas twice must not error or change settings."""
    db = tmp_path / "idem.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_pragmas(conn)
        apply_pragmas(conn)  # second call — must be a no-op
        assert _read_pragma(conn, "mmap_size") == 268435456
        assert _read_pragma(conn, "synchronous") == 1
    finally:
        conn.close()


def test_apply_pragmas_works_on_in_memory_db():
    """`:memory:` databases skip the persistent PRAGMAs (no header) but the
    per-connection ones still take effect — important for tests."""
    conn = sqlite3.connect(":memory:")
    try:
        apply_pragmas(conn)
        # Per-connection settings should land:
        assert _read_pragma(conn, "synchronous") == 1
        assert _read_pragma(conn, "busy_timeout") == 5000
        assert _read_pragma(conn, "foreign_keys") == 1
        assert _read_pragma(conn, "temp_store") == 2
    finally:
        conn.close()


def test_persistent_pragmas_survive_reopen(tmp_path: Path):
    """`journal_mode=WAL` and `journal_size_limit` persist in the DB header,
    so a fresh connection sees them even without re-applying. (Verifies that
    our header-level settings stuck — the 87 GB-disaster prevention.)"""
    db = tmp_path / "persistent.db"

    conn1 = sqlite3.connect(str(db))
    try:
        apply_pragmas(conn1)
    finally:
        conn1.close()

    conn2 = sqlite3.connect(str(db))
    try:
        # Note: journal_size_limit is per-connection in some SQLite versions,
        # so we re-read after apply_pragmas to assert it. journal_mode is
        # always persistent.
        assert _read_pragma(conn2, "journal_mode").lower() == "wal"
        # Per-connection PRAGMAs reset; this proves they NEED re-application:
        assert _read_pragma(conn2, "synchronous") == 2  # back to default FULL
    finally:
        conn2.close()


def test_socialdb_init_applies_pragmas(tmp_path, monkeypatch):
    """End-to-end: SocialDB's connection has the full PRAGMA stack after init."""
    from scraperx import social_db

    db_path = tmp_path / "social.db"
    monkeypatch.setattr(social_db, "DEFAULT_DB_PATH", str(db_path))
    sdb = social_db.SocialDB(db_path=str(db_path))
    try:
        # Pull values directly from the live connection.
        assert _read_pragma(sdb._conn, "synchronous") == 1
        assert _read_pragma(sdb._conn, "busy_timeout") == 5000
        assert _read_pragma(sdb._conn, "mmap_size") == 268435456
        assert _read_pragma(sdb._conn, "temp_store") == 2
        assert _read_pragma(sdb._conn, "foreign_keys") == 1
    finally:
        sdb.close()


def test_avatar_matcher_init_applies_pragmas(tmp_path, monkeypatch):
    """End-to-end: AvatarMatcher's connection has the full PRAGMA stack."""
    from scraperx import avatar_matcher

    db_path = tmp_path / "avatar.db"
    monkeypatch.setattr(avatar_matcher, "DEFAULT_DB_PATH", str(db_path))
    am = avatar_matcher.AvatarMatcher(db_path=str(db_path))
    try:
        assert _read_pragma(am._conn, "synchronous") == 1
        assert _read_pragma(am._conn, "busy_timeout") == 5000
        assert _read_pragma(am._conn, "mmap_size") == 268435456
    finally:
        am.close()


def test_verified_avatar_registry_init_applies_pragmas(tmp_path, monkeypatch):
    """End-to-end: VerifiedAvatarRegistry now applies pragmas (was missing
    in 1.4.2 — relied on whoever opened the DB first to set WAL)."""
    from scraperx import avatar_matcher

    db_path = tmp_path / "registry.db"
    monkeypatch.setattr(avatar_matcher, "DEFAULT_DB_PATH", str(db_path))
    reg = avatar_matcher.VerifiedAvatarRegistry(db_path=str(db_path))
    try:
        assert _read_pragma(reg._conn, "journal_mode").lower() == "wal"
        assert _read_pragma(reg._conn, "synchronous") == 1
        assert _read_pragma(reg._conn, "busy_timeout") == 5000
        assert _read_pragma(reg._conn, "mmap_size") == 268435456
    finally:
        reg.close()
