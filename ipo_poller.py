"""
ipo_poller.py
GQ FinXray US — Feature 8. Upcoming US IPO listings from FMP's IPO calendar.

FMP's /stable/ipos-calendar returns: date, symbol, company, exchange, actions,
shares, priceRange ("18.00-20.00" as a string), marketCap.

FIXES OVER THE PREVIOUS VERSION
-------------------------------
* `shares` and `marketCap` arrive inconsistently — sometimes a number, sometimes
  a comma-formatted string ("12,500,000"), sometimes null. The impact test did
  `shares > 0` on the raw value, which raises TypeError against None and against
  a string on Python 3, and the resulting exception dropped the entire IPO. All
  numeric fields now go through `_num()`.
* Listings whose date has already passed are filtered out. FMP keeps recently
  priced deals in the window, and `days_until` happily rendered "In -3 days".
* Every alert carries `extra["headline"]`; this poller does not run through
  ai_pipeline, so nothing else would supply one and alert_formatter would render
  a headline-less message.

IPO alerts (feature 8) are watchlist-scoped: an IPO alert reaches only the users
who watch that specific ticker. Add an upcoming IPO's ticker to your watchlist
before its listing date to get the heads-up.

The gate is applied in run_ipo_poller, before anything is summarised. It used to
sit downstream, so every calendar entry was written to `alerts` and then dropped
at delivery for having no audience — real LLM spend on messages that could not
be sent. Filtering the calendar against the watchlist first means an unwatched
IPO costs one list comparison and nothing else.

Runs once a day via the scheduler in main.py.
"""

import os
import logging
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

# US Eastern. The rest of the codebase (etf_flow_poller, watchlist_heatmap,
# technical_poller) already anchors market dates here; this module did not.
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

# A billion-dollar-plus deal moves its sector and gets broad coverage; anything
# smaller is a normal listing and does not warrant a HIGH-impact interrupt.
LARGE_DEAL_USD = 1_000_000_000


def _num(value):
    """
    Float or None, tolerant of the three shapes FMP actually returns for numeric
    fields: a number, a comma/currency-formatted string, or null. Returning None
    rather than 0 keeps "unknown" distinguishable from "genuinely zero", which
    matters for the deal-size test below.
    """
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
    """Today's date in US Eastern.

    This module used bare date.today(). On a UTC server, from about 20:00 ET the
    UTC date has already rolled over, so an IPO listing later *today* in market
    terms tested as "already listed" and was dropped — permanently, because the
    poller runs once a day and the date is only further in the past next time.
    """
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
    Dedup on (ticker, signature) rather than ticker alone. `signature` bundles
    the fields that change when FMP updates a listing — date, price range, share
    count — so a postponed or repriced deal produces a fresh alert while an
    unchanged row stays quiet. Ticker-only dedup would permanently suppress
    every update after the first.

    The `extra->>signature` filter works because tag_extra MERGES feature_id and
    feature_name into the caller's dict rather than nesting it under a key, so
    `signature` stays a top-level field of `extra`.

    Returns None if the check could not be performed — the caller skips the IPO
    rather than risk re-alerting a deal users already saw.
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
        # Link to NASDAQ IPO calendar
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
    """4-10 words, Title Case, no ticker, no hype — matches Prompt_H1's rules."""
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
        # FMP keeps recently priced deals in the window; a "heads-up" about a
        # listing that already happened is worse than no alert.
        return False

    # Build the signature from NORMALISED values. FMP returns `shares` as a bare
    # number, a comma-formatted string ("12,500,000") or null for the same
    # unchanged listing on different days (this module's own docstring documents
    # that inconsistency). Hashing the raw field meant a representation flip
    # produced a "new" signature and re-alerted an IPO the user had already seen.
    # priceRange is a band string ("$18.00-$20.00"), so normalise whitespace and
    # case rather than coercing it to a number.
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

        # Watchlist gate, applied HERE rather than downstream.
        #
        # An IPO alert is delivered only to users who watch that specific ticker,
        # so an IPO nobody follows has no recipient. Previously every calendar
        # entry was summarised and written to `alerts` anyway, then dropped at
        # delivery for having no audience — 9 such alerts in 48h, each costing an
        # LLM call. Filtering up front means we only ever spend tokens on a deal
        # someone is actually waiting for.
        #
        # To receive one, add the IPO's ticker to your watchlist before it lists.
        watched = get_watched_tickers()
        if not watched:
            logger.info("[IPO] No user watches any ticker — skipping %d calendar entries.",
                        len(ipos))
            return

        candidates = [i for i in ipos
                      if ((i or {}).get("symbol") or "").strip().upper() in watched]
        if not candidates:
            logger.info("[IPO] %d upcoming IPO(s), none watchlisted — nothing to write.",
                        len(ipos))
            return

        written = 0
        for ipo in candidates:
            try:
                if process_ipo(ipo):
                    written += 1
            except Exception as e:
                log_poller_error(POLLER, "process_ipo", e,
                                 {"symbol": (ipo or {}).get("symbol")})

        logger.info("[IPO] Done. %d calendar entries, %d watchlisted, %d alerts written "
                    "(as of %s).", len(ipos), len(candidates), written,
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    except Exception as e:
        log_poller_error(POLLER, "run_ipo_poller", e)
        logger.exception("[IPO] Run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_ipo_poller()
