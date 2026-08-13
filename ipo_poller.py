"""
ipo_poller.py
GQ FinXray US — Feature 8. Upcoming US IPO listings from FMP's IPO calendar.

FIXED (Aug 13 2026):
- Removed impossible watchlist gate that silenced entire feature
- IPOs are market-wide; all users receive unless opted out
- Filter only by listing_date < today (already happened)
"""

import os
import logging
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import log_poller_error, get_watched_tickers

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

SOURCE = "FMP_IPO"
FILING_TYPE = "IPO_UPCOMING"
POLLER = "ipo_poller"

IPO_LOOKAHEAD_DAYS = 30

LARGE_DEAL_USD = 1_000_000_000


def _num(value):
    """Float or None, tolerant of various shapes FMP returns."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def parse_price_range(price_range_str):
    """'18.00-20.00' or '$18-$20' or '19' -> (low, high). (None, None) if absent."""
    if not price_range_str:
        return None, None
    parts = [p for p in str(price_range_str).replace("$", "").split("-") if p.strip()]
    if len(parts) >= 2:
        low, high = _num(parts[0]), _num(parts[1])
        if low is not None and high is not None:
            return (low, high) if low <= high else (high, low)
        return low, high
    if len(parts) == 1:
        v = _num(parts[0])
        return v, v
    return None, None


def format_shares(shares):
    shares = _num(shares)
    if not shares or shares <= 0:
        return "TBD"
    if shares >= 1_000_000:
        return f"{shares / 1_000_000:.1f}M shares"
    if shares >= 1_000:
        return f"{shares / 1_000:.0f}K shares"
    return f"{shares:,.0f} shares"


def format_usd(amount):
    if not amount or amount <= 0:
        return None
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    return f"${amount:,.0f}"


def parse_listing_date(start_date_str):
    """date object or None."""
    if not start_date_str:
        return None
    text = str(start_date_str).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _today_et():
    """Today's date in US Eastern."""
    return datetime.now(ET).date()


def timing_label(listing_date):
    if not listing_date:
        return "date to be confirmed"
    delta = (listing_date - _today_et()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return f"in {delta} days"


def already_sent(ticker, signature):
    """
    Dedup on (ticker, signature) rather than ticker alone. Signature captures
    date, price range, share count — so a postponed or repriced deal produces
    a fresh alert while an unchanged row stays quiet.
    """
    try:
        result = (supabase.table("alerts")
                  .select("id")
                  .eq("ticker", ticker)
                  .eq("source", SOURCE)
                  .eq("filing_type", FILING_TYPE)
                  .eq("extra->>signature", signature)
                  .limit(1)
                  .execute())
        return bool(result.data)
    except Exception as e:
        log_poller_error(POLLER, "already_sent", e, {"ticker": ticker, "signature": signature})
        return None


def save_alert(ticker, summary, impact, extra):
    try:
        filing_url = "https://www.nasdaq.com/market-activity/ipos"

        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "delivered": False,
            "extra": tag_extra(extra, SOURCE, FILING_TYPE),
            "filing_url": filing_url,
        }).execute()
        logger.info("[IPO] Alert saved: %s (%s)", ticker, impact)
        return True
    except Exception as e:
        log_poller_error(POLLER, "save_alert", e, {"ticker": ticker})
        return False


VENUE_NAMES = {
    "NASDAQ": "Nasdaq", "NYSE": "NYSE", "AMEX": "AMEX",
    "NYSEAMERICAN": "NYSE American", "CBOE": "Cboe",
}


def build_headline(exchange, price_low, price_high):
    """4-10 words, Title Case, no ticker, no hype."""
    first_token = (exchange or "").split()[0].upper() if exchange else ""
    venue = VENUE_NAMES.get(first_token, "")
    venue_word = f"{venue} " if venue else ""

    if price_low and price_high:
        band = (f"${price_low:.2f} To ${price_high:.2f}" if price_low != price_high
                else f"${price_low:.2f}")
        return f"Upcoming {venue_word}Listing Priced At {band}"
    return f"Upcoming {venue_word}Listing With Price Range Not Set"


