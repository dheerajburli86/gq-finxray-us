"""
etf_flow_poller.py
GQ FinXray US — ETF Flow Alerts
Detects unusual buying/selling pressure in major sector ETFs.
When volume spikes + price moves in one direction = institutional flow signal.
Runs every 60 minutes via scheduler in main.py.
"""

import os
import logging
import requests
from datetime import datetime, timezone, date
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)

EODHD_API_KEY = os.getenv("EODHD_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Thresholds
VOLUME_SPIKE_THRESHOLD = 1.5   # volume > 1.5x 20-day average
PRICE_MOVE_THRESHOLD = 1.0     # price moved > 1% in one direction

# ETFs to monitor — one per sector
ETF_UNIVERSE = [
    {"ticker": "SPY",  "name": "S&P 500 ETF",                "category": "Broad Market"},
    {"ticker": "QQQ",  "name": "NASDAQ 100 ETF",              "category": "Technology"},
    {"ticker": "IWM",  "name": "Russell 2000 ETF",            "category": "Small Cap"},
    {"ticker": "XLK",  "name": "Technology Select ETF",       "category": "Technology"},
    {"ticker": "XLF",  "name": "Financial Select ETF",        "category": "Finance"},
    {"ticker": "XLE",  "name": "Energy Select ETF",           "category": "Energy"},
    {"ticker": "XLV",  "name": "Health Care Select ETF",      "category": "Healthcare"},
    {"ticker": "XLI",  "name": "Industrial Select ETF",       "category": "Industrials"},
    {"ticker": "XLY",  "name": "Consumer Discretionary ETF",  "category": "Consumer"},
    {"ticker": "GLD",  "name": "Gold ETF",                    "category": "Commodities"},
    {"ticker": "TLT",  "name": "20+ Year Treasury ETF",       "category": "Bonds"},
]


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def already_sent_today(ticker, flow_direction):
    """Check if this ETF flow alert already fired today."""
    sb = get_supabase()
    today = date.today().isoformat()
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", ticker) \
        .eq("source", "ETF_FLOW") \
        .eq("filing_type", flow_direction) \
        .gte("created_at", f"{today}T00:00:00+00:00") \
        .execute()
    return len(result.data) > 0


def save_alert(ticker, filing_type, summary, impact, extra=None):
    """Insert ETF flow alert into Supabase alerts table."""
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": "ETF_FLOW",
        "filing_type": filing_type,
        "delivered": False,
        "extra": extra or {},
        "filing_url": None
    }).execute()
    logger.info(f"[ETF FLOW] Saved alert: {ticker} | {filing_type} | {impact}")


def fetch_realtime(ticker):
    """Fetch live price and volume for an ETF."""
    symbol = f"{ticker}.US"
    try:
        r = requests.get(
            f"https://eodhd.com/api/real-time/{symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json"},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"[ETF FLOW] Real-time fetch failed for {ticker}: {e}")
    return None


def fetch_avg_volume(ticker):
    """Fetch 20-day average volume from EOD history."""
    symbol = f"{ticker}.US"
    try:
        r = requests.get(
            f"https://eodhd.com/api/eod/{symbol}",
            params={"api_token": EODHD_API_KEY, "fmt": "json", "order": "d", "limit": 20},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and len(data) >= 5:
                volumes = [d["volume"] for d in data if "volume" in d]
                return sum(volumes) / len(volumes)
    except Exception as e:
        logger.error(f"[ETF FLOW] Avg volume fetch failed for {ticker}: {e}")
    return None


def check_etf(etf_info):
    """Check a single ETF for unusual flow signal."""
    ticker = etf_info["ticker"]
    name = etf_info["name"]
    category = etf_info["category"]

    price_data = fetch_realtime(ticker)
    if not price_data:
        return None

    price = price_data.get("close", 0)
    prev_close = price_data.get("previousClose", 0)
    volume = price_data.get("volume", 0)
    change_p = price_data.get("change_p", 0)

    if not price or not prev_close or not volume:
        return None

    avg_volume = fetch_avg_volume(ticker)
    if not avg_volume or avg_volume == 0:
        return None

    volume_ratio = volume / avg_volume

    # Check thresholds
    if volume_ratio < VOLUME_SPIKE_THRESHOLD:
        return None
    if abs(change_p) < PRICE_MOVE_THRESHOLD:
        return None

    # Determine flow direction
    if change_p > 0:
        flow = "INFLOW"
        emoji = "🟢"
        signal = "Bullish flow — institutional buying likely"
        impact = "HIGH" if volume_ratio >= 2.0 else "MEDIUM"
    else:
        flow = "OUTFLOW"
        emoji = "🔴"
        signal = "Bearish flow — institutional selling likely"
        impact = "HIGH" if volume_ratio >= 2.0 else "MEDIUM"

    if already_sent_today(ticker, flow):
        logger.info(f"[ETF FLOW] Already sent {flow} for {ticker} today, skipping.")
        return None

    sign = "+" if change_p > 0 else ""
    summary = (
        f"{emoji} *ETF Flow Alert — ${ticker}*\n\n"
        f"*ETF:* {name} ({category})\n"
        f"*Price:* ${price:.2f} ({sign}{change_p:.2f}%)\n"
        f"*Volume:* {int(volume):,} ({volume_ratio:.1f}x 20-day average)\n"
        f"*Flow:* {flow}\n"
        f"*Signal:* {signal}\n"
        f"_Source: EODHD ETF Flow | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    save_alert(ticker, flow, summary, impact, {
        "name": name,
        "category": category,
        "price": price,
        "change_p": round(change_p, 2),
        "volume": int(volume),
        "avg_volume_20d": int(avg_volume),
        "volume_ratio": round(volume_ratio, 2),
        "flow": flow,
        "signal": signal
    })

    return flow


def run_etf_flow_poller():
    """Main entry point — called by scheduler in main.py every 60 mins."""
    logger.info("[ETF FLOW] Starting ETF flow poller...")
    alerts_generated = 0

    for etf in ETF_UNIVERSE:
        try:
            result = check_etf(etf)
            if result:
                alerts_generated += 1
                logger.info(f"[ETF FLOW] {etf['ticker']} — {result} signal fired")
        except Exception as e:
            logger.error(f"[ETF FLOW] Error checking {etf['ticker']}: {e}")

    logger.info(f"[ETF FLOW] Done. {len(ETF_UNIVERSE)} ETFs checked, {alerts_generated} alerts generated.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_flow_poller()
