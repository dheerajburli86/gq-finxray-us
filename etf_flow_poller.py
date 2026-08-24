"""
etf_flow_poller.py
GQ FinXray US — Feature 7. Rewritten on Massive (was EODHD).

Simplification vs the EODHD version: EODHD needed two calls per ETF
(real-time quote, then a separate 20-day EOD history pull just to compute
average volume). Massive's single-ticker snapshot
(/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}) returns today's
day.v (volume) AND prevDay.v (prior session volume) in one response, so
the volume-ratio check here is a same-day-vs-prior-session comparison in
one call instead of two. (Prior session vs a rolling 20-day average is a
slightly different baseline — noted in case the alert cadence looks
different than before; can layer a rolling average back in via
massive_client.get_aggregates() if the 1-day baseline proves too noisy.)

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


def already_sent_today(ticker, flow_direction):
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


def save_alert(ticker, filing_type, summary, impact, extra=None, link=None):
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
    if link:
        alert_dict["link"] = link
    sb.table("alerts").insert(alert_dict).execute()
    logger.info(f"[ETF FLOW] Saved alert: {ticker} | {filing_type} | {impact}")


def check_etf(etf_info):
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

    if volume_ratio < VOLUME_SPIKE_THRESHOLD:
        return None
    if abs(change_p) < PRICE_MOVE_THRESHOLD:
        return None

    if change_p > 0:
        flow, emoji, signal = "INFLOW", "🟢", "Bullish flow — institutional buying likely"
    else:
        flow, emoji, signal = "OUTFLOW", "🔴", "Bearish flow — institutional selling likely"
    impact = "HIGH" if volume_ratio >= 2.0 else "MEDIUM"

    if already_sent_today(ticker, flow):
        logger.info(f"[ETF FLOW] Already sent {flow} for {ticker} today, skipping.")
        return None

    sign = "+" if change_p > 0 else ""
    summary = (
        f"{emoji} *ETF Flow Alert — ${ticker}*\n\n"
        f"*ETF:* {name} ({category})\n"
        f"*Price:* ${price:.2f} ({sign}{change_p:.2f}%)\n"
        f"*Volume:* {int(volume):,} ({volume_ratio:.1f}x prior session)\n"
        f"*Flow:* {flow}\n"
        f"*Signal:* {signal}\n"
        f"_Source: Massive ETF Snapshot | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    save_alert(ticker, flow, summary, impact, {
        "name": name, "category": category, "price": price,
        "change_p": round(change_p, 2), "volume": int(volume),
        "prev_volume": int(prev_volume), "volume_ratio": round(volume_ratio, 2),
        "flow": flow, "signal": signal
    })
    # No link by design: an ETF flow signal is computed here from price and
    # volume thresholds. There is no document behind it -- not on FMP, not
    # anywhere -- so any URL attached would be decoration that 404s or
    # misleads. FMP confirmed their snapshot/quote endpoints carry no URL
    # field. Absent link beats broken link.

    return flow


def run_etf_flow_poller():
    logger.info("[ETF FLOW] Starting ETF flow poller (Massive)...")
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
