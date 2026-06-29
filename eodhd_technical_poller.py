"""
eodhd_technical_poller.py
GQ FinXray US — Technical Alerts Poller

ARCHITECTURE:
- Uses EODHD Screener API to cover ALL US listed stocks (~18,000) in a single call per indicator
- Instead of polling each ticker individually (18,000 × 5 = 90,000 calls), 
  screener returns ALL stocks hitting a threshold in one call (5 calls total)
- For SMA200 crossover: still per-watchlist-ticker (needs yesterday's price to detect crossover)
- Result: ~50 EODHD calls/day covers entire US market vs 18M calls for per-ticker approach

Runs every 60 minutes via scheduler in main.py.
"""

import os
import logging
import requests
from datetime import datetime, timezone, date, timedelta
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EODHD_API_KEY = os.getenv("EODHD_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ── Thresholds ────────────────────────────────────────────────────────────────
RSI_OVERBOUGHT        = 70
RSI_OVERSOLD          = 30
VOLUME_SPIKE_MULT     = 2.0    # volume > 2x 20-day average
MIN_PRICE             = 1.0    # ignore penny stocks below $1
MIN_VOLUME            = 50000  # ignore illiquid stocks


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_watchlist_tickers():
    """Pull all distinct tickers from watchlists table."""
    try:
        sb = get_supabase()
        result = sb.table("watchlists").select("ticker").execute()
        return list(set(row["ticker"] for row in result.data))
    except Exception as e:
        logger.error(f"[TECHNICAL] Failed to fetch watchlist: {e}")
        return []


def already_sent_today(ticker, alert_type):
    """Check if this alert already fired today for this ticker."""
    try:
        sb = get_supabase()
        today = date.today().isoformat()
        result = sb.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", "EODHD_TECHNICAL") \
            .eq("filing_type", alert_type) \
            .gte("created_at", f"{today}T00:00:00+00:00") \
            .execute()
        return len(result.data) > 0
    except:
        return False


def save_alert(ticker, alert_type, summary, impact, extra=None):
    """Insert alert into Supabase alerts table."""
    try:
        sb = get_supabase()
        sb.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "EODHD_TECHNICAL",
            "filing_type": alert_type,
            "delivered": False,
            "extra": extra or {},
            "filing_url": None
        }).execute()
        logger.info(f"[TECHNICAL] Alert saved: {ticker} | {alert_type} | {impact}")
    except Exception as e:
        logger.error(f"[TECHNICAL] Failed to save alert: {e}")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── SCREENER API — covers ALL US stocks ──────────────────────────────────────
def fetch_screener(filters, sort_field="volume", sort_order="desc", limit=500):
    """
    Call EODHD Screener API with given filters.
    Returns list of stocks matching the criteria.
    One call covers all US listed stocks.
    """
    try:
        r = requests.post(
            "https://eodhd.com/api/screener",
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            json={
                "filters": filters,
                "sort": [{"field": sort_field, "order": sort_order}],
                "limit": limit,
                "offset": 0
            },
            timeout=30
        )
        if r.status_code == 200:
            return r.json().get("data", [])
        else:
            logger.error(f"[SCREENER] Failed: {r.status_code} {r.text[:100]}")
            return []
    except Exception as e:
        logger.error(f"[SCREENER] Error: {e}")
        return []


def run_rsi_screener():
    """
    Find ALL US stocks with RSI > 70 (overbought) or RSI < 30 (oversold).
    One screener call covers entire US market.
    """
    logger.info("[TECHNICAL] Running RSI screener for all US stocks...")
    alerts = 0

    # RSI Overbought — one call
    overbought = fetch_screener([
        {"field": "rsi_14d", "operator": ">=", "value": RSI_OVERBOUGHT},
        {"field": "exchange", "operator": "=", "value": "US"},
        {"field": "close", "operator": ">=", "value": MIN_PRICE},
        {"field": "volume", "operator": ">=", "value": MIN_VOLUME},
    ], sort_field="rsi_14d", sort_order="desc", limit=200)

    for stock in overbought:
        ticker = stock.get("code", "").replace(".US", "")
        rsi = stock.get("rsi_14d")
        price = stock.get("close")
        if not ticker or already_sent_today(ticker, "RSI_OVERBOUGHT"):
            continue
        summary = (
            f"📈 *Technical Alert — RSI Overbought*\n\n"
            f"*Ticker:* ${ticker}\n"
            f"*Price:* ${float(price):.2f}\n"
            f"*RSI(14):* {float(rsi):.1f} (above {RSI_OVERBOUGHT})\n"
            f"*Signal:* Stock may be overbought — potential pullback zone.\n"
            f"_Source: EODHD Screener | {now_utc()}_"
        )
        save_alert(ticker, "RSI_OVERBOUGHT", summary, "MEDIUM", {"rsi": rsi, "price": price})
        alerts += 1

    # RSI Oversold — one call
    oversold = fetch_screener([
        {"field": "rsi_14d", "operator": "<=", "value": RSI_OVERSOLD},
        {"field": "exchange", "operator": "=", "value": "US"},
        {"field": "close", "operator": ">=", "value": MIN_PRICE},
        {"field": "volume", "operator": ">=", "value": MIN_VOLUME},
    ], sort_field="rsi_14d", sort_order="asc", limit=200)

    for stock in oversold:
        ticker = stock.get("code", "").replace(".US", "")
        rsi = stock.get("rsi_14d")
        price = stock.get("close")
        if not ticker or already_sent_today(ticker, "RSI_OVERSOLD"):
            continue
        summary = (
            f"📉 *Technical Alert — RSI Oversold*\n\n"
            f"*Ticker:* ${ticker}\n"
            f"*Price:* ${float(price):.2f}\n"
            f"*RSI(14):* {float(rsi):.1f} (below {RSI_OVERSOLD})\n"
            f"*Signal:* Stock may be oversold — potential bounce zone.\n"
            f"_Source: EODHD Screener | {now_utc()}_"
        )
        save_alert(ticker, "RSI_OVERSOLD", summary, "MEDIUM", {"rsi": rsi, "price": price})
        alerts += 1

    logger.info(f"[TECHNICAL] RSI screener: {len(overbought)} overbought, {len(oversold)} oversold → {alerts} new alerts")
    return alerts


