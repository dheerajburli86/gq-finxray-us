"""
massive_client.py
GQ FinXray US — Massive (formerly Polygon.io) shared client.

Massive rebranded from Polygon.io in 2026 but kept the same REST surface
(confirmed against massive.com/docs on 2026-07-27):

  base URL           -> https://api.massive.com
  auth                -> ?apiKey=... query param (or Authorization: Bearer)
  aggregates/bars      -> /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}
  previous day bar     -> /v2/aggs/ticker/{ticker}/prev
  single snapshot      -> /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
  full market snapshot -> /v2/snapshot/locale/us/markets/stocks/tickers
  grouped daily        -> /v2/aggs/grouped/locale/us/market/stocks/{date}
  news                 -> /v2/reference/news
  RSI                  -> /v1/indicators/rsi/{ticker}
  SMA                  -> /v1/indicators/sma/{ticker}
  ETF fund flows       -> /etf-global/v1/fund-flows

Massive is used here for anything that benefits from one-shot, whole-market
snapshots (ETF flow, technical screeners) and for crypto, where its
websocket/aggregates coverage is stronger than FMP's. See
claude/us-market-data-licensing-risk.md for the commercial-tier licensing
gap ($1,999/mo Business tier needed for redistribution) that still needs
closing before this scales past the free/dev tier.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")
BASE_URL = "https://api.massive.com"


def _get(path, params=None, timeout=20, retries=2):
    if params is None:
        params = {}
    params["apiKey"] = MASSIVE_API_KEY
    url = f"{BASE_URL}{path}"

    wait = 3
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(wait)
                wait *= 2
                continue
            print(f"[MASSIVE] {path} returned {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"[MASSIVE] Request failed for {path} (attempt {attempt + 1}): {e}")
            time.sleep(wait)
            wait *= 2
    return None


# ── Snapshots ─────────────────────────────────────────────────────────────────
def get_snapshot(ticker):
    """One call -> today's OHLC, volume, prevDay OHLC/volume, last trade/quote."""
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    if data and data.get("status") in ("OK", "success") and data.get("ticker"):
        return data["ticker"]
    return None


def get_full_market_snapshot(tickers=None):
    """
    Whole-market snapshot in ONE call — covers 10,000+ tickers.
    Used by technical_poller.py to replace EODHD's per-indicator screener
    for volume-spike / 52-week breadth scans without per-ticker calls.
    """
    params = {}
    if tickers:
        params["tickers"] = ",".join(tickers)
    data = _get("/v2/snapshot/locale/us/markets/stocks/tickers", params)
    if data and isinstance(data.get("tickers"), list):
        return data["tickers"]
    return []


def get_prev_close(ticker):
    data = _get(f"/v2/aggs/ticker/{ticker}/prev")
    if data and isinstance(data.get("results"), list) and data["results"]:
        return data["results"][0]
    return None


def get_aggregates(ticker, multiplier=1, timespan="day", from_date=None, to_date=None, limit=5000):
    path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
    data = _get(path, {"limit": limit, "sort": "desc", "adjusted": "true"})
    if data and isinstance(data.get("results"), list):
        return data["results"]
    return []


def get_grouped_daily(date_str):
    """Every US stock's OHLCV for a single trading day — one call."""
    data = _get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}", {"adjusted": "true"})
    if data and isinstance(data.get("results"), list):
        return data["results"]
    return []


# ── News ──────────────────────────────────────────────────────────────────────
def get_news(ticker=None, limit=10):
    params = {"limit": limit, "order": "desc", "sort": "published_utc"}
    if ticker:
        params["ticker"] = ticker
    data = _get("/v2/reference/news", params)
    if data and isinstance(data.get("results"), list):
        return data["results"]
    return []


# ── Technical indicators ──────────────────────────────────────────────────────
def get_rsi(ticker, window=14, timespan="day", series_type="close", limit=1):
    data = _get(f"/v1/indicators/rsi/{ticker}", {
        "timespan": timespan, "window": window, "series_type": series_type,
        "order": "desc", "limit": limit
    })
    if data and isinstance(data.get("results"), dict):
        values = data["results"].get("values", [])
        return values
    return []


def get_sma(ticker, window=200, timespan="day", series_type="close", limit=1):
    data = _get(f"/v1/indicators/sma/{ticker}", {
        "timespan": timespan, "window": window, "series_type": series_type,
        "order": "desc", "limit": limit
    })
    if data and isinstance(data.get("results"), dict):
        values = data["results"].get("values", [])
        return values
    return []


# ── ETF Global (fund flows) ───────────────────────────────────────────────────
def get_fund_flows(ticker, limit=1):
    """Get real ETF fund flow data from Massive ETF Global dataset.
    
    Returns net daily capital flow through creation/redemption process.
    Positive = inflows (institutional buying)
    Negative = outflows (institutional selling)
    """
    data = _get(f"/etf-global/v1/fund-flows", {
        "ticker": ticker,
        "order": "desc",
        "limit": limit
    })
    if data and isinstance(data.get("results"), list) and data["results"]:
        return data["results"][0]
    return None


# ── Crypto (replaces TwelveData test scripts — Massive covers crypto too) ────
def get_crypto_snapshot(pair):
    """pair like 'X:BTCUSD'."""
    data = _get(f"/v2/snapshot/locale/global/markets/crypto/tickers/{pair}")
    if data and data.get("ticker"):
        return data["ticker"]
    return None


if __name__ == "__main__":
    print("Snapshot AAPL:", get_snapshot("AAPL"))
