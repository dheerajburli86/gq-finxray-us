#!/usr/bin/env python3
"""
verify_payload_log.py — is the payload logger actually live?

The table is created by a migration that has to be run by hand against
Supabase; delivery.py degrades to a warning if it is missing, so a logger
that was never enabled looks exactly like one with nothing to log.

    python verify_payload_log.py            # check + show recent rows
    python verify_payload_log.py --write-test   # prove a write round-trips

Run where SUPABASE_URL / SUPABASE_KEY are set.
"""
import os
import sys
from collections import Counter

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

URL, KEY = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
if not URL or not KEY:
    sys.exit("SUPABASE_URL / SUPABASE_KEY not set — run this where the app runs.")

sb = create_client(URL, KEY)
MIGRATION = "migrations/2026-09-08_payload_log.sql"

print("=" * 66)
print("payload_log — readiness check")
print("=" * 66)

try:
    rows = (sb.table("payload_log")
            .select("*").order("created_at", desc=True).limit(20).execute().data or [])
except Exception as e:
    msg = str(e)
    print(f"\nTABLE NOT USABLE: {msg[:200]}")
    if "does not exist" in msg or "PGRST205" in msg or "42P01" in msg:
        print(f"\n  The migration has NOT been run. Apply it in the Supabase")
        print(f"  SQL editor:\n      {MIGRATION}")
        print("\n  Until then, delivery.py logs a warning per payload and")
        print("  keeps delivering normally — nothing else breaks.")
    sys.exit(1)

print("\nTable exists and is readable ✓")
print(f"rows returned (most recent 20): {len(rows)}")

if not rows:
    print("\n  No rows yet. Expected if no alert carrying a structured payload")
    print("  or SEC JSON links has been DELIVERED since the table was created —")
    print("  it is written at delivery time, not when the alert is generated.")
else:
    print("\nby payload_type:")
    for t, n in Counter(r.get("payload_type") for r in rows).most_common():
        print(f"   {n:>3}  {t}")
    print("\nmost recent:")
    for r in rows[:5]:
        link = r.get("frontend_link") or "(no link)"
        print(f"   {r.get('created_at', '?')[:19]}  {r.get('ticker'):<6} "
              f"{r.get('payload_type'):<14} {link[:70]}")

if "--write-test" in sys.argv:
    print("\n" + "-" * 66)
    print("write test")
    print("-" * 66)
    probe = {
        "alert_id": None,
        "ticker": "TEST",
        "payload_type": "verify",
        "filing_type": "VERIFY",
        "source": "VERIFY",
        "payload": {"type": "verify", "note": "verify_payload_log.py probe"},
        "frontend_link": None,
    }
    try:
        sb.table("payload_log").insert(probe).execute()
        print("insert ok ✓")
        back = (sb.table("payload_log").select("*")
                .eq("ticker", "TEST").limit(5).execute().data or [])
        print(f"read back {len(back)} probe row(s) ✓")
        for r in back:
            sb.table("payload_log").delete().eq("id", r["id"]).execute()
        print("probe rows cleaned up ✓")
        print("\nThe logger can write. It will record every delivered alert")
        print("carrying a structured payload or SEC JSON links.")
    except Exception as e:
        print(f"WRITE FAILED: {str(e)[:200]}")
        print("Check the key's grants / RLS policy on payload_log.")
        sys.exit(1)

print("\nDone.")
