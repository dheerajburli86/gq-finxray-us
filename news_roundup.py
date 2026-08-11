"""
news_roundup.py
GQ FinXray US — Feature 10, ETF Xray.

The morning/evening AI news digests that used to live here were removed; that
role is now filled by macro_policy_roundup.py (Feature 13). What remains is the
structured ETF fundamentals snapshot.

WHAT CHANGED, AND WHY
---------------------
* **Every external call is guarded.** This function previously made 42
  consecutive unguarded FMP calls plus two unguarded Supabase calls. `fmp_client`
  raises `FMPError` on a persistent 429, `schedule` re-raises whatever a job
  raises, and the scheduler loop had no handler — so one rate-limit burst here
  killed the scheduler thread and silently stopped EVERY poller in the system
  while the process stayed alive and looked healthy. main.py now wraps jobs in
  `safe_job`, but a module that can take down the process on a bad afternoon
  should not rely solely on its caller's seatbelt.

* **It no longer posts to a hardcoded channel.** It writes one market-wide alert
  row; delivery.py routes it to users who have not switched market-wide alerts
  off. Nobody receives it who did not ask for it.

* **Plain text, not pre-formatted Markdown.** The delivery layer renders HTML and
  escapes the body, so the old `*bold*` markers would have rendered as literal
  asterisks.

* **It refuses to publish an empty snapshot.** The old `if not lines` check was
  unreachable because `lines` always contained the category headers, so a total
  FMP outage still published a message of 21 "data unavailable" rows.
"""

import os
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Publish only if at least this fraction of the universe actually resolved.
# Below it, a snapshot is more misleading than useful.
MIN_COVERAGE = 0.6

ETF_UNIVERSE = [
    {"ticker": "SPY",  "name": "S&P 500",                     "category": "Broad Market"},
    {"ticker": "DIA",  "name": "Dow Jones Industrial Average", "category": "Broad Market"},
    {"ticker": "IWM",  "name": "Russell 2000",                 "category": "Small Cap"},
    {"ticker": "QQQ",  "name": "Nasdaq 100",                   "category": "Technology"},
    {"ticker": "XLK",  "name": "Technology Select",            "category": "Technology"},
    {"ticker": "XLC",  "name": "Communication Services Select","category": "Communication Services"},
    {"ticker": "XLF",  "name": "Financial Select",             "category": "Finance"},
    {"ticker": "XLE",  "name": "Energy Select",                "category": "Energy"},
    {"ticker": "XLV",  "name": "Health Care Select",           "category": "Healthcare"},
    {"ticker": "XLI",  "name": "Industrial Select",            "category": "Industrials"},
    {"ticker": "XLY",  "name": "Consumer Discretionary Select","category": "Consumer Discretionary"},
    {"ticker": "XLP",  "name": "Consumer Staples Select",      "category": "Consumer Staples"},
    {"ticker": "XLB",  "name": "Materials Select",             "category": "Materials"},
    {"ticker": "XLU",  "name": "Utilities Select",             "category": "Utilities"},
    {"ticker": "XLRE", "name": "Real Estate Select",           "category": "Real Estate"},
    {"ticker": "GLD",  "name": "Gold",                         "category": "Commodities"},
    {"ticker": "SLV",  "name": "Silver",                       "category": "Commodities"},
    {"ticker": "TLT",  "name": "20+ Year Treasury",            "category": "Bonds"},
    {"ticker": "HYG",  "name": "High Yield Corporate Bond",    "category": "Bonds"},
    {"ticker": "EFA",  "name": "MSCI EAFE Developed",          "category": "International"},
    {"ticker": "EEM",  "name": "MSCI Emerging Markets",        "category": "International"},
]


def _already_sent_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        res = (supabase.table("alerts").select("id")
               .eq("ticker", "MARKET").eq("source", "ETF_XRAY")
               .gte("created_at", f"{today}T00:00:00+00:00").limit(1).execute())
        return bool(res.data)
    except Exception as e:
        # Fail closed: if we cannot confirm it has not been sent, do not send.
        logger.error("[ETF XRAY] Dedup check failed (%s) — skipping this run", e)
        return True


