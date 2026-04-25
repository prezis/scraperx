"""DVSA (UK Driver and Vehicle Standards Agency) recall ingester.

Public CSV at https://www.check-vehicle-recalls.service.gov.uk/documents/RecallsFile.csv
~17k total rows, ~975 BMW (incl. MINI under BMW make), since 1992.
Open Government Licence v3.0. No anti-bot.

Schema (comma-delimited):
  Launch Date, Recalls Number, Make, Recalls Model Information, Concern,
  Defect, Remedy, Vehicle Numbers, Manufacturer Ref, Model, VIN Start,
  Vin End, Build Start, Build End

Usage:
  python -m scraperx.bmw_corpus.recalls.dvsa
  python -m scraperx.bmw_corpus.recalls.dvsa --dry-run
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

DVSA_CSV_URL = (
    "https://www.check-vehicle-recalls.service.gov.uk/documents/RecallsFile.csv"
)
USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair knowledge for Polish workshop ML; "
    "contact: przemyslaw.palyska@gmail.com)"
)
TIMEOUT_S = 60


def _parse_date(s: str) -> str | None:
    """DVSA dates are 'DD/MM/YYYY'. Returns ISO 8601 date or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    s = s.strip().replace(",", "")
    try:
        return int(s)
    except ValueError:
        return None


def _year_from_iso(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(iso[:4])
    except (ValueError, TypeError):
        return None


def fetch_csv() -> str:
    req = urllib.request.Request(
        DVSA_CSV_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def parse_bmw_rows(csv_text: str) -> list[dict]:
    """Aggregate BMW Group rows by `Recalls Number` (one record per recall).

    DVSA CSV emits one row per (recall × affected model) — a single recall
    R/YYYY/NNN can have 30-44 model rows. We collapse on Recalls Number and
    UNION the affected models into bmw_models. Year range is min/max across
    all model rows. First non-empty Concern/Defect/Remedy text wins (they
    are identical across rows of the same recall — verified empirically).

    Captures Make in {BMW, MINI, ROLLS-ROYCE, BMW MOTORRAD} (BMW Group fleet).
    """
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    grouped: dict[str, dict] = {}

    for row in reader:
        make = (row.get("Make") or "").strip().upper()
        if make not in {"BMW", "MINI", "ROLLS-ROYCE", "BMW MOTORRAD"}:
            continue
        ref = (row.get("Recalls Number") or "").strip()
        if not ref:
            continue

        launch_iso = _parse_date(row.get("Launch Date") or "")
        build_start = _parse_date(row.get("Build Start") or "")
        build_end = _parse_date(row.get("Build End") or "")
        ys = _year_from_iso(build_start)
        ye = _year_from_iso(build_end)

        model = (row.get("Model") or "").strip()
        model_info = (row.get("Recalls Model Information") or "").strip()

        body_parts = [
            f"Concern: {(row.get('Concern') or '').strip()}",
            f"Defect: {(row.get('Defect') or '').strip()}",
            f"Remedy: {(row.get('Remedy') or '').strip()}",
        ]
        body_text = "\n".join(p for p in body_parts if p.split(": ", 1)[1])
        if not body_text.strip():
            continue

        if ref not in grouped:
            grouped[ref] = {
                "source": "dvsa",
                "source_id": ref,
                "source_url": "https://www.check-vehicle-recalls.service.gov.uk/",
                "source_lang": "en",
                "content_type": "recall",
                "title": model_info or model or None,
                "body_text": body_text,
                "raw_payload": dict(row),  # first row's full record
                "metadata_json": {
                    "make": make,
                    "models_seen": [],
                    "model_informations_seen": [],
                    "manufacturer_refs": [],
                    "vehicle_numbers_total": 0,
                    "vin_ranges": [],
                    "build_start": build_start,
                    "build_end": build_end,
                    "row_count": 0,
                },
                "bmw_models": [],
                "year_from": ys,
                "year_to": ye,
                "published_at": launch_iso,
                "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        rec = grouped[ref]
        meta = rec["metadata_json"]
        # Accumulate models (dedup, preserve order)
        if model and model not in rec["bmw_models"]:
            rec["bmw_models"].append(model)
        if model and model not in meta["models_seen"]:
            meta["models_seen"].append(model)
        if model_info and model_info not in meta["model_informations_seen"]:
            meta["model_informations_seen"].append(model_info)
        mref = (row.get("Manufacturer Ref") or "").strip()
        if mref and mref not in meta["manufacturer_refs"]:
            meta["manufacturer_refs"].append(mref)
        vins_lo = (row.get("VIN Start") or "").strip()
        vins_hi = (row.get("Vin End") or "").strip()
        if vins_lo or vins_hi:
            meta["vin_ranges"].append({"start": vins_lo or None, "end": vins_hi or None})
        vn = _parse_int(row.get("Vehicle Numbers") or "")
        if vn:
            meta["vehicle_numbers_total"] += vn
        meta["row_count"] += 1
        # Min/max year span across model rows
        if ys is not None:
            rec["year_from"] = min(rec["year_from"], ys) if rec["year_from"] is not None else ys
        if ye is not None:
            rec["year_to"] = max(rec["year_to"], ye) if rec["year_to"] is not None else ye

    # Finalize: empty bmw_models → None
    out = []
    for rec in grouped.values():
        if not rec["bmw_models"]:
            rec["bmw_models"] = None
        out.append(rec)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="DVSA UK BMW Group recall ingester")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("Fetching DVSA CSV: %s", DVSA_CSV_URL)
    csv_text = fetch_csv()
    log.info("CSV bytes: %d", len(csv_text))

    rows = parse_bmw_rows(csv_text)
    log.info("BMW Group rows extracted: %d", len(rows))

    if rows:
        sample = rows[0]
        log.info(
            "Sample: id=%s model=%s pub=%s body_len=%d",
            sample["source_id"], sample.get("bmw_models"),
            sample.get("published_at"), len(sample.get("body_text", "")),
        )

    # By-make breakdown
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
