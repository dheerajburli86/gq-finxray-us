"""
ipo_poller.py
GQ FinXray US — Feature 8, upcoming US IPOs (FMP).

FMP's /stable/ipos-calendar returns: date, symbol, company, exchange,
actions, shares, priceRange ("18.00-20.00" style string), marketCap.
FMP returns a single ipos-calendar shape (rather than splitting into
price_from/price_to/offer_price/filing_date separately) — priceRange is
parsed here into from/to since FMP gives it as one string.

Runs every 24 hours via scheduler in main.py.
"""

import logging
import re
from datetime import datetime, timezone, date, timedelta
from urllib.parse import quote_plus
from supabase import create_client
from dotenv import load_dotenv
import os

import fmp_client
import edgar_link
from feature_map import tag_extra
from gquants_format_converter import s1_to_ipo

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
IPO_LOOKAHEAD_DAYS = 30

# How far back to look for the registrant's own S-1. An S-1 is filed well ahead
# of the listing — commonly one to six months, occasionally over a year when a
# deal is postponed and revived.
S1_LOOKBACK_DAYS = int(os.getenv("GQ_IPO_S1_LOOKBACK_DAYS", "400"))

# Legal-form suffixes only. Descriptive words ("Technologies", "Holdings") are
# deliberately NOT stripped: they are often the only thing separating two real
# registrants, and removing them turns a near-miss into a confident wrong match.
_LEGAL_SUFFIXES = {
    "inc", "inc.", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "lp", "llp", "plc", "sa", "nv", "ag", "ab", "as",
}


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _norm_company(name):
    """Company name reduced to comparable tokens: lowercase, no punctuation, no
    legal suffix, no leading 'the'."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    words = [w for w in cleaned.split() if w and w not in _LEGAL_SUFFIXES]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _names_match(fmp_name, edgar_name):
    """
    True when two company names denote the same registrant.

    Exact match on normalised tokens, or one being a token-boundary prefix of
    the other ("Acme Robotics" vs "Acme Robotics Holdings") — but only when the
    shorter side carries at least two tokens. A single-token prefix would happily
    match "Acme" to "Acme Biosciences", which are different companies, and a
    wrong match here attaches the wrong prospectus to a live IPO alert.
    """
    a, b = _norm_company(fmp_name), _norm_company(edgar_name)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short.split()) < 2:
        return False
    return long_.startswith(short + " ")


def find_captured_s1(company_name):
    """
    The S-1 our own EDGAR poller captured for this registrant, or None.

    This is the Feature 8 merge. poll_sec_s1_async captures every S-1 filed,
    market-wide, with the CIK and the filing URL; FMP's ipos-calendar carries the
    deal terms but "no per-deal identifier or URL" (see process_ipo). Joining
    them gives the alert a real prospectus link plus the CIK, instead of a second
    live round trip to FMP that may resolve nothing.

    Joined on COMPANY NAME, not ticker: an S-1 registrant is pre-listing, so
    ticker_from_cik() cannot resolve it and these rows carry ticker="UNKNOWN" by
    construction. Filtered server-side on the most distinctive token so this
    reads a handful of rows rather than every S-1 of the last year.
    """
    tokens = [w for w in _norm_company(company_name).split() if len(w) >= 4]
    if not tokens:
        return None
    anchor = max(tokens, key=len)

    try:
        cutoff = (date.today() - timedelta(days=S1_LOOKBACK_DAYS)).isoformat()
        rows = (get_supabase().table("raw_filings")
                .select("company_name, filing_url, extra, filed_at")
                .eq("source", "SEC_IPO")
                .ilike("company_name", f"%{anchor}%")
                .gte("filed_at", cutoff)
                .order("filed_at", desc=True)
                .limit(25)
                .execute()).data or []
    except Exception as e:
        # A merge failure must not cost the alert. The FMP-only path below still
        # produces a complete, correct IPO alert.
        logger.warning("[IPO] S-1 lookup failed for %s: %s", company_name, e)
        return None

    for row in rows:
        if _names_match(company_name, row.get("company_name")):
            extra = row.get("extra") or {}
            return {
                "url": row.get("filing_url"),
                "cik": extra.get("cik"),
                "sec_json": extra.get("sec_json"),
                "filed_at": row.get("filed_at"),
                "company_name": row.get("company_name"),
            }
    return None


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
        f"🏦 IPO Alert — Upcoming Listing\n\n"
        f"Company: {name}\n"
        f"Ticker: ${ticker}\n"
        f"Exchange: {exchange}\n"
        f"Listing Date: {start_date} ({timing})\n"
        f"Price Range: {price_str}\n"
        f"Shares Offered: {shares_str}\n"
        f"Status: {actions if actions else 'N/A'}\n"
        f"_Source: FMP IPO Calendar | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    # ── The S-1 merge ─────────────────────────────────────────────────────────
    # FMP's ipos-calendar carries no per-deal identifier or URL, so the
    # registration statement is the only per-company document available. Two
    # ways to get it, in order of reliability:
    #
    #   1. OUR OWN CAPTURE. poll_sec_s1_async already reads every S-1 filed,
    #      market-wide, and stores the CIK and filing URL. That row is the
    #      registrant's actual filing, matched by name.
    #   2. FMP's SEC-filings endpoint, keyed on the ticker. Only works once FMP
    #      has associated the symbol with the filing, which for a pre-listing
    #      company is often not yet true — this was the sole path before, which
    #      is why IPO alerts frequently shipped with no link at all.
    captured = find_captured_s1(name)
    extra = {
        "name": name, "exchange": exchange, "start_date": start_date,
        "price_from": price_from, "price_to": price_to,
        "shares": shares, "market_cap": market_cap, "actions": actions,
        "signature": signature,
    }

    if captured and captured.get("url"):
        s1_url = captured["url"]
        extra["s1_source"] = "edgar_capture"
        if captured.get("cik"):
            extra["cik"] = captured["cik"]
        if captured.get("sec_json"):
            extra["sec_json"] = captured["sec_json"]
        if captured.get("filed_at"):
            extra["s1_filed_at"] = captured["filed_at"]

        # The structured `ipo` payload, for payload_log and the frontend that
        # will eventually render it instead of a text summary. Built only on
        # this branch because it needs the CIK and the real filing date, which
        # only our own capture supplies — the FMP-only path has neither, and a
        # payload with invented values is worse than no payload.
        extra["structured_payload"] = s1_to_ipo(
            company_name=name,
            ticker=ticker,
            price_range=price_str if price_str != "TBD" else None,
            shares=shares_str if shares_str != "TBD" else None,
            deal_size=f"${float(market_cap) / 1_000_000_000:.2f}B" if market_cap else None,
            listing_date=start_date,
            form_link=s1_url,
            cik=captured.get("cik") or "",
            filing_date=captured.get("filed_at"),
        )
        logger.info("[IPO] %s matched our captured S-1 (CIK %s)",
                    ticker, captured.get("cik") or "?")
    else:
        s1_url = edgar_link.find_filing_url(
            ticker, form_type="S-1", target_date=start_date, window_days=-1
        ) or edgar_link.find_filing_url(
            ticker, form_type="S-1",
            target_date=(date.today() - timedelta(days=120)).isoformat(),
            window_days=130
        )
        if s1_url:
            extra["s1_source"] = "fmp_lookup"

    if not s1_url:
        # A registrant whose S-1 we could not resolve still has an EDGAR
        # presence, and that page is where the S-1 appears once it is filed.
        # Without this the whole feature shipped unlinked whenever the lookup
        # missed — which, for a company that has not started trading, is most
        # of the time.
        s1_url = ("https://www.sec.gov/cgi-bin/browse-edgar"
                  f"?action=getcompany&company={quote_plus(name or ticker)}"
                  "&type=S-1&dateb=&owner=include&count=40")
        extra["s1_source"] = "edgar_search"

    save_alert(ticker, summary, impact, extra, link=s1_url)


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
