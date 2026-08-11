"""
alert_formatter.py
GQ FinXray US — turns an `alerts` row into the message a user actually reads.

Quality decisions made here, and why:

1. HTML parse mode, not Markdown. Telegram's legacy Markdown breaks on a single
   unmatched `*` or `_` — and company names ("AT&T Inc.", "Jones_Lang") and
   LLM-written summaries produce those constantly. A broken parse means the API
   rejects the whole message and the alert is silently lost. HTML needs only
   `& < >` escaped, which is deterministic.

2. Every alert leads with a headline. The India system's alerts read as
   "News Flash: Bank Addresses Lawsuits, Governance, and Post-Merger Outlook"
   — a scannable title above the paragraph. The US alerts had no equivalent;
   they opened straight into a wall of summary. ai_pipeline.py now generates a
   headline (Prompt_H1) and stores it in extra.headline; this renders it.

3. Company name, not just the ticker. "$HDFCBANK — HDFC Bank Limited" reads as
   a real alert; "$HDFCBANK" alone reads as a database row. company_name is
   already carried on raw_filings and is now propagated into alerts.extra.

4. Live price with direction. Fetched once per ticker per minute via a TTL cache
   — without the cache, a burst of 50 alerts for the same ticker would fire 50
   identical FMP quote calls.

5. A source link when one exists. An alert the reader cannot verify is a rumour.
"""

import html
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import fmp_client
from feature_map import feature_footer, resolve_feature

ET = ZoneInfo("America/New_York")

DISCLAIMER_URL = "https://gquants.com/disclaimer"
MANAGE_URL = "https://gquants.com/build"

IMPACT_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}

SOURCE_LABELS = {
    "SEC_EDGAR": "SEC EDGAR",
    "FMP_NEWS": "FMP News",
    "CNBC": "CNBC",
    "REUTERS": "Reuters",
    "MARKETWATCH": "MarketWatch",
    "BLOOMBERG": "Bloomberg",
    "NASDAQ": "Nasdaq",
    "IBD": "Investor's Business Daily",
    "FORTUNE": "Fortune",
    "CNN": "CNN Business",
    "FMP": "FMP Fundamentals",
    "FMP_FUNDAMENTALS": "FMP Fundamentals",
    "TECHNICAL": "Technical (Massive/FMP)",
    "FMP_IPO": "FMP IPO Calendar",
    "FMP_TRANSCRIPT": "Earnings Call Transcript",
    "FMP_ANALYST": "Analyst Consensus (FMP)",
    "ETF_FLOW": "ETF Flow (Massive)",
    "ETF_XRAY": "ETF Xray",
    "SECTOR_HEATMAP": "Sector Heatmap",
    "MACRO_ROUNDUP": "Macro & Policy",
    "LARGE_TRADE": "Large Trade",
}

FILING_TYPE_LABELS = {
    "8-K":  "8-K Current Report",
    "10-Q": "10-Q Quarterly Report",
    "10-K": "10-K Annual Report",
    "S-1":  "S-1 Registration",
    "4":    "Form 4 Insider Transaction",
    "NEWS": "News",
    "RESULT_SNAPSHOT": "Quarterly Results",
    "EARNINGS_CALENDAR": "Earnings Calendar",
    "EARNINGS_TRANSCRIPT": "Earnings Call",
    "INSIDER_FMP": "Insider Transaction",
    "BULK_DEAL": "Large Block Trade",
    "LARGE_TRADE": "Large Trade",
    "ANALYST_RATING": "Analyst Rating",
    "IPO_UPCOMING": "Upcoming IPO",
    "MACRO_BRIEFING": "Macro Digest",
    "RSI_OVERBOUGHT": "RSI Overbought",
    "RSI_OVERSOLD": "RSI Oversold",
    "52W_HIGH": "52-Week High",
    "52W_LOW": "52-Week Low",
    "VOLUME_SPIKE": "Volume Spike",
    "SMA200_CROSSOVER_UP": "200-SMA Crossover Up",
    "SMA200_CROSSOVER_DOWN": "200-SMA Crossover Down",
    "INFLOW": "ETF Inflow",
    "OUTFLOW": "ETF Outflow",
}


# ── Quote cache ───────────────────────────────────────────────────────────────
# A burst of alerts for one ticker (an 8-K, a Form 4 and a news item landing in
# the same delivery cycle) would otherwise fire one FMP quote call each. 60s TTL
# is well inside the useful lifetime of a price shown on an alert.
_QUOTE_TTL_SECONDS = 60
_quote_cache = {}


