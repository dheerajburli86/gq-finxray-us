"""
market_reports.py
GQ FinXray US — the five scheduled market reports, extracted from main.py.

WHAT CHANGED IN THE EXTRACTION
------------------------------
The old versions in main.py each built a Markdown string and pushed it straight
to one shared Telegram channel via `asyncio.run(...)` from inside a scheduler
thread. That bypassed every routing, preference and rate-limit rule the delivery
layer exists to enforce, and it meant a user who had turned market-wide messages
off still got five of them a day.

Each function now writes exactly ONE row to `alerts`:

    source="MACRO_ROUNDUP", filing_type="MACRO_BRIEFING",
    ticker="MARKET", impact="MEDIUM", delivered=False

feature_map resolves that pair to feature 13, which is market_wide, so
delivery.py fans it out to active users who have `receive_market_wide` on —
and to nobody else. Nothing here imports telegram.

WHY THERE IS NO 6,353-TICKER LOOP
---------------------------------
"Top movers" is the obvious place to accidentally quote the whole stocks table.
FMP's company-screener cannot rank by intraday change (it returns price and
volume, not change%), so the universe is built once per day from the screener's
large-cap, actively-traded results, capped at MOVERS_UNIVERSE_SIZE, and only
that capped set gets quoted. If the screener is unavailable the universe falls
back to a fixed mega-cap list. Either way the per-report cost is bounded and
known, not proportional to the size of the stocks table.

SAFE TO SCHEDULE
----------------
Every function swallows its own exceptions and logs to poller_error_log. None of
them raise, so `schedule.run_pending()` can never be unwound by one of them even
if main.py's safe_job wrapper were removed.
"""

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ET = ZoneInfo("America/New_York")

SOURCE = "MACRO_ROUNDUP"
FILING_TYPE = "MACRO_BRIEFING"

INDICES = [
    ("^GSPC", "S&P 500", "SPY"),
    ("^IXIC", "Nasdaq Composite", "QQQ"),
    ("^DJI", "Dow Jones Industrial", "DIA"),
]

COMMODITIES = [
    ("GCUSD", "Gold"),
    ("CLUSD", "WTI Crude"),
    ("NGUSD", "Natural Gas"),
]

# Bounded on purpose — see the module docstring. 30 quotes per report is ~150
# FMP calls a day across all five reports, which is predictable and small.
MOVERS_UNIVERSE_SIZE = 30
MOVERS_SHOWN = 5

# Used when the screener is unavailable. Deliberately mega-cap and liquid: these
# are the names a "top movers" line is expected to be drawn from anyway.
FALLBACK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B",
    "LLY", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ", "ABBV",
    "WMT", "NFLX", "CRM", "AMD", "BAC", "KO", "PEP", "ADBE", "CVX", "ORCL",
]


# ── Universe (cached for a day) ───────────────────────────────────────────────
_universe_cache = {"date": None, "tickers": None}


def _movers_universe():
    """
    Large-cap actively-traded tickers to quote for the movers sections.
    Rebuilt at most once per ET calendar day.
    """
    today = datetime.now(ET).date()
    if _universe_cache["date"] == today and _universe_cache["tickers"]:
        return _universe_cache["tickers"]

    tickers = []
    try:
        rows = fmp_client.screener({
            "marketCapMoreThan": 50_000_000_000,
            "volumeMoreThan": 1_000_000,
            "priceMoreThan": 5,
            "isActivelyTrading": "true",
            "country": "US",
            "limit": 200,
        })
        # The screener has no sort parameter, so rank locally by market cap and
        # keep the head of the list.
        rows = [r for r in (rows or [])
                if r.get("symbol") and not r.get("isEtf") and not r.get("isFund")]
        rows.sort(key=lambda r: float(r.get("marketCap") or 0), reverse=True)
        tickers = [r["symbol"] for r in rows[:MOVERS_UNIVERSE_SIZE]]
    except Exception as e:
        logger.warning("[REPORTS] Screener unavailable (%s); using fallback universe", e)

    if not tickers:
        tickers = list(FALLBACK_UNIVERSE[:MOVERS_UNIVERSE_SIZE])

    _universe_cache.update({"date": today, "tickers": tickers})
    return tickers


# ── Quote helpers ─────────────────────────────────────────────────────────────
def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value):
    v = _num(value)
    if v is None:
        return ""
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _quote(symbol):
    try:
        q = fmp_client.get_quote(symbol)
    except Exception as e:
        logger.warning("[REPORTS] Quote failed for %s: %s", symbol, e)
        return None
    if not q:
        return None
    price = _num(q.get("price"))
    if price is None or price == 0:
        return None
    return {"price": price, "change_pct": _num(q.get("changePercentage"))}


