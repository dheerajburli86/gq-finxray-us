#!/usr/bin/env python3
"""
trigger_earnings_transcript_alerts.py
GQ FinXray US — Trigger earnings call transcript alerts to users' watchlists.

This script processes pending earnings transcripts and sends them to users
who have those tickers on their watchlist, with proper JSON links included.

Usage:
    python trigger_earnings_transcript_alerts.py
"""

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

import gquants_format_converter as gq_fmt
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ET = ZoneInfo("America/New_York")
SOURCE = "FMP_TRANSCRIPT"
FILING_TYPE = "EARNINGS_TRANSCRIPT"


def get_processed_transcripts():
    """
    Find earnings transcripts that were processed by ai_pipeline
    and turned into summarized alerts, but haven't been delivered yet.
    """
    try:
        result = supabase.table("alerts") \
            .select("*") \
            .eq("source", SOURCE) \
            .eq("filing_type", FILING_TYPE) \
            .eq("delivered", False) \
            .order("created_at", desc=True) \
            .limit(50) \
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"[TRANSCRIPT_ALERT] Failed to fetch processed transcripts: {e}")
        return []


def get_users_for_ticker(ticker):
    """
    Get all users who have this ticker on their watchlist.
    Returns list of user dicts with user_id, username.
    """
    try:
        result = supabase.table("watchlists") \
            .select("user_id, username") \
            .eq("ticker", ticker) \
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"[TRANSCRIPT_ALERT] Failed to fetch users for {ticker}: {e}")
        return []


def send_alert_to_user(user_id, alert_data):
    """
    Send an individual alert to a specific user.
    Returns True if successful.
    """
    try:
        # Mark as delivered to this user
        alert_copy = dict(alert_data)
        alert_copy["delivered"] = True
        alert_copy["extra"]["user_id"] = user_id
        alert_copy["extra"]["delivered_at"] = datetime.now(ET).isoformat()

        # Insert the user-specific alert
        supabase.table("alerts").insert(alert_copy).execute()
        return True
    except Exception as e:
        logger.error(f"[TRANSCRIPT_ALERT] Failed to send alert to user {user_id}: {e}")
        return False


def trigger_transcript_alerts():
    """
    Main trigger function: find processed transcripts and send to watchlist users.
    """
    logger.info("[TRANSCRIPT_ALERT] Starting earnings transcript alert trigger...")

    transcripts = get_processed_transcripts()
    if not transcripts:
        logger.info("[TRANSCRIPT_ALERT] No pending transcript alerts to send.")
        return

    logger.info(f"[TRANSCRIPT_ALERT] Found {len(transcripts)} processed transcript(s)")

    sent_count = 0
    failed_count = 0

    for transcript_alert in transcripts:
        ticker = transcript_alert.get("ticker", "UNKNOWN")
        if ticker == "UNKNOWN":
            continue

        logger.info(f"[TRANSCRIPT_ALERT] Processing {ticker}...")

        # Get all users watching this ticker
        users = get_users_for_ticker(ticker)
        if not users:
            logger.info(f"[TRANSCRIPT_ALERT] {ticker}: No users watching this ticker")
            continue

        logger.info(f"[TRANSCRIPT_ALERT] {ticker}: Found {len(users)} users watching")

        # Send to each user
        for user in users:
            user_id = user.get("user_id")
            username = user.get("username", "Unknown")

            if send_alert_to_user(user_id, transcript_alert):
                logger.info(f"[TRANSCRIPT_ALERT] {ticker} → {username} ({user_id}): SENT")
                sent_count += 1
            else:
                logger.warning(f"[TRANSCRIPT_ALERT] {ticker} → {username} ({user_id}): FAILED")
                failed_count += 1

    logger.info(
        f"[TRANSCRIPT_ALERT] Done. Sent: {sent_count}, Failed: {failed_count}"
    )


def format_transcript_alert_with_links(ticker, company_name, year, quarter, summary, impact):
    """
    Create a properly formatted alert with working JSON link.
    """
    # Build proper FMP link
    fmp_link = f"https://financialmodelingprep.com/api/v4/earning-call-transcript?symbol={ticker}&year={year}&quarter={quarter}"

    extra = tag_extra({
        "ticker": ticker,
        "company_name": company_name,
        "year": year,
        "quarter": quarter,
        "fmp_link": fmp_link,
        "created_at": datetime.now(ET).isoformat(),
    }, SOURCE, FILING_TYPE)

    return {
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": SOURCE,
        "filing_type": FILING_TYPE,
        "filing_url": fmp_link,
        "extra": extra,
        "delivered": False,
    }


def create_alert_for_transcript(ticker, company_name, year, quarter, summary, impact):
    """
    Create a new alert for an earnings transcript with proper links.
    """
    try:
        alert_data = format_transcript_alert_with_links(
            ticker, company_name, year, quarter, summary, impact
        )
        supabase.table("alerts").insert(alert_data).execute()
        logger.info(
            f"[TRANSCRIPT_ALERT] Created alert for {ticker} Q{quarter} FY{year}"
        )
        return True
    except Exception as e:
        if "duplicate" not in str(e).lower():
            logger.error(
                f"[TRANSCRIPT_ALERT] Failed to create alert for {ticker}: {e}"
            )
        return False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    trigger_transcript_alerts()