def run_52week_screener():
    """
    Find ALL US stocks hitting 52-week high or low.
    One screener call covers entire US market.
    """
    logger.info("[TECHNICAL] Running 52-week high/low screener...")
    alerts = 0

    # 52-week highs
    highs = fetch_screener([
        {"field": "52w_high", "operator": "=", "value": 1},
        {"field": "exchange", "operator": "=", "value": "US"},
        {"field": "close", "operator": ">=", "value": MIN_PRICE},
        {"field": "volume", "operator": ">=", "value": MIN_VOLUME},
    ], sort_field="volume", sort_order="desc", limit=200)

    for stock in highs:
        ticker = stock.get("code", "").replace(".US", "")
        price = stock.get("close")
        high_52 = stock.get("52w_high_price", price)
        if not ticker or already_sent_today(ticker, "52W_HIGH"):
            continue
        summary = (
            f"🚀 *Technical Alert — 52-Week High*\n\n"
            f"*Ticker:* ${ticker}\n"
            f"*Price:* ${float(price):.2f}\n"
            f"*52-Week High:* ${float(high_52):.2f}\n"
            f"*Signal:* Stock hit a new 52-week high — strong bullish momentum.\n"
            f"_Source: EODHD Screener | {now_utc()}_"
        )
        save_alert(ticker, "52W_HIGH", summary, "HIGH", {"price": price, "high_52w": high_52})
        alerts += 1

    # 52-week lows
    lows = fetch_screener([
        {"field": "52w_low", "operator": "=", "value": 1},
        {"field": "exchange", "operator": "=", "value": "US"},
        {"field": "close", "operator": ">=", "value": MIN_PRICE},
        {"field": "volume", "operator": ">=", "value": MIN_VOLUME},
    ], sort_field="volume", sort_order="desc", limit=200)

    for stock in lows:
        ticker = stock.get("code", "").replace(".US", "")
        price = stock.get("close")
        low_52 = stock.get("52w_low_price", price)
        if not ticker or already_sent_today(ticker, "52W_LOW"):
            continue
        summary = (
            f"⚠️ *Technical Alert — 52-Week Low*\n\n"
            f"*Ticker:* ${ticker}\n"
            f"*Price:* ${float(price):.2f}\n"
            f"*52-Week Low:* ${float(low_52):.2f}\n"
            f"*Signal:* Stock hit a new 52-week low — watch for further downside.\n"
            f"_Source: EODHD Screener | {now_utc()}_"
        )
        save_alert(ticker, "52W_LOW", summary, "HIGH", {"price": price, "low_52w": low_52})
        alerts += 1

    logger.info(f"[TECHNICAL] 52W screener: {len(highs)} highs, {len(lows)} lows → {alerts} new alerts")
    return alerts


def run_volume_spike_screener():
    """
    Find ALL US stocks with unusual volume (>2x 20-day average).
    One screener call covers entire US market.
    """
    logger.info("[TECHNICAL] Running volume spike screener...")
    alerts = 0

    spikes = fetch_screener([
        {"field": "volume_change", "operator": ">=", "value": VOLUME_SPIKE_MULT * 100},  # % change
        {"field": "exchange", "operator": "=", "value": "US"},
        {"field": "close", "operator": ">=", "value": MIN_PRICE},
        {"field": "volume", "operator": ">=", "value": MIN_VOLUME},
    ], sort_field="volume_change", sort_order="desc", limit=200)

    for stock in spikes:
        ticker = stock.get("code", "").replace(".US", "")
        price = stock.get("close")
        volume = stock.get("volume")
        volume_change = stock.get("volume_change", 0)
        if not ticker or already_sent_today(ticker, "VOLUME_SPIKE"):
            continue
        ratio = (volume_change / 100) + 1 if volume_change else VOLUME_SPIKE_MULT
        summary = (
            f"📊 *Technical Alert — Volume Spike*\n\n"
            f"*Ticker:* ${ticker}\n"
            f"*Price:* ${float(price):.2f}\n"
            f"*Volume:* {int(volume):,} ({ratio:.1f}x average)\n"
            f"*Signal:* Unusual trading volume — potential significant move ahead.\n"
            f"_Source: EODHD Screener | {now_utc()}_"
        )
        save_alert(ticker, "VOLUME_SPIKE", summary, "MEDIUM", {
            "price": price,
            "volume": volume,
            "volume_change_pct": volume_change,
            "ratio": round(ratio, 2)
        })
        alerts += 1

    logger.info(f"[TECHNICAL] Volume screener: {len(spikes)} spikes → {alerts} new alerts")
    return alerts