def _index_block():
    """(lines, data) for the three headline indices, ETF proxy as fallback."""
    lines, data = [], {}
    for symbol, name, proxy in INDICES:
        q = _quote(symbol)
        label = name
        if not q and proxy:
            q = _quote(proxy)
            label = f"{name} (via {proxy})"
        if not q:
            continue
        lines.append(f"- {label}: {q['price']:,.2f} ({_pct(q['change_pct'])})")
        data[name] = {"price": round(q["price"], 2), "change_pct": q["change_pct"]}
    return lines, data


def _commodity_block():
    lines, data = [], {}
    for symbol, name in COMMODITIES:
        q = _quote(symbol)
        if not q:
            continue
        lines.append(f"- {name}: ${q['price']:,.2f} ({_pct(q['change_pct'])})")
        data[name] = {"price": round(q["price"], 2), "change_pct": q["change_pct"]}
    return lines, data


def _movers_block():
    """
    (gainer_lines, loser_lines, data). One quote per universe ticker, bounded by
    MOVERS_UNIVERSE_SIZE. Tickers with no change data are dropped rather than
    treated as 0.00%, which would otherwise pad the "losers" list with names
    that simply failed to fetch.
    """
    universe = _movers_universe()
    rows = []
    for ticker in universe:
        q = _quote(ticker)
        if q and q["change_pct"] is not None:
            rows.append({"ticker": ticker, "price": q["price"],
                         "change_pct": q["change_pct"]})
        # Sleep on every iteration, including failures — a run of 404s should
        # not turn into an unthrottled burst against the API.
        time.sleep(0.05)

    if not rows:
        return [], [], {}

    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    gainers = [r for r in rows if r["change_pct"] > 0][:MOVERS_SHOWN]
    losers = [r for r in reversed(rows) if r["change_pct"] < 0][:MOVERS_SHOWN]

    g_lines = [f"- {r['ticker']}: {_pct(r['change_pct'])} at ${r['price']:,.2f}" for r in gainers]
    l_lines = [f"- {r['ticker']}: {_pct(r['change_pct'])} at ${r['price']:,.2f}" for r in losers]
    return g_lines, l_lines, {"gainers": gainers, "losers": losers,
                              "universe_size": len(universe), "quoted": len(rows)}


# ── Headlines ─────────────────────────────────────────────────────────────────
def _direction(pct, up_word, down_word, flat_word):
    if pct is None:
        return None
    if pct >= 0.25:
        return up_word
    if pct <= -0.25:
        return down_word
    return flat_word


def _headline(report_type, index_data, has_movers):
    """
    4–10 words, Title Case, no hype — derived from the S&P move when it was
    retrieved, and from a neutral description of the report when it was not.
    """
    sp = (index_data.get("S&P 500") or {}).get("change_pct")

    if report_type == "PREMARKET":
        word = _direction(sp, "Higher", "Lower", "Little Changed")
        return (f"Indices Indicated {word} Ahead Of The Open" if word
                else "Pre-Market Snapshot Ahead Of The Open")

    if report_type == "MARKET_OPEN":
        word = _direction(sp, "Higher", "Lower", "Little Changed")
        return f"Stocks Open {word} In Early Trading" if word else "Opening Bell Market Snapshot"

    if report_type == "MIDDAY":
        word = _direction(sp, "Higher", "Lower", "Little Changed")
        return f"Stocks Trade {word} At Midday" if word else "Midday Market Pulse Check"

    if report_type == "MARKET_CLOSE":
        word = _direction(sp, "Higher", "Lower", "Little Changed")
        return f"Stocks Close {word} On The Session" if word else "Closing Bell Market Summary"

    if report_type == "AFTERHOURS":
        return ("Notable Movers In After Hours Trading" if has_movers
                else "After Hours Session Snapshot")

    return "Market Report"


# ── Storage ───────────────────────────────────────────────────────────────────
def _looks_like_a_closed_market(index_data):
    """
    Every index returning exactly 0.00% is not a flat tape — on a live session
    the odds of three indices printing an exact zero change are nil. It means
    stale quotes: a weekend, a market holiday, or a feed serving last close. In
    that case there is no report worth sending.
    """
    if not index_data:
        return False
    changes = [v.get("change_pct") for v in index_data.values()]
    return bool(changes) and all(c == 0 for c in changes)


def _store(title, body_sections, extra):
    """
    Write one market-wide alert. `body_sections` is [(heading, [lines]), ...];
    empty sections are dropped. Returns True if a row was written.
    """
    populated = [(h, lines) for h, lines in body_sections if lines]
    if not populated:
        logger.error("[REPORTS] %s — no data in any section; nothing written.", title)
        return False

    stamp = datetime.now(ET).strftime("%B %d, %Y · %I:%M %p ET").replace(" 0", " ")
    lines = [f"{title} — {stamp}", ""]
    for heading, section in populated:
        lines.append(heading)
        lines.extend(section)
        lines.append("")
    summary = "\n".join(lines).strip()

    payload = dict(extra)
    payload["sections_populated"] = [h for h, _ in populated]
    payload["generated_at"] = datetime.now(ET).isoformat()

    try:
        supabase.table("alerts").insert({
            "ticker": "MARKET",
            "summary": summary,
            "impact": "MEDIUM",
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "extra": tag_extra(payload, SOURCE, FILING_TYPE),
            "delivered": False,
            "filing_url": None,
        }).execute()
        logger.info("[REPORTS] %s stored (%d sections)", title, len(populated))
        return True
    except Exception as e:
        logger.error("[REPORTS] Failed to store %s: %s", title, e)
        log_poller_error("market_reports", extra.get("report_type", "report"), e,
                         {"title": title})
        return False


