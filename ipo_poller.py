"""
ipo_poller.py
GQ FinXray US — replaces eodhd_ipo_poller.py. Feature 8.

FMP's /stable/ipos-calendar returns: date, symbol, company, exchange,
actions, shares, priceRange ("18.00-20.00" style string), marketCap.
That's a different shape than EODHD's calendar/ipos (which split into
price_from/price_to/offer_price/filing_date separately) — priceRange is
parsed here into from/to since FMP gives it as one string.

Runs every 24 hours via scheduler in main.py.
"""

import logging
from datetime import datetime, timezone, date, timedelta
from supabase import create_client
from dotenv import load_dotenv
import os

import fmp_client
import edgar_link
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
IPO_LOOKAHEAD_DAYS = 30


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def already_sent(ticker, signature):
    """
    Dedup on (ticker, signature) rather than ticker alone. `signature` bundles
    the fields that actually change when FMP updates a listing (listing date,
    price range, share count) -- an IPO getting postponed or repriced is real
    news worth a fresh alert, not a duplicate of the original one. Plain
    ticker-only dedup would permanently suppress every update after the first
    alert for that ticker.
    """
    sb = get_supabase()
    result = sb.table("alerts") \
        .select("id") \
        .eq("ticker", ticker) \
        .eq("source", "FMP_IPO") \
        .eq("filing_type", "IPO_UPCOMING") \
        .eq("extra->>signature", signature) \
        .execute()
    return len(result.data) > 0


def save_alert(ticker, summary, impact, extra=None, link=None):
    sb = get_supabase()
    alert_dict = {
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": "FMP_IPO",
        "filing_type": "IPO_UPCOMING",
        "delivered": False,
        "extra": tag_extra(extra, "FMP_IPO", "IPO_UPCOMING")
    }
    if link:
        alert_dict["link"] = link
    sb.table("alerts").insert(alert_dict).execute()
    logger.info(f"[IPO] Saved alert: {ticker}")


def parse_price_range(price_range_str):
    if not price_range_str:
        return None, None
    try:
        parts = str(price_range_str).replace("$", "").split("-")
        if len(parts) == 2:
            return float(parts[0].strip()), float(parts[1].strip())
        if len(parts) == 1 and parts[0].strip():
            v = float(parts[0].strip())
            return v, v
    except Exception:
        pass
    return None, None


def format_shares(shares):
    if not shares or shares == 0:
        return "TBD"
    try:
        shares = float(shares)
    except Exception:
        return "TBD"
    if shares >= 1_000_000:
        return f"{shares / 1_000_000:.1f}M shares"
    if shares >= 1_000:
        return f"{shares / 1_000:.0f}K shares"
    return f"{shares:,.0f} shares"


def days_until(start_date_str):
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
    ticker = (ipo.get("symbol") or "").strip()
    name = ipo.get("company", "Unknown Company")
    exchange = ipo.get("exchange", "N/A")
    start_date = ipo.get("date", "")
    shares = ipo.get("shares", 0)
    market_cap = ipo.get("marketCap", 0)
    actions = ipo.get("actions", "")

    if not ticker or not start_date:
        return

    signature = f"{start_date}|{ipo.get('priceRange', '')}|{shares}"
    if already_sent(ticker, signature):
        logger.info(f"[IPO] Already sent alert for {ticker} with this date/price/shares, skipping.")
        return

    price_from, price_to = parse_price_range(ipo.get("priceRange"))
    if price_from and price_to:
        price_str = f"${price_from:.2f} – ${price_to:.2f}" if price_from != price_to else f"${price_from:.2f}"
    else:
        price_str = "TBD"

    shares_str = format_shares(shares)
    timing = days_until(start_date)

    impact = "MEDIUM"
    if market_cap and float(market_cap) >= 1_000_000_000:
        impact = "HIGH"
    elif price_to and shares and price_to > 0 and shares > 0 and (price_to * shares) >= 1_000_000_000:
        impact = "HIGH"

    summary = (
        f"🏦 *IPO Alert — Upcoming Listing*\n\n"
        f"*Company:* {name}\n"
        f"*Ticker:* ${ticker}\n"
        f"*Exchange:* {exchange}\n"
        f"*Listing Date:* {start_date} ({timing})\n"
        f"*Price Range:* {price_str}\n"
        f"*Shares Offered:* {shares_str}\n"
        f"*Status:* {actions if actions else 'N/A'}\n"
        f"_Source: FMP IPO Calendar | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    # The IPO's own S-1 registration statement, resolved via FMP's SEC-filings
    # endpoint. Replaces the market-wide Nasdaq IPO calendar, which loaded but
    # showed the reader every IPO in the market instead of this one. FMP's
    # ipos-calendar carries no per-deal identifier or URL, so the S-1 is the
    # only per-company document available. Looks back 120 days because the S-1
    # is filed well before the listing date.
    s1_url = edgar_link.find_filing_url(
        ticker, form_type="S-1", target_date=start_date, window_days=-1
    ) or edgar_link.find_filing_url(
        ticker, form_type="S-1",
        target_date=(date.today() - timedelta(days=120)).isoformat(),
        window_days=130
    )

    save_alert(ticker, summary, impact, {
        "name": name, "exchange": exchange, "start_date": start_date,
        "price_from": price_from, "price_to": price_to,
        "shares": shares, "market_cap": market_cap, "actions": actions,
        "signature": signature
    }, link=s1_url)


def run_ipo_poller():
    logger.info("[IPO] Starting IPO deep dive poller (FMP)...")
    from_date = date.today().isoformat()
    to_date = (date.today() + timedelta(days=IPO_LOOKAHEAD_DAYS)).isoformat()
    ipos = fmp_client.get_ipo_calendar(from_date, to_date)

    if not ipos:
        logger.info("[IPO] No upcoming IPOs found.")
        return

    logger.info(f"[IPO] Found {len(ipos)} upcoming IPOs, processing...")
    for ipo in ipos:
        try:
            process_ipo(ipo)
        except Exception as e:
            logger.error(f"[IPO] Error processing IPO {ipo.get('symbol', 'unknown')}: {e}")

    logger.info(f"[IPO] Done. Processed {len(ipos)} IPOs.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_ipo_poller()