def _fmt_aum(value):
    try:
        aum = float(value or 0)
    except (TypeError, ValueError):
        return None
    if aum >= 1_000_000_000:
        return f"${aum / 1_000_000_000:.1f}B"
    if aum >= 1_000_000:
        return f"${aum / 1_000_000:.0f}M"
    return None


def _fetch_one(etf):
    """Fetch a single ETF. Returns a dict or None — never raises."""
    ticker = etf["ticker"]
    try:
        quote = fmp_client.get_quote(ticker)
    except Exception as e:
        logger.warning("[ETF XRAY] Quote failed for %s: %s", ticker, e)
        return None
    if not quote or quote.get("price") in (None, ""):
        return None

    try:
        price = float(quote["price"])
        change = float(quote.get("changePercentage") or 0)
        volume = float(quote.get("volume") or 0)
    except (TypeError, ValueError):
        return None

    # Fundamentals are a nice-to-have; the endpoint is unverified upstream and a
    # failure here must not lose the price row.
    expense = aum = None
    try:
        info = fmp_client.get_etf_info(ticker)
        if info:
            expense = info.get("expenseRatio") or info.get("expense_ratio")
            aum = _fmt_aum(info.get("aum") or info.get("netAssets"))
    except Exception:
        pass

    return {**etf, "price": price, "change": change, "volume": volume,
            "expense": expense, "aum": aum}


def _build_summary(rows):
    by_category = {}
    for r in rows:
        by_category.setdefault(r["category"], []).append(r)

    lines = []
    for category, items in by_category.items():
        lines.append(f"{category}")
        for r in sorted(items, key=lambda x: x["change"], reverse=True):
            arrow = "▲" if r["change"] >= 0 else "▼"
            sign = "+" if r["change"] >= 0 else ""
            detail = []
            if r["expense"]:
                detail.append(f"exp {r['expense']}")
            if r["aum"]:
                detail.append(f"AUM {r['aum']}")
            suffix = f" · {' · '.join(detail)}" if detail else ""
            lines.append(f"  {arrow} {r['ticker']} {r['name']} — ${r['price']:,.2f} "
                         f"({sign}{r['change']:.2f}%){suffix}")
        lines.append("")

    return "\n".join(lines).strip()


def _headline(rows):
    best = max(rows, key=lambda r: r["change"])
    worst = min(rows, key=lambda r: r["change"])
    if best["change"] <= 0:
        return "Broad ETF Weakness Across Sectors"
    if worst["change"] >= 0:
        return "Broad ETF Strength Across Sectors"
    return f"{best['name']} Leads As {worst['name']} Lags"


def run_etf_xray():
    """Daily structured ETF snapshot. Never raises."""
    try:
        if _already_sent_today():
            logger.info("[ETF XRAY] Already sent today — skipping")
            return

        rows = []
        for etf in ETF_UNIVERSE:
            row = _fetch_one(etf)
            if row:
                rows.append(row)

        coverage = len(rows) / len(ETF_UNIVERSE)
        if coverage < MIN_COVERAGE:
            logger.error("[ETF XRAY] Only %d/%d ETFs resolved (%.0f%%) — not publishing",
                         len(rows), len(ETF_UNIVERSE), coverage * 100)
            log_poller_error("news_roundup", "run_etf_xray",
                             f"insufficient coverage: {len(rows)}/{len(ETF_UNIVERSE)}",
                             {"coverage": coverage})
            return

        summary = _build_summary(rows)
        stamp = datetime.now().strftime("%B %d, %Y")

        supabase.table("alerts").insert({
            "ticker": "MARKET",
            "summary": f"ETF Xray — {stamp}\n\n{summary}",
            "impact": "MEDIUM",
            "source": "ETF_XRAY",
            "filing_type": "ETF_XRAY",
            "delivered": False,          # delivery.py fans this out market-wide
            "extra": tag_extra({
                "headline": _headline(rows),
                "company_name": "US ETF Universe",
                "etf_count": len(rows),
                "coverage_pct": round(coverage * 100, 1),
                "source_name": "FMP",
            }, "ETF_XRAY", "ETF_XRAY"),
        }).execute()

        logger.info("[ETF XRAY] Published — %d/%d ETFs", len(rows), len(ETF_UNIVERSE))

    except Exception as e:
        logger.error("[ETF XRAY] Run failed: %s", e)
        log_poller_error("news_roundup", "run_etf_xray", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_xray()
