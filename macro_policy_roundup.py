"""
macro_policy_roundup.py
GQ FinXray US — Feature 13: Macro & Policy Digest.

WHAT THIS EMITS
---------------
One market-wide `alerts` row per run: source="MACRO_ROUNDUP",
filing_type="MACRO_BRIEFING", ticker="MARKET", impact="MEDIUM", delivered=False.
feature_map marks feature 13 market_wide, so delivery.py fans it out to every
active user who has not turned market-wide messages off. This module does not
touch Telegram and does not know who the users are.

DESIGN RULES THIS FILE OBEYS
----------------------------
1. **Every external call is individually guarded.** A macro digest is eight or
   nine independent lookups. If the commodities endpoint is down, the reader
   should still get rates, indices, the calendar and the headlines — losing one
   section is a much better outcome than losing the digest.

2. **Never publish an empty or all-zero digest.** If nothing at all came back,
   the run logs and returns without writing a row. A digest reading "S&P 500:
   $0.00 (0.00%)" is worse than no digest: it looks authoritative and is wrong.

3. **The headline is derived, never invented.** It is composed from the
   direction of the 10-year yield and the S&P, and only from whichever of those
   was actually retrieved. If neither was, the alert ships with no headline
   rather than a fabricated one.

4. **`summary` is plain text.** alert_formatter escapes it for Telegram's HTML
   parse mode, so a literal "<b>" here would render as visible tag characters,
   and a stray "&" in an event name would be double-escaped. No markup.
"""

import os
import logging
from datetime import datetime, timedelta
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

MACRO_KEYWORDS = (
    "fed", "fomc", "federal reserve", "powell", "rate cut", "rate hike",
    "interest rate", "cpi", "inflation", "ppi", "payroll", "jobless",
    "unemployment", "gdp", "pmi", "ism ", "treasury", "yield", "bond market",
    "tariff", "trade deal", "opec", "crude", "oil price", "recession",
    "consumer sentiment", "retail sales", "dollar index", "stimulus",
    "debt ceiling", "government shutdown",
)

MAX_HEADLINES = 6
MAX_CALENDAR_EVENTS = 8


# ── Formatting helpers ────────────────────────────────────────────────────────
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
    """One guarded quote. Returns None on any failure or a zero/blank price."""
    try:
        q = fmp_client.get_quote(symbol)
    except Exception as e:
        logger.warning("[MACRO] Quote failed for %s: %s", symbol, e)
        return None
    if not q:
        return None
    price = _num(q.get("price"))
    if price is None or price == 0:
        return None
    return {"price": price, "change": _num(q.get("change")),
            "change_pct": _num(q.get("changePercentage"))}


# ── Section: rates & volatility ───────────────────────────────────────────────
def _ten_year_yield():
    """
    The 10-year Treasury yield, in percent, plus its day change in percentage
    points. Tries ^TNX first and falls back to the treasury-rates series.

    ^TNX is historically quoted at ten times the yield (42.50 for 4.25%) and
    some feeds still serve it that way, so anything above 20 is divided by ten —
    no US 10-year yield in the modern era is remotely near 20%, which makes that
    a safe discriminator rather than a guess.
    """
    q = _quote("^TNX")
    if q:
        yield_pct, change = q["price"], q["change"]
        if yield_pct > 20:
            yield_pct /= 10.0
            if change is not None:
                change /= 10.0
        return {"yield": yield_pct, "change": change, "source": "^TNX"}

    try:
        today = datetime.now(ET).date()
        rows = fmp_client.get_treasury_rates(
            (today - timedelta(days=10)).isoformat(), today.isoformat())
    except Exception as e:
        logger.warning("[MACRO] Treasury-rates fallback failed: %s", e)
        return None

    # Sort explicitly rather than trusting the endpoint's ordering.
    rows = [r for r in (rows or []) if _num(r.get("year10")) is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)

    latest = _num(rows[0].get("year10"))
    prev = _num(rows[1].get("year10")) if len(rows) > 1 else None
    return {"yield": latest,
            "change": (latest - prev) if prev is not None else None,
            "source": "treasury-rates"}


