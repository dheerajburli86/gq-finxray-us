"""
etf_flow_poller.py — Feature 7
GQ FinXray US — ETF Momentum Alerts (Volume + Price Spikes)

NOT real fund flow data. This detects volume/price momentum as a proxy signal:
- Volume spike (>1.5x prior session) + price move (±1.0%) = institutional activity
- Green = bullish momentum (inflow-like)
- Red = bearish momentum (outflow-like)

Uses Massive snapshot data (included in Stocks Advanced plan).
No link because this is a computed signal, not a source document.
Runs every 60 minutes via scheduler in main.py.
"""

import logging
from datetime import datetime, timezone, date
from dotenv import load_dotenv
from supabase import create_client
import os

import massive_client
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

VOLUME_SPIKE_THRESHOLD = 1.5
PRICE_MOVE_THRESHOLD = 1.0

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


def already_sent_today(ticker, signal_type):
    sb = get_supabase()
    today = date.today().isoformat()
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", ticker) \
        .eq("source", "ETF_FLOW") \
        .eq("filing_type", signal_type) \
        .gte("created_at", f"{today}T00:00:00+00:00") \
        .execute()
    return len(result.data) > 0


def save_alert(ticker, filing_type, summary, impact, extra=None):
    sb = get_supabase()
    alert_dict = {
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": "ETF_FLOW",
        "filing_type": filing_type,
        "delivered": False,
        "extra": tag_extra(extra, "ETF_FLOW", filing_type)
    }
    sb.table("alerts").insert(alert_dict).execute()
    logger.info(f"[ETF FLOW] Saved momentum alert: {ticker} | {filing_type} | {impact}")


def check_etf_momentum(etf_info):
    """Check for volume/price momentum spike (proxy for institutional activity)."""
    ticker = etf_info["ticker"]
    name = etf_info["name"]
    category = etf_info["category"]

    snap = massive_client.get_snapshot(ticker)
    if not snap:
        return None

    day = snap.get("day", {}) or {}
    prev = snap.get("prevDay", {}) or {}
    price = day.get("c") or prev.get("c")
    prev_close = prev.get("c")
    volume = day.get("v")
    prev_volume = prev.get("v")

    if not price or not prev_close or not volume or not prev_volume:
        return None

    change_p = ((price - prev_close) / prev_close) * 100
    volume_ratio = volume / prev_volume

    # Both conditions must be met
    if volume_ratio < VOLUME_SPIKE_THRESHOLD or abs(change_p) < PRICE_MOVE_THRESHOLD:
        return None

    # Determine momentum direction
    if change_p > 0:
        signal_type = "BULLISH_MOMENTUM"
        emoji = "🟢"
        signal_text = "Bullish momentum — volume spike + price up"
    else:
        signal_type = "BEARISH_MOMENTUM"
        emoji = "🔴"
        signal_text = "Bearish momentum — volume spike + price down"

    impact = "HIGH" if volume_ratio >= 2.0 else "MEDIUM"

    if already_sent_today(ticker, signal_type):
        logger.info(f"[ETF FLOW] Already sent {signal_type} for {ticker} today, skipping.")
        return None

    sign = "+" if change_p > 0 else ""
    summary = (
        f"{emoji} *ETF Momentum Alert — *\n\n"
        f"*ETF:* {name} ({category})\n"
        f"*Price:*  ({sign}{change_p:.2f}%)\n"
        f"*Volume:* {int(volume):,} ({volume_ratio:.1f}x prior session)\n"
        f"*Signal:* {signal_text}\n"
        f"_Note: This is a momentum signal based on volume/price, not official fund flow data._\n"
        f"_Source: Massive Market Snapshot | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    save_alert(ticker, signal_type, summary, impact, {
        "name": name,
        "category": category,
        "price": float(price),
        "change_p": round(change_p, 2),
        "volume": int(volume),
        "prev_volume": int(prev_volume),
        "volume_ratio": round(volume_ratio, 2),
        "signal": signal_text
    })

    return signal_type


def run_etf_flow_poller():
    """Poll ETF momentum signals (volume + price spikes)."""
    logger.info("[ETF FLOW] Starting ETF momentum poller...")
    alerts_generated = 0
    for etf in ETF_UNIVERSE:
        try:
            result = check_etf_momentum(etf)
            if result:
                alerts_generated += 1
                logger.info(f"[ETF FLOW] {etf['ticker']} — {result} detected")
        except Exception as e:
            logger.error(f"[ETF FLOW] Error checking {etf['ticker']}: {e}")
    logger.info(f"[ETF FLOW] Done. {len(ETF_UNIVERSE)} ETFs checked, {alerts_generated} alerts generated.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_flow_poller()
