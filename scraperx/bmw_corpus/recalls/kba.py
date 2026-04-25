"""KBA (Kraftfahrt-Bundesamt) German recall ingester.

Public CSV API at https://www.kba-online.de/rrdb/buerger/api/rueckruf/export
Returns ~7.6k rows total, ~200 BMW. Daily-fresh. No anti-bot.

Schema (semicolon-delimited):
  KBA-Referenznummer;Rückrufcode des Herstellers;Veröffentlichungsdatum;
  Marke;Modell;Mangelbezeichnung;Produktionszeitraum von;Produktionszeitraum bis;
  Hotline des Herstellers;Rückrufseite des Herstellers;Mangelbeschreibung;
  Titel der Maßnahme;Beschreibung der Maßnahme;
  Mögliche Eingrenzung der betroffenen Modelle;
  Bekannte Vorfälle mit Sach- und/oder Personenschäden;
  Anzahl potentiell betroffene Fahrzeugteile und Fahrzeugzubehör weltweit;
  Anzahl potentiell betroffene Fahrzeugteile und Fahrzeugzubehör deutschlandweit;
  Überwachung der Rückrufaktion durch das KBA

Usage:
  python -m scraperx.bmw_corpus.recalls.kba
  python -m scraperx.bmw_corpus.recalls.kba --dry-run   # fetch + parse, no write
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import urllib.request
from datetime import datetime, timezone

from scraperx.bmw_corpus._output import write_batch

log = logging.getLogger(__name__)

KBA_CSV_URL = (
    "https://www.kba-online.de/rrdb/buerger/api/rueckruf/export"
    "?format=csv&type=cars"
)
USER_AGENT = (
    "ZenonAI-WorkshopBot/0.1 "
    "(BMW repair knowledge for Polish workshop ML; "
    "contact: przemyslaw.palyska@gmail.com)"
)
TIMEOUT_S = 60


def _parse_date(s: str) -> str | None:
    """KBA dates are 'DD.MM.YYYY'. Returns ISO 8601 date string or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_int(s: str) -> int | None:
    if not s:
        return None
    s = s.strip().replace(".", "").replace(",", "")
    try:
        return int(s)
    except ValueError:
        return None


def _extract_year_range(produktionszeitraum_von: str, produktionszeitraum_bis: str) -> tuple[int | None, int | None]:
    def y(d: str) -> int | None:
        iso = _parse_date(d)
        if iso:
            try:
                return int(iso[:4])
            except (ValueError, TypeError):
                return None
        return None
    return y(produktionszeitraum_von), y(produktionszeitraum_bis)


def fetch_csv() -> str:
    req = urllib.request.Request(
        KBA_CSV_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def parse_bmw_rows(csv_text: str) -> list[dict]:
    """Filter rows where Marke == 'BMW' (cars only). Returns normalized dicts."""
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f, delimiter=";")
    out = []
    for row in reader:
        marke = (row.get("Marke") or "").strip()
        if marke.upper() != "BMW":
            continue
        ref = (row.get("KBA-Referenznummer") or "").strip()
        if not ref:
            continue
        year_from, year_to = _extract_year_range(
            row.get("Produktionszeitraum von") or "",
            row.get("Produktionszeitraum bis") or "",
        )
        modell = (row.get("Modell") or "").strip()
        body_parts = [
            f"Mangelbezeichnung: {row.get('Mangelbezeichnung', '').strip()}",
            f"Mangelbeschreibung: {row.get('Mangelbeschreibung', '').strip()}",
            f"Titel der Maßnahme: {row.get('Titel der Maßnahme', '').strip()}",
            f"Beschreibung der Maßnahme: {row.get('Beschreibung der Maßnahme', '').strip()}",
        ]
        body_text = "\n".join(p for p in body_parts if p.split(": ", 1)[1])

        if not body_text.strip():
            continue

        record = {
            "source": "kba",
            "source_id": ref,
            "source_url": "https://www.kba-online.de/rrdb/buerger/",
            "source_lang": "de",
            "content_type": "recall",
            "title": (row.get("Titel der Maßnahme") or "").strip() or None,
            "body_text": body_text,
            "raw_payload": dict(row),
            "metadata_json": {
                "manufacturer_recall_code": (row.get("Rückrufcode des Herstellers") or "").strip() or None,
                "manufacturer_hotline": (row.get("Hotline des Herstellers") or "").strip() or None,
                "manufacturer_recall_url": (row.get("Rückrufseite des Herstellers") or "").strip() or None,
                "model_constraint": (row.get("Mögliche Eingrenzung der betroffenen Modelle") or "").strip() or None,
                "incidents_known": (row.get("Bekannte Vorfälle mit Sach- und/oder Personenschäden") or "").strip() or None,
                "units_affected_global": _parse_int(row.get("Anzahl potentiell betroffene Fahrzeugteile und Fahrzeugzubehör weltweit") or ""),
                "units_affected_de": _parse_int(row.get("Anzahl potentiell betroffene Fahrzeugteile und Fahrzeugzubehör deutschlandweit") or ""),
                "kba_supervised": (row.get("Überwachung der Rückrufaktion durch das KBA") or "").strip() or None,
            },
            "bmw_models": [modell] if modell else None,
            "year_from": year_from,
            "year_to": year_to,
            "published_at": _parse_date(row.get("Veröffentlichungsdatum") or ""),
            "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        out.append(record)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="KBA BMW recall ingester")
    p.add_argument("--dry-run", action="store_true", help="Parse but don't write JSONL")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("Fetching KBA CSV: %s", KBA_CSV_URL)
    csv_text = fetch_csv()
    log.info("CSV bytes: %d", len(csv_text))

    rows = parse_bmw_rows(csv_text)
    log.info("BMW rows extracted: %d", len(rows))

    if rows:
        sample = rows[0]
        log.info(
            "Sample: id=%s model=%s pub=%s body_len=%d",
            sample["source_id"],
            sample.get("bmw_models"),
            sample.get("published_at"),
            len(sample.get("body_text", "")),
        )

    if args.dry_run:
        log.info("DRY RUN — no write")
        return 0

    if not rows:
        log.warning("No BMW rows — nothing to write")
        return 0

    path, n = write_batch(rows)
    log.info("Wrote %d rows to %s", n, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
