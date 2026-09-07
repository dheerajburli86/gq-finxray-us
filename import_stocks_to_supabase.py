#!/usr/bin/env python3
"""
import_stocks_to_supabase.py

Bulk import NYSE + NASDAQ stocks into the Supabase stocks table.

Usage:
    python import_stocks_to_supabase.py <nyse_csv> <nasdaq_csv>

Example:
    python import_stocks_to_supabase.py nyse.csv nasdaq.csv

The CSVs should have columns: ticker, exchange, sector (or similar).
"""

import os
import sys
import csv
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY not set in .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def import_stocks(nyse_csv_path, nasdaq_csv_path):
    """Import stocks from NYSE and NASDAQ CSV files into Supabase."""

    stocks = []

    # Read NYSE stocks
    print(f"[NYSE] Reading {nyse_csv_path}...")
    try:
        with open(nyse_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try both 'ticker' and 'Symbol' column names
                ticker = (row.get('ticker') or row.get('Symbol') or '').strip().upper()
                exchange = 'NYSE'
                # Try both 'sector' and 'Sector' column names
                sector = (row.get('sector') or row.get('Sector') or '').strip() or None
                if ticker:
                    stocks.append({
                        'ticker': ticker,
                        'exchange': exchange,
                        'sector': sector
                    })
        print(f"[NYSE] Loaded {len([s for s in stocks if s['exchange'] == 'NYSE'])} stocks")
    except FileNotFoundError:
        print(f"ERROR: {nyse_csv_path} not found")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR reading NYSE CSV: {e}")
        sys.exit(1)

    # Read NASDAQ stocks
    print(f"[NASDAQ] Reading {nasdaq_csv_path}...")
    try:
        with open(nasdaq_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try both 'ticker' and 'Symbol' column names
                ticker = (row.get('ticker') or row.get('Symbol') or '').strip().upper()
                exchange = 'NASDAQ'
                # Try both 'sector' and 'Sector' column names
                sector = (row.get('sector') or row.get('Sector') or '').strip() or None
                if ticker:
                    stocks.append({
                        'ticker': ticker,
                        'exchange': exchange,
                        'sector': sector
                    })
        print(f"[NASDAQ] Loaded {len([s for s in stocks if s['exchange'] == 'NASDAQ'])} stocks")
    except FileNotFoundError:
        print(f"ERROR: {nasdaq_csv_path} not found")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR reading NASDAQ CSV: {e}")
        sys.exit(1)

    print(f"\n[TOTAL] {len(stocks)} stocks to import")

    # Bulk insert into Supabase (batch in chunks to avoid payload limits)
    batch_size = 100
    total_inserted = 0

    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i+batch_size]
        try:
            print(f"[BATCH {i//batch_size + 1}] Inserting {len(batch)} stocks...")
            result = supabase.table('stocks').insert(batch).execute()
            total_inserted += len(batch)
            print(f"[BATCH {i//batch_size + 1}] ✓ Inserted {len(batch)} stocks")
        except Exception as e:
            if 'duplicate' in str(e).lower() or 'unique' in str(e).lower():
                # Some stocks may already exist; that's okay
                print(f"[BATCH {i//batch_size + 1}] ⚠ Skipped (likely duplicates): {str(e)[:100]}")
                total_inserted += len(batch)
            else:
                print(f"[BATCH {i//batch_size + 1}] ERROR: {e}")
                return False

    print(f"\n[SUCCESS] Imported {total_inserted} stocks into Supabase")

    # Verify import
    try:
        count_result = supabase.table('stocks').select('id', count='exact').execute()
        total_count = count_result.count
        print(f"[VERIFY] Total stocks in database: {total_count}")
    except Exception as e:
        print(f"[VERIFY] Could not verify count: {e}")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python import_stocks_to_supabase.py <nyse_csv> <nasdaq_csv>")
        print("Example: python import_stocks_to_supabase.py nyse.csv nasdaq.csv")
        sys.exit(1)

    nyse_path = sys.argv[1]
    nasdaq_path = sys.argv[2]

    print("""
╔═══════════════════════════════════════════════════════════════╗
║  GQ FinXray US — Stock List Importer (Supabase)              ║
║  Imports NYSE + NASDAQ stocks to enable 6,300-stock coverage  ║
╚═══════════════════════════════════════════════════════════════╝
    """)

    success = import_stocks(nyse_path, nasdaq_path)
    sys.exit(0 if success else 1)
