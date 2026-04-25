"""Canada Transport (Defect Investigations and Recalls Division) recall ingester.

Public CSV from open.canada.ca — Open Government Licence Canada (ca-ogl-lgo).
~928k total rows, ~7,073 BMW rows (incl. MINI + ROLLS-ROYCE + BMW motorcycles).
Bilingual: every text field has _ETXT (English) + _FTXT (French) variants.

Schema (CSV, comma-delimited, double-quoted):
  RECALL_NUMBER_NUM, YEAR, MANUFACTURER_RECALL_NO_TXT,
  CATEGORY_ETXT, CATEGORY_FTXT, MAKE_NAME_NM, MODEL_NAME_NM,
  UNIT_AFFECTED_NBR, SYSTEM_TYPE_ETXT, SYSTEM_TYPE_FTXT,
  NOTIFICATION_TYPE_ETXT, NOTIFICATION_TYPE_FTXT,
  COMMENT_ETXT, COMMENT_FTXT, RECALL_DATE_DTE

Source CSV emits one row per (recall × affected model). We aggregate on
RECALL_NUMBER_NUM, accumulating models into bmw_models. Bilingual body
text populates body_text (EN) + translated_en is identity, translated_fr
captures FR — useful for downstream translation pipeline.

Usage:
  python -m scraperx.bmw_corpus.recalls.canada
  python -m scraperx.bmw_corpus.recalls.canada --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import urllib.request
from datetime import datetime, timezone

from scraperx.bmw_corpus._output import write_batch

log = logging.getLogger(__name__)

CANADA_CSV_URL = "https://opendatatc.tc.canada.ca/vrdb_full_monthly.csv"
USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair knowledge for Polish workshop ML; "
    "contact: przemyslaw.palyska@gmail.com)"
)
TIMEOUT_S = 120  # large CSV (~50MB)


def _parse_date(s: str) -> str | None:
    """Canada dates appear as 'YYYY-MM-DD' in modern entries."""
    if not s:
        return None
    s = s.strip().strip('"')
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_int_from_float(s: str) -> int | None:
    if not s:
        return None
    s = s.strip().strip('"')
    try:
        return int(float(s))
    except ValueError:
        return None


def fetch_csv() -> str:
    req = urllib.request.Request(
        CANADA_CSV_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def parse_bmw_rows(csv_text: str) -> list[dict]:
    """Aggregate BMW Group rows by RECALL_NUMBER_NUM (one record per recall).

    Captures Make in {BMW, MINI, ROLLS-ROYCE} (BMW Group fleet).
    """
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    grouped: dict[str, dict] = {}

    for row in reader:
        make = (row.get("MAKE_NAME_NM") or "").strip().upper()
        if make not in {"BMW", "MINI", "ROLLS-ROYCE", "ROLLS ROYCE"}:
            continue
        ref = (row.get("RECALL_NUMBER_NUM") or "").strip()
        if not ref:
            continue

        published_iso = _parse_date(row.get("RECALL_DATE_DTE") or "")
        # YEAR is integer manufacturing year (per-model row)
        try:
            year = int((row.get("YEAR") or "").strip().strip('"'))
        except ValueError:
            year = None

        model = (row.get("MODEL_NAME_NM") or "").strip()
        category_en = (row.get("CATEGORY_ETXT") or "").strip()
        system_en = (row.get("SYSTEM_TYPE_ETXT") or "").strip()
        system_fr = (row.get("SYSTEM_TYPE_FTXT") or "").strip()
        notif_en = (row.get("NOTIFICATION_TYPE_ETXT") or "").strip()
        notif_fr = (row.get("NOTIFICATION_TYPE_FTXT") or "").strip()
        comment_en = (row.get("COMMENT_ETXT") or "").strip()
        comment_fr = (row.get("COMMENT_FTXT") or "").strip()
        units = _parse_int_from_float(row.get("UNIT_AFFECTED_NBR") or "")
        mfr_ref = (row.get("MANUFACTURER_RECALL_NO_TXT") or "").strip()

        if not comment_en and not comment_fr:
            continue

        body_en_parts = [
            f"Category: {category_en}" if category_en else "",
            f"System: {system_en}" if system_en else "",
            f"Notification: {notif_en}" if notif_en else "",
            f"Comment: {comment_en}" if comment_en else "",
        ]
        body_text = "\n".join(p for p in body_en_parts if p).strip()
        body_fr_parts = [
            f"Système: {system_fr}" if system_fr else "",
            f"Notification: {notif_fr}" if notif_fr else "",
            f"Commentaire: {comment_fr}" if comment_fr else "",
        ]
        body_fr = "\n".join(p for p in body_fr_parts if p).strip() or None

        if ref not in grouped:
            grouped[ref] = {
                "source": "canada",
                "source_id": ref,
                "source_url": (
                    "https://recalls-rappels.canada.ca/en/recall-search/"
                    f"vehicle-recall/{ref}"
                ),
                "source_lang": "en",
                "content_type": "recall",
                "title": (system_en or category_en) or None,
                "body_text": body_text,
                "raw_payload": dict(row),  # first row's full record
                "metadata_json": {
                    "category_en": category_en or None,
                    "category_fr": (row.get("CATEGORY_FTXT") or "").strip() or None,
                    "system_type_en": system_en or None,
                    "system_type_fr": system_fr or None,
                    "notification_type_en": notif_en or None,
                    "notification_type_fr": notif_fr or None,
                    "manufacturer_refs": [],
                    "units_affected_total": 0,
                    "models_seen": [],
                    "row_count": 0,
                    "make": make,
                },
                "bmw_models": [],
                "year_from": year,
                "year_to": year,
                "published_at": published_iso,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
                "translated_en": body_text,  # already EN (source_lang=en)
                "translated_pl": None,        # filled by separate enrichment
            }
            # Stash FR body in metadata for downstream PL translation pipeline
            grouped[ref]["metadata_json"]["body_fr"] = body_fr

        rec = grouped[ref]
        meta = rec["metadata_json"]
        if model and model not in rec["bmw_models"]:
            rec["bmw_models"].append(model)
        if model and model not in meta["models_seen"]:
            meta["models_seen"].append(model)
        if mfr_ref and mfr_ref not in meta["manufacturer_refs"]:
            meta["manufacturer_refs"].append(mfr_ref)
        if units:
            meta["units_affected_total"] += units
        meta["row_count"] += 1
        # Min/max year span
        if year is not None:
            rec["year_from"] = (
                min(rec["year_from"], year) if rec["year_from"] is not None else year
            )
            rec["year_to"] = (
                max(rec["year_to"], year) if rec["year_to"] is not None else year
            )

    out = []
    for rec in grouped.values():
        if not rec["bmw_models"]:
            rec["bmw_models"] = None
        out.append(rec)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Canada Transport BMW Group recall ingester")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("Fetching Canada CSV: %s", CANADA_CSV_URL)
    csv_text = fetch_csv()
    log.info("CSV bytes: %d", len(csv_text))

    rows = parse_bmw_rows(csv_text)
    log.info("BMW Group recall records (aggregated): %d", len(rows))

    if rows:
        sample = rows[0]
        log.info(
            "Sample: id=%s models=%s pub=%s body_len=%d",
            sample["source_id"], sample.get("bmw_models"),
            sample.get("published_at"), len(sample.get("body_text", "")),
        )

    by_make: dict[str, int] = {}
    for r in rows:
        m = r["metadata_json"].get("make") or "?"
        by_make[m] = by_make.get(m, 0) + 1
    log.info("By-make: %s", sorted(by_make.items(), key=lambda x: -x[1]))

    if args.dry_run:
        log.info("DRY RUN — no write")
        return 0
    if not rows:
        log.warning("No BMW rows — nothing to write")
        return 0

    path, n = write_batch(rows)
    log.info("Wrote %d records to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
