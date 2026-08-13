"""
massive_client.py
GQ FinXray US — Massive (formerly Polygon.io) shared client. v2.

WHAT CHANGED IN v2 (2026-08-03)
-------------------------------
The v1 client used ~15% of what the $199/mo Advanced tier actually provides.
Advanced includes real-time data, websockets, tick-level trades and quotes,
UNLIMITED API calls, 20+ years of history, and full-market snapshots. v1 only
touched snapshots, aggregates, news and two indicators.

Added here:
  get_trades()          -> tick-level tape. This is the legal US answer to
                           India's bulk/block deal feed: the US has no
                           counterparty disclosure regime, but every large
                           print is on the tape with size, price, exchange
                           and condition codes.
  get_gainers()/losers()-> server-computed top movers, one call, replaces
                           looping quotes over a hardcoded ticker list.
  get_market_status()   -> real open/closed/extended-hours from the exchange,
                           replacing hardcoded clock arithmetic that can't
                           know about market holidays.
  get_ticker_details()  -> company reference data.
  get_dividends()/splits()-> corporate actions.

Also fixed: _get() mutated the caller's params dict by injecting the API key
into it (same bug that was in fmp_client), and non-200s were logged without
distinguishing a wrong path from an expired key.

  base URL           -> https://api.massive.com
  auth                -> ?apiKey=... query param
  aggregates/bars      -> /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}
  previous day bar     -> /v2/aggs/ticker/{ticker}/prev
  single snapshot      -> /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
  full market snapshot -> /v2/snapshot/locale/us/markets/stocks/tickers
  gainers / losers     -> /v2/snapshot/locale/us/markets/stocks/{direction}
  grouped daily        -> /v2/aggs/grouped/locale/us/market/stocks/{date}
  tick trades          -> /v3/trades/{ticker}
  news                 -> /v2/reference/news
  ticker details       -> /v3/reference/tickers/{ticker}
  market status        -> /v1/marketstatus/now
  dividends            -> /stocks/v1/dividends
  splits               -> /stocks/v1/splits
  RSI                  -> /v1/indicators/rsi/{ticker}
  SMA                  -> /v1/indicators/sma/{ticker}

LICENSING: the Advanced tier is "individual use only, non-pros only." Fine for
private use; a commercial Business tier is required before this serves paying
subscribers. See claude/us-market-data-licensing-risk.md.
"""

import os
import time
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")
BASE_URL = "https://api.massive.com"


class MassiveError(Exception):
    pass


class MassivePathError(MassiveError):
    """404 -- wrong endpoint path. A code bug, not a data gap."""
    pass


def _get(path, params=None, timeout=20, retries=2):
    """GET against Massive. Returns parsed JSON or None.

    Copies params rather than mutating the caller's dict -- v1 injected
    apiKey directly into whatever was passed in, which leaked the key into
    any dict a caller reused or logged later.
    """
    params = dict(params or {})
    params["apiKey"] = MASSIVE_API_KEY
    url = f"{BASE_URL}{path}"

    wait = 3
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            print(f"[MASSIVE] Request failed for {path} (attempt {attempt + 1}): {e}")
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except Exception as e:
                print(f"[MASSIVE] {path}: 200 but body was not JSON -- {e}")
                return None

        if r.status_code == 429:
            # Advanced tier is unlimited calls, so a 429 here means something
            # unusual (burst limit or account issue) -- worth seeing the body.
            print(f"[MASSIVE] 429 on {path}: {r.text[:200]}")
            time.sleep(wait)
            wait *= 2
            continue

        if r.status_code == 404:
            print(f"[MASSIVE] *** PATH BUG *** {path} returned 404 -- endpoint path is wrong.")
            return None

        if r.status_code in (401, 403):
            print(f"[MASSIVE] *** KEY/PLAN *** {path} returned {r.status_code}: {r.text[:200]} | "
                  f"Key invalid or endpoint not on your tier.")
            return None

        print(f"[MASSIVE] {path} returned {r.status_code}: {r.text[:200]}")
        return None

    return None


def _results(data, default=None):
    """Massive wraps payloads in {'results': ...}. Unwrap safely.

    Guards the dict check BEFORE calling .get(). Several Massive endpoints return
    a bare JSON array rather than the wrapper object, and a list is truthy, so
    the previous `data.get("results")` raised AttributeError on exactly those
    responses — surfacing as an unrelated crash inside whichever poller asked.
    A bare array is already the payload, so pass it straight through.
    """
    fallback = [] if default is None else default
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("results"), (list, dict)):
        return data["results"]
    return fallback


# ── Snapshots ─────────────────────────────────────────────────────────────────
def get_snapshot(ticker):
    """One call -> today's OHLC, volume, prevDay OHLC/volume, last trade/quote."""
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    if data and data.get("ticker"):
        return data["ticker"]
    return None


def get_full_market_snapshot(tickers=None):
    """
    Whole-market snapshot in ONE call -- covers 10,000+ tickers with day and
    prevDay OHLCV each.

    This is the single most valuable endpoint on the plan. Anything that
    currently loops per-ticker for price, volume or previous close should be
    reading from this instead. Use market_data.snapshot_all() rather than
    calling this directly, so multiple callers share one fetch per cycle.
    """
    params = {}
    if tickers:
        params["tickers"] = ",".join(tickers)
    data = _get("/v2/snapshot/locale/us/markets/stocks/tickers", params)
    if data and isinstance(data.get("tickers"), list):
        return data["tickers"]
    return []


