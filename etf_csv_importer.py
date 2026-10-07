"""
etf_csv_importer.py

Import TradingView ETF screener CSV into Supabase etf_master.

- Handles "Symbol" / "Ticker" and "Name" / "Description" columns
- Strips exchange prefixes ("NASDAQ:QQQ" -> ticker QQQ, exchange NASDAQ)
- De-duplicates tickers inside the CSV (Postgres rejects a batch that hits
  the same key twice)
- Upserts on the `ticker` unique key (not the id PK), in batches
- Re-runnable: existing tickers get name/exchange refreshed, new ones inserted

Usage:
    python etf_csv_importer.py "US_ETF_GQF.csv"
"""

import os
import sys
import csv
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BATCH_SIZE = 500

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def _parse_symbol(raw):
    """'NASDAQ:QQQ' -> ('QQQ', 'NASDAQ'); 'SPY' -> ('SPY', None)."""
    raw = (raw or "").strip().upper()
    if ":" in raw:
        exch, tick = raw.split(":", 1)
        return tick.strip(), exch.strip() or None
    return raw, None


def load_csv(csv_path):
    """Parse CSV into a de-duplicated {ticker: record} dict."""
    records = {}
    skipped = 0
    dupes = 0

    # utf-8-sig strips the BOM that Excel/TradingView exports sometimes add
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")

        cols = {c.strip(): c for c in reader.fieldnames}
        sym_col = cols.get("Symbol") or cols.get("Ticker")
        name_col = cols.get("Name") or cols.get("Description")
        exch_col = cols.get("Exchange")
        if not sym_col or not name_col:
            raise ValueError(f"Missing Symbol/Name columns. Found: {reader.fieldnames}")

        now = datetime.now(timezone.utc).isoformat()
        for row in reader:
            ticker, prefix_exch = _parse_symbol(row.get(sym_col))
            name = (row.get(name_col) or "").strip()
            if not ticker or not name or len(ticker) > 20:
                skipped += 1
                continue

            exchange = (row.get(exch_col) or "").strip() if exch_col else ""
            exchange = (exchange or prefix_exch or "US")[:20]

            if ticker in records:
                dupes += 1
            records[ticker] = {
                "ticker": ticker,
                "name": name,
                "exchange": exchange,
                "updated_at": now,
            }

    logger.info(f"[IMPORTER] Parsed {len(records)} unique ETFs "
                f"(skipped {skipped} invalid, merged {dupes} duplicate rows)")
    return list(records.values())


def upsert_records(records):
    """Upsert in batches on the ticker unique key."""
    done = 0
    failed = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        try:
            supabase.table("etf_master").upsert(batch, on_conflict="ticker").execute()
            done += len(batch)
            logger.info(f"[IMPORTER] Batch {i // BATCH_SIZE + 1}: upserted {len(batch)} (total {done})")
        except Exception as e:
            failed += len(batch)
            logger.error(f"[IMPORTER] Batch {i // BATCH_SIZE + 1} failed: {e}")
    return done, failed


def get_etf_count():
    """Exact row count in etf_master."""
    try:
        result = supabase.table("etf_master").select("ticker", count="exact").limit(1).execute()
        return result.count or 0
    except Exception as e:
        logger.error(f"[IMPORTER] Failed to get count: {e}")
        return -1


def import_etf_csv(csv_path):
    if not os.path.exists(csv_path):
        logger.error(f"[IMPORTER] File not found: {csv_path}")
        return 0

    before = get_etf_count()
    logger.info(f"[IMPORTER] ETFs in DB before import: {before}")

    try:
        records = load_csv(csv_path)
    except Exception as e:
        logger.error(f"[IMPORTER] CSV read failed: {e}")
        return 0

    if not records:
        logger.error("[IMPORTER] No valid records found in CSV")
        return 0

    logger.info(f"[IMPORTER] Sample: {[r['ticker'] for r in records[:5]]}")
    done, failed = upsert_records(records)

    after = get_etf_count()
    logger.info(f"[IMPORTER] Done. Upserted {done}, failed {failed}. "
                f"DB count: {before} -> {after}")
    return done


if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error('Usage: python etf_csv_importer.py "US_ETF_GQF.csv"')
        sys.exit(1)
    sys.exit(0 if import_etf_csv(sys.argv[1]) else 1)
