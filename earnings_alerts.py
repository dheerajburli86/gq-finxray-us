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
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

# Every date FMP hands back for an earnings event is a US market date.
ET = ZoneInfo("America/New_York")

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
    # BUGFIX 2026-08-19: read `eps`, which does not exist on FMP's /stable/
    # earnings-calendar response. The field is `epsActual` (the bare `eps` name
    # is legacy v3). `actual` was therefore always None, this function always
    # returned None, and every event was skipped — Feature 4 could not emit a
    # single alert. Legacy names kept as fallbacks.
    actual = (earnings_event.get('epsActual')
              if earnings_event.get('epsActual') is not None
              else earnings_event.get('eps'))
    estimate = (earnings_event.get('epsEstimated')
                if earnings_event.get('epsEstimated') is not None
                else earnings_event.get('epsEstimate'))

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

        # /stable/earnings-calendar has no per-symbol filter, so it returns the
        # whole market for the window. get_earnings_for_ticker() downloads that
        # entire payload and throws away everything but one symbol -- calling it
        # per ticker fetched the same market-wide calendar once for every watched
        # name. Fetch the window once and index it.
        today = datetime.now(timezone.utc).date()
        try:
            calendar = fmp_client.get_earnings_calendar(
                (today - timedelta(days=7)).isoformat(),
                (today + timedelta(days=1)).isoformat(),
            ) or []
        except Exception as e:
            logger.error(f"[EARNINGS] Calendar fetch failed: {e}")
            return

        watched_upper = {str(t).upper() for t in watched}
        by_ticker = {}
        for ev in calendar:
            sym = (ev.get("symbol") or "").upper()
            if sym in watched_upper:
                by_ticker.setdefault(sym, []).append(ev)

        for ticker in sorted(by_ticker):
            earnings = by_ticker[ticker]
            if not earnings:
                continue

            for event in earnings:
                # BUGFIX 2026-08-19: this read announcementTime / announcementDate
                # / reportingDate. NONE of those fields exist on FMP's /stable/
                # earnings-calendar payload, whose shape is:
                #   {symbol, date, epsActual, epsEstimated,
                #    revenueActual, revenueEstimated, lastUpdated}
                # `announcement_date` was therefore None on every event and the
                # `if not announcement_date: continue` below discarded the entire
                # calendar on every run, forever. The date field is `date`.
                event_date_str = (event.get('date')
                                  or event.get('announcementDate')
                                  or event.get('reportingDate'))
                if not event_date_str:
                    continue

                try:
                    event_date = datetime.fromisoformat(
                        str(event_date_str).replace('Z', '+00:00'))
                    if event_date.tzinfo is None:
                        # FMP returns a bare calendar date for this field. Treat
                        # it as US market time, not UTC, or a same-day result is
                        # up to five hours out and can fall outside the window.
                        event_date = event_date.replace(tzinfo=ET).astimezone(timezone.utc)
                except Exception:
                    continue

                # Only process earnings from last 24 hours (to catch fresh results)
                if (datetime.now(timezone.utc) - event_date).total_seconds() > 86400:
                    continue

                surprise = detect_eps_surprise(event)
                if not surprise:
                    continue  # Not yet announced

                # A beat is exactly as newsworthy as a miss, and this module is
                # named for surprise detection in both directions. Reporting only
                # misses discarded half of every earnings season.
                is_miss = surprise['is_miss']
                filing_type = "EARNINGS_MISS" if is_miss else "EARNINGS_BEAT"
                verb = "missed" if is_miss else "beat"
                gap = abs(surprise['beat_amount'])

                if is_miss:
                    misses_found += 1
                logger.info(
                    f"[EARNINGS] {ticker} {filing_type}: "
                    f"actual={surprise['eps_actual']}, "
                    f"est={surprise['eps_estimate']}, "
                    f"delta={surprise['beat_amount']:+.4f}"
                )

                try:
                    # NOTE: the column is `filed_at`, not `filing_date` -- there is
                    # no `filing_date` column on raw_filings, so every insert here
                    # used to fail with an undefined-column error that was
                    # swallowed by the warning below. This feature has never
                    # written a row.
                    supabase.table("raw_filings").insert({
                        "ticker": ticker,
                        "company_name": event.get('symbol', ticker),
                        "source": "FMP",
                        "filing_type": filing_type,
                        "filing_url": (
                            "https://site.financialmodelingprep.com/calendar/earnings"
                            f"?symbol={ticker}#{announcement_date}"
                        ),
                        "filed_at": announcement_date,
                        "content_hash": f"{ticker}-{announcement_date}-{filing_type}",
                        "raw_text": (
                            f"{ticker} reported quarterly earnings per share of "
                            f"${surprise['eps_actual']} against a consensus estimate of "
                            f"${surprise['eps_estimate']}, {verb} expectations by "
                            f"${gap:.4f} per share."
                        ),
                        "status": "PENDING",
                        "extra": tag_extra(
                            {
                                "eps_actual": surprise['eps_actual'],
                                "eps_estimate": surprise['eps_estimate'],
                                "beat_amount": surprise['beat_amount'],
                                "announcement_date": announcement_date,
                                "source_name": "FMP Earnings Calendar",
                            },
                            "FMP",
                            filing_type,
                        ),
                    }).execute()
                    new_alerts += 1
                except Exception as e:
                    if "duplicate" not in str(e).lower() and "unique" not in str(e).lower():
                        logger.warning(f"[EARNINGS] Failed to insert {ticker}: {e}")

        logger.info(f"[EARNINGS] Done — {new_alerts} new alerts, {misses_found} misses found")

    except Exception as e:
        logger.error(f"[EARNINGS] Poll failed: {e}")
        # log_poller_error takes (poller_name, job_name, error). Calling it with
        # two arguments raised TypeError from inside this handler, replacing the
        # real failure with a stack trace about the logger itself.
        log_poller_error(POLLER_NAME, "poll_earnings_for_tickers", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poll_earnings_for_tickers()
