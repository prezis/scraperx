"""Common JSONL output writer for bmw_corpus modules.

All scrapers (KBA, NHTSA, e90post, Reddit, etc.) write normalized records
through this module. Schema mirrors future-gear external_repair_corpus
(alembic 012).

Atomic append (open-write-close per record) so the future-gear ingester
can tail safely without partial-line race conditions.

Idempotency happens DOWNSTREAM in the ingester (UNIQUE(source, source_id)
constraint). Writers are free to emit duplicates; ingester upserts.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default output root. Override via env when running.
_DEFAULT_OUTPUT_ROOT = Path.home() / "ai" / "scraperx" / "output" / "bmw-trails"

# Per-process lock to serialize writes to the same file from threads.
_write_lock = threading.Lock()


def get_output_root() -> Path:
    return Path(os.environ.get("BMW_CORPUS_OUTPUT_ROOT", _DEFAULT_OUTPUT_ROOT))


def _file_for(source: str, when: datetime | None = None) -> Path:
    when = when or datetime.now(tz=timezone.utc)
    yyyy_mm = when.strftime("%Y-%m")
    p = get_output_root() / source / f"{yyyy_mm}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Record schema (must mirror external_repair_corpus columns)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {
    "source",         # nhtsa | kba | dvsa | reddit | e90post | ...
    "source_id",      # natural ID for upsert idempotency
    "content_type",   # recall | forum_thread | forum_post | reddit_post | reddit_comment | tsb
    "body_text",      # main content, source language
}

OPTIONAL_FIELDS = {
    "source_url",
    "source_lang",     # default 'en'
    "title",
    "raw_payload",     # original JSON shape
    "metadata_json",   # per-source extra fields
    "bmw_models",      # list[str]
    "year_from",
    "year_to",
    "published_at",    # ISO 8601 string
    "scraped_at",      # ISO 8601 string (default: now)
    "bmw_relevance_score",
}

ALL_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS


def normalize(record: dict[str, Any]) -> dict[str, Any]:
    """Validate + fill defaults. Returns a copy. Raises ValueError on bad input."""
    missing = REQUIRED_FIELDS - record.keys()
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")

    out = {k: v for k, v in record.items() if k in ALL_FIELDS}
    out.setdefault("source_lang", "en")
    out.setdefault("scraped_at", datetime.now(tz=timezone.utc).isoformat())

    # Coerce datetime objects to ISO strings.
    for k in ("published_at", "scraped_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat()

    # Normalize models to list of strings.
    if "bmw_models" in out and out["bmw_models"] is not None:
        models = out["bmw_models"]
        if isinstance(models, str):
            out["bmw_models"] = [models]
        elif isinstance(models, (list, tuple, set)):
            out["bmw_models"] = sorted({str(m).strip() for m in models if str(m).strip()})

    return out


def write_record(record: dict[str, Any]) -> Path:
    """Append one normalized record to the source's monthly JSONL file."""
    rec = normalize(record)
    path = _file_for(rec["source"])
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    return path


def write_batch(records: list[dict[str, Any]]) -> tuple[Path | None, int]:
    """Write a batch atomically — group by source/month and append per file."""
    if not records:
        return None, 0
    by_file: dict[Path, list[str]] = {}
    for r in records:
        rec = normalize(r)
        path = _file_for(rec["source"])
        by_file.setdefault(path, []).append(
            json.dumps(rec, ensure_ascii=False, default=str)
        )
    last_path: Path | None = None
    total = 0
    with _write_lock:
        for path, lines in by_file.items():
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            last_path = path
            total += len(lines)
    return last_path, total
