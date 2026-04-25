"""BMW external knowledge corpus — multi-source ingestion for ML training.

Sources land normalized JSONL records to:
  ~/ai/scraperx/output/bmw-trails/<source>/<YYYY-MM>.jsonl

A future-gear ingester tails this directory and upserts into the
external_repair_corpus SQL table (UNIQUE(source, source_id)).

Sources online:
  - kba       (DE recalls, daily fresh, public CSV)
  - nhtsa     (US recalls, weekly, modern api.nhtsa.gov)
  - reddit    (r/BMW + model subs + r/MechanicAdvice BMW filter)
  - e90post   (vBulletin 3, no anti-bot)

Sources blocked (legal):
  - motor_talk.de       (Content-Signal: ai-train=no)
  - bimmerforums.com    (Cloudflare Bot Fight + AI-bot blocklist)
  - drive2.ru           (DDoS-Guard + named-banned ClaudeBot etc.)
"""
