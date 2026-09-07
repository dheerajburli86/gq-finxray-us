#!/usr/bin/env python3
"""Verify that SEC filing ingestion is now working after the backoff fix"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY not set")
    sys.exit(1)

import requests
import json

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

print("=" * 80)
print("SEC FILING INGESTION CHECK")
print("=" * 80)

# 1. Check raw_filings by type
print("\n1. RAW_FILINGS BY TYPE (last 500 entries):")
print("-" * 80)
try:
    url = f"{SUPABASE_URL}/rest/v1/raw_filings?select=filing_type&order=created_at.desc&limit=500"
    resp = requests.get(url, headers=headers, timeout=10)

    if resp.status_code == 200:
        rows = resp.json()
        type_counts = {}
        for r in rows:
            ft = r.get("filing_type", "?")
            type_counts[ft] = type_counts.get(ft, 0) + 1

        print(f"Total: {len(rows)} entries\n")
        for ft in sorted(type_counts.keys()):
            print(f"  {ft:<20} {type_counts[ft]:>5}")

        # Check for SEC types
        sec_types = {"8-K", "10-Q", "10-K", "FORM_4", "S-1"}
        found_sec = any(t in type_counts for t in sec_types)
        print(f"\nSEC filings found: {'✓ YES' if found_sec else '✗ NO'}")
    else:
        print(f"Error: {resp.status_code}")
        if resp.text:
            print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"Exception: {e}")

# 2. Check alerts by feature
print("\n2. ALERTS BY FEATURE (last 200 entries):")
print("-" * 80)
try:
    url = f"{SUPABASE_URL}/rest/v1/alerts?select=extra&order=created_at.desc&limit=200"
    resp = requests.get(url, headers=headers, timeout=10)

    if resp.status_code == 200:
        rows = resp.json()
        feature_counts = {}
        for r in rows:
            extra = r.get("extra", {})
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except:
                    extra = {}
            fid = extra.get("feature_id", 0)
            fname = extra.get("feature_name", "Unknown")
            key = f"Feature {fid}: {fname}"
            feature_counts[key] = feature_counts.get(key, 0) + 1

        print(f"Total: {len(rows)} alerts\n")
        for feat in sorted(feature_counts.keys()):
            print(f"  {feat:<40} {feature_counts[feat]:>5}")

        # Check specifically for Feature 1
        feature_1 = any(k.startswith("Feature 1") for k in feature_counts.keys())
        print(f"\nFeature 1 (SEC Filings) alerts: {'✓ YES' if feature_1 else '✗ NO'}")
    else:
        print(f"Error: {resp.status_code}")
        if resp.text:
            print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"Exception: {e}")

print("\n" + "=" * 80)
print("INTERPRETATION:")
print("=" * 80)
print("""
✓ SEC filings found + Feature 1 alerts = Fix worked! SEC ingestion restored.
✗ Still no SEC filings = Either not deployed yet or fix needs adjustment.
✗ Feature 1 alerts but no SEC filings = Alerts cached from before, wait for new ones.
""")
