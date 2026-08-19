#!/usr/bin/env python3
"""
Diagnostic: check what's in raw_filings and alerts.
Run: SUPABASE_URL=... SUPABASE_KEY=... python3 diagnostic.py
"""
import os
from datetime import datetime, timezone, timedelta
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_KEY environment variables")
    exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

print("=" * 80)
print("DIAGNOSTIC: GQ FinXray US Alert System")
print("=" * 80)
print()

# Check watchlist
print("1. WATCHLIST")
wl = supabase.table("watchlists").select("ticker").execute()
wl_tickers = set(t.get("ticker").upper() for t in (wl.data or []) if t.get("ticker"))
print(f"   Watched tickers: {len(wl_tickers)}")
if wl_tickers:
    print(f"   {sorted(wl_tickers)[:10]}{'...' if len(wl_tickers) > 10 else ''}")
print()

# Check raw_filings (PENDING)
print("2. raw_filings (PENDING, <24h)")
fresh_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
res = supabase.table("raw_filings").select("*").eq("status", "PENDING").gte("created_at", fresh_cutoff).order("created_at", desc=True).limit(500).execute()
pending = res.data or []
print(f"   Total PENDING: {len(pending)}")

if pending:
    types_count = {}
    for f in pending:
        ft = (f.get("filing_type") or "UNKNOWN").upper()
        types_count[ft] = types_count.get(ft, 0) + 1
    print(f"   By type:")
    for ft in sorted(types_count.keys()):
        print(f"     {ft}: {types_count[ft]}")
else:
    print(f"   (No rows pending)")
print()

# Check alerts (undelivered)
print("3. alerts (undelivered)")
res = supabase.table("alerts").select("*").eq("delivered", False).order("created_at", desc=True).limit(100).execute()
undeliv = res.data or []
print(f"   Undelivered alerts: {len(undeliv)}")

if undeliv:
    types_count = {}
    for a in undeliv:
        ft = (a.get("filing_type") or "UNKNOWN").upper()
        types_count[ft] = types_count.get(ft, 0) + 1
    print(f"   By type:")
    for ft in sorted(types_count.keys()):
        print(f"     {ft}: {types_count[ft]}")
else:
    print(f"   (No undelivered alerts)")
print()

# Check alerts (all, delivered or not, last 24h)
print("4. alerts (all, <24h)")
res = supabase.table("alerts").select("*").gte("created_at", fresh_cutoff).order("created_at", desc=True).limit(100).execute()
recent = res.data or []
print(f"   Total alerts in last 24h: {len(recent)}")

if recent:
    types_count = {}
    for a in recent:
        ft = (a.get("filing_type") or "UNKNOWN").upper()
        types_count[ft] = types_count.get(ft, 0) + 1
    print(f"   By type:")
    for ft in sorted(types_count.keys()):
        print(f"     {ft}: {types_count[ft]}")
else:
    print(f"   (No alerts in last 24h)")
print()

print("=" * 80)
if len(pending) == 0 and len(undeliv) == 0 and len(recent) == 0:
    print("ISSUE: Zero rows at every stage. Pollers may not be running or watchlist may be empty.")
elif len(pending) == 0 and len(undeliv) == 0 and len(recent) > 0:
    print("OK: Recent alerts exist and were delivered (no backlog).")
elif len(pending) > 0:
    print("INVESTIGATION NEEDED:")
    print(f"  - {len(pending)} rows stuck in PENDING status")
    print(f"  - Check if ai_pipeline.py is running (run_pipeline loop)")
    print(f"  - Check ai_pipeline.py logs for failures")
else:
    print("WARNING: Check logs and verify pollers are running.")
print("=" * 80)
