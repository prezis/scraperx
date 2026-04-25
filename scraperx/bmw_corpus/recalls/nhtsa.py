"""NHTSA (US National Highway Traffic Safety Admin) BMW recall ingester.

Modern API at api.nhtsa.gov. The legacy Socrata endpoints (in
~/Documents/future-gear/scripts/ingest_nhtsa_tsbs.py) are stale; this
script replaces them.

Pipeline:
  1. vPIC GetModelsForMakeYear for BMW × 2000..2024 → unique model strings
  2. recallsByVehicle for each (year, model) → recall records
  3. Dedupe by NHTSACampaignNumber (~250-400 unique BMW campaigns)
  4. Write JSONL via bmw_corpus._output

Polite throttle: 1 req/s. Full pass = ~25 years × ~50 models = ~1250 calls
≈ 21 minutes.

Usage:
  python -m scraperx.bmw_corpus.recalls.nhtsa
  python -m scraperx.bmw_corpus.recalls.nhtsa --dry-run
  python -m scraperx.bmw_corpus.recalls.nhtsa --year-from 2018 --year-to 2024
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable

from scraperx.bmw_corpus._output import write_batch

log = logging.getLogger(__name__)

USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair knowledge for Polish workshop ML; "
    "contact: przemyslaw.palyska@gmail.com)"
)
TIMEOUT_S = 30
THROTTLE_S = 1.0  # 1 req/s — be polite

VPIC_MODELS_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/bmw/modelyear/{year}?format=json"
RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle?make=BMW&model={model}&modelYear={year}"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _vpic_models_for_year(year: int) -> list[str]:
    """Returns list of BMW model strings for a given year per vPIC."""
    try:
        data = _get_json(VPIC_MODELS_URL.format(year=year))
    except urllib.error.URLError as e:
        log.warning("vPIC %s failed: %s", year, e)
        return []
    results = data.get("Results") or []
    out = []
    for r in results:
        name = (r.get("Model_Name") or "").strip()
        # Filter motorcycles — they have prefixes like "R", "S", "K" + numeric model
        # e.g. "R 1200 GS", "S 1000 RR", "K 1600 GTL". Cars are like "X5", "M3", "535i", "i3".
        # Heuristic: motorcycles start with R/S/K/F + space + digits
        if name and not _looks_like_motorcycle(name):
            out.append(name)
    return out


def _looks_like_motorcycle(model: str) -> bool:
    # BMW motorcycles: R/S/K/F/G/HP series, typically "R 1200 GS" pattern
    parts = model.split()
    if len(parts) < 2:
        return False
    if parts[0] in ("R", "S", "K", "F", "G", "HP", "C"):
        # Second token starts with digit (cc displacement)?
        if parts[1] and parts[1][0].isdigit() and int(parts[1].split("/")[0][:1]) > 0:
            return True
    return False


def _recalls_for(year: int, model: str) -> list[dict]:
    safe_model = urllib.parse.quote(model, safe="")
    url = RECALLS_URL.format(model=safe_model, year=year)
    try:
        data = _get_json(url)
    except urllib.error.URLError as e:
        log.warning("recalls %s/%s failed: %s", year, model, e)
        return []
    return data.get("results") or []


def _normalize(raw: dict, year: int, model: str) -> dict:
    campaign = (raw.get("NHTSACampaignNumber") or "").strip()
    summary = (raw.get("Summary") or "").strip()
    consequence = (raw.get("Consequence") or "").strip()
    remedy = (raw.get("Remedy") or "").strip()
    notes = (raw.get("Notes") or "").strip()
    component = (raw.get("Component") or "").strip()

    body_parts = []
    if component:
        body_parts.append(f"Component: {component}")
    if summary:
        body_parts.append(f"Summary: {summary}")
    if consequence:
        body_parts.append(f"Consequence: {consequence}")
    if remedy:
        body_parts.append(f"Remedy: {remedy}")
    if notes:
        body_parts.append(f"Notes: {notes}")
    body_text = "\n".join(body_parts) or summary or "(no body)"

    pub_date = (raw.get("ReportReceivedDate") or "").strip()
    if pub_date:
        # NHTSA returns ISO-ish "DD/MM/YYYY" — try both common formats
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                pub_date = datetime.strptime(pub_date, fmt).date().isoformat()
                break
            except ValueError:
                continue
    else:
        pub_date = None

    return {
        "source": "nhtsa",
        "source_id": campaign,
        "source_url": f"https://api.nhtsa.gov/recalls/campaignNumber?campaignNumber={campaign}",
        "source_lang": "en",
        "content_type": "recall",
        "title": component or f"Recall {campaign}",
        "body_text": body_text,
        "raw_payload": raw,
        "metadata_json": {
            "park_it": raw.get("parkIt"),
            "park_outside": raw.get("parkOutSide"),
            "over_the_air_update": raw.get("overTheAirUpdate"),
            "manufacturer": raw.get("Manufacturer"),
            "potential_units_affected": raw.get("PotentialNumberofUnitsAffected"),
        },
        "bmw_models": [model],
        "year_from": year,
        "year_to": year,
        "published_at": pub_date,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def crawl(
    year_from: int = 2000,
    year_to: int = 2024,
    throttle_s: float = THROTTLE_S,
) -> list[dict]:
    seen_campaigns: dict[str, dict] = {}
    total_calls = 0

    for year in range(year_from, year_to + 1):
        log.info("YEAR %d — fetching vPIC models", year)
        models = _vpic_models_for_year(year)
        log.info("  %d non-motorcycle models", len(models))
        time.sleep(throttle_s)
        total_calls += 1

        for i, model in enumerate(models, 1):
            recalls = _recalls_for(year, model)
            if recalls:
                log.info(
                    "  [%d/%d] %s × %d recalls", i, len(models), model, len(recalls)
                )
            for r in recalls:
                campaign = (r.get("NHTSACampaignNumber") or "").strip()
                if not campaign:
                    continue
                if campaign in seen_campaigns:
                    # Already seen — append the model to bmw_models list
                    existing = seen_campaigns[campaign]
                    existing_models = set(existing["bmw_models"])
                    existing_models.add(model)
                    existing["bmw_models"] = sorted(existing_models)
                    continue
                seen_campaigns[campaign] = _normalize(r, year, model)
            time.sleep(throttle_s)
            total_calls += 1

    log.info("Crawl done: %d calls, %d unique campaigns", total_calls, len(seen_campaigns))
    return list(seen_campaigns.values())


def main() -> int:
    p = argparse.ArgumentParser(description="NHTSA BMW recall ingester")
    p.add_argument("--year-from", type=int, default=2000)
    p.add_argument("--year-to", type=int, default=datetime.now().year)
    p.add_argument("--throttle", type=float, default=THROTTLE_S)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    log.info(
        "NHTSA BMW recall crawl: years=%d-%d throttle=%.2fs dry_run=%s",
        args.year_from, args.year_to, args.throttle, args.dry_run,
    )

    records = crawl(args.year_from, args.year_to, args.throttle)
    log.info("Got %d unique campaigns", len(records))

    if args.dry_run:
        log.info("DRY RUN — no write")
        for r in records[:3]:
            log.info(
                "  sample %s — %s — models=%s",
                r["source_id"], (r["title"] or "")[:60], r.get("bmw_models"),
            )
        return 0

    if records:
        path, n = write_batch(records)
        log.info("Wrote %d records to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