def _rates_section():
    lines, data = [], {}

    ten_year = _ten_year_yield()
    if ten_year and ten_year["yield"] is not None:
        chg = ten_year["change"]
        chg_txt = f" ({'+' if chg >= 0 else ''}{chg:.2f} pp)" if chg is not None else ""
        lines.append(f"- US 10-Year Treasury: {ten_year['yield']:.2f}%{chg_txt}")
        data["ten_year_yield"] = round(ten_year["yield"], 3)
        if chg is not None:
            data["ten_year_change_pp"] = round(chg, 3)

    vix = _quote("^VIX")
    if vix:
        lines.append(f"- VIX (volatility): {vix['price']:.2f} ({_pct(vix['change_pct'])})")
        data["vix"] = round(vix["price"], 2)
        data["vix_change_pct"] = vix["change_pct"]

    return lines, data


# ── Section: indices ──────────────────────────────────────────────────────────
def _indices_section():
    """
    Index symbols first, matching ETF second. Index coverage (^GSPC etc.) is
    plan-dependent on FMP; the ETFs track the same benchmarks closely enough for
    a directional digest and are available on every tier.
    """
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


# ── Section: commodities ──────────────────────────────────────────────────────
def _commodities_section():
    lines, data = [], {}
    for symbol, name in COMMODITIES:
        q = _quote(symbol)
        if not q:
            continue
        lines.append(f"- {name}: ${q['price']:,.2f} ({_pct(q['change_pct'])})")
        data[name] = {"price": round(q["price"], 2), "change_pct": q["change_pct"]}
    return lines, data


# ── Section: economic calendar ────────────────────────────────────────────────
def _is_us(row):
    country = str(row.get("country") or row.get("currency") or "").strip().upper()
    return country in ("US", "USD", "USA", "UNITED STATES")


def _is_high_impact(row):
    return str(row.get("impact") or "").strip().lower() == "high"