def process_ipo(ipo):
    """Returns True when an alert was written."""
    ticker = (ipo.get("symbol") or "").strip().upper()
    name = (ipo.get("company") or "").strip() or "Unknown Company"
    exchange = (ipo.get("exchange") or "").strip() or "N/A"
    start_date_raw = ipo.get("date") or ""
    actions = (ipo.get("actions") or "").strip()

    shares = _num(ipo.get("shares"))
    market_cap = _num(ipo.get("marketCap"))

    if not ticker or not start_date_raw:
        return False

    listing_date = parse_listing_date(start_date_raw)
    if listing_date and listing_date < _today_et():
        # Already happened — skip
        return False

    # Build signature for dedup
    price_sig = " ".join(str(ipo.get("priceRange") or "").split()).upper()
    signature = f"{start_date_raw}|{price_sig}|{shares}"
    seen = already_sent(ticker, signature)
    if seen is None or seen:
        return False

    price_low, price_high = parse_price_range(ipo.get("priceRange"))
    if price_low and price_high:
        price_str = (f"${price_low:.2f}–${price_high:.2f}" if price_low != price_high
                     else f"${price_low:.2f}")
    else:
        price_str = "not yet set"

    deal_size = price_high * shares if (price_high and shares) else None
    deal_size_str = format_usd(deal_size)
    market_cap_str = format_usd(market_cap)

    impact = "HIGH" if ((market_cap and market_cap >= LARGE_DEAL_USD)
                        or (deal_size and deal_size >= LARGE_DEAL_USD)) else "MEDIUM"

    parts = [
        f"{name} is scheduled to list on {exchange} under the ticker {ticker} on "
        f"{start_date_raw} ({timing_label(listing_date)}).",
        f"The offering is priced at {price_str}" if price_str != "not yet set"
        else "A price range has not been set yet",
    ]
    if shares and shares > 0:
        parts[-1] += f" across {format_shares(shares)}"
    if deal_size_str:
        parts[-1] += f", implying a deal size of about {deal_size_str}"
    parts[-1] += "."
    if market_cap_str:
        parts.append(f"FMP puts the expected market capitalisation at {market_cap_str}.")
    if actions:
        parts.append(f"Current status: {actions}.")

    summary = " ".join(parts)

    extra = {
        "headline": build_headline(exchange, price_low, price_high),
        "company_name": name,
        "exchange": exchange,
        "start_date": start_date_raw,
        "price_from": price_low,
        "price_to": price_high,
        "shares": shares,
        "market_cap": market_cap,
        "deal_size": deal_size,
        "actions": actions,
        "signature": signature,
    }
    return save_alert(ticker, summary, impact, extra)


def run_ipo_poller():
    """One pass over the forward IPO calendar. Never raises."""
    try:
        logger.info("[IPO] Starting IPO poller (FMP calendar, %d-day lookahead)...",
                    IPO_LOOKAHEAD_DAYS)
        from_date = date.today().isoformat()
        to_date = (date.today() + timedelta(days=IPO_LOOKAHEAD_DAYS)).isoformat()

        try:
            ipos = fmp_client.get_ipo_calendar(from_date, to_date)
        except Exception as e:
            log_poller_error(POLLER, "get_ipo_calendar", e,
                             {"from": from_date, "to": to_date})
            return

        if not ipos:
            logger.info("[IPO] No upcoming IPOs in the window %s to %s.", from_date, to_date)
            return

        # FIXED: NO watchlist gate. IPO_UPCOMING is market-wide.
        # A pre-listing ticker is not on anybody's watchlist yet, so gating
        # on watchlist silenced the entire feature. Delivery layer handles
        # per-user opt-out via receive_market_wide preference.
        candidates = ipos

        written = 0
        for ipo in candidates:
            try:
                if process_ipo(ipo):
                    written += 1
            except Exception as e:
                log_poller_error(POLLER, "process_ipo", e,
                                 {"symbol": (ipo or {}).get("symbol")})

        logger.info("[IPO] Done. %d calendar entries, %d alerts written "
                    "(as of %s).", len(ipos), written,
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    except Exception as e:
        log_poller_error(POLLER, "run_ipo_poller", e)
        logger.exception("[IPO] Run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_ipo_poller()
