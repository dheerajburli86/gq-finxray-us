#!/usr/bin/env python3
"""
Diagnostic script for GQ FinXray US system.
Run this to check if all components are properly initialized.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

print("=" * 80)
print("GQ FINXRAY US — DIAGNOSTIC CHECK")
print("=" * 80)
print(f"Timestamp: {datetime.now().isoformat()}")
print()

# ── Step 1: Check imports ────────────────────────────────────────────────────────
print("[1/5] Checking imports...")
print("-" * 80)
required_modules = [
    "schedule", "dotenv", "supabase", "requests", "telegram", "asyncio"
]
missing = []
for module in required_modules:
    try:
        __import__(module)
        print(f"  ✓ {module}")
    except ModuleNotFoundError:
        print(f"  ✗ {module} — MISSING")
        missing.append(module)

if missing:
    print(f"\nWARNING: {len(missing)} modules missing. Install with: pip install {' '.join(missing)}")
else:
    print("\nAll required modules present.")

# ── Step 2: Check environment variables ──────────────────────────────────────────
print("\n[2/5] Checking environment variables...")
print("-" * 80)
required_vars = [
    "SUPABASE_URL", "SUPABASE_KEY", "FMP_API_KEY", "DEEPINFRA_API_KEY", "TELEGRAM_TOKEN"
]
env_status = {}
for var in required_vars:
    value = os.getenv(var)
    if value:
        # Show first 10 and last 5 chars for security
        masked = value[:10] + "..." + value[-5:] if len(value) > 20 else "***"
        print(f"  ✓ {var}: {masked}")
        env_status[var] = True
    else:
        print(f"  ✗ {var}: NOT SET")
        env_status[var] = False

if not all(env_status.values()):
    print(f"\nWARNING: {sum(1 for v in env_status.values() if not v)} critical vars missing")

# ── Step 3: Validate schedule registration ───────────────────────────────────────
print("\n[3/5] Validating schedule registration...")
print("-" * 80)
try:
    # Import the schedule building functions
    from main import build_schedule, _lane_jobs, lane, LANE_BUDGETS

    # Clear any existing jobs
    import schedule as sched_lib
    for l in ["sec", "news", "market"]:
        s = sched_lib.Scheduler()
        sched_lib._default_scheduler = s

    # Build the schedule
    build_schedule()

    # Check registration
    total_jobs = 0
    for lane_name in ["sec", "news", "market"]:
        jobs = _lane_jobs.get(lane_name, [])
        sched = lane(lane_name)
        print(f"\n  Lane: {lane_name}")
        print(f"    Registered in _lane_jobs: {len(jobs)}")
        print(f"    Registered in scheduler: {len(sched.get_jobs())}")
        print(f"    Budget: {LANE_BUDGETS.get(lane_name, 120)}s")

        if jobs:
            for job_info in jobs[:5]:  # Show first 5
                print(f"      - {job_info['name']:30} | Interval: {job_info['spec']}")
            if len(jobs) > 5:
                print(f"      ... and {len(jobs) - 5} more")

        total_jobs += len(jobs)

    print(f"\n  Total jobs: {total_jobs}")
    if total_jobs == 0:
        print("  ERROR: No jobs registered!")
    elif total_jobs < 20:
        print("  WARNING: Fewer jobs than expected")
    else:
        print("  ✓ Job registration looks healthy")

except Exception as e:
    print(f"  ERROR: {e}")
    import traceback
    traceback.print_exc()

# ── Step 4: Check feature definitions ────────────────────────────────────────────
print("\n[4/5] Checking feature definitions...")
print("-" * 80)
try:
    from feature_map import FEATURES, resolve_feature

    print(f"  Total features defined: {len(FEATURES)}")

    # Test feature resolution
    test_cases = [
        ("FMP_NEWS", "NEWS", "Should be feature 2"),
        ("SEC_EDGAR", "8-K", "Should be feature 1"),
        ("TECHNICAL", "RSI_OVERBOUGHT", "Should be feature 6"),
        ("FMP_NEWS", "EARNINGS_CALENDAR", "Should be feature 4"),
        ("LARGE_TRADE", "LARGE_TRADE", "Should be feature 5"),
    ]

    print("\n  Feature resolution tests:")
    for source, filing_type, expected in test_cases:
        fid, fname = resolve_feature(source, filing_type)
        status = "✓" if fid > 0 else "✗"
        print(f"    {status} ({source:20}, {filing_type:20}) -> Feature {fid}: {fname}")

except Exception as e:
    print(f"  ERROR: {e}")

# ── Step 5: Test Supabase connectivity (if credentials available) ─────────────────
print("\n[5/5] Testing Supabase connectivity...")
print("-" * 80)
if env_status.get("SUPABASE_URL") and env_status.get("SUPABASE_KEY"):
    try:
        from supabase import create_client
        from dotenv import load_dotenv
        load_dotenv()

        sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

        # Test a simple query
        res = sb.table("poller_runs").select("count", count="exact").execute()
        count = res.count or 0
        print(f"  ✓ Connected to Supabase")
        print(f"  ✓ poller_runs table has {count} entries")

        # Check for recent errors
        err_res = sb.table("poller_error_log").select("*").order("created_at", desc=True).limit(5).execute()
        errors = err_res.data or []
        if errors:
            print(f"  ✓ Found {len(errors)} recent errors")
            print("\n    Most recent errors:")
            for err in errors[:3]:
                created = err.get("created_at", "")[:19]
                job = err.get("job_name", "?")
                msg = err.get("error_message", "")[:60]
                print(f"      {created} | {job:25} | {msg}")
        else:
            print(f"  ✓ No errors logged")

    except Exception as e:
        print(f"  ✗ Supabase connection failed: {e}")
else:
    print("  ⊘ Skipping (credentials not set)")

# ── Summary ──────────────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("RECOMMENDATIONS")
print("=" * 80)
print("""
If only Feature 2 (news) is alerting:

1. Check the APPLICATION LOGS for:
   - [STARTUP sec] and [STARTUP news] and [STARTUP market] messages
   - Any [LANE sec] or [LANE market] errors
   - [JOB FAILED] messages for sec/market lane jobs

2. Query the database:
   SELECT * FROM poller_runs
   ORDER BY created_at DESC LIMIT 50;

   This will show which jobs have actually run.

3. Check for errors:
   SELECT * FROM poller_error_log
   ORDER BY created_at DESC LIMIT 20;

4. Verify watchlist is not empty:
   SELECT COUNT(DISTINCT ticker) as tickers
   FROM watchlists;

5. Check if raw_filings has content beyond NEWS:
   SELECT DISTINCT filing_type, COUNT(*)
   FROM raw_filings
   GROUP BY filing_type
   ORDER BY COUNT(*) DESC;

Common causes of multi-feature outages:
- load_cik_map() failing on SEC lane startup
- FMP API quota exceeded or keys invalid
- Empty watchlist causing filters to skip everything
- Database connectivity issues for specific tables
- Airflow/scheduler daemon not running
""")
print("=" * 80)