def _cached_quote(ticker):
    now = time.monotonic()
    hit = _quote_cache.get(ticker)
    if hit and (now - hit[0]) < _QUOTE_TTL_SECONDS:
        return hit[1]
    try:
        q = fmp_client.get_quote(ticker)
    except Exception:
        q = None
    _quote_cache[ticker] = (now, q)
    return q


def _price_line(ticker):
    """'📈 $214.29  🟢 +1.24%' or '' when the price is unavailable."""
    if not ticker or ticker.upper() in ("MARKET", "UNKNOWN", ""):
        return ""
    q = _cached_quote(ticker)
    if not q or q.get("price") in (None, ""):
        return ""
    try:
        price = float(q["price"])
        chg = float(q.get("changePercentage") or 0)
    except (TypeError, ValueError):
        return ""
    arrow = "🟢" if chg >= 0 else "🔴"
    sign = "+" if chg >= 0 else ""
    return f"📈 <b>${price:,.2f}</b>  {arrow} {sign}{chg:.2f}%"


def esc(text):
    """Escape for Telegram HTML parse mode. Only & < > are special."""
    return html.escape(str(text or ""), quote=False)


def _source_link(extra):
    """Find whatever URL the poller stored, under any of the keys used across pollers."""
    for key in ("url", "filing_url", "source_url", "link", "article_url"):
        val = (extra or {}).get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return None


def build_message(alert, reason=None):
    """
    Render one alert row into Telegram HTML.

    `reason` is the one-line explanation of why THIS user is receiving it —
    "AAPL is on your watchlist" vs "market-wide alert". Telling the reader why
    they got something is the difference between an alert and spam.
    """
    ticker      = (alert.get("ticker") or "UNKNOWN").upper()
    impact      = (alert.get("impact") or "LOW").upper()
    summary     = alert.get("summary") or ""
    source      = alert.get("source") or ""
    filing_type = alert.get("filing_type") or ""
    extra       = alert.get("extra") or {}

    company  = extra.get("company_name") or extra.get("company") or ""
    headline = extra.get("headline") or extra.get("title") or ""

    emoji        = IMPACT_EMOJI.get(impact, "🟢")
    source_name  = SOURCE_LABELS.get(source, source.replace("_", " ").title())
    type_label   = FILING_TYPE_LABELS.get(filing_type, filing_type.replace("_", " ").title())
    time_str     = datetime.now(ET).strftime("%I:%M %p ET").lstrip("0")

    lines = []

    # ── Header: impact · ticker — company ────────────────────────────────────
    if ticker in ("MARKET", "UNKNOWN"):
        lines.append(f"{emoji} <b>{esc(type_label)}</b>")
    else:
        head = f"{emoji} <b>{impact}</b> · <b>${esc(ticker)}</b>"
        if company and company.upper() != ticker:
            head += f" — {esc(company)}"
        lines.append(head)
        price = _price_line(ticker)
        if price:
            lines.append(price)

    # ── Headline ─────────────────────────────────────────────────────────────
    if headline:
        lines.append("")
        lines.append(f"<b>{esc(headline)}</b>")

    # ── Body ─────────────────────────────────────────────────────────────────
    if summary:
        lines.append("")
        lines.append(esc(summary))

    # ── Provenance ───────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"📋 {esc(source_name)} · {esc(type_label)} · {time_str}")

    url = _source_link(extra)
    if url:
        lines.append(f'🔗 <a href="{esc(url)}">View source</a>')

    # ── Footer ───────────────────────────────────────────────────────────────
    lines.append("")
    if reason:
        lines.append(f"<i>{esc(reason)}</i>")
    lines.append(f'<i>Disclaimer: <a href="{DISCLAIMER_URL}">gquants.com/disclaimer</a></i>')
    lines.append(f'📊 <a href="{MANAGE_URL}">Manage your watchlist</a>')
    lines.append(f"🏷 {esc(feature_footer(source, filing_type))}")

    msg = "\n".join(lines)

    # Telegram hard-caps a message at 4096 characters. Truncate the body rather
    # than let the API reject the whole alert.
    if len(msg) > 4000:
        msg = msg[:3990] + "\n…"
    return msg


def delivery_reason(alert):
    """The 'why am I getting this' line — always watchlist-based."""
    ticker = (alert.get("ticker") or "").upper()
    return f"You're receiving this because {ticker} is on your watchlist."
