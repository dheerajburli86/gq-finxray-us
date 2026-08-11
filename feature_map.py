"""
feature_map.py
GQ FinXray US — the feature registry, in one place.

Every alert stored in Supabase (`alerts.extra.feature_id` / `extra.feature_name`)
and every Telegram message gets tagged with exactly one feature, so alert
quality can be monitored feature-by-feature.

Mapping is keyed by (source, filing_type) since that is what every poller
already stores. `resolve_feature()` does a best-effort match; if nothing matches
it falls back to feature 0 ("Unmapped") rather than crashing.

WATCHLIST-ONLY DELIVERY
-______________________
All features are now watchlist-scoped. The `is_market_wide()` function is kept for
backwards compatibility but always returns False. Users only receive alerts for
companies on their watchlist — there are no broadcast features. Alerts about IPOs,
sector heatmaps, macro digests, and ETF flows are dropped if the ticker is not
watchlisted.
"""

FEATURES = {
    1: {
        "name": "SEC EDGAR Filings",
        "detail": "8-K, 10-Q, 10-K, S-1, Form 4 — real-time SEC EDGAR RSS polling.",
        "sources": {"SEC_EDGAR"},
        "filing_types": {"8-K", "10-Q", "10-K", "S-1", "4"},
        "market_wide": False,
    },
    2: {
        "name": "Company & Sector News",
        "detail": "Ticker/sector news (FMP) + CNBC/MarketWatch/Bloomberg RSS aggregation.",
        "sources": {"FMP_NEWS", "CNBC", "REUTERS", "MARKETWATCH", "NASDAQ",
                    "IBD", "FORTUNE", "CNN", "BLOOMBERG", "YAHOO", "SEEKINGALPHA"},
        "filing_types": {"NEWS"},
        "market_wide": False,
    },
    3: {
        "name": "Result Snapshot",
        "detail": "Structured quarterly/annual financials triggered off 10-Q/10-K filings.",
        "sources": {"FMP", "FMP_FUNDAMENTALS"},
        "filing_types": {"RESULT_SNAPSHOT"},
        "market_wide": False,
    },
    4: {
        "name": "Earnings Calendar Heads-Up",
        "detail": "24h-ahead earnings date/time + EPS estimate alert for watchlisted tickers.",
        "sources": {"FMP_NEWS", "FMP"},
        "filing_types": {"EARNINGS_CALENDAR"},
        "market_wide": False,
    },
    5: {
        "name": "Insider Transactions & Large Trades",
        "detail": "FMP insider-trading feed (exec/board buy-sell) + large block/bulk trades.",
        "sources": {"FMP_NEWS", "FMP", "LARGE_TRADE"},
        "filing_types": {"INSIDER_FMP", "BULK_DEAL", "LARGE_TRADE"},
        "market_wide": False,
    },
    6: {
        "name": "Technical Alerts",
        "detail": "RSI overbought/oversold, 52-week high/low, volume spike, 200-SMA crossover.",
        "sources": {"TECHNICAL"},
        "filing_types": {
            "RSI_OVERBOUGHT", "RSI_OVERSOLD", "52W_HIGH", "52W_LOW",
            "VOLUME_SPIKE", "SMA200_CROSSOVER_UP", "SMA200_CROSSOVER_DOWN",
        },
        "market_wide": False,
    },
    7: {
        "name": "ETF Flow Alerts",
        "detail": "Institutional inflow/outflow signal from ETF volume + price-move thresholds.",
        "sources": {"ETF_FLOW"},
        "filing_types": {"INFLOW", "OUTFLOW"},
        "market_wide": False,
    },
    8: {
        "name": "IPO Deep Dive",
        "detail": "Upcoming US IPO alerts — pricing, share count, deal size, listing date.",
        "sources": {"FMP_IPO"},
        "filing_types": {"IPO_UPCOMING"},
        "market_wide": False,
    },
    9: {
        "name": "Sector Heatmap",
        "detail": "Daily/weekly/monthly S&P 500 GICS sector ETF heatmap image.",
        "sources": {"SECTOR_HEATMAP"},
        "filing_types": {"HEATMAP_DAILY_MIDDAY", "HEATMAP_DAILY_AFTERNOON",
                         "HEATMAP_WEEKLY", "HEATMAP_MONTHLY"},
        "market_wide": False,
    },
    10: {
        "name": "ETF Xray",
        "detail": "Structured ETF fundamentals snapshot — expense ratio, AUM, holdings.",
        "sources": {"ETF_XRAY"},
        "filing_types": {"ETF_XRAY"},
        "market_wide": False,
    },
    11: {
        "name": "Earnings Call Transcripts",
        "detail": "Full transcript pulled via FMP when EDGAR flags a 10-Q/10-K, "
                  "AI-summarized through the same S.1/S.3/V.1 pipeline as news & filings.",
        "sources": {"FMP_TRANSCRIPT"},
        "filing_types": {"EARNINGS_TRANSCRIPT"},
        "market_wide": False,
    },
    12: {
        "name": "Analyst Ratings & Price Targets",
        "detail": "Consensus rating changes and price-target revisions from FMP.",
        "sources": {"FMP_ANALYST"},
        "filing_types": {"ANALYST_RATING", "PRICE_TARGET"},
        "market_wide": False,
    },
    13: {
        "name": "Macro & Policy Digest",
        "detail": "Fed decisions, Treasury yields, jobs/inflation prints, commodities, USD.",
        "sources": {"MACRO_ROUNDUP"},
        "filing_types": {"MACRO_BRIEFING"},
        "market_wide": False,
    },
}