def get_gainers(include_otc=False):
    """Top gainers, computed server-side. One call."""
    data = _get("/v2/snapshot/locale/us/markets/stocks/gainers",
                {"include_otc": str(include_otc).lower()})
    if data and isinstance(data.get("tickers"), list):
        return data["tickers"]
    return []


def get_losers(include_otc=False):
    """Top losers, computed server-side. One call."""
    data = _get("/v2/snapshot/locale/us/markets/stocks/losers",
                {"include_otc": str(include_otc).lower()})
    if data and isinstance(data.get("tickers"), list):
        return data["tickers"]
    return []


def get_prev_close(ticker):
    r = _results(_get(f"/v2/aggs/ticker/{ticker}/prev"))
    return r[0] if r else None


def get_aggregates(ticker, multiplier=1, timespan="day", from_date=None, to_date=None,
                   limit=5000, sort="desc"):
    path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
    return _results(_get(path, {"limit": limit, "sort": sort, "adjusted": "true"}), [])


def get_grouped_daily(date_str):
    """Every US stock's OHLCV for a single trading day -- one call."""
    return _results(_get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}",
                         {"adjusted": "true"}), [])


# ── Tick tape (Feature: Large Trades — the legal US bulk/block deal analogue) ─
def get_trades(ticker, timestamp_gte=None, timestamp_lte=None, limit=50000, order="desc"):
    """Tick-level trade tape for a ticker.

    Each record carries price, size (share count), exchange id, trade id and
    condition codes. There is no US disclosure regime naming counterparties
    the way SEBI requires for Indian bulk/block deals -- but the print itself
    is public and arrives in seconds rather than next day.

    timestamp_gte / timestamp_lte accept either a nanosecond epoch integer or
    a YYYY-MM-DD date string.

    Note on volume: this endpoint returns EVERY trade, and a liquid megacap can
    print hundreds of thousands in an hour. Always bound the window and filter
    for notional size locally -- see large_trades_poller.py.
    """
    params = {"limit": limit, "order": order, "sort": "timestamp"}
    if timestamp_gte is not None:
        params["timestamp.gte"] = timestamp_gte
    if timestamp_lte is not None:
        params["timestamp.lte"] = timestamp_lte
    return _results(_get(f"/v3/trades/{ticker}", params), [])


def get_last_trade(ticker):
    trades = get_trades(ticker, limit=1)
    return trades[0] if trades else None


# ── Reference data ────────────────────────────────────────────────────────────
def get_market_status():
    """Live exchange status: {'market': 'open'|'closed'|'extended-hours', ...}.

    Replaces hardcoded clock arithmetic. A hand-rolled 09:30-16:00 check has
    no idea about Thanksgiving, Good Friday, half-days, or an unscheduled
    close -- and firing 'crossover' alerts off a stale snapshot on a market
    holiday is exactly the kind of thing subscribers notice.
    """
    return _get("/v1/marketstatus/now")


def is_market_open(allow_extended=False):
    """True when US equities are actually trading right now, per the exchange."""
    status = get_market_status()
    if not status:
        return None  # unknown -- caller decides whether to proceed
    market = (status.get("market") or "").lower()
    if market == "open":
        return True
    if allow_extended and market == "extended-hours":
        return True
    return False


def get_ticker_details(ticker):
    r = _results(_get(f"/v3/reference/tickers/{ticker}"), {})
    return r if isinstance(r, dict) and r else None


def get_dividends(ticker=None, limit=50):
    params = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return _results(_get("/stocks/v1/dividends", params), [])


def get_splits(ticker=None, limit=50):
    params = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return _results(_get("/stocks/v1/splits", params), [])


# ── News ──────────────────────────────────────────────────────────────────────
def get_news(ticker=None, limit=10):
    params = {"limit": limit, "order": "desc", "sort": "published_utc"}
    if ticker:
        params["ticker"] = ticker
    return _results(_get("/v2/reference/news", params), [])


# ── Technical indicators ──────────────────────────────────────────────────────
def get_rsi(ticker, window=14, timespan="day", series_type="close", limit=1):
    r = _results(_get(f"/v1/indicators/rsi/{ticker}", {
        "timespan": timespan, "window": window, "series_type": series_type,
        "order": "desc", "limit": limit
    }), {})
    return r.get("values", []) if isinstance(r, dict) else []


def get_sma(ticker, window=200, timespan="day", series_type="close", limit=1):
    r = _results(_get(f"/v1/indicators/sma/{ticker}", {
        "timespan": timespan, "window": window, "series_type": series_type,
        "order": "desc", "limit": limit
    }), {})
    return r.get("values", []) if isinstance(r, dict) else []


# ── Crypto ────────────────────────────────────────────────────────────────────
def get_crypto_snapshot(pair):
    """pair like 'X:BTCUSD'."""
    data = _get(f"/v2/snapshot/locale/global/markets/crypto/tickers/{pair}")
    if data and data.get("ticker"):
        return data["ticker"]
    return None


if __name__ == "__main__":
    print("Market status:", get_market_status())
    print("Snapshot AAPL:", get_snapshot("AAPL"))
