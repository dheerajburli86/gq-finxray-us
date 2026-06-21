"""
eodhd_technical_poller.py
GQ FinXray US — Technical Alerts Poller
Checks RSI, SMA200 crossover, 52-week high/low, volume spike for all watchlisted tickers.
Runs every 60 minutes via scheduler in main.py.
"""

import os
import logging
import requests
from datetime import datetime, timezone, date
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EODHD_API_KEY = os.getenv("EODHD_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Thresholds
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
VOLUME_SPIKE_MULTIPLIER = 2.0  # volume > 2x 20-day average


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_watchlist_tickers():
    """Pull all distinct tickers from watchlists table."""
    sb = get_supabase()
    result = sb.table("watchlists").select("ticker").execute()
    tickers = list(set(row["ticker"] for row in result.data))
    return tickers


def already_sent_today(ticker, alert_type):
    """Check if this alert already fired today for this ticker."""
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


def save_alert(ticker, alert_type, summary, impact, extra=None):
    """Insert alert into Supabase alerts table."""
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
    logger.info(f"[TECHNICAL] Saved alert: {ticker} | {alert_type} | {impact}")


def fetch_rsi(ticker):
    """Fetch latest RSI(14) for ticker."""
    symbol = f"{ticker}.US"
    url = f"https://eodhd.com/api/technical/{symbol}?function=rsi&period=14&api_token={EODHD_API_KEY}&fmt=json&order=d&limit=1"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data and isinstance(data, list):
            return data[0].get("rsi")
    except Exception as e:
        logger.error(f"[TECHNICAL] RSI fetch failed for {ticker}: {e}")
    return None


def fetch_sma200_and_price(ticker):
    """Fetch SMA200 and current price for ticker."""
    symbol = f"{ticker}.US"
    sma_url = f"https://eodhd.com/api/technical/{symbol}?function=sma&period=200&api_token={EODHD_API_KEY}&fmt=json&order=d&limit=1"
    price_url = f"https://eodhd.com/api/real-time/{symbol}?api_token={EODHD_API_KEY}&fmt=json"
    try:
        sma_r = requests.get(sma_url, timeout=15).json()
        price_r = requests.get(price_url, timeout=15).json()
        sma = sma_r[0].get("sma") if sma_r and isinstance(sma_r, list) else None
        price = price_r.get("close") if price_r else None
        prev_close = price_r.get("previousClose") if price_r else None
        volume = price_r.get("volume") if price_r else None
        return sma, price, prev_close, volume
    except Exception as e:
        logger.error(f"[TECHNICAL] SMA/Price fetch failed for {ticker}: {e}")
    return None, None, None, None


def fetch_52week(ticker):
    """Fetch 52-week high and low using last 252 trading days of EOD data."""
    symbol = f"{ticker}.US"
    url = f"https://eodhd.com/api/eod/{symbol}?api_token={EODHD_API_KEY}&fmt=json&order=d&limit=252"
    try:
        r = requests.get(url, timeout=15).json()
        if r and isinstance(r, list) and len(r) > 10:
            closes = [d["close"] for d in r if "close" in d]
            volumes = [d["volume"] for d in r if "volume" in d]
            high_52 = max(closes)
            low_52 = min(closes)
            avg_volume_20 = sum(volumes[:20]) / len(volumes[:20]) if len(volumes) >= 20 else None
            return high_52, low_52, avg_volume_20
    except Exception as e:
        logger.error(f"[TECHNICAL] 52-week fetch failed for {ticker}: {e}")
    return None, None, None


def check_ticker(ticker):
    """Run all technical checks for a single ticker and generate alerts."""
    alerts_generated = []
    logger.info(f"[TECHNICAL] Checking {ticker}...")

    # --- RSI Check ---
    rsi = fetch_rsi(ticker)
    if rsi is not None:
        if rsi >= RSI_OVERBOUGHT and not already_sent_today(ticker, "RSI_OVERBOUGHT"):
            summary = (
                f"📈 *Technical Alert — RSI Overbought*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*RSI(14):* {rsi:.1f} (above {RSI_OVERBOUGHT})\n"
                f"*Signal:* Stock may be overbought — potential pullback zone.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "RSI_OVERBOUGHT", summary, "MEDIUM", {"rsi": rsi})
            alerts_generated.append("RSI_OVERBOUGHT")

        elif rsi <= RSI_OVERSOLD and not already_sent_today(ticker, "RSI_OVERSOLD"):
            summary = (
                f"📉 *Technical Alert — RSI Oversold*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*RSI(14):* {rsi:.1f} (below {RSI_OVERSOLD})\n"
                f"*Signal:* Stock may be oversold — potential bounce zone.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "RSI_OVERSOLD", summary, "MEDIUM", {"rsi": rsi})
            alerts_generated.append("RSI_OVERSOLD")

    # --- SMA200 Crossover + Volume Spike ---
    sma200, price, prev_close, volume = fetch_sma200_and_price(ticker)

    if sma200 and price and prev_close:
        # Price crossed ABOVE SMA200
        if prev_close < sma200 and price > sma200 and not already_sent_today(ticker, "SMA200_CROSSOVER_UP"):
            summary = (
                f"🟢 *Technical Alert — 200-SMA Breakout*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*Price:* ${price:.2f} (crossed above 200-SMA)\n"
                f"*200-SMA:* ${sma200:.2f}\n"
                f"*Signal:* Bullish crossover — price moved above long-term trend.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "SMA200_CROSSOVER_UP", summary, "HIGH", {"price": price, "sma200": sma200})
            alerts_generated.append("SMA200_CROSSOVER_UP")

        # Price crossed BELOW SMA200
        elif prev_close > sma200 and price < sma200 and not already_sent_today(ticker, "SMA200_CROSSOVER_DOWN"):
            summary = (
                f"🔴 *Technical Alert — 200-SMA Breakdown*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*Price:* ${price:.2f} (crossed below 200-SMA)\n"
                f"*200-SMA:* ${sma200:.2f}\n"
                f"*Signal:* Bearish crossover — price dropped below long-term trend.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "SMA200_CROSSOVER_DOWN", summary, "HIGH", {"price": price, "sma200": sma200})
            alerts_generated.append("SMA200_CROSSOVER_DOWN")

    # --- 52-Week High/Low + Volume Spike ---
    high_52, low_52, avg_volume_20 = fetch_52week(ticker)

    if high_52 and low_52 and price:
        # 52-week high
        if price >= high_52 and not already_sent_today(ticker, "52W_HIGH"):
            summary = (
                f"🚀 *Technical Alert — 52-Week High*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*Price:* ${price:.2f}\n"
                f"*52-Week High:* ${high_52:.2f}\n"
                f"*Signal:* Stock hit a new 52-week high — strong momentum.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "52W_HIGH", summary, "HIGH", {"price": price, "high_52w": high_52})
            alerts_generated.append("52W_HIGH")

        # 52-week low
        elif price <= low_52 and not already_sent_today(ticker, "52W_LOW"):
            summary = (
                f"⚠️ *Technical Alert — 52-Week Low*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*Price:* ${price:.2f}\n"
                f"*52-Week Low:* ${low_52:.2f}\n"
                f"*Signal:* Stock hit a new 52-week low — watch for further downside.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "52W_LOW", summary, "HIGH", {"price": price, "low_52w": low_52})
            alerts_generated.append("52W_LOW")

    # --- Volume Spike ---
    if volume and avg_volume_20 and price:
        if volume >= avg_volume_20 * VOLUME_SPIKE_MULTIPLIER and not already_sent_today(ticker, "VOLUME_SPIKE"):
            ratio = volume / avg_volume_20
            summary = (
                f"📊 *Technical Alert — Volume Spike*\n\n"
                f"*Ticker:* ${ticker}\n"
                f"*Volume:* {volume:,} ({ratio:.1f}x average)\n"
                f"*20-Day Avg Volume:* {int(avg_volume_20):,}\n"
                f"*Price:* ${price:.2f}\n"
                f"*Signal:* Unusual volume detected — potential significant move.\n"
                f"_Source: EODHD Technical | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )
            save_alert(ticker, "VOLUME_SPIKE", summary, "MEDIUM", {
                "volume": volume,
                "avg_volume_20": int(avg_volume_20),
                "ratio": round(ratio, 2),
                "price": price
            })
            alerts_generated.append("VOLUME_SPIKE")

    return alerts_generated


def run_technical_poller():
    """Main entry point — called by scheduler in main.py every 60 mins."""
    logger.info("[TECHNICAL] Starting technical alerts poller...")
    tickers = get_watchlist_tickers()
    if not tickers:
        logger.info("[TECHNICAL] No tickers in watchlist, skipping.")
        return

    total_alerts = 0
    for ticker in tickers:
        try:
            alerts = check_ticker(ticker)
            total_alerts += len(alerts)
        except Exception as e:
            logger.error(f"[TECHNICAL] Error processing {ticker}: {e}")

    logger.info(f"[TECHNICAL] Done. {len(tickers)} tickers checked, {total_alerts} alerts generated.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    run_technical_poller()
