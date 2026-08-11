"""
earnings_transcript_poller.py
GQ FinXray US — Feature 11. Earnings call transcripts.

This is the feature EODHD could never cover at any tier, and one of the reasons
FMP Ultimate is in the stack: /stable/earning-call-transcript covers 8,000+ US
companies with a decade of quarterly history.

HOW IT IS WIRED
---------------
The trigger already exists. When edgar_poller.py stores a 10-Q or 10-K, that is
the signal a transcript for the matching fiscal quarter should now be available.
This poller picks those filings up, works out which quarter they report on, pulls
the transcript, and writes it into `raw_filings` with
filing_type="EARNINGS_TRANSCRIPT" so it runs through the same ai_pipeline.py
chain as everything else — gibberish check, relevance check, S.1T summarisation
on the escalating word ladder, V.1 validation, headline, impact, dedup.

It deliberately does NOT write an `alerts` row. The previous version wrote one
itself *and* queued the transcript for the pipeline, so anything that did work
produced two alerts for the same call.

WHY THIS FEATURE HAD NEVER FIRED
--------------------------------
It filtered candidate filings against a direct read of the `watchlists` table
that returned a small, stale set, then intersected that with the 25 most recent
10-Q/10-K rows in `raw_filings`. Since edgar_poller used to ingest every filing
market-wide, on any filing-deadline day those 25 rows were entirely unwatchlisted
companies and the intersection was empty — every single run. The lifetime alert
count for Feature 11 was zero.

Two things fix it. edgar_poller now only stores watchlisted filings at all, and
the filter here goes through watchlist_util.get_watched_tickers(), the same
cached source of truth every other poller uses.
"""

import os
import re
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "earnings_transcript_poller"
SOURCE = "FMP_TRANSCRIPT"
FILING_TYPE = "EARNINGS_TRANSCRIPT"

# Roughly maps a filing date back to the fiscal quarter it reports ON, not the
# quarter it was FILED in. A 10-Q covering Q1 (period end 31 March) is typically
# filed 30-45 days later, in early-to-mid May — reading the quarter straight off
# the filed month would call that Q2. Backing the date up by ~40 days lands in
# the right fiscal quarter far more often. It is still a heuristic: fiscal
# calendars vary and this does not know each company's own reporting lag, which
# is why get_best_transcript() cross-checks against FMP's own list of available
# transcript dates rather than trusting it blindly.
FILING_LAG_DAYS = 40

# How far back to look for trigger filings. A transcript is usually posted within
# a day or two of the call, which is itself usually within a few days of the
# filing — beyond a fortnight the event is no longer news.
TRIGGER_LOOKBACK_DAYS = 14

MAX_TICKERS_PER_RUN = 25
MIN_TRANSCRIPT_CHARS = 200
TICKER_GAP_SECONDS = 0.5


