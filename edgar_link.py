"""
edgar_link.py
GQ FinXray US — single source of truth for alert source links.

WHY THIS MODULE EXISTS
----------------------
FMP support confirmed (Aug 2026) two things that decide our entire linking
strategy:

  1. Only the SEC-filings endpoint family returns canonical public URLs.
     Each record carries `link` (EDGAR index page) and `finalLink` (the
     actual document). Every other FMP endpoint is data-only.

  2. There is NO documented, stable public URL scheme for FMP's own website.

So the rule this module enforces is: a source link may only be attached to
an alert when a real document exists behind it. Alerts computed from data
(ETF flow, technical signals, analyst consensus) get NO link rather than a
fabricated one -- a 404 is worse than an absent link, because it destroys
trust in every other link we send.

Historic bug this replaces: several pollers wrote a synthetic dedup key
(e.g. "fmp_insider_AAPL_2026-08-20_John_Smith") into the `filing_url`
column, and downstream rendering turned that into a "View source" link.
That is the direct cause of the 404s on Features 5 and 11.
"""

import logging
from datetime import datetime, timedelta

import fmp_client

logger = logging.getLogger(__name__)

# Features whose alerts are derived from computation, not from a document.
# Listed explicitly so that "no link" is a deliberate, reviewable decision
# rather than an accident of a missing field.
NO_SOURCE_DOCUMENT = {
    "INFLOW", "OUTFLOW",                    # F7  — computed from price/volume
    "EARNINGS_TRANSCRIPT",                  # F11 — FMP-hosted text, no public URL
    "ANALYST_RATING", "PRICE_TARGET",       # F12 — aggregated consensus
    "RSI_OVERBOUGHT", "RSI_OVERSOLD",       # F6  — computed indicators
    "52W_HIGH", "52W_LOW", "VOLUME_SPIKE",
    "SMA200_CROSSOVER_UP", "SMA200_CROSSOVER_DOWN",
    "HEATMAP_DAILY_09", "HEATMAP_DAILY_13", # F9
    "HEATMAP_WEEKLY", "HEATMAP_MONTHLY",
    "MORNING_ROUNDUP", "EVENING_ROUNDUP",   # F10
    "ETF_XRAY", "MACRO_DIGEST",             # F10 / F13
    "WATCHLIST_HEATMAP",                    # F14
}


def is_real_url(value):
    """Guards against synthetic dedup keys reaching the link slot."""
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def has_source_document(filing_type):
    return (filing_type or "").upper() not in NO_SOURCE_DOCUMENT


def find_filing_url(ticker, form_type, target_date=None, window_days=10,
                    prefer_document=False):
    """Resolve the EDGAR URL for a specific filing.

    This is the Form-4 -> accession -> EDGAR pattern FMP recommends, generalised
    to any form type (also used for S-1 on IPO alerts).

    target_date  : the transaction/filing date the alert is about. The closest
                   filing on or after it is chosen, since a Form 4 is filed a
                   day or two after the transaction it reports.
    prefer_document: return `finalLink` (the document itself) instead of `link`
                   (the EDGAR index page). Index page is the default -- it shows
                   the whole submission including exhibits, which is friendlier
                   for a reader arriving from a Telegram alert.

    Returns a URL string, or None. None is a valid outcome and callers MUST
    treat it as "send the alert without a link" rather than substituting a
    guess.
    """
    if not ticker:
        return None

    try:
        if target_date:
            anchor = datetime.strptime(str(target_date)[:10], "%Y-%m-%d")
        else:
            anchor = datetime.utcnow()
    except Exception:
        anchor = datetime.utcnow()

    from_date = (anchor - timedelta(days=2)).strftime("%Y-%m-%d")
    to_date = (anchor + timedelta(days=window_days)).strftime("%Y-%m-%d")

    try:
        filings = fmp_client.get_sec_filings(ticker, from_date, to_date,
                                             form_type=form_type)
    except Exception as e:
        logger.warning(f"[EDGAR LINK] lookup failed for {ticker} {form_type}: {e}")
        return None

    if not filings:
        logger.info(f"[EDGAR LINK] no {form_type} found for {ticker} "
                    f"in {from_date}..{to_date}")
        return None

    # Earliest filing on/after the anchor date is the one the alert describes.
    def sort_key(f):
        return str(f.get("filingDate") or f.get("fillingDate") or "")

    filings = sorted(filings, key=sort_key)

    primary = "finalLink" if prefer_document else "link"
    secondary = "link" if prefer_document else "finalLink"

    for f in filings:
        url = f.get(primary) or f.get(secondary)
        if is_real_url(url):
            return url

    return None