def _fmt_event_time(raw):
    """'2026-08-07 08:30:00' -> 'Aug 07, 08:30 ET'. Falls back to the raw string."""
    text = str(raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
        return dt.strftime("%b %d, %H:%M ET") if fmt != "%Y-%m-%d" else dt.strftime("%b %d")
    return text


def _calendar_section():
    today = datetime.now(ET).date()
    try:
        rows = fmp_client.get_economic_calendar(today.isoformat(),
                                                (today + timedelta(days=1)).isoformat())
    except Exception as e:
        logger.warning("[MACRO] Economic calendar failed: %s", e)
        return [], []

    events = [r for r in (rows or []) if _is_us(r) and _is_high_impact(r)]
    # High-impact US prints are the only ones worth a line in a digest this short.
    # If the feed happened to flag none, fall back to all US events for the day
    # rather than showing an empty calendar section.
    if not events:
        events = [r for r in (rows or []) if _is_us(r)]

    events.sort(key=lambda r: str(r.get("date") or ""))
    events = events[:MAX_CALENDAR_EVENTS]

    lines, payload = [], []
    for e in events:
        name = str(e.get("event") or "").strip()
        if not name:
            continue
        bits = []
        for label, key in (("est.", "estimate"), ("prev.", "previous"), ("actual", "actual")):
            val = e.get(key)
            if val not in (None, "", "null"):
                bits.append(f"{label} {val}")
        detail = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {_fmt_event_time(e.get('date'))} — {name}{detail}")
        payload.append({"event": name, "date": e.get("date"), "impact": e.get("impact"),
                        "estimate": e.get("estimate"), "previous": e.get("previous"),
                        "actual": e.get("actual")})
    return lines, payload


# ── Section: headlines ────────────────────────────────────────────────────────
def _headlines_section():
    try:
        news = fmp_client.get_general_news(limit=50)
    except Exception as e:
        logger.warning("[MACRO] General news failed: %s", e)
        return [], []

    lines, payload, seen = [], [], set()
    for item in news or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        blob = f"{title} {item.get('text') or ''}".lower()
        if not any(k in blob for k in MACRO_KEYWORDS):
            continue
        key = title.lower()[:90]
        if key in seen:
            continue
        seen.add(key)

        publisher = str(item.get("publisher") or item.get("site") or "").strip()
        lines.append(f"- {title}" + (f" ({publisher})" if publisher else ""))
        payload.append({"title": title, "publisher": publisher, "url": item.get("url")})
        if len(lines) >= MAX_HEADLINES:
            break
    return lines, payload


# ── Headline ──────────────────────────────────────────────────────────────────
def _derive_headline(rates_data, index_data):
    """
    4–10 words, Title Case, composed only from figures actually retrieved.
    Returns "" when neither the yield nor the S&P was available — an unheadlined
    digest is fine; a headline asserting a move nobody measured is not.
    """
    yield_chg = rates_data.get("ten_year_change_pp")
    sp = (index_data.get("S&P 500") or {}).get("change_pct")

    yields_phrase = ""
    if yield_chg is not None:
        # 3bp is roughly the daily noise floor on the 10-year; below it, "steady"
        # is the honest description.
        if yield_chg >= 0.03:
            yields_phrase = "Yields Climb"
        elif yield_chg <= -0.03:
            yields_phrase = "Yields Ease"
        else:
            yields_phrase = "Yields Hold Steady"

    stocks_phrase = ""
    if sp is not None:
        if sp >= 0.5:
            stocks_phrase = "Stocks Advance"
        elif sp <= -0.5:
            stocks_phrase = "Stocks Retreat"
        else:
            stocks_phrase = "Stocks Close Little Changed"

    if yields_phrase and stocks_phrase:
        return f"{yields_phrase} As {stocks_phrase}"
    if stocks_phrase:
        return f"{stocks_phrase} Across Major Indices"
    if yields_phrase:
        return f"{yields_phrase} In Treasury Market"
    return ""


# ── Storage ───────────────────────────────────────────────────────────────────
def _already_sent_today():
    """
    Guards against a restart at the scheduled minute producing two digests.
    Measured from ET midnight because the schedule is US market time.
    """
    midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        rows = (supabase.table("alerts")
                .select("id")
                .eq("source", SOURCE)
                .eq("filing_type", FILING_TYPE)
                .eq("ticker", "MARKET")
                .gte("created_at", midnight.isoformat())
                .limit(1)
                .execute()).data or []
        return bool(rows)
    except Exception as e:
        # If the check itself fails, prefer sending once more over going silent.
        logger.warning("[MACRO] Duplicate check failed (%s); proceeding", e)
        return False


def _store(summary, extra):
    # Link to Federal Reserve for macro/policy information
    filing_url = "https://www.federalreserve.gov/newsevents/"

    supabase.table("alerts").insert({
        "ticker": "MARKET",
        "summary": summary,
        "impact": "MEDIUM",
        "source": SOURCE,
        "filing_type": FILING_TYPE,
        "extra": tag_extra(extra, SOURCE, FILING_TYPE),
        "delivered": False,
        "filing_url": filing_url,
    }).execute()


# ── Entry point ───────────────────────────────────────────────────────────────
def run_macro_policy_roundup():
    """Build and store one macro digest. Never raises — a scheduler calls this."""
    try:
        if _already_sent_today():
            logger.info("[MACRO] Digest already published today; skipping.")
            return

        rates_lines, rates_data = _rates_section()
        index_lines, index_data = _indices_section()
        cmdty_lines, cmdty_data = _commodities_section()
        cal_lines, cal_data = _calendar_section()
        news_lines, news_data = _headlines_section()

        sections = [
            ("Rates & Volatility", rates_lines),
            ("Indices", index_lines),
            ("Commodities", cmdty_lines),
            ("On The Calendar", cal_lines),
            ("Headlines", news_lines),
        ]
        populated = [(title, lines) for title, lines in sections if lines]

        if not populated:
            logger.error("[MACRO] Every source came back empty — no digest written. "
                         "Check the FMP key and quota.")
            log_poller_error("macro_policy_roundup", "run_macro_policy_roundup",
                             "All macro sources returned no data; digest suppressed.",
                             {"sections": [t for t, _ in sections]})
            return

        stamp = datetime.now(ET).strftime("%B %d, %Y · %I:%M %p ET").replace(" 0", " ")
        body = [f"Macro & Policy Digest — {stamp}", ""]
        for title, lines in populated:
            body.append(title)
            body.extend(lines)
            body.append("")
        summary = "\n".join(body).strip()

        extra = {
            "report_type": "MACRO_DIGEST",
            "generated_at": datetime.now(ET).isoformat(),
            "rates": rates_data,
            "indices": index_data,
            "commodities": cmdty_data,
            "calendar": cal_data,
            "headlines_list": news_data,
            "sections_populated": [t for t, _ in populated],
        }
        headline = _derive_headline(rates_data, index_data)
        if headline:
            extra["headline"] = headline

        _store(summary, extra)
        logger.info("[MACRO] Digest stored — %d/%d sections populated (%s)",
                    len(populated), len(sections), headline or "no headline")

    except Exception as e:
        logger.exception("[MACRO] Run failed: %s", e)
        log_poller_error("macro_policy_roundup", "run_macro_policy_roundup", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    run_macro_policy_roundup()
