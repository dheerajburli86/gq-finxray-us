"""
earnings_transcript_poller.py
GQ FinXray US — Feature 10. Earnings call transcripts.

Earnings call transcripts come from FMP (confirmed available at
any tier — see AAPL_Finnhub_Complete_Report*.pdf in the project) and the
whole reason FMP Ultimate is in the stack now: FMP's
/stable/earning-call-transcript covers 8,000+ US companies, 10+ years of
history, quarterly and annual.

Wiring, per the integration plan already sketched in the project's own
reference material: the trigger already exists — when edgar_poller.py
detects a 10-Q or 10-K for a watchlisted ticker, that is the signal a
transcript for the matching quarter should now exist at FMP. This poller
picks those filings up, pulls the transcript, and drops it into
raw_filings with filing_type="EARNINGS_TRANSCRIPT" so it goes through the
EXACT SAME ai_pipeline.py flow as news/announcements: gibberish check,
relevance check, S.1/S.3 retry-with-escalating-word-limit summarisation
(70->75->80->...->100 words), V.1 validation, impact classification,
dedup, then an alert. No separate summarisation path was built for this —
that was the point of routing it through raw_filings like everything else.

Runs every 30 minutes via scheduler in main.py, same cadence as
result_snapshot.py since it rides the same 10-Q/10-K trigger.
"""
import os
import time
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

import gquants_format_converter as gq_fmt
import fmp_client

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Roughly maps a filing date back to the fiscal quarter it's reporting ON
# (not the quarter it was FILED in). A 10-Q covering Q1 (period end Mar 31)
# is typically filed ~30-45 days later, in early-to-mid May -- reading the
# quarter straight off the filed month would misread that as Q2. Backing the
# date up by ~40 days before deriving month/quarter lands in the right
# fiscal quarter far more often. This is still a heuristic (fiscal calendars
# vary by company, and this doesn't know each company's specific reporting
# lag) — get_best_transcript() below double-checks against FMP's own
# transcripts-dates-by-symbol list rather than trusting this blindly, so a
# wrong guess here just means it falls through to "pick the most recent
# available transcript" instead of the exact quarter.
FILING_LAG_DAYS = 40

# How far back a 10-Q/10-K can be and still be worth checking for a transcript.
TRIGGER_LOOKBACK_DAYS = int(os.getenv("GQ_TRANSCRIPT_LOOKBACK_DAYS", "21"))