# The personal watchlist heatmap is generated per user and delivered directly by
# watchlist_heatmap.py, so it never goes through the shared alerts fan-out. It is
# listed here only so the footer can name it.
WATCHLIST_HEATMAP_FEATURE = 14
FEATURES[WATCHLIST_HEATMAP_FEATURE] = {
    "name": "Watchlist Heatmap",
    "detail": "Per-user performance heatmap of the stocks on that user's watchlist.",
    "sources": {"WATCHLIST_HEATMAP"},
    "filing_types": {"HEATMAP_WATCHLIST_MIDDAY", "HEATMAP_WATCHLIST_EOD"},
    "market_wide": False,   # personal by construction
}

TOTAL_FEATURES = len(FEATURES)

UNMAPPED = {"name": "Unmapped", "detail": "Did not match a known source/filing_type pair."}


def resolve_feature(source, filing_type):
    """Returns (feature_id, feature_name) for a given alert's source + filing_type."""
    source = (source or "").upper()
    filing_type = filing_type or ""

    for fid, info in FEATURES.items():
        if source in info["sources"] and (not info["filing_types"] or filing_type in info["filing_types"]):
            return fid, info["name"]

    # Second pass: match on source alone, but ONLY when that source belongs to
    # exactly one feature. Several sources ("FMP_NEWS", "FMP") are shared across
    # features distinguished solely by filing_type -- guessing there would let an
    # unrecognized filing_type silently resolve to whichever feature iterates
    # first, instead of surfacing as "Unmapped".
    candidates = [fid for fid, info in FEATURES.items() if source in info["sources"]]
    if len(candidates) == 1:
        fid = candidates[0]
        return fid, FEATURES[fid]["name"]

    return 0, UNMAPPED["name"]


def is_market_wide(source, filing_type):
    """
    True  -> broad market message; deliver to every active user who has not opted
             out of market-wide alerts (there is no ticker to match a watchlist on).
    False -> ticker-scoped; deliver ONLY to users whose watchlist contains the
             alert's ticker.

    Unmapped alerts default to False (ticker-scoped). That is the conservative
    choice: a misrouted ticker alert reaches a smaller audience, whereas
    defaulting to market-wide would broadcast unknown content to everyone.
    """
    fid, _ = resolve_feature(source, filing_type)
    if fid == 0:
        return False
    return bool(FEATURES[fid].get("market_wide", False))


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
        return "Feature: Unmapped"
    return f"Feature {fid} — {fname}"


if __name__ == "__main__":
    checks = [
        ("SEC_EDGAR", "8-K"), ("TECHNICAL", "RSI_OVERBOUGHT"),
        ("FMP_TRANSCRIPT", "EARNINGS_TRANSCRIPT"), ("LARGE_TRADE", "LARGE_TRADE"),
        ("FMP_ANALYST", "ANALYST_RATING"), ("MACRO_ROUNDUP", "MACRO_BRIEFING"),
        ("SECTOR_HEATMAP", "HEATMAP_WEEKLY"), ("FMP_IPO", "IPO_UPCOMING"),
        ("BOGUS", "X"),
    ]
    for src, ft in checks:
        fid, name = resolve_feature(src, ft)
        print(f"{src:18} {ft:24} -> {fid:>2} {name:<38} market_wide={is_market_wide(src, ft)}")
