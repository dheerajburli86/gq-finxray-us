#!/usr/bin/env python3
"""
trigger_earnings_transcript_alerts.py
GQ FinXray US — Feature 10. Re-queue an earnings call transcript alert so it
goes out to every user watching that ticker, carrying a working source link.

WHY THIS DOES NOT SEND ANYTHING ITSELF
--------------------------------------
delivery.py owns fan-out. It resolves the audience from the watchlist, applies
each user's min_impact / muted_features / daily cap, renders the Telegram
message, and records one row per (alert, user) in `alert_deliveries` — whose
UNIQUE key is what makes a crash mid-fan-out safe to retry. Sending from here
too would duplicate all of that and bypass the ledger.

So this script's only job is to put a well-formed row in `alerts` with
delivered=False. The running delivery loop picks it up within a second.

TWO THINGS THAT MAKE A RE-SEND FAIL, BOTH HANDLED HERE
------------------------------------------------------
1. `alert_deliveries` suppresses a resend of the SAME alert id to the same user
   once it is SENT. Flipping delivered back to False on the original row
   therefore re-queues it and then delivers it to nobody.
2. delivery._expire_stale_alerts() retires anything undelivered and older than
   GQ_MAX_ALERT_AGE_MINUTES (90 by default) — without sending it. An old row
   flipped back to False is silently re-retired on the next cycle, usually
   within a minute.

Both are avoided the same way: insert a NEW row (new id, fresh created_at)
rather than reviving the old one.

Usage:
    python trigger_earnings_transcript_alerts.py            # everything missing a link
    python trigger_earnings_transcript_alerts.py AVGO       # just this ticker
"""

import os
import sys
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

SOURCE = "FMP_TRANSCRIPT"
FILING_TYPE = "EARNINGS_TRANSCRIPT"


def transcript_link(ticker, year, quarter):
    """
    Raw-JSON source link, served by our own `transcript` edge function.

    Deliberately not FMP: /api/ answers 401 unauthenticated, embedding our
    apikey would publish a paid credential to every subscriber, and FMP's
    public transcript pages 404. The transcript text is already in
    raw_filings, so the alert links to the copy we hold.
    """
    base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    if not (ticker and year and quarter and base):
        return None
    return (f"{base}/functions/v1/transcript"
            f"?ticker={ticker}&year={year}&quarter={quarter}")


def fetch_transcript_alerts(ticker=None, limit=50):
    try:
        q = (supabase.table("alerts")
             .select("*")
             .eq("source", SOURCE)
             .eq("filing_type", FILING_TYPE))
        if ticker:
            q = q.eq("ticker", ticker.upper())
        return (q.order("created_at", desc=True).limit(limit).execute()).data or []
    except Exception as e:
        logger.error("[TRANSCRIPT_ALERT] Could not read transcript alerts: %s", e)
        return []


def has_watchers(ticker):
    """No point queueing an alert nobody will match."""
    try:
        rows = (supabase.table("watchlists")
                .select("user_id")
                .eq("ticker", ticker)
                .limit(1)
                .execute()).data or []
        return len(rows) > 0
    except Exception as e:
        logger.error("[TRANSCRIPT_ALERT] Watchlist lookup failed for %s: %s", ticker, e)
        return False


def requeue(alert):
    """
    Clone one transcript alert into a fresh undelivered row carrying the link.

    Returns True if a row was queued.
    """
    ticker = (alert.get("ticker") or "").upper()
    extra = dict(alert.get("extra") or {})
    link = transcript_link(ticker, extra.get("year"), extra.get("quarter"))

    if not link:
        logger.warning("[TRANSCRIPT_ALERT] %s: no year/quarter in extra, cannot "
                       "build a source link — skipping", ticker)
        return False

    if not has_watchers(ticker):
        logger.info("[TRANSCRIPT_ALERT] %s: nobody is watching this ticker", ticker)
        return False

    extra["fmp_link"] = link
    extra["requeued_at"] = datetime.now(timezone.utc).isoformat()
    extra["requeued_from"] = alert.get("id")
    payload = extra.get("structured_payload")
    if isinstance(payload, dict):
        payload["fmp_link"] = link

    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": alert.get("summary"),
            "impact": alert.get("impact") or "MEDIUM",
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "filing_url": link,
            "extra": extra,
            "delivered": False,
        }).execute()
    except Exception as e:
        logger.error("[TRANSCRIPT_ALERT] %s: insert failed: %s", ticker, e)
        return False

    # Stamp the row we cloned so an unattended re-run cannot queue it twice.
    try:
        origin_extra = dict(alert.get("extra") or {})
        origin_extra["requeued"] = True
        (supabase.table("alerts")
         .update({"extra": origin_extra})
         .eq("id", alert.get("id"))
         .execute())
    except Exception as e:
        logger.warning("[TRANSCRIPT_ALERT] %s: queued, but could not stamp the "
                       "source row (re-running may duplicate): %s", ticker, e)
    return True


def trigger_transcript_alerts(ticker=None):
    logger.info("[TRANSCRIPT_ALERT] Re-queueing earnings transcript alerts%s",
                f" for {ticker.upper()}" if ticker else "")

    alerts = fetch_transcript_alerts(ticker)
    if not alerts:
        logger.info("[TRANSCRIPT_ALERT] No transcript alerts found.")
        return 0

    # One re-send per ticker+quarter, newest row wins.
    latest = {}
    for a in alerts:
        extra = a.get("extra") or {}
        key = (a.get("ticker"), extra.get("year"), extra.get("quarter"))
        latest.setdefault(key, a)

    queued = 0
    for alert in latest.values():
        # An explicit ticker means "send me this one now" — otherwise only
        # repair the rows that actually went out without a source link.
        if not ticker:
            if alert.get("filing_url") or alert.get("link"):
                continue
            if (alert.get("extra") or {}).get("requeued"):
                continue
        if requeue(alert):
            logger.info("[TRANSCRIPT_ALERT] %s: queued for delivery", alert.get("ticker"))
            queued += 1

    logger.info("[TRANSCRIPT_ALERT] Done. %d alert(s) queued — delivery.py will "
                "fan them out to watchers on its next cycle.", queued)
    return queued


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    trigger_transcript_alerts(sys.argv[1] if len(sys.argv) > 1 else None)
