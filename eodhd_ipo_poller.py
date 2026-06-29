"""
eodhd_ipo_poller.py
GQ FinXray US — IPO Deep Dive Alert Poller
Fetches upcoming IPOs from EODHD calendar and sends structured alerts.
Runs every 24 hours via scheduler in main.py.
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

IPO_LOOKAHEAD_DAYS = 30  # fetch IPOs up to 30 days ahead


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def already_sent(ticker, start_date):
    """Check if this IPO alert was already sent for this specific ticker."""
    sb = get_supabase()
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", ticker) \
        .eq("source", "EODHD_IPO") \
        .eq("filing_type", "IPO_UPCOMING") \
        .execute()
    return len(result.data) > 0


def save_alert(ticker, summary, impact, extra=None):
    """Insert IPO alert into Supabase alerts table."""
    sb = get_supabase()
    sb.table("alerts").insert({
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": "EODHD_IPO",
        "filing_type": "IPO_UPCOMING",
        "delivered": False,
        "extra": extra or {},
        "filing_url": None
    }).execute()
    logger.info(f"[IPO] Saved alert: {ticker}")


def fetch_upcoming_ipos():
    """Fetch upcoming IPOs from EODHD calendar."""
    from_date = date.today().isoformat()
    to_date = (date.today() + timedelta(days=IPO_LOOKAHEAD_DAYS)).isoformat()
    url = (
        f"https://eodhd.com/api/calendar/ipos"
        f"?api_token={EODHD_API_KEY}&fmt=json"
        f"&from={from_date}&to={to_date}"
    )
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        return data.get("ipos", [])
    except Exception as e:
        logger.error(f"[IPO] Failed to fetch IPO calendar: {e}")
        return []


def format_price_range(price_from, price_to, offer_price):
    """Format price range string cleanly."""
    if offer_price and offer_price > 0:
        return f"${offer_price:.2f} (offer price)"
    if price_from and price_to and price_from > 0 and price_to > 0:
        return f"${price_from:.2f} – ${price_to:.2f}"
    if price_from and price_from > 0:
        return f"${price_from:.2f}+"
    return "TBD"


def format_shares(shares):
    """Format share count in human-readable form."""
    if not shares or shares == 0:
        return "TBD"
    if shares >= 1_000_000:
        return f"{shares / 1_000_000:.1f}M shares"
    if shares >= 1_000:
        return f"{shares / 1_000:.0f}K shares"
    return f"{shares:,} shares"


def days_until(start_date_str):
    """Return number of days until IPO date."""
    try:
        ipo_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        delta = (ipo_date - date.today()).days
        if delta == 0:
            return "Today"
        elif delta == 1:
            return "Tomorrow"
        else:
            return f"In {delta} days"
    except Exception:
        return ""


def process_ipo(ipo):
    """Process a single IPO entry and generate alert if not already sent."""
    ticker = ipo.get("code", "").replace(".US", "").strip()
    name = ipo.get("name", "Unknown Company")
    exchange = ipo.get("exchange", "N/A")
    start_date = ipo.get("start_date", "")
    filing_date = ipo.get("filing_date", "")
    price_from = ipo.get("price_from", 0)
    price_to = ipo.get("price_to", 0)
    offer_price = ipo.get("offer_price", 0)
    shares = ipo.get("shares", 0)
    deal_type = ipo.get("deal_type", "")

    if not ticker or not start_date:
        return

    if already_sent(ticker, start_date):
        logger.info(f"[IPO] Already sent alert for {ticker} ({start_date}), skipping.")
        return

    price_str = format_price_range(price_from, price_to, offer_price)
    shares_str = format_shares(shares)
    timing = days_until(start_date)

    # Estimate deal size for impact
    impact = "MEDIUM"
    if offer_price and shares and offer_price > 0 and shares > 0:
        deal_size = offer_price * shares
        if deal_size >= 1_000_000_000:
            impact = "HIGH"
        elif deal_size >= 100_000_000:
            impact = "MEDIUM"
        else:
            impact = "LOW"
    elif price_to and shares and price_to > 0 and shares > 0:
        deal_size = price_to * shares
        if deal_size >= 1_000_000_000:
            impact = "HIGH"

    summary = (
        f"🏦 *IPO Alert — Upcoming Listing*\n\n"
        f"*Company:* {name}\n"
        f"*Ticker:* ${ticker}\n"
        f"*Exchange:* {exchange}\n"
        f"*Listing Date:* {start_date} ({timing})\n"
        f"*Price Range:* {price_str}\n"
        f"*Shares Offered:* {shares_str}\n"
        f"*Deal Type:* {deal_type if deal_type else 'N/A'}\n"
        f"_Source: EODHD IPO Calendar | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    save_alert(ticker, summary, impact, {
        "name": name,
        "exchange": exchange,
        "start_date": start_date,
        "filing_date": filing_date,
        "price_from": price_from,
        "price_to": price_to,
        "offer_price": offer_price,
        "shares": shares,
        "deal_type": deal_type
    })


def run_ipo_poller():
    """Main entry point — called by scheduler in main.py every 24 hours."""
    logger.info("[IPO] Starting IPO deep dive poller...")
    ipos = fetch_upcoming_ipos()

    if not ipos:
        logger.info("[IPO] No upcoming IPOs found.")
        return

    logger.info(f"[IPO] Found {len(ipos)} upcoming IPOs, processing...")
    for ipo in ipos:
        try:
            process_ipo(ipo)
        except Exception as e:
            logger.error(f"[IPO] Error processing IPO {ipo.get('code', 'unknown')}: {e}")

    logger.info(f"[IPO] Done. Processed {len(ipos)} IPOs.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    run_ipo_poller()
