#!/usr/bin/env python3
"""Minimal table access test"""

import os
from dotenv import load_dotenv
import requests
import json

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY not set")
    exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

print("Testing table access...\n")

tables = ["raw_filings", "alerts", "poller_runs", "watchlists", "stocks"]

for table in tables:
    print(f"{table}:")
    try:
        # Try to get just 1 row
        url = f"{SUPABASE_URL}/rest/v1/{table}?limit=1"
        resp = requests.get(url, headers=headers, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✓ Accessible - {len(data)} rows returned")
            if data:
                print(f"    Sample keys: {list(data[0].keys())[:5]}")
        else:
            print(f"  ✗ Status {resp.status_code}")
            if resp.text:
                print(f"    {resp.text[:100]}")
    except Exception as e:
        print(f"  ✗ Error: {e}")
    print()
