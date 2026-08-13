"""
earnings_alerts.py
GQ FinXray US — Earnings calendar and EPS surprise detection.

Polls /stable/earnings-calendar for scheduled and actual earnings.
Detects EPS misses (actual < estimated) and generates alerts.

Key from FMP support:
- /stable/earnings-calendar provides both scheduled dates and actual results
- Actual EPS appears within hours of market close
- Poll daily, or every 2-4 hours during earnings season
"""

import os
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "earnings_alerts"


def get_earnings_for_ticker(ticker, days_back=7, days_forward=1):
    """
    Fetch earnings calendar events for a single ticker.
    Returns list of earnings events (scheduled + actual).

    NOTE: this module is not currently registered in main.py — nothing calls it.
    It is kept working so that wiring it up is a one-line change rather than a
    debugging session.

    The previous implementation called fmp_client.get_earnings_calendar(ticker),
    but that function's signature is (from_date, to_date) and the endpoint has no
    per-symbol filter. Every call raised TypeError, was swallowed by the except
    below, and returned [] — so the module reported "no earnings misses" forever
    while never actually looking. Fetch the window, then filter by symbol here.
    """
    try:
        today = datetime.now(timezone.utc).date()
        data = fmp_client.get_earnings_calendar(
            (today - timedelta(days=days_back)).isoformat(),
            (today + timedelta(days=days_forward)).isoformat(),
        )
        if not isinstance(data, list):
            return []
        want = (ticker or "").upper()
        return [e for e in data if (e.get("symbol") or "").upper() == want]
    except Exception as e:
        logger.error(f"[EARNINGS] Failed to fetch {ticker}: {e}")
        return []


def detect_eps_surprise(earnings_event):
    """
    Detect if actual EPS < estimated EPS (earnings miss).

    Returns:
        dict with 'is_miss', 'eps_actual', 'eps_estimate', 'beat_amount' or None
    """
    actual = earnings_event.get('eps')  # actual EPS
    estimate = earnings_event.get('epsEstimated')  # estimated EPS

    if actual is None or estimate is None:
        return None  # Not yet announced or missing data

    try:
        actual = float(actual)
        estimate = float(estimate)
    except (ValueError, TypeError):
        return None

    is_miss = actual < estimate
    beat_amount = actual - estimate

    return {
        "is_miss": is_miss,
        "eps_actual": actual,
        "eps_estimate": estimate,
        "beat_amount": beat_amount,
    }


def poll_earnings_for_tickers():
    """
    Poll earnings calendar for watched tickers.
    Detect misses and create alerts.
    """
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[EARNINGS] No watched tickers")
            return

        logger.info(f"[EARNINGS] Polling {len(watched)} tickers for earnings")

        new_alerts = 0
        misses_found = 0

        for ticker in sorted(watched):
            earnings = get_earnings_for_ticker(ticker)
            if not earnings:
                continue

            for event in earnings:
                announcement_time = event.get('announcementTime')
                announcement_date = event.get('announcementDate')
                reporting_date = event.get('reportingDate')

                # Skip future/unannounced
                if not announcement_date:
                    continue

                # Skip if already processed
                event_date_str = announcement_date or reporting_date
                try:
                    event_date = datetime.fromisoformat(event_date_str.replace('Z', '+00:00'))
                except:
                    continue

                # Only process earnings from last 24 hours (to catch fresh results)
                if (datetime.now(timezone.utc) - event_date).total_seconds() > 86400:
                    continue

                surprise = detect_eps_surprise(event)
                if not surprise:
                    continue  # Not yet announced

                if surprise['is_miss']:
                    misses_found += 1
                    logger.info(
                        f"[EARNINGS] {ticker} MISS: "
                        f"actual={surprise['eps_actual']}, "
                        f"est={surprise['eps_estimate']}, "
                        f"beat={surprise['beat_amount']}"
                    )

                    # Store as alert
                    try:
                        supabase.table("raw_filings").insert({
                            "ticker": ticker,
                            "company_name": event.get('symbol', ticker),
                            "source": "FMP",
                            "filing_type": "EARNINGS_MISS",
                            "filing_url": "",
                            "filing_date": announcement_date,
                            "content_hash": f"{ticker}-{announcement_date}-earnings",
                            "raw_text": (
                                f"EPS Miss: {ticker} reported ${surprise['eps_actual']} "
                                f"vs estimate ${surprise['eps_estimate']} "
                                f"(beat: ${surprise['beat_amount']})"
                            ),
                            "status": "PENDING",
                            "extra": tag_extra(
                                {
                                    "eps_actual": surprise['eps_actual'],
                                    "eps_estimate": surprise['eps_estimate'],
                                    "beat_amount": surprise['beat_amount'],
                                },
                                "FMP",
                                "EARNINGS_MISS"
                            ),
                        }).execute()
                        new_alerts += 1
                    except Exception as e:
                        logger.warning(f"[EARNINGS] Failed to insert {ticker}: {e}")

        logger.info(f"[EARNINGS] Done — {new_alerts} new alerts, {misses_found} misses found")

    except Exception as e:
        logger.error(f"[EARNINGS] Poll failed: {e}")
        log_poller_error(POLLER_NAME, str(e))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poll_earnings_for_tickers()
