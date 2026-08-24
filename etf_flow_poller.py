"""
etf_flow_poller.py — Feature 7
GQ FinXray US — Real ETF Fund Flow Alerts using Massive /etf-global/v1/fund-flows

Real fund flow = net daily capital through creation/redemption process.
Positive = inflows (institutional buying)
Negative = outflows (institutional selling)

Uses actual flow data, not proxy signals from price/volume.
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

# Alert thresholds for fund flow (in dollars)
INFLOW_THRESHOLD = 100_000_000    # Alert if inflow > $100M
OUTFLOW_THRESHOLD = -100_000_000  # Alert if outflow < -$100M

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


def get_fund_flow_link(ticker):
    """Link to Massive ETF Global fund flow data."""
    return f"https://api.massive.com/etf-global/v1/fund-flows?ticker={ticker}"


def check_etf_flow(etf_info):
    """Check real ETF fund flow from Massive /etf-global/v1/fund-flows."""
    ticker = etf_info["ticker"]
    name = etf_info["name"]
    category = etf_info["category"]

    # Get fund flow data from Massive
    flow_data = massive_client.get_fund_flows(ticker)
    if not flow_data:
        logger.debug(f"[ETF FLOW] No flow data for {ticker}")
        return None

    # Extract fund flow value
    fund_flow = flow_data.get("fund_flow")
    flow_date = flow_data.get("date")
    
    if fund_flow is None:
        return None

    # Determine flow direction and check thresholds
    if fund_flow > INFLOW_THRESHOLD:
        flow_type, emoji, signal = "INFLOW", "🟢", "Strong institutional buying"
        impact = "HIGH" if fund_flow > 500_000_000 else "MEDIUM"
    elif fund_flow < OUTFLOW_THRESHOLD:
        flow_type, emoji, signal = "OUTFLOW", "🔴", "Significant institutional selling"
        impact = "HIGH" if fund_flow < -500_000_000 else "MEDIUM"
    else:
        # Flow is within normal range, no alert
        return None

    # Check dedup
    if already_sent_today(ticker, flow_type):
        logger.info(f"[ETF FLOW] Already sent {flow_type} for {ticker} today, skipping.")
        return None

    # Format flow value in millions
    flow_millions = fund_flow / 1_000_000
    sign = "+" if fund_flow > 0 else ""
    
    summary = (
        f"{emoji} *ETF Fund Flow Alert — ${ticker}*\n\n"
        f"*ETF:* {name} ({category})\n"
        f"*Fund Flow:* {sign}${flow_millions:,.0f}M\n"
        f"*Flow Type:* {flow_type}\n"
        f"*Signal:* {signal}\n"
        f"*Date:* {flow_date}\n"
        f"_Source: Massive ETF Global Fund Flows | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    save_alert(
        ticker, flow_type, summary, impact,
        {
            "name": name,
            "category": category,
            "fund_flow": float(fund_flow),
            "flow_millions": round(flow_millions, 2),
            "flow_type": flow_type,
            "signal": signal,
            "date": flow_date
        },
        link=get_fund_flow_link(ticker)
    )

    return flow_type


def run_etf_flow_poller():
    """Poll real ETF fund flows from Massive."""
    logger.info("[ETF FLOW] Starting ETF flow poller (real fund flows)...")
    alerts_generated = 0
    for etf in ETF_UNIVERSE:
        try:
            result = check_etf_flow(etf)
            if result:
                alerts_generated += 1
                logger.info(f"[ETF FLOW] {etf['ticker']} — {result} detected")
        except Exception as e:
            logger.error(f"[ETF FLOW] Error checking {etf['ticker']}: {e}")
    logger.info(f"[ETF FLOW] Done. {len(ETF_UNIVERSE)} ETFs checked, {alerts_generated} alerts generated.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_flow_poller()
