"""
feature_map.py
GQ FinXray US — the 11 features, in one place.

Every alert stored in Supabase (`alerts.extra.feature_id` /
`extra.feature_name`) and every Telegram message gets tagged with exactly
one of these, so alert performance can be tested/monitored feature-by-
feature (this is the whole point — requested so testing can tell which of
the 11 features a given alert came from at a glance).

Mapping is keyed by (source, filing_type) since that's what every poller
already stores. `resolve_feature()` does a best-effort match; if nothing
matches it falls back to feature 0 ("Unmapped") rather than crashing —
better to see "Unmapped" in a test channel than lose the alert.
"""

FEATURES = {
    1: {
        "name": "SEC EDGAR Filings",
        "detail": "8-K, 10-Q, 10-K, S-1, Form 4 — real-time SEC EDGAR RSS polling.",
        "sources": {"SEC_EDGAR"},
        "filing_types": {"8-K", "10-Q", "10-K", "S-1", "4"},
    },
    2: {
        "name": "Company & Sector News",
        "detail": "Ticker/sector news (FMP) + CNBC/MarketWatch/Bloomberg RSS aggregation.",
        "sources": {"FMP_NEWS", "CNBC", "REUTERS", "MARKETWATCH", "NASDAQ", "IBD", "FORTUNE", "CNN", "BLOOMBERG"},
        "filing_types": {"NEWS"},
    },
    3: {
        "name": "Result Snapshot",
        "detail": "Structured quarterly/annual financials triggered off 10-Q/10-K filings.",
        "sources": {"FMP", "FMP_FUNDAMENTALS"},
        "filing_types": {"RESULT_SNAPSHOT"},
    },
    4: {
        "name": "Earnings Calendar Heads-Up",
        "detail": "24h-ahead earnings date/time + EPS estimate alert for watchlisted tickers.",
        "sources": {"FMP_NEWS", "FMP"},
        "filing_types": {"EARNINGS_CALENDAR"},
    },
    5: {
        "name": "Insider Transactions & Large Deals",
        "detail": "FMP insider-trading feed (exec/board buy-sell) + bulk/block deal flags ($1M+). "
                  "Note: raw SEC Form 4 filings themselves resolve under Feature 1 (SEC EDGAR "
                  "Filings), since they're keyed source=SEC_EDGAR, not source=FMP_NEWS/FMP.",
        "sources": {"FMP_NEWS", "FMP"},
        "filing_types": {"INSIDER_FMP", "BULK_DEAL"},
    },
    6: {
        "name": "Technical Alerts",
        "detail": "RSI overbought/oversold, 52-week high/low, volume spike, 200-SMA crossover.",
        "sources": {"TECHNICAL"},
        "filing_types": {
            "RSI_OVERBOUGHT", "RSI_OVERSOLD", "52W_HIGH", "52W_LOW",
            "VOLUME_SPIKE", "SMA200_CROSSOVER_UP", "SMA200_CROSSOVER_DOWN",
        },
    },
    7: {
        "name": "ETF Flow Alerts",
        "detail": "Institutional inflow/outflow signal from ETF volume + price-move thresholds.",
        "sources": {"ETF_FLOW"},
        "filing_types": {"INFLOW", "OUTFLOW"},
    },
    8: {
        "name": "IPO Deep Dive",
        "detail": "Upcoming US IPO alerts — pricing, share count, deal size, listing date.",
        "sources": {"FMP_IPO"},
        "filing_types": {"IPO_UPCOMING"},
    },
    9: {
        "name": "Sector Heatmap",
        "detail": "Daily/weekly/monthly S&P 500 GICS sector ETF heatmap image.",
        "sources": {"SECTOR_HEATMAP"},
        "filing_types": {"HEATMAP_DAILY_09", "HEATMAP_DAILY_13", "HEATMAP_WEEKLY", "HEATMAP_MONTHLY"},
    },
    10: {
        "name": "News Roundup & ETF Xray",
        "detail": "Morning/evening AI news digest + structured ETF fundamentals snapshot.",
        "sources": {"NEWS_ROUNDUP", "ETF_XRAY"},
        "filing_types": {"MORNING_ROUNDUP", "EVENING_ROUNDUP", "ETF_XRAY"},
    },
    11: {
        "name": "Earnings Call Transcripts",
        "detail": "NEW — full transcript pulled via FMP when EDGAR flags a 10-Q/10-K, "
                  "AI-summarized through the same S.1/S.3/V.1 pipeline as news & filings.",
        "sources": {"FMP_TRANSCRIPT"},
        "filing_types": {"EARNINGS_TRANSCRIPT"},
    },
}

UNMAPPED = {"name": "Unmapped", "detail": "Did not match a known source/filing_type pair."}


def resolve_feature(source, filing_type):
    """Returns (feature_id, feature_name) for a given alert's source + filing_type."""
    source = (source or "").upper()
    filing_type = filing_type or ""
    for fid, info in FEATURES.items():
        if source in info["sources"] and (not info["filing_types"] or filing_type in info["filing_types"]):
            return fid, info["name"]
    # Second pass: match on source alone, but ONLY when that source belongs to
    # exactly one feature. Several sources (e.g. "FMP_NEWS", "FMP") are shared
    # across multiple features distinguished solely by filing_type -- if we
    # guessed here for a shared source, an unrecognized/typo'd filing_type
    # would silently resolve to whichever feature happens to iterate first in
    # dict order, instead of surfacing as "Unmapped" the way this module's own
    # docstring promises. Only fall back when the source is unambiguous.
    candidates = [fid for fid, info in FEATURES.items() if source in info["sources"]]
    if len(candidates) == 1:
        fid = candidates[0]
        return fid, FEATURES[fid]["name"]
    return 0, UNMAPPED["name"]


def tag_extra(extra, source, filing_type):
    """Merge feature_id/feature_name into an alert's extra dict (does not mutate input)."""
    fid, fname = resolve_feature(source, filing_type)
    merged = dict(extra or {})
    merged["feature_id"] = fid
    merged["feature_name"] = fname
    return merged


def feature_footer(source, filing_type):
    """One-line footer appended to every Telegram alert for quick visual testing."""
    fid, fname = resolve_feature(source, filing_type)
    if fid == 0:
        return "🏷 Feature: Unmapped"
    return f"🏷 Feature {fid}/11 — {fname}"


if __name__ == "__main__":
    for src, ft in [("SEC_EDGAR", "8-K"), ("TECHNICAL", "RSI_OVERBOUGHT"),
                    ("FMP_TRANSCRIPT", "EARNINGS_TRANSCRIPT"), ("BOGUS", "X")]:
        print(src, ft, "->", resolve_feature(src, ft))
