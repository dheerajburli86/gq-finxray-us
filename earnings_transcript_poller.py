"""
earnings_transcript_poller.py
GQ FinXray US — Feature 5. Earnings call transcripts via FMP API.

Fetches earnings call transcripts for recently-filed 10-Q/10-K documents.
Stores in raw_filings with filing_type=EARNINGS_TRANSCRIPT for AI pipeline
summarization, then generates alerts with sentiment classification.

FMP Ultimate tier includes 8,000+ US companies, 10+ years of history.

Runs every 30 minutes via scheduler in main.py.
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Filing lag: 10-Q filed ~30-45 days after quarter end, 10-K filed ~60-90 days after year end
FILING_LAG_DAYS = 40


def _guess_period(filed_dt):
    """Estimate fiscal quarter from filing date."""
    period_end_estimate = filed_dt - timedelta(days=FILING_LAG_DAYS)
    quarter = max(1, min(4, ((period_end_estimate.month - 1) // 3) + 1))
    return period_end_estimate.year, quarter


def transcript_already_fetched(ticker, year, quarter):
    """Check if transcript already stored."""
    try:
        result = supabase.table("raw_filings").select("id").eq(
            "ticker", ticker
        ).eq("filing_type", "EARNINGS_TRANSCRIPT").contains(
            "extra", {"year": year, "quarter": quarter}
        ).limit(1).execute()
        return len(result.data) > 0
    except Exception:
        return False


def get_best_transcript(ticker, filed_at_iso, form_type="10-Q"):
    """
    Match transcript to fiscal quarter based on filing date.
    Falls back to most recent available transcript if guessed quarter not found.
    """
    try:
        filed_dt = datetime.fromisoformat(filed_at_iso.replace("Z", "+00:00"))
    except Exception:
        filed_dt = datetime.now(timezone.utc)

    # 10-K reports full prior fiscal year (usually Q4 of previous year)
    if form_type == "10-K":
        guess_year = filed_dt.year - 1 if filed_dt.month <= 4 else filed_dt.year
        guess_quarter = 4
    else:
        guess_year, guess_quarter = _guess_period(filed_dt)

    # Try to match guessed quarter first
    available = fmp_client.get_transcript_dates(ticker)
    if available:
        for entry in available:
            year = entry.get("fiscalYear") or entry.get("year")
            quarter = entry.get("period") or entry.get("quarter")
            try:
                quarter = int(str(quarter).upper().replace("Q", ""))
            except Exception:
                continue
            if year == guess_year and quarter == guess_quarter:
                if not transcript_already_fetched(ticker, year, quarter):
                    return year, quarter

        # Fallback: most recent available transcript not yet fetched
        for entry in available:
            year = entry.get("fiscalYear") or entry.get("year")
            quarter = entry.get("period") or entry.get("quarter")
            try:
                quarter = int(str(quarter).upper().replace("Q", ""))
            except Exception:
                continue
            if not transcript_already_fetched(ticker, year, quarter):
                return year, quarter
        return None, None

    # No dates list — try guessed quarter directly
    if not transcript_already_fetched(ticker, guess_year, guess_quarter):
        return guess_year, guess_quarter
    return None, None


def store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
    """Store transcript in raw_filings for AI pipeline processing."""
    content = transcript.get("content", "") if isinstance(transcript, dict) else str(transcript)
    if not content or len(content) < 200:
        logger.info(f"[TRANSCRIPT] {ticker} Q{quarter} FY{year} transcript too short/empty, skipping.")
        return False

    try:
        supabase.table("raw_filings").insert({
            "source": "FMP_TRANSCRIPT",
            "filing_type": "EARNINGS_TRANSCRIPT",
            "ticker": ticker,
            "company_name": company_name,
            "raw_text": content,
            "filing_url": f"fmp_transcript_{ticker}_{year}_Q{quarter}",
            "filed_at": transcript.get("date", datetime.now(timezone.utc).isoformat()) if isinstance(transcript, dict) else datetime.now(timezone.utc).isoformat(),
            "status": "PENDING",
            "extra": tag_extra({
                "year": year,
                "quarter": quarter,
                "title": f"{company_name} Q{quarter} FY{year} Earnings Call Transcript",
                "source": "FMP_TRANSCRIPT",
                "alert_created": False,
            })
        }).execute()
        logger.info(f"[TRANSCRIPT] Stored {ticker} Q{quarter} FY{year} → PENDING for AI pipeline")
        return True
    except Exception as e:
        if "duplicate" not in str(e).lower():
            logger.error(f"[TRANSCRIPT] Failed to store transcript: {e}")
        return False


def find_recent_10q_10k():
    """
    Query most recent 10-Q/10-K filings across ALL companies
    (not filtered by watchlist since watchlist is in-memory in bot for now).

    In production: filter to watchlist to save FMP API calls.
    For now: process all to ensure coverage.
    """
    try:
        result = supabase.table("raw_filings").select(
            "ticker, company_name, filing_type, filed_at"
        ).in_("filing_type", ["10-Q", "10-K"]).order(
            "filed_at", desc=True
        ).limit(100).execute()

        rows = result.data or []
        return rows[:50]  # Process top 50 most recent filings
    except Exception as e:
        logger.error(f"[TRANSCRIPT] Failed to query recent 10-Q/10-K filings: {e}")
        return []


def run_earnings_transcript_poller():
    """Main poller: fetch transcripts for recent 10-Q/10-K filings."""
    logger.info("[TRANSCRIPT] Starting earnings call transcript poller...")

    filings = find_recent_10q_10k()
    if not filings:
        logger.info("[TRANSCRIPT] No recent 10-Q/10-K filings found")
        return

    logger.info(f"[TRANSCRIPT] Found {len(filings)} recent filings, checking for transcripts...")

    processed = 0
    seen_tickers = set()

    for filing in filings:
        ticker = filing.get("ticker", "UNKNOWN")
        if ticker == "UNKNOWN" or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

        # Find best matching transcript
        year, quarter = get_best_transcript(
            ticker,
            filing.get("filed_at", ""),
            filing.get("filing_type", "10-Q")
        )
        if year is None:
            continue

        # Fetch transcript from FMP
        transcript = fmp_client.get_earnings_transcript(ticker, year, quarter)
        if not transcript:
            logger.debug(f"[TRANSCRIPT] No transcript available yet for {ticker} Q{quarter} FY{year}")
            continue

        # Store for pipeline
        company_name = filing.get("company_name", ticker)
        if store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
            processed += 1

        time.sleep(0.5)  # Rate limiting

    logger.info(f"[TRANSCRIPT] Complete. {processed} new transcript(s) queued for AI pipeline")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_earnings_transcript_poller()
