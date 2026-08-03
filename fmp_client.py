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
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # env var may already be set by the platform (Railway) or the shell

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


# ── Provider health accounting ────────────────────────────────────────────────
# FMP's backend degrades intermittently: endpoints that return clean data in
# 1.3s will start throwing 504 Gateway Time-out an hour later, then recover.
# Without this counter every such episode looks like a broken endpoint, and
# we waste time re-verifying paths that were never wrong. A run with a pile of
# 5xx/timeouts and zero 404s means FMP is unwell, not that the code is.
_health = {"ok": 0, "server_error": 0, "timeout": 0, "not_found": 0, "rate_limited": 0, "auth": 0}


def get_health():
    """Snapshot of how FMP has been behaving since process start (or last reset)."""
    return dict(_health)


def reset_health():
    for k in _health:
        _health[k] = 0


def is_degraded():
    """True when FMP is throwing server errors/timeouts but no path errors --
    i.e. the endpoints are right and the provider is just having a bad time."""
    bad = _health["server_error"] + _health["timeout"]
    return bad > 0 and _health["not_found"] == 0


# 5xx are transient by definition -- the request was valid, the server failed
# to serve it. Worth retrying, unlike a 404 which will never fix itself.
_RETRYABLE_STATUS = (500, 502, 503, 504, 520, 522, 524)

DEFAULT_TIMEOUT = 30


def _get(path, params=None, timeout=DEFAULT_TIMEOUT, retries=2, raise_on_path_error=False):
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
            _health["timeout"] += 1
            print(f"[FMP] Timeout/network on {path} (attempt {attempt + 1}/{retries + 1}): "
                  f"{type(e).__name__}")
            if attempt < retries:
                time.sleep(wait)
                wait *= 2
            continue

        if r.status_code == 200:
            _health["ok"] += 1
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
            _health["rate_limited"] += 1
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code in _RETRYABLE_STATUS:
            # FMP's own backend failed. The request was fine -- retry it.
            # Previously this fell through to the generic branch and returned
            # None on the FIRST 504, with no retry at all, so a brief upstream
            # blip permanently killed that poll cycle's data.
            _health["server_error"] += 1
            print(f"[FMP] Server error {r.status_code} on {path} "
                  f"(attempt {attempt + 1}/{retries + 1}) -- provider-side, retrying")
            if attempt < retries:
                time.sleep(wait)
                wait *= 2
            continue

        if r.status_code == 404:
            _health["not_found"] += 1
            print(f"[FMP] *** PATH BUG *** {path} returned 404. "
                  f"params={_redact(params)} | This endpoint path is wrong -- "
                  f"check https://site.financialmodelingprep.com/developer/docs")
            if raise_on_path_error:
                raise FMPPathError(f"{path} -> 404 (wrong endpoint path)")
            return None

        if r.status_code in (401, 402, 403):
            _health["auth"] += 1
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
def screener(params, timeout=60):
    """
    params example: {"marketCapMoreThan": 1e9, "volumeMoreThan": 50000,
                      "priceMoreThan": 1, "exchange": "NASDAQ", "limit": 500}

    Given a generous 60s timeout and only one retry: this endpoint scans the
    whole universe server-side and routinely takes longer than the 20s default
    used everywhere else. With the old settings it burned 84 seconds across
    three doomed attempts and still returned nothing. Only fmp_scraper.py (a
    standalone CLI tool) calls this -- it is NOT on the live alert path, so a
    slow call here cannot stall the scheduler.
    """
    data = _get("company-screener", params, timeout=timeout, retries=1)
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
def get_earnings_transcript(ticker, year, quarter, timeout=90):
    """Full transcript. Returns dict with 'content' or None.

    Long timeout on purpose: a full earnings call is 50-100KB+ of text and
    FMP regularly takes 20-60s to serve one. The 20s default was timing this
    out intermittently -- it worked in one preflight run and failed in the
    next, which reads like a broken endpoint but is really just an impatient
    client. Retries kept low since each attempt is expensive."""
    return _first(_get("earning-call-transcript",
                       {"symbol": ticker, "year": year, "quarter": quarter},
                       timeout=timeout, retries=1))


def get_latest_transcripts(limit=50, page=0, timeout=90):
    """FIXED: was /stable/latest-transcripts (404).
    Correct path is /stable/earning-call-transcript-latest.
    Same long-timeout reasoning as get_earnings_transcript()."""
    data = _get("earning-call-transcript-latest", {"limit": limit, "page": page},
                timeout=timeout, retries=1)
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
    """FIXED 2026-08-03: was /stable/cashflow-statement (404).
    Correct path is /stable/cash-flow-statement -- hyphenated. Note FMP's own
    docs page for this lives at the slug 'cashflow-statement' while the actual
    API path is 'cash-flow-statement', which is how this one slipped through."""
    data = _get("cash-flow-statement", {"symbol": ticker, "period": period, "limit": limit})
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


# ── 13F institutional ownership ───────────────────────────────────────────────
# Included in the Ultimate tier you're already paying for, and previously
# unused. This is the honest US answer to "who is accumulating this?" -- the
# thing India's bulk/block deal feed answered in real time and the US only
# answers quarterly, ~45 days after quarter end.
def get_institutional_ownership_latest(page=0, limit=100):
    """Most recent 13F filings across all managers."""
    data = _get("institutional-ownership/latest", {"page": page, "limit": limit})
    return data if isinstance(data, list) else []


def get_holder_performance(cik):
    """Performance summary for one institutional manager, by CIK.
    e.g. Berkshire Hathaway = 0001067983"""
    data = _get("institutional-ownership/holder-performance-summary", {"cik": cik})
    return data if isinstance(data, list) else []


def get_industry_summary(year, quarter):
    """Institutional positioning by industry for a given quarter."""
    data = _get("institutional-ownership/industry-summary", {"year": year, "quarter": quarter})
    return data if isinstance(data, list) else []


# ── Analyst grades & price targets ────────────────────────────────────────────
# Rating changes and price-target revisions are reliably market-moving and
# cheap to poll. Nothing in the system currently uses them.
def get_grades(ticker, limit=20):
    """Latest analyst grades / rating actions for a ticker.

    FMP returns the FULL history here -- AAPL comes back with ~1,800 rows
    going back years. Only the most recent handful are alert-worthy, so this
    trims client-side (the endpoint has no limit param) to keep callers from
    accidentally holding a megabyte of stale ratings in memory."""
    data = _get("grades", {"symbol": ticker})
    if not isinstance(data, list):
        return []
    data.sort(key=lambda r: r.get("date", ""), reverse=True)
    return data[:limit]


def get_price_target_consensus(ticker):
    """Consensus price target -- high, low, median, consensus."""
    return _first(_get("price-target-consensus", {"symbol": ticker}))


def get_ratings_snapshot(ticker):
    """Overall ratings snapshot (score + component ratings)."""
    return _first(_get("ratings-snapshot", {"symbol": ticker}))


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
