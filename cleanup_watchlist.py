#!/usr/bin/env python3
"""
GQ FinXray US — Watchlist Cleanup
Remove dead tickers (SPCX) and update renamed ones (SQ→XYZ) in Supabase
Usage: python cleanup_watchlist.py
"""

import os
import sys
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Error: SUPABASE_URL and SUPABASE_KEY not found in .env")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

print("=== GQ FinXray US — Watchlist Cleanup ===\n")

# 1. Remove SPCX (delisted)
try:
    print("[1/2] Removing SPCX (delisted ETF)...")
    result = sb.table("watchlists").delete().eq("ticker", "SPCX").execute()
    print(f"      ✅ Deleted {len(result.data) if result.data else 0} SPCX row(s)")
except Exception as e:
    print(f"      ❌ Error: {e}")
    sys.exit(1)

# 2. Update SQ → XYZ
try:
    print("[2/2] Updating SQ → XYZ (Block renamed ticker Jan 2025)...")
    result = sb.table("watchlists").update({"ticker": "XYZ"}).eq("ticker", "SQ").execute()
    print(f"      ✅ Updated {len(result.data) if result.data else 0} SQ row(s) to XYZ")
except Exception as e:
    print(f"      ❌ Error: {e}")
    sys.exit(1)

print("\n✅ Watchlist cleanup complete!\n")
print("Next steps:")
print("  git add watchlist_util.py cleanup_watchlist.py")
print("  git commit -m 'Remove SPCX from dead tickers, update SQ→XYZ mapping'")
print("  git push origin main")