def _guess_period(filed_dt):
    period_end_estimate = filed_dt - timedelta(days=FILING_LAG_DAYS)
    quarter = max(1, min(4, ((period_end_estimate.month - 1) // 3) + 1))
    return period_end_estimate.year, quarter


def transcript_already_fetched(ticker, year, quarter):
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "EARNINGS_TRANSCRIPT") \
            .eq("extra->>year", str(year)) \
            .eq("extra->>quarter", str(quarter)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def get_best_transcript(ticker, filed_at_iso, form_type="10-Q"):
    """
    Pick the transcript matching the filing's fiscal quarter if FMP lists it,
    otherwise fall back to the most recent available transcript for the ticker
    that hasn't been fetched yet.
    """
    try:
        filed_dt = datetime.fromisoformat(filed_at_iso.replace("Z", "+00:00"))
    except Exception:
        filed_dt = datetime.now(timezone.utc)

    if form_type == "10-K":
        # A 10-K reports on the full prior fiscal year, so it maps to Q4, and
        # calendar-year filers commonly file it Jan-Apr of the FOLLOWING
        # year — meaning the year in the filed date is usually one ahead of
        # the fiscal year being reported on. The lag-based _guess_period()
        # heuristic (tuned for quarterly filings' ~40-day lag) under-corrects
        # for this, so 10-Ks get their own simpler rule instead.
        guess_year = filed_dt.year - 1 if filed_dt.month <= 4 else filed_dt.year
        guess_quarter = 4
    else:
        guess_year, guess_quarter = _guess_period(filed_dt)

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
        # Fall back: most recent available transcript not yet fetched
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

    # No dates list available — just try the guessed quarter directly.
    if not transcript_already_fetched(ticker, guess_year, guess_quarter):
        return guess_year, guess_quarter
    return None, None


def store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
    content = transcript.get("content", "") if isinstance(transcript, dict) else str(transcript)
    if not content or len(content) < 200:
        logger.info(f"[TRANSCRIPT] {ticker} Q{quarter} FY{year} transcript too short/empty, skipping.")
        return False

    # FMP URL without API key — safe for user-facing alerts
    fmp_link = f"https://financialmodelingprep.com/api/v4/earning-call-transcript?symbol={ticker}&year={year}&quarter={quarter}"

    try:
        supabase.table("raw_filings").insert({
            "source": "FMP_TRANSCRIPT",
            "filing_type": "EARNINGS_TRANSCRIPT",
            "ticker": ticker,
            "company_name": company_name,
            "raw_text": content,
            "filing_url": fmp_link,
            "filed_at": transcript.get("date", datetime.now(timezone.utc).isoformat()) if isinstance(transcript, dict) else datetime.now(timezone.utc).isoformat(),
            "status": "PENDING",
            "extra": {
                "year": year, "quarter": quarter,
                "title": f"{company_name} Q{quarter} FY{year} Earnings Call Transcript",
                "source": "FMP Earnings Call Transcript",
                "structured_payload": gq_fmt.earnings_transcript(
                    ticker=ticker,
                    company_name=company_name,
                    quarter=f"Q{quarter}",
                    fiscal_year=str(year),
                    transcript_text=content,
                    fmp_link=fmp_link,
                    filing_date=str(transcript.get("date", "")) if isinstance(transcript, dict) else "",
                ),
            }
        }).execute()
        logger.info(f"[TRANSCRIPT] Stored {ticker} Q{quarter} FY{year} -> PENDING for AI pipeline")
        return True
    except Exception as e:
        if "duplicate" not in str(e).lower():
            logger.error(f"[TRANSCRIPT] Failed to store transcript: {e}")
        return False


def get_watchlist_tickers():
    try:
        result = supabase.table("watchlists").select("ticker").execute()
        return set(row["ticker"] for row in (result.data or []))
    except Exception as e:
        logger.error(f"[TRANSCRIPT] Failed to load watchlist tickers: {e}")
        return set()


def find_recent_10q_10k(watchlist_set):
    """
    Same trigger surface as result_snapshot.py — 10-Q/10-K filings that have
    landed recently. IMPORTANT: edgar_poller.py's `poll_edgar_generic` ingests
    every 10-Q/10-K filed by ANY public company market-wide (it polls SEC
    EDGAR's global "current filings" feed, not a watchlist-scoped one) — so
    unlike the comment that used to sit here claimed, tickers landing in
    raw_filings are NOT already limited to watchlisted companies. On a
    quarter-end filing-deadline day, hundreds of unrelated 10-Qs can crowd
    out a watchlisted company's filing from this top-25-most-recent window,
    and every transcript lookup below costs a call against FMP's priciest
    "Ultimate" tier (the only plan that includes transcripts at all) —
    filtering to the watchlist here avoids spending that specifically
    expensive quota on companies nobody is actually following.
    """
    if not watchlist_set:
        return []
    # BOUNDED WINDOW. This had no time filter at all: it took the 100 most recent
    # 10-Q/10-K rows ever recorded and re-checked the same ~25 watchlisted ones
    # every 30 minutes, forever. transcript_already_fetched() stopped duplicate
    # STORES, but only after get_best_transcript() had already spent a
    # transcripts-dates lookup per ticker against FMP's Ultimate tier — the most
    # expensive quota in the stack — to learn nothing. A transcript that has not
    # appeared within a few weeks of the filing is not going to.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRIGGER_LOOKBACK_DAYS)).isoformat()
    try:
        result = supabase.table("raw_filings") \
            .select("ticker, company_name, filing_type, filed_at") \
            .in_("filing_type", ["10-Q", "10-K"]) \
            .gte("filed_at", cutoff) \
            .order("filed_at", desc=True) \
            .limit(100) \
            .execute()
        rows = result.data or []
        return [r for r in rows if r.get("ticker") in watchlist_set][:25]
    except Exception as e:
        logger.error(f"[TRANSCRIPT] Failed to query recent 10-Q/10-K filings: {e}")
        return []


def run_earnings_transcript_poller():
    logger.info("[TRANSCRIPT] Starting earnings call transcript poller (FMP)...")
    watchlist_set = get_watchlist_tickers()
    filings = find_recent_10q_10k(watchlist_set)
    if not filings:
        logger.info("[TRANSCRIPT] No recent watchlisted 10-Q/10-K filings to check.")
        return

    processed = 0
    seen_tickers = set()
    for filing in filings:
        ticker = filing.get("ticker", "UNKNOWN")
        if ticker == "UNKNOWN" or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

        year, quarter = get_best_transcript(ticker, filing.get("filed_at", ""), filing.get("filing_type", "10-Q"))
        if year is None:
            continue

        transcript = fmp_client.get_earnings_transcript(ticker, year, quarter)
        if not transcript:
            logger.info(f"[TRANSCRIPT] No transcript available yet for {ticker} Q{quarter} FY{year}")
            continue

        company_name = filing.get("company_name", ticker)
        if store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
            processed += 1
        time.sleep(0.5)

    logger.info(f"[TRANSCRIPT] Done. {processed} new transcript(s) queued for AI summarisation.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_earnings_transcript_poller()
