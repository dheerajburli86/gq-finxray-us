"""
fmp_client.py
GQ FinXray US — Financial Modeling Prep (FMP) shared client.

Replaces EODHD across the codebase. All endpoints below are the current
"stable" FMP API (https://financialmodelingprep.com/stable/*), confirmed
against FMP's own developer docs on 2026-07-27:

  quote                      -> /stable/quote
  stock news                 -> /stable/stock-news
  general news                -> /stable/general-news
  company screener            -> /stable/company-screener
  ipo calendar                -> /stable/ipos-calendar
  insider trading             -> /stable/insider-trading/search
  earnings call transcript    -> /stable/earning-call-transcript
  income statement            -> /stable/income-statement
  balance sheet               -> /stable/balance-sheet-statement
  cash flow                   -> /stable/cashflow-statement
  key metrics                 -> /stable/key-metrics
  company profile             -> /stable/profile
  historical EOD prices       -> /stable/historical-price-eod/full
  RSI                         -> /stable/technical-indicators/rsi
  SMA                         -> /stable/technical-indicators/sma
  commodities quote           -> /stable/quote (symbol=GCUSD, CLUSD, NGUSD, ...)
  earnings calendar            -> /stable/earnings-calendar

The analyst-coverage, macro and calendar helpers added at the bottom of this
file (Features 12 and 13) were written from FMP's published stable-API endpoint
names but have NOT been exercised against a live key from this environment.
Each one is documented with the path it calls, returns []/None on any failure,
and every caller degrades to "section unavailable" rather than raising — so a
renamed or paywalled path shows up as a missing section, not an outage. If one
returns 404 in production, check the current FMP docs for the renamed path; the
`_get()` wrapper already prints the status code and body for exactly that.

NOTE ON LICENSING (see claude/us-market-data-licensing-risk.md in the
project): FMP's personal-tier ToS prohibits redistribution to third
parties / multi-user products. This client does not change that — it's
a pure mechanical swap from EODHD. Get the commercial Data Display and
Licensing Agreement sorted with FMP sales before this goes further into
production with a growing subscriber base.
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


def _get(path, params=None, timeout=20, retries=2):
    """Generic GET against the FMP stable API. Returns parsed JSON or None.

    Raises FMPError specifically when the request fails with a PERSISTENT
    429 (rate limit) across the entire retry budget -- this used to just
    fall through to a plain `return None`, indistinguishable from "this
    symbol has no data" or "some other status code came back." Callers that
    need to tell "the API key is rate-limited/exhausted" apart from an
    ordinary empty result (fmp_scraper.py in particular, so it can stop a
    bulk scrape early instead of grinding through hundreds more doomed
    retries) should catch FMPError specifically.
    """
    # Copy rather than mutate the caller's dict -- callers like screener()
    # pass their params straight through, and injecting "apikey" into their
    # original object would leak the API key into it for any later re-use
    # (logging it, reusing it against a different client, etc).
    params = dict(params or {})
    params["apikey"] = FMP_API_KEY
    url = f"{BASE_URL}/{path.lstrip('/')}"

    wait = 3
    last_was_429 = False
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                last_was_429 = True
                time.sleep(wait)
                wait *= 2
                continue
            last_was_429 = False
            print(f"[FMP] {path} returned {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            last_was_429 = False
            print(f"[FMP] Request failed for {path} (attempt {attempt + 1}): {e}")
            time.sleep(wait)
            wait *= 2
    if last_was_429:
        raise FMPError(f"{path}: persistent HTTP 429 across {retries + 1} attempts -- rate limit/quota exhausted")
    return None


# ── Quotes ────────────────────────────────────────────────────────────────────
def get_quote(ticker):
    """Single real-time-ish quote. Returns dict or None.
    Fields include: price, change, changePercentage, dayLow, dayHigh,
    yearLow, yearHigh, volume, avgVolume, previousClose."""
    data = _get("quote", {"symbol": ticker})
    if data and isinstance(data, list) and data:
        return data[0]
    return None


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
def get_stock_news(ticker, limit=10):
    data = _get("stock-news", {"symbols": ticker, "limit": limit})
    return data if isinstance(data, list) else []


def get_general_news(limit=25, page=0):
    data = _get("general-news", {"limit": limit, "page": page})
    return data if isinstance(data, list) else []


# ── Screener (market-wide, replaces EODHD screener) ──────────────────────────
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


# ── Earnings call transcripts (Feature 11 — new) ─────────────────────────────
def get_earnings_transcript(ticker, year, quarter):
    """Returns full transcript text (list of dicts w/ 'content') or None."""
    data = _get("earning-call-transcript", {"symbol": ticker, "year": year, "quarter": quarter})
    if data and isinstance(data, list) and data:
        return data[0]
    return None


def get_latest_transcripts(limit=50):
    data = _get("latest-transcripts", {"limit": limit})
    return data if isinstance(data, list) else []


def get_transcript_dates(ticker):
    """List of (year, quarter, date) tuples available for a ticker."""
    data = _get("transcripts-dates-by-symbol", {"symbol": ticker})
    return data if isinstance(data, list) else []


# ── Fundamentals ──────────────────────────────────────────────────────────────
def get_profile(ticker):
    data = _get("profile", {"symbol": ticker})
    if data and isinstance(data, list) and data:
        return data[0]
    return None


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
    """Daily OHLCV, matches EODHD's /eod/ shape closely enough for calc_returns()."""
    data = _get("historical-price-eod/full", {"symbol": ticker, "from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


def get_etf_info(ticker):
    """ETF expense ratio / net assets / AUM. Endpoint path not independently
    verified against FMP's current docs (unlike the others in this file) —
    every caller treats a None/empty result as "not available" and degrades
    gracefully rather than crashing, same as the rest of this client."""
    data = _get("etf/info", {"symbol": ticker})
    if data and isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


# ── Technical indicators (used by technical_poller.py as a screener fallback) ─
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


# ══════════════════════════════════════════════════════════════════════════════
# Analyst coverage — Feature 12 (analyst_ratings_poller.py)
#
# Paths below follow FMP's published stable-API naming but have not been
# exercised against a live key from here. All of them return []/None on any
# non-200, so a renamed path degrades the analyst poller to "no change
# detected" instead of raising. If one 404s, `_get()` prints the status and
# body — check the current FMP docs for the replacement path.
# ══════════════════════════════════════════════════════════════════════════════
def get_analyst_estimates(ticker, period="annual", limit=1):
    """
    Forward revenue/EPS estimates. Endpoint: /stable/analyst-estimates

    Returns a list of estimate dicts (most recent first) or [] if unavailable.
    Confirm the path against FMP's current docs if it 404s.
    """
    data = _get("analyst-estimates", {"symbol": ticker, "period": period, "limit": limit})
    return data if isinstance(data, list) else []


def get_price_target_consensus(ticker):
    """
    Aggregate street price target. Endpoint: /stable/price-target-consensus

    Returns a dict with targetHigh / targetLow / targetConsensus / targetMedian,
    or None when the symbol has no coverage (which is the common case for small
    caps, not an error). Confirm the path against FMP's current docs if it 404s.
    """
    data = _get("price-target-consensus", {"symbol": ticker})
    if data and isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


def get_price_target_news(ticker, limit=10):
    """
    Individual analyst price-target revisions with publisher and date.
    Endpoint: /stable/price-target-news

    Returns a list (newest first) or []. Confirm the path against FMP's
    current docs if it 404s.
    """
    data = _get("price-target-news", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []


def get_grades_consensus(ticker):
    """
    Distribution of analyst ratings. Endpoint: /stable/grades-consensus

    Returns a dict with strongBuy / buy / hold / sell / strongSell counts plus a
    `consensus` label ("Buy", "Hold", ...), or None when nobody covers the name.
    Confirm the path against FMP's current docs if it 404s.
    """
    data = _get("grades-consensus", {"symbol": ticker})
    if data and isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


def get_grades_historical(ticker, limit=10):
    """
    Historical snapshots of the ratings distribution, newest first.
    Endpoint: /stable/grades-historical

    Useful as a cross-check on a detected consensus change. Returns [] on
    failure. Confirm the path against FMP's current docs if it 404s.
    """
    data = _get("grades-historical", {"symbol": ticker, "limit": limit})
    return data if isinstance(data, list) else []


# ══════════════════════════════════════════════════════════════════════════════
# Macro & indices — Feature 13 (macro_policy_roundup.py, market_reports.py)
# ══════════════════════════════════════════════════════════════════════════════
def get_index_quote(symbol):
    """
    Quote for an index symbol: ^GSPC (S&P 500), ^IXIC (Nasdaq Composite),
    ^DJI (Dow), ^TNX (10-year Treasury yield), ^VIX (volatility).

    Thin wrapper on get_quote — FMP serves indices through the same /stable/quote
    endpoint. Kept separate so callers read clearly and so the caret-prefixed
    symbols are documented in one place. Index coverage is plan-dependent; every
    caller falls back to the matching ETF (SPY/QQQ/DIA) when this returns None.
    """
    return get_quote(symbol)


def get_treasury_rates(from_date, to_date):
    """
    US Treasury par yield curve by date. Endpoint: /stable/treasury-rates

    Returns a list of dicts keyed by maturity (month1, year2, year10, year30...)
    for each date in range, or []. Confirm the path against FMP's current docs
    if it 404s.
    """
    data = _get("treasury-rates", {"from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


# Indicator names FMP accepts on the economic-indicators endpoint. Passing an
# unrecognised name generally yields an empty body rather than an error, so this
# list exists to keep callers from silently asking for nothing.
ECONOMIC_INDICATORS = (
    "GDP", "realGDP", "CPI", "inflationRate", "unemploymentRate",
    "federalFunds", "retailSales", "consumerSentiment",
)


def get_economic_indicator(name, from_date=None, to_date=None):
    """
    Time series for one macro indicator. Endpoint: /stable/economic-indicators

    `name` should be one of ECONOMIC_INDICATORS. Returns a list of
    {date, value} dicts (newest first) or []. Confirm the path against FMP's
    current docs if it 404s.
    """
    params = {"name": name}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    data = _get("economic-indicators", params)
    return data if isinstance(data, list) else []


def get_economic_calendar(from_date, to_date):
    """
    Scheduled macro releases (CPI, payrolls, FOMC, PMI...) in a date range.
    Endpoint: /stable/economics-calendar

    Returns a list of dicts with date/country/event/impact/actual/estimate/
    previous, or []. Note the endpoint is "economics-calendar" (plural noun)
    while the indicator endpoint is "economic-indicators" — that asymmetry is
    FMP's, not a typo. Confirm against FMP's current docs if it 404s.
    """
    data = _get("economics-calendar", {"from": from_date, "to": to_date})
    return data if isinstance(data, list) else []


if __name__ == "__main__":
    print("Quote AAPL:", get_quote("AAPL"))