# ── SMA200 CROSSOVER — still per-watchlist (needs yesterday's price) ──────────
def fetch_sma200_and_prices(ticker):
    """Fetch SMA200, today's price, and yesterday's price for crossover detection."""
    symbol = f"{ticker}.US"
    try:
        sma_r = requests.get(
            f"https://eodhd.com/api/technical/{symbol}",
            params={"function": "sma", "period": 200, "api_token": EODHD_API_KEY,
                    "fmt": "json", "order": "d", "limit": 1},
            timeout=15
        ).json()

        rt_r = requests.get(
            f"https://eodhd.com/api/real-time/{symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            timeout=15
        ).json()

        sma200 = float(sma_r[0]["sma"]) if sma_r and isinstance(sma_r, list) else None
        price = float(rt_r.get("close", 0)) if rt_r else None
        prev_close = float(rt_r.get("previousClose", 0)) if rt_r else None
        return sma200, price, prev_close
    except Exception as e:
        logger.error(f"[TECHNICAL] SMA200 fetch failed for {ticker}: {e}")
        return None, None, None


def run_sma200_crossover(tickers):
    """
    Check SMA200 crossover for watchlisted tickers only.
    Needs yesterday's price to detect the crossover — can't do this via screener.
    """
    logger.info(f"[TECHNICAL] Running SMA200 crossover for {len(tickers)} watchlisted tickers...")
    alerts = 0

    for ticker in tickers:
        try:
            sma200, price, prev_close = fetch_sma200_and_prices(ticker)
            if not all([sma200, price, prev_close]):
                continue

            # Crossed ABOVE
            if prev_close < sma200 and price > sma200:
                if not already_sent_today(ticker, "SMA200_CROSSOVER_UP"):
                    summary = (
                        f"🟢 *Technical Alert — 200-SMA Breakout*\n\n"
                        f"*Ticker:* ${ticker}\n"
                        f"*Price:* ${price:.2f} (crossed above 200-SMA)\n"
                        f"*200-SMA:* ${sma200:.2f}\n"
                        f"*Signal:* Bullish crossover — price moved above long-term trend.\n"
                        f"_Source: EODHD Technical | {now_utc()}_"
                    )
                    save_alert(ticker, "SMA200_CROSSOVER_UP", summary, "HIGH",
                               {"price": price, "sma200": sma200, "prev_close": prev_close})
                    alerts += 1

            # Crossed BELOW
            elif prev_close > sma200 and price < sma200:
                if not already_sent_today(ticker, "SMA200_CROSSOVER_DOWN"):
                    summary = (
                        f"🔴 *Technical Alert — 200-SMA Breakdown*\n\n"
                        f"*Ticker:* ${ticker}\n"
                        f"*Price:* ${price:.2f} (crossed below 200-SMA)\n"
                        f"*200-SMA:* ${sma200:.2f}\n"
                        f"*Signal:* Bearish crossover — price dropped below long-term trend.\n"
                        f"_Source: EODHD Technical | {now_utc()}_"
                    )
                    save_alert(ticker, "SMA200_CROSSOVER_DOWN", summary, "HIGH",
                               {"price": price, "sma200": sma200, "prev_close": prev_close})
                    alerts += 1

        except Exception as e:
            logger.error(f"[TECHNICAL] SMA200 error for {ticker}: {e}")

    logger.info(f"[TECHNICAL] SMA200 crossover: {alerts} alerts")
    return alerts


# ── Main entry point ──────────────────────────────────────────────────────────
def run_technical_poller():
    """
    Main entry point called by scheduler every 60 minutes.

    Coverage:
    - RSI alerts: ALL US listed stocks via screener (~2 EODHD calls)
    - 52-week high/low: ALL US listed stocks via screener (~2 EODHD calls)
    - Volume spike: ALL US listed stocks via screener (~1 EODHD call)
    - SMA200 crossover: watchlisted tickers only (needs crossover detection)

    Total EODHD calls: ~5 for broad market + (watchlist size × 2) for SMA200
    """
    logger.info("[TECHNICAL] ===== Starting Technical Alerts Poller =====")

    total = 0

    # Screener-based — covers ALL US stocks
    total += run_rsi_screener()
    total += run_52week_screener()
    total += run_volume_spike_screener()

    # Per-ticker — watchlisted only (crossover detection)
    watchlist = get_watchlist_tickers()
    if watchlist:
        total += run_sma200_crossover(watchlist)

    logger.info(f"[TECHNICAL] ===== Done. Total alerts generated: {total} =====")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_technical_poller()
