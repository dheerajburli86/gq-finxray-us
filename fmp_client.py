"""
fmp_client.py
GQ FinXray US — Financial Modeling Prep (FMP) shared client.

ENDPOINT PATHS RE-VERIFIED against FMP's live developer docs on 2026-08-03.
Four paths in the previous version were wrong and returned HTTP 404 silently:

  BROKEN                          ->  CORRECT
  /stable/stock-news              ->  /stable/news/stock?symbols=
  /stable/general-news            ->  /stable/news/general-latest
  /stable/latest-transcripts      ->  /stable/earning-call-transcript-latest
  /stable/transcripts-dates-by-symbol -> /stable/earning-call-transcript-dates

The first two killed Feature 2 (Company & Sector News) outright — every FMP
news call 404'd, returned None, and surfaced as "no articles" rather than as
an error. Confirmed against the production database: FMP_NEWS produced
EARNINGS_CALENDAR alerts but zero NEWS alerts, ever.

Verified-correct and left unchanged:
  quote                      -> /stable/quote?symbol=
  company screener           -> /stable/company-screener
  earnings calendar          -> /stable/earnings-calendar
  ipo calendar               -> /stable/ipos-calendar
  insider trading            -> /stable/insider-trading/search
  earnings call transcript   -> /stable/earning-call-transcript?symbol=&year=&quarter=
  company profile            -> /stable/profile
  income statement           -> /stable/income-statement
  balance sheet              -> /stable/balance-sheet-statement
  cash flow                  -> /stable/cashflow-statement
  key metrics                -> /stable/key-metrics
  historical EOD prices      -> /stable/historical-price-eod/full
  ETF info (expense/AUM)     -> /stable/etf/info?symbol=   (previously doubted; it's correct)
  RSI                        -> /stable/technical-indicators/rsi
  SMA                        -> /stable/technical-indicators/sma

Run fmp_preflight.py to re-confirm all of the above against your live key
before any deploy. Never guess a path again.

NOTE ON LICENSING (see claude/us-market-data-licensing-risk.md in the
project): FMP's personal-tier ToS prohibits redistribution to third
parties / multi-user products. Get the commercial Data Display and
Licensing Agreement sorted with FMP sales before this grows a subscriber
base.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FMP_API_KEY = os.getenv("FMP_API_KEY")
BASE_URL = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    """Raised when FMP returns a hard failure worth distinguishing from 'no data'."""
    pass


class FMPPathError(FMPError):
    """Raised on a 404. Almost always a wrong endpoint path, not a missing symbol.

    Kept distinct because a 404 is a CODE bug that will never fix itself,
    whereas an empty 200 is legitimately 'no data for this symbol right now'.
    The old client collapsed both into `return None`, which is exactly how
    four dead endpoints survived in production unnoticed.
    """
    pass


class FMPPlanError(FMPError):
    """Raised on 401/402/403 — key invalid, or endpoint not on your plan."""
    pass


def _redact(params):
    """Params minus the API key, safe to print in logs."""
    return {k: v for k, v in (params or {}).items() if k != "apikey"}


def _get(path, params=None, timeout=20, retries=2, raise_on_path_error=False):
    """Generic GET against the FMP stable API. Returns parsed JSON or None.

    Error handling is deliberately loud now. Previously every non-200 except
    429 printed one line and returned None, so a permanently-wrong path was
    indistinguishable from a quiet market. Now:
      404      -> logged as a PATH BUG (optionally raised)
      401/402/403 -> logged as a KEY/PLAN problem
      429      -> retried with backoff, raises FMPError if it never clears
      200 with {"Error Message": ...} -> treated as failure, not as data
    """
    params = dict(params or {})
    params["apikey"] = FMP_API_KEY
    url = f"{BASE_URL}/{path.lstrip('/')}"

    wait = 3
    last_was_429 = False

    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            last_was_429 = False
            print(f"[FMP] Request failed for {path} (attempt {attempt + 1}): {e}")
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:
                print(f"[FMP] {path}: 200 but body was not JSON -- {e} | {r.text[:200]}")
                return None
            # FMP returns HTTP 200 with an error envelope for some failures.
            # Without this check those come back as a truthy dict and get
            # mistaken for real data downstream.
            if isinstance(data, dict) and ("Error Message" in data or "error" in data):
                msg = data.get("Error Message") or data.get("error")
                print(f"[FMP] {path}: API error in 200 body -- {msg}")
                return None
            return data

        if r.status_code == 429:
            last_was_429 = True
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code == 404:
            print(f"[FMP] *** PATH BUG *** {path} returned 404. "
                  f"params={_redact(params)} | This endpoint path is wrong -- "
                  f"check https://site.financialmodelingprep.com/developer/docs")
            if raise_on_path_error:
                raise FMPPathError(f"{path} -> 404 (wrong endpoint path)")
            return None

        if r.status_code in (401, 402, 403):
            print(f"[FMP] *** KEY/PLAN *** {path} returned {r.status_code}: {r.text[:200]} | "
                  f"Either FMP_API_KEY is invalid or this endpoint is not on your plan.")
            return None

        last_was_429 = False
        print(f"[FMP] {path} returned {r.status_code}: {r.text[:200]}")
        return None

    if last_was_429:
        raise FMPError(f"{path}: persistent HTTP 429 across {retries + 1} attempts -- rate limit/quota exhausted")
    return None


def _first(data):
    """FMP returns single objects wrapped in a list. Unwrap safely."""
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


# ── Quotes ────────────────────────────────────────────────────────────────────
def get_quote(ticker):
    """Single real-time-ish quote. Returns dict or None.
    Fields include: price, change, changePercentage, dayLow, dayHigh,
    yearLow, yearHigh, volume, avgVolume, previousClose."""
    return _first(_get("quote", {"symbol": ticker}))


def get_quotes(tickers):
    """Fetch quotes for a list of tickers. FMP's stable /quote endpoint is
    single-symbol; this loops with a small delay to stay rate-limit friendly."""
    out = {}
    for t in tickers:
        q = get_quote(t)
        if q:
            out[t] = q
        time.sleep(0.05)
    return out


def get_commodity_quote(commodity_symbol):
    """e.g. GCUSD (gold), CLUSD (crude oil), NGUSD (natural gas)."""
    return get_quote(commodity_symbol)


# ── News ──────────────────────────────────────────────────────────────────────
# FIXED 2026-08-03. Both of these were 404ing on every single call.
def get_stock_news(ticker, limit=10, page=0):
    """Company-specific news. FMP path is /stable/news/stock (NOT /stable/stock-news).

    `symbols` accepts a comma-separated list, so this also works for a batch:
    get_stock_news("AAPL,MSFT,NVDA").
    """
    data = _get("news/stock", {"symbols": ticker, "limit": limit, "page": page})
    return data if isinstance(data, list) else []


def get_general_news(limit=25, page=0):
    """Broad market/business news. FMP path is /stable/news/general-latest
    (NOT /stable/general-news)."""
    data = _get("news/general-latest", {"limit": limit, "page": page})
    return data if isinstance(data, list) else []


def get_latest_stock_news(limit=50, page=0):
    """Market-wide latest stock-tagged news. Useful as a wider net than the
    per-ticker sweep -- these articles arrive already carrying a symbol."""
    data = _get("news/stock-latest", {"limit": limit, "page": page})
    return data if isinstance(data, list) else []


def get_press_releases(ticker, limit=10, page=0):
    """Company press releases -- distinct from press coverage."""
    data = _get("news/press-releases", {"symbols": ticker, "limit": limit, "page": page})
    return data if isinstance(data, list) else []


# ── Screener (market-wide) ────────────────────────────────────────────────────
def screener(params):
    """
    params example: {"marketCapMoreThan": 1e9, "volumeMoreThan": 50000,
                      "priceMoreThan": 1, "exchange": "NASDAQ", "limit": 500}
    """
    data = _get("company-screener", params)
    return data if isinstance(data, list) else []


# ── Calendars ─────────────────────────────────────────────────────────────────
def get_earnings_calendar(from_date, to_date):
    data = _get("earnings-calendar", {"from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


def get_ipo_calendar(from_date, to_date):
    data = _get("ipos-calendar", {"from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


# ── Insider trading ───────────────────────────────────────────────────────────
def get_insider_trading(ticker, page=0, limit=50):
    data = _get("insider-trading/search", {"symbol": ticker, "page": page, "limit": limit})
    return data if isinstance(data, list) else []


# ── Earnings call transcripts (Feature 11) ───────────────────────────────────
def get_earnings_transcript(ticker, year, quarter):
    """Full transcript. Returns dict with 'content' or None."""
    return _first(_get("earning-call-transcript",
                       {"symbol": ticker, "year": year, "quarter": quarter}))


def get_latest_transcripts(limit=50, page=0):
    """FIXED: was /stable/latest-transcripts (404).
    Correct path is /stable/earning-call-transcript-latest."""
    data = _get("earning-call-transcript-latest", {"limit": limit, "page": page})
    return data if isinstance(data, list) else []


def get_transcript_dates(ticker):
    """FIXED: was /stable/transcripts-dates-by-symbol (404).
    Correct path is /stable/earning-call-transcript-dates.
    Returns list of {quarter, fiscalYear, date} for the symbol."""
    data = _get("earning-call-transcript-dates", {"symbol": ticker})
    return data if isinstance(data, list) else []


# ── Fundamentals ──────────────────────────────────────────────────────────────
def get_profile(ticker):
    return _first(_get("profile", {"symbol": ticker}))


def get_income_statement(ticker, period="quarter", limit=8):
    data = _get("income-statement", {"symbol": ticker, "period": period, "limit": limit})
    return data if isinstance(data, list) else []


def get_balance_sheet(ticker, period="quarter", limit=8):
    data = _get("balance-sheet-statement", {"symbol": ticker, "period": period, "limit": limit})
    return data if isinstance(data, list) else []


def get_cash_flow(ticker, period="quarter", limit=8):
    data = _get("cashflow-statement", {"symbol": ticker, "period": period, "limit": limit})
    return data if isinstance(data, list) else []


def get_key_metrics(ticker, period="quarter", limit=8):
    data = _get("key-metrics", {"symbol": ticker, "period": period, "limit": limit})
    return data if isinstance(data, list) else []


def get_historical_prices(ticker, from_date, to_date):
    """Daily OHLCV."""
    data = _get("historical-price-eod/full", {"symbol": ticker, "from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


def get_etf_info(ticker):
    """ETF expense ratio / AUM / holdings count.
    Path /stable/etf/info CONFIRMED correct against FMP docs 2026-08-03 --
    the previous docstring's uncertainty was unfounded."""
    return _first(_get("etf/info", {"symbol": ticker}))


# ── Technical indicators ──────────────────────────────────────────────────────
def get_rsi(ticker, period_length=14, timeframe="1day"):
    data = _get("technical-indicators/rsi", {
        "symbol": ticker, "periodLength": period_length, "timeframe": timeframe
    })
    return data if isinstance(data, list) else []


def get_sma(ticker, period_length=200, timeframe="1day"):
    data = _get("technical-indicators/sma", {
        "symbol": ticker, "periodLength": period_length, "timeframe": timeframe
    })
    return data if isinstance(data, list) else []


if __name__ == "__main__":
    print("Run fmp_preflight.py for a full endpoint check.")
    print("Quote AAPL:", get_quote("AAPL"))