def _guard(report_type):
    """Decorator-free helper: wraps a report body so a scheduler can call it."""
    def wrap(fn):
        def inner():
            try:
                return fn()
            except Exception as e:
                logger.exception("[REPORTS] %s failed: %s", report_type, e)
                log_poller_error("market_reports", report_type, e)
                return False
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


# ── The five reports ──────────────────────────────────────────────────────────
@_guard("PREMARKET")
def send_premarket_report():
    """09:25 ET — where the tape and macro backdrop stand before the open."""
    index_lines, index_data = _index_block()
    cmdty_lines, cmdty_data = _commodity_block()

    if _looks_like_a_closed_market(index_data):
        logger.info("[REPORTS] Pre-market — quotes look stale (market closed); skipping.")
        return False

    return _store("Pre-Market Report", [
        ("Indices", index_lines),
        ("Commodities", cmdty_lines),
    ], {
        "report_type": "PREMARKET",
        "indices": index_data,
        "commodities": cmdty_data,
        "headline": _headline("PREMARKET", index_data, False),
    })


@_guard("MARKET_OPEN")
def send_market_open_report():
    """09:35 ET — first read of the session plus the earliest movers."""
    index_lines, index_data = _index_block()
    gainers, losers, movers_data = _movers_block()

    if _looks_like_a_closed_market(index_data):
        logger.info("[REPORTS] Market open — quotes look stale (market closed); skipping.")
        return False

    return _store("Market Open", [
        ("Indices At The Open", index_lines),
        ("Early Gainers", gainers),
        ("Early Losers", losers),
    ], {
        "report_type": "MARKET_OPEN",
        "indices": index_data,
        "movers": movers_data,
        "headline": _headline("MARKET_OPEN", index_data, bool(gainers or losers)),
    })


@_guard("MIDDAY")
def send_midday_report():
    """12:30 ET — halfway checkpoint."""
    index_lines, index_data = _index_block()
    gainers, losers, movers_data = _movers_block()

    if _looks_like_a_closed_market(index_data):
        logger.info("[REPORTS] Midday — quotes look stale (market closed); skipping.")
        return False

    return _store("Midday Pulse", [
        ("Indices", index_lines),
        ("Top Gainers", gainers),
        ("Top Losers", losers),
    ], {
        "report_type": "MIDDAY",
        "indices": index_data,
        "movers": movers_data,
        "headline": _headline("MIDDAY", index_data, bool(gainers or losers)),
    })


@_guard("MARKET_CLOSE")
def send_market_close_report():
    """16:05 ET — final levels, the day's extremes, and the macro backdrop."""
    index_lines, index_data = _index_block()
    gainers, losers, movers_data = _movers_block()
    cmdty_lines, cmdty_data = _commodity_block()

    if _looks_like_a_closed_market(index_data):
        logger.info("[REPORTS] Close — quotes look stale (market closed); skipping.")
        return False

    return _store("Market Close Report", [
        ("Final Index Levels", index_lines),
        ("Top Gainers", gainers),
        ("Top Losers", losers),
        ("Commodities", cmdty_lines),
    ], {
        "report_type": "MARKET_CLOSE",
        "indices": index_data,
        "movers": movers_data,
        "commodities": cmdty_data,
        "headline": _headline("MARKET_CLOSE", index_data, bool(gainers or losers)),
    })


@_guard("AFTERHOURS")
def send_afterhours_report():
    """
    16:45 ET — movers only.

    No stale-quote guard here: after the close every index legitimately shows the
    session's final change, so the all-zero test would not fire anyway. Instead
    the report is simply not written when no mover data came back.
    """
    gainers, losers, movers_data = _movers_block()
    if not gainers and not losers:
        logger.info("[REPORTS] After-hours — no mover data; nothing written.")
        return False

    return _store("After-Hours Movers", [
        ("Gainers", gainers),
        ("Losers", losers),
    ], {
        "report_type": "AFTERHOURS",
        "movers": movers_data,
        "headline": _headline("AFTERHOURS", {}, True),
    })


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    which = sys.argv[1] if len(sys.argv) > 1 else "midday"
    {
        "premarket": send_premarket_report,
        "open": send_market_open_report,
        "midday": send_midday_report,
        "close": send_market_close_report,
        "afterhours": send_afterhours_report,
    }.get(which, send_midday_report)()
