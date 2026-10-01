#!/usr/bin/env python3
"""
clear_backlog.py — retire the accumulated queue and start fresh.

The pipeline and delivery queues were strict FIFO with no freshness gate, so
a backlog meant stale alerts went out ahead of breaking news. The code now
expires stale rows on its own every cycle; this clears what is already
sitting there in one shot.

Retires, it does not delete: PENDING filings become EXPIRED and undelivered
alerts become delivered=True. The rows and their summaries stay in the tables
so you can still see what happened -- they just stop being queued. Nothing
that has already been sent to a user is touched.

    python clear_backlog.py                 # dry run, shows what WOULD change
    python clear_backlog.py --yes           # retire everything still queued
    python clear_backlog.py --yes --older-than 90   # only rows older than 90m
    python clear_backlog.py --yes --purge   # hard DELETE instead of retire

Run where SUPABASE_URL / SUPABASE_KEY are set.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

URL, KEY = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
if not URL or not KEY:
    sys.exit("SUPABASE_URL / SUPABASE_KEY not set — run this where the app runs.")

sb = create_client(URL, KEY)

ap = argparse.ArgumentParser()
ap.add_argument("--yes", action="store_true", help="actually apply the change")
ap.add_argument("--older-than", type=int, default=0, metavar="MINUTES",
                help="only touch rows older than this (default: everything queued)")
ap.add_argument("--purge", action="store_true",
                help="hard DELETE instead of retiring in place (irreversible)")
args = ap.parse_args()

cutoff = None
if args.older_than:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=args.older_than)).isoformat()

scope = f"older than {args.older_than} minutes" if cutoff else "ALL queued rows"
print("=" * 68)
print(f"Backlog cleanup — {scope}")
print(f"Mode: {'PURGE (delete)' if args.purge else 'retire in place'}"
      f"{'' if args.yes else '   [DRY RUN — nothing will change]'}")
print("=" * 68)


def survey(table, **filters):
    try:
        q = sb.table(table).select("*")
        for col, val in filters.items():
            q = q.eq(col, val)
        if cutoff:
            q = q.lt("created_at", cutoff)
        return q.execute().data or []
    except Exception as e:
        print(f"  ! could not read {table}: {e}")
        return []


pending = survey("raw_filings", status="PENDING")
undelivered = survey("alerts", delivered=False)

print(f"\nPENDING raw_filings : {len(pending)}")
for ft, n in Counter(f.get("filing_type") for f in pending).most_common(10):
    print(f"    {n:>5}  {ft}")
if pending:
    oldest = min((f.get("created_at") or "") for f in pending)
    print(f"    oldest: {oldest}")

print(f"\nundelivered alerts  : {len(undelivered)}")
for src, n in Counter(a.get("source") for a in undelivered).most_common(10):
    print(f"    {n:>5}  {src}")
if undelivered:
    oldest = min((a.get("created_at") or "") for a in undelivered)
    print(f"    oldest: {oldest}")

if not pending and not undelivered:
    print("\nQueues are already clear. Nothing to do.")
    sys.exit(0)

if not args.yes:
    print("\nDRY RUN — re-run with --yes to apply.")
    sys.exit(0)


def apply(table, filters, patch):
    """Retire or purge, chunked by id so a huge backlog cannot blow the URL."""
    ids = [r["id"] for r in filters if r.get("id")]
    done = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        try:
            if args.purge:
                sb.table(table).delete().in_("id", chunk).execute()
            else:
                sb.table(table).update(patch).in_("id", chunk).execute()
            done += len(chunk)
        except Exception as e:
            print(f"  ! {table} chunk {i} failed: {e}")
    return done


n1 = apply("raw_filings", pending, {"status": "EXPIRED"})
print(f"\nraw_filings : {n1} {'deleted' if args.purge else 'marked EXPIRED'}")

n2 = apply("alerts", undelivered, {"delivered": True})
print(f"alerts      : {n2} {'deleted' if args.purge else 'retired (delivered=True, not sent)'}")

print("\nDone. Queues are clear — the next cycle starts on fresh content only.")
print("Ongoing staleness is now handled automatically by the pipeline and")
print("delivery loops (GQ_MAX_CONTENT_AGE_MINUTES / GQ_MAX_ALERT_AGE_MINUTES).")