def _guess_period(filed_dt):
    period_end_estimate = filed_dt - timedelta(days=FILING_LAG_DAYS)
    quarter = max(1, min(4, ((period_end_estimate.month - 1) // 3) + 1))
    return period_end_estimate.year, quarter


def transcript_already_fetched(ticker, year, quarter):
    """
    Fails CLOSED. On a database error this reports True so the run skips the
    ticker: re-fetching burns FMP's priciest tier and re-queues a duplicate for
    the whole LLM chain, whereas skipping just defers it to the next 30-minute
    cycle.
    """
    try:
        rows = (supabase.table("raw_filings")
                .select("id")
                .eq("ticker", ticker)
                .eq("filing_type", FILING_TYPE)
                .eq("extra->>year", str(year))
                .eq("extra->>quarter", str(quarter))
                .limit(1).execute()).data
        return bool(rows)
    except Exception as e:
        logger.error("[TRANSCRIPT] Dedup check failed for %s Q%s FY%s: %s",
                     ticker, quarter, year, e)
        return True


def get_best_transcript(ticker, filed_at_iso, form_type="10-Q"):
    """
    Which (year, quarter) to request for this filing.

    Prefers the quarter the filing appears to report on when FMP lists a
    transcript for it, and otherwise falls back to the most recent available
    transcript that has not been fetched yet. Returns (None, None) when there is
    nothing new to pull.
    """
    try:
        filed_dt = datetime.fromisoformat((filed_at_iso or "").replace("Z", "+00:00"))
        if filed_dt.tzinfo is None:
            filed_dt = filed_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        filed_dt = datetime.now(timezone.utc)

    if form_type == "10-K":
        # A 10-K reports on the full prior fiscal year, so it maps to Q4, and
        # calendar-year filers commonly file it in Jan-Apr of the FOLLOWING year
        # — the year in the filed date is usually one ahead of the fiscal year
        # being reported on. The ~40-day lag heuristic under-corrects for that,
        # so 10-Ks get their own simpler rule.
        guess_year = filed_dt.year - 1 if filed_dt.month <= 4 else filed_dt.year
        guess_quarter = 4
    else:
        guess_year, guess_quarter = _guess_period(filed_dt)

    try:
        available = fmp_client.get_transcript_dates(ticker)
    except Exception as e:
        logger.warning("[TRANSCRIPT] transcripts-dates lookup failed for %s: %s", ticker, e)
        available = []

    parsed = []
    for entry in available or []:
        year = entry.get("fiscalYear") or entry.get("year")
        quarter = entry.get("period") or entry.get("quarter")
        try:
            year = int(year)
            quarter = int(str(quarter).upper().replace("Q", ""))
        except (TypeError, ValueError):
            continue
        if 1 <= quarter <= 4:
            parsed.append((year, quarter))

    if parsed:
        # Exact match on the inferred fiscal quarter first.
        for year, quarter in parsed:
            if year == guess_year and quarter == guess_quarter:
                if not transcript_already_fetched(ticker, year, quarter):
                    return year, quarter
                return None, None
        # Otherwise the newest transcript we do not already hold.
        for year, quarter in sorted(parsed, reverse=True):
            if not transcript_already_fetched(ticker, year, quarter):
                return year, quarter
        return None, None

    # No dates list — try the inferred quarter directly.
    if not transcript_already_fetched(ticker, guess_year, guess_quarter):
        return guess_year, guess_quarter
    return None, None


def _transcript_content(transcript):
    if isinstance(transcript, dict):
        return (transcript.get("content") or transcript.get("transcript") or "").strip()
    if isinstance(transcript, str):
        return transcript.strip()
    return ""


def _transcript_date(transcript):
    if isinstance(transcript, dict):
        raw = transcript.get("date") or transcript.get("publishedDate")
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace(" ", "T"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return datetime.now(timezone.utc).isoformat()


def store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
    """Queue the transcript for ai_pipeline. Returns True when a row was created."""
    content = _transcript_content(transcript)
    if len(content) < MIN_TRANSCRIPT_CHARS:
        logger.info("[TRANSCRIPT] %s Q%s FY%s is empty or truncated; skipping.",
                    ticker, quarter, year)
        return False

    title = f"{company_name} Q{quarter} FY{year} Earnings Call Transcript"
    # Real FMP link to earnings call transcripts (clickable for users)
    # Also stable enough for UNIQUE(filing_url, ticker) index to prevent duplicates
    filing_url = f"https://site.financialmodelingprep.com/earnings-call-transcript/{ticker.upper()}"

    try:
        supabase.table("raw_filings").insert({
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "ticker": ticker,
            "company_name": company_name,
            "raw_text": content,
            "filing_url": filing_url,
            "filed_at": _transcript_date(transcript),
            "status": "PENDING",
            "content_hash": hashlib.sha256(
                re.sub(r"\s+", " ", content.lower()).strip().encode("utf-8", "ignore")
            ).hexdigest(),
            "extra": tag_extra({
                "year": year,
                "quarter": quarter,
                "title": title,
                "company_name": company_name,
                "source_name": "FMP Earnings Call Transcript",
            }, SOURCE, FILING_TYPE),
        }).execute()
        logger.info("[TRANSCRIPT] Queued %s Q%s FY%s (%d chars) for summarisation",
                    ticker, quarter, year, len(content))
        return True
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            logger.debug("[TRANSCRIPT] %s Q%s FY%s already queued.", ticker, quarter, year)
            return False
        logger.error("[TRANSCRIPT] Insert failed for %s Q%s FY%s: %s", ticker, quarter, year, e)
        log_poller_error(POLLER_NAME, "store_transcript", e,
                         {"ticker": ticker, "year": year, "quarter": quarter})
        return False


def find_recent_10q_10k(watched):
    """
    Trigger filings: recent 10-Q/10-K rows in `raw_filings` for watched tickers,
    newest first, one row per ticker.

    The watchlist filter is applied here, not in the query, because PostgREST
    caps an `.in_()` list and the watchlist can grow; pulling a bounded recent
    window and filtering locally is stable at any watchlist size.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRIGGER_LOOKBACK_DAYS)).isoformat()
    try:
        rows = (supabase.table("raw_filings")
                .select("ticker, company_name, filing_type, filed_at")
                .in_("filing_type", ["10-Q", "10-K"])
                .gte("filed_at", cutoff)
                .order("filed_at", desc=True)
                .limit(200).execute()).data or []
    except Exception as e:
        logger.error("[TRANSCRIPT] Trigger query failed: %s", e)
        log_poller_error(POLLER_NAME, "find_recent_10q_10k", e)
        return []

    out, seen = [], set()
    for r in rows:
        ticker = (r.get("ticker") or "").upper()
        if not ticker or ticker in seen or ticker not in watched:
            continue
        seen.add(ticker)
        out.append({**r, "ticker": ticker})
        if len(out) >= MAX_TICKERS_PER_RUN:
            break
    return out


def run_earnings_transcript_poller():
    """One pass. Never raises — a scheduler calls this every 30 minutes."""
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[TRANSCRIPT] No watchlisted tickers; nothing to poll.")
            return

        filings = find_recent_10q_10k(watched)
        if not filings:
            logger.info("[TRANSCRIPT] No recent watchlisted 10-Q/10-K filings to check.")
            return

        logger.info("[TRANSCRIPT] Checking transcripts for %d ticker(s)", len(filings))

        queued = 0
        for filing in filings:
            ticker = filing["ticker"]
            try:
                year, quarter = get_best_transcript(
                    ticker, filing.get("filed_at", ""), filing.get("filing_type", "10-Q"))
                if year is None:
                    continue

                transcript = fmp_client.get_earnings_transcript(ticker, year, quarter)
                if not transcript:
                    logger.info("[TRANSCRIPT] Not published yet: %s Q%s FY%s",
                                ticker, quarter, year)
                    continue

                company_name = filing.get("company_name") or ticker
                if store_transcript_for_pipeline(ticker, company_name, year, quarter, transcript):
                    queued += 1
            except Exception as e:
                logger.error("[TRANSCRIPT] %s failed: %s", ticker, e)
                log_poller_error(POLLER_NAME, "process_ticker", e, {"ticker": ticker})
            time.sleep(TICKER_GAP_SECONDS)

        logger.info("[TRANSCRIPT] Done — %d transcript(s) queued for AI summarisation.", queued)

    except Exception as e:
        logger.exception("[TRANSCRIPT] Run failed: %s", e)
        log_poller_error(POLLER_NAME, "run_earnings_transcript_poller", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_earnings_transcript_poller()
