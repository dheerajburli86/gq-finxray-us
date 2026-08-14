#!/usr/bin/env python3
"""Diagnose which features are actually producing alerts"""

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

print("=" * 80)
print("ALERT DIAGNOSIS")
print("=" * 80)

# 1. raw_filings by type
print("\n1. RAW_FILINGS QUEUE (what pollers wrote):")
print("-" * 80)
url = f"{SUPABASE_URL}/rest/v1/raw_filings?select=filing_type&limit=1000"
resp = requests.get(url, headers=headers)
if resp.status_code == 200:
    rows = resp.json()
    type_counts = {}
    status_counts = {}
    for r in rows:
        ft = r.get("filing_type", "?")
        st = r.get("status", "?")
        type_counts[ft] = type_counts.get(ft, 0) + 1
        status_counts[st] = status_counts.get(st, 0) + 1

    print(f"Total raw_filings: {len(rows)}\n")
    print("By filing_type:")
    for ft in sorted(type_counts.keys()):
        print(f"  {ft:<20} {type_counts[ft]:>5}")

    print("\nBy status:")
    for st in sorted(status_counts.keys()):
        print(f"  {st:<20} {status_counts[st]:>5}")
else:
    print(f"Error: {resp.status_code}")

# 2. alerts by feature
print("\n2. ALERTS BY FEATURE (what reached users):")
print("-" * 80)
url = f"{SUPABASE_URL}/rest/v1/alerts?select=extra,filing_type&limit=500"
resp = requests.get(url, headers=headers)
if resp.status_code == 200:
    rows = resp.json()
    feature_counts = {}
    type_counts = {}

    for r in rows:
        extra = r.get("extra", {})
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except:
                extra = {}

        fid = extra.get("feature_id", "?")
        fname = extra.get("feature_name", "Unknown")
        key = f"Feature {fid}: {fname}"
        feature_counts[key] = feature_counts.get(key, 0) + 1

        ft = r.get("filing_type", "?")
        type_counts[ft] = type_counts.get(ft, 0) + 1

    print(f"Total alerts: {len(rows)}\n")
    print("By feature:")
    for feat in sorted(feature_counts.keys()):
        print(f"  {feat:<40} {feature_counts[feat]:>5}")

    print("\nBy filing_type:")
    for ft in sorted(type_counts.keys()):
        print(f"  {ft:<20} {type_counts[ft]:>5}")
else:
    print(f"Error: {resp.status_code}")

# 3. Analysis
print("\n" + "=" * 80)
print("DIAGNOSIS:")
print("=" * 80)

if len(rows) > 0:
    features = set(f.split(":")[0] for f in feature_counts.keys())
    if len(features) == 1 and "Feature 2" in list(feature_counts.keys())[0]:
        print("\n🔴 PROBLEM: Only Feature 2 alerts exist")
        print("\nThis means:")
        print("  - SEC pollers (Feature 1): Not writing to raw_filings, OR")
        print("  - AI pipeline: Not creating alerts from raw SEC data, OR")
        print("  - Other pollers (3-13): Not running at all")
        print("\nNext: Check if raw_filings has non-NEWS entries")
        if "NEWS" in type_counts and type_counts.get("NEWS", 0) > len(rows) * 0.9:
            print(f"      raw_filings is {type_counts.get('NEWS', 0)}/{len(rows)} NEWS = {100*type_counts.get('NEWS', 0)/len(rows):.0f}%")
            print("      → SEC/technical/earnings pollers NOT writing to raw_filings")
    else:
        print(f"\n✓ Multiple features detected: {features}")
else:
    print("\n🔴 No alerts at all!")
