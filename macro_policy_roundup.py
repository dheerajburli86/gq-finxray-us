"""
macro_policy_roundup.py
GQ FinXray US — Consolidated macro & policy digest. Feature 13.

Aggregates:
- Federal Reserve decisions (rate changes, policy shifts)
- Treasury yield movements
- Economic data (jobs, inflation, GDP, PMI)
- Commodity moves (oil, gold, natural gas)
- Currency movements (USD index, EUR/USD, etc.)
- Geopolitical events impacting markets

Runs daily at 7 PM ET (after market close + news flow)
Stores as news_roundup table + sends to Telegram as macro briefing.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot

import fmp_client

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Today's macro/policy events to monitor
MACRO_KEYWORDS = {
    "FED_DECISION": ["federal reserve", "fomc", "interest rate decision", "policy decision"],
    "JOBS_DATA": ["nonfarm payroll", "employment report", "jobs added", "jobless claims"],
    "INFLATION": ["inflation", "cpi", "pce", "producer price"],
    "GDP": ["gross domestic product", "gdp growth", "economic growth"],
    "PMI": ["purchasing managers index", "pmi", "manufacturing", "services"],
    "TREASURY": ["10-year yield", "treasury yield", "treasury rate"],
    "OIL": ["crude oil", "wti", "brent", "oil prices"],
    "GOLD": ["gold prices", "spot gold"],
    "USD": ["dollar index", "usd index", "dollar strength"],
    "GEOPOLITICS": ["ukraine", "middle east", "trade war", "sanctions"],
}


def fetch_macro_events_today():
    """Fetch macro/economic events from the past 24 hours from news sources."""
    events = []

    try:
        # Use FMP's market news to get macro-related items
        news_data = fmp_client.get_market_news(limit=50)  # Last 50 news items
        if not news_data:
            return events

        for item in news_data:
            headline = item.get("title", "").lower()
            summary = item.get("text", "").lower()
            published = item.get("publishedDate", "")

            # Filter for macro/policy keywords
            for event_type, keywords in MACRO_KEYWORDS.items():
                if any(kw in headline or kw in summary for kw in keywords):
                    events.append({
                        "type": event_type,
                        "headline": item.get("title", ""),
                        "summary": item.get("text", "")[:150],
                        "source": item.get("site", ""),
                        "url": item.get("link", ""),
                        "published": published,
                    })
                    break  # Only classify once

    except Exception as e:
        logger.error(f"[MACRO] Failed to fetch macro events: {e}")

    return events


def fetch_macro_prices():
    """Fetch current macro price levels."""
    prices = {}

    try:
        # US 10-year Treasury yield
        tnx = fmp_client.get_quote("TNX")  # Ticker for 10-year yield
        if tnx:
            prices["10Y_Yield"] = f"{tnx.get('price', 0):.2f}%"

        # Oil (WTI Crude)
        oil = fmp_client.get_quote("WTIC")  # WTI Crude
        if oil:
            prices["Oil_WTI"] = f"${oil.get('price', 0):.2f}"

        # Gold
        gold = fmp_client.get_commodity_quote("GCUSD")
        if gold:
            prices["Gold"] = f"${gold.get('price', 0):.2f}"

        # US Dollar Index
        dxy = fmp_client.get_quote("DXY")
        if dxy:
            change = dxy.get("changePercentage", 0)
            arrow = "↑" if change >= 0 else "↓"
            prices["USD_Index"] = f"{dxy.get('price', 0):.2f} ({arrow} {abs(change):.2f}%)"

    except Exception as e:
        logger.error(f"[MACRO] Failed to fetch macro prices: {e}")

    return prices


def aggregate_into_roundup(events, prices):
    """Aggregate events and prices into a readable roundup."""
    if not events and not prices:
        return None

    sections = []

    # Event summary by type
    event_summary = {}
    for event in events:
        event_type = event.get("type", "")
        if event_type not in event_summary:
            event_summary[event_type] = []
        event_summary[event_type].append(event)

    # Build US Macro section
    us_section_items = []
    for event_type in ["FED_DECISION", "JOBS_DATA", "INFLATION", "GDP", "PMI"]:
        if event_type in event_summary:
            latest = event_summary[event_type][0]  # Most recent
            headline = latest.get("headline", "")[:50] + "..."
            us_section_items.append(f"• {headline}")

    # Build commodity/FX section
    commodities_items = []
    if "TREASURY" in event_summary or "10Y_Yield" in prices:
        commodities_items.append(f"• 10Y Yield: {prices.get('10Y_Yield', 'N/A')}")
    if "OIL" in event_summary or "Oil_WTI" in prices:
        commodities_items.append(f"• WTI Crude: {prices.get('Oil_WTI', 'N/A')}")
    if "GOLD" in event_summary or "Gold" in prices:
        commodities_items.append(f"• Gold: {prices.get('Gold', 'N/A')}")
    if "USD" in event_summary or "USD_Index" in prices:
        commodities_items.append(f"• USD Index: {prices.get('USD_Index', 'N/A')}")

    # Build geopolitical section
    geo_items = []
    if "GEOPOLITICS" in event_summary:
        for event in event_summary["GEOPOLITICS"][:2]:
            geo_items.append(f"• {event.get('headline', '')[:60]}")

    # Format output
    roundup_text = "*📊 US Macro & Policy Digest*\n\n"

    if us_section_items:
        roundup_text += "*Economic Data*\n" + "\n".join(us_section_items) + "\n\n"

    if commodities_items:
        roundup_text += "*Commodities & FX*\n" + "\n".join(commodities_items) + "\n\n"

    if geo_items:
        roundup_text += "*Geopolitics*\n" + "\n".join(geo_items) + "\n\n"

    return roundup_text.strip()


def store_macro_roundup(roundup_text):
    """Store macro roundup in alerts table for Telegram delivery."""
    try:
        alert_data = {
            "ticker": "MARKET",
            "source": "MACRO_ROUNDUP",
            "filing_type": "MACRO_BRIEFING",
            "summary": roundup_text,
            "impact": "MEDIUM",
            "delivered": False,
            "extra": {"roundup_type": "DAILY_MACRO", "content": roundup_text}
        }

        supabase.table("alerts").insert(alert_data).execute()
        logger.info("[MACRO] Macro roundup stored as alert")

        return True
    except Exception as e:
        logger.error(f"[MACRO] Failed to store macro roundup: {e}")
        return False


def run_macro_policy_roundup():
    """
    Main function: fetch macro events, aggregate, and store.
    Called daily at 7 PM ET via scheduler.
    """
    logger.info("[MACRO] Starting macro & policy roundup...")

    try:
        # Fetch today's macro events
        events = fetch_macro_events_today()
        logger.info(f"[MACRO] Found {len(events)} macro events")

        # Fetch current macro prices
        prices = fetch_macro_prices()
        logger.info(f"[MACRO] Fetched {len(prices)} macro price levels")

        # Aggregate into roundup
        roundup = aggregate_into_roundup(events, prices)
        if not roundup:
            logger.warning("[MACRO] No macro events or prices to report")
            return

        # Store as alert
        if store_macro_roundup(roundup):
            logger.info("[MACRO] Macro roundup complete and stored")
        else:
            logger.error("[MACRO] Failed to store macro roundup")

    except Exception as e:
        logger.error(f"[MACRO] Macro roundup failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_macro_policy_roundup()
