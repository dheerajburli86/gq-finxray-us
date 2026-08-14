#!/usr/bin/env python3
"""Quick check: what are the pollers actually producing?"""

import os
from dotenv import load_dotenv
import requests

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

print("=" * 80)
print("POLLER OUTPUT CHECK")
print("=" * 80)

# Check raw_filings by type
print("\n1. RAW_FILINGS BY TYPE (last 200):")
print("-" * 80)
url = f"{SUPABASE_URL}/rest/v1/raw_filings?select=filing_type,count&order=created_at.desc&limit=200"
resp = requests.get(url, headers={**headers, "Prefer": "count=exact"})
if resp.status_code == 200:
    rows = resp.json()
    type_counts = {}
    for r in rows:
        ft = r.get("filing_type", "?")
        type_counts[ft] = type_counts.get(ft, 0) + 1

    for ft in sorted(type_counts.keys()):
        print(f"  {ft:<20} {type_counts[ft]:>5} entries")
else:
    print(f"Error: {resp.status_code}")

# Check alerts by feature
print("\n2. ALERTS BY FEATURE (last 50):")
print("-" * 80)
url = f"{SUPABASE_URL}/rest/v1/alerts?select=extra,count&order=created_at.desc&limit=50"
resp = requests.get(url, headers={**headers, "Prefer": "count=exact"})
if resp.status_code == 200:
    rows = resp.json()
    feature_counts = {}
    for r in rows:
        import json
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

    for feat in sorted(feature_counts.keys()):
        print(f"  {feat:<40} {feature_counts[feat]:>5} alerts")
else:
    print(f"Error: {resp.status_code}")

# Check poller_runs to see which jobs ran
print("\n3. POLLER JOBS (last 20 runs):")
print("-" * 80)
url = f"{SUPABASE_URL}/rest/v1/poller_runs?select=job_name,lane,status&order=started_at.desc&limit=20"
resp = requests.get(url, headers=headers)
if resp.status_code == 200:
    rows = resp.json()
    seen = set()
    for r in rows:
        job = r.get("job_name", "?")
        lane = r.get("lane", "?")
        status = r.get("status", "?")
        key = (job, lane)
        if key not in seen:
            status_emoji = "✓" if status == "OK" else "⚠" if status == "SLOW" else "❌"
            print(f"  {status_emoji} {job:<30} [{lane:<6}] {status}")
            seen.add(key)
else:
    print(f"Error: {resp.status_code}")

print("\n" + "=" * 80)
print("INTERPRETATION:")
print("=" * 80)
print("""
If raw_filings only has NEWS → SEC/technical/earnings pollers not writing
If alerts only has Feature 2 → other features not being created
If poller_runs missing sec/market jobs → jobs not running at all
""")
