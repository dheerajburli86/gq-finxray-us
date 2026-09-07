"""
fmp_preflight.py
GQ FinXray US — endpoint probe for Financial Modeling Prep.

Hits every FMP endpoint the system depends on, exactly once, using your live
FMP_API_KEY, and reports which ones are alive. Run this BEFORE any deploy.

Why this exists: four endpoint paths were wrong in production for weeks and
nobody knew, because the old client turned a 404 into `return None`, which is
indistinguishable from "no news right now." This script makes a dead endpoint
impossible to miss.

Usage:
    python fmp_preflight.py

Exit code 0 = every CRITICAL endpoint is alive.
Exit code 1 = at least one critical endpoint is broken. Do not deploy.
"""

import os
import sys
import time
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

import fmp_client

TICKER = "AAPL"
ETF = "SPY"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def _shape(result):
    """One-line description of what came back."""
    if result is None:
        return "None"
    if isinstance(result, list):
        if not result:
            return "empty list"
        first = result[0]
        if isinstance(first, dict):
            keys = list(first.keys())[:4]
            return f"{len(result)} rows, keys: {', '.join(keys)}..."
        return f"{len(result)} items"
    if isinstance(result, dict):
        keys = list(result.keys())[:4]
        return f"dict, keys: {', '.join(keys)}..."
    return str(result)[:60]


def probe(feature, label, path, fn, critical=True, empty_ok=False):
    """Run one endpoint check and print the verdict."""
    try:
        t0 = time.time()
        result = fn()
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        print(f"  {RED}FAIL{RESET}  {label:<28} {DIM}{path}{RESET}")
        print(f"        {RED}exception: {e}{RESET}")
        return {"feature": feature, "label": label, "ok": False, "critical": critical}

    empty = result is None or (isinstance(result, (list, dict)) and len(result) == 0)

    if empty and not empty_ok:
        mark = f"{RED}FAIL{RESET}" if critical else f"{YELLOW}WARN{RESET}"
        print(f"  {mark}  {label:<28} {DIM}{path}{RESET}")
        print(f"        {DIM}returned: {_shape(result)}  ({ms}ms)"
              f"  <- check the [FMP] line above for the real cause{RESET}")
        return {"feature": feature, "label": label, "ok": False, "critical": critical}

    note = " (empty is acceptable here)" if empty and empty_ok else ""
    print(f"  {GREEN}OK{RESET}    {label:<28} {DIM}{path}{RESET}")
    print(f"        {DIM}{_shape(result)}  ({ms}ms){note}{RESET}")
    return {"feature": feature, "label": label, "ok": True, "critical": critical}


def main():
    if not fmp_client.FMP_API_KEY:
        print(f"{RED}FMP_API_KEY is not set. Nothing to test.{RESET}")
        sys.exit(1)

    key = fmp_client.FMP_API_KEY
    print("=" * 78)
    print("  GQ FinXray US — FMP endpoint preflight")
    print(f"  key: {key[:6]}...{key[-4:]}   base: {fmp_client.BASE_URL}")
    print("=" * 78)

    today = date.today()
    week_ahead = (today + timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()
    results = []

    print(f"\n{DIM}--- Feature 2: Company & Sector News (was 404, now fixed) ---{RESET}")
    results.append(probe(2, "stock news (per ticker)", "/stable/news/stock",
                         lambda: fmp_client.get_stock_news(TICKER, limit=5)))
    results.append(probe(2, "general market news", "/stable/news/general-latest",
                         lambda: fmp_client.get_general_news(limit=5)))
    results.append(probe(2, "latest stock news sweep", "/stable/news/stock-latest",
                         lambda: fmp_client.get_latest_stock_news(limit=5), critical=False))
    results.append(probe(2, "press releases", "/stable/news/press-releases",
                         lambda: fmp_client.get_press_releases(TICKER, limit=5), critical=False))

    print(f"\n{DIM}--- Features 3, 9, 10: Quotes & Prices ---{RESET}")
    results.append(probe(9, "quote", "/stable/quote",
                         lambda: fmp_client.get_quote(TICKER)))
    results.append(probe(9, "commodity quote (gold)", "/stable/quote",
                         lambda: fmp_client.get_commodity_quote("GCUSD"), critical=False))
    results.append(probe(9, "historical EOD prices", "/stable/historical-price-eod/full",
                         lambda: fmp_client.get_historical_prices(ETF, month_ago, today.isoformat())))
    results.append(probe(10, "ETF info (expense/AUM)", "/stable/etf/info",
                         lambda: fmp_client.get_etf_info(ETF), critical=False))

    print(f"\n{DIM}--- Feature 3: Result Snapshot ---{RESET}")
    results.append(probe(3, "income statement", "/stable/income-statement",
                         lambda: fmp_client.get_income_statement(TICKER, limit=2)))
    results.append(probe(3, "balance sheet", "/stable/balance-sheet-statement",
                         lambda: fmp_client.get_balance_sheet(TICKER, limit=2), critical=False))
    results.append(probe(3, "cash flow", "/stable/cashflow-statement",
                         lambda: fmp_client.get_cash_flow(TICKER, limit=2), critical=False))
    results.append(probe(3, "key metrics", "/stable/key-metrics",
                         lambda: fmp_client.get_key_metrics(TICKER, limit=2), critical=False))
    results.append(probe(3, "company profile", "/stable/profile",
                         lambda: fmp_client.get_profile(TICKER), critical=False))

    print(f"\n{DIM}--- Features 4, 5, 8: Calendars & Insider ---{RESET}")
    results.append(probe(4, "earnings calendar", "/stable/earnings-calendar",
                         lambda: fmp_client.get_earnings_calendar(today.isoformat(), week_ahead)))
    results.append(probe(5, "insider trading", "/stable/insider-trading/search",
                         lambda: fmp_client.get_insider_trading(TICKER, limit=5)))
    results.append(probe(8, "IPO calendar", "/stable/ipos-calendar",
                         lambda: fmp_client.get_ipo_calendar(today.isoformat(), week_ahead),
                         empty_ok=True))

    print(f"\n{DIM}--- Feature 6: Technical Indicators ---{RESET}")
    results.append(probe(6, "RSI", "/stable/technical-indicators/rsi",
                         lambda: fmp_client.get_rsi(TICKER)))
    results.append(probe(6, "SMA 200", "/stable/technical-indicators/sma",
                         lambda: fmp_client.get_sma(TICKER)))

    print(f"\n{DIM}--- Feature 11: Earnings Call Transcripts (2 were 404, now fixed) ---{RESET}")
    results.append(probe(11, "transcript dates", "/stable/earning-call-transcript-dates",
                         lambda: fmp_client.get_transcript_dates(TICKER)))
    results.append(probe(11, "latest transcripts", "/stable/earning-call-transcript-latest",
                         lambda: fmp_client.get_latest_transcripts(limit=5)))

    dates = []
    try:
        dates = fmp_client.get_transcript_dates(TICKER) or []
    except Exception:
        pass
    if dates:
        d = dates[0]
        yr = d.get("fiscalYear") or d.get("year")
        qt = d.get("quarter")
        results.append(probe(11, f"full transcript {TICKER} Q{qt} {yr}",
                             "/stable/earning-call-transcript",
                             lambda: fmp_client.get_earnings_transcript(TICKER, yr, qt)))
    else:
        print(f"  {YELLOW}SKIP{RESET}  full transcript fetch        "
              f"{DIM}(no dates returned to test with){RESET}")

    print(f"\n{DIM}--- Misc ---{RESET}")
    results.append(probe(0, "company screener", "/stable/company-screener",
                         lambda: fmp_client.screener({"exchange": "NASDAQ", "limit": 5}),
                         critical=False))

    # ── Verdict ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    failed = [r for r in results if not r["ok"]]
    crit_failed = [r for r in failed if r["critical"]]
    warned = [r for r in failed if not r["critical"]]

    print(f"  {len(results) - len(failed)}/{len(results)} endpoints alive")

    if warned:
        print(f"\n  {YELLOW}Non-critical issues ({len(warned)}):{RESET}")
        for r in warned:
            print(f"    - Feature {r['feature']}: {r['label']}")
        print(f"  {DIM}These degrade a feature but won't stop alerts.{RESET}")

    if crit_failed:
        print(f"\n  {RED}CRITICAL FAILURES ({len(crit_failed)}) — DO NOT DEPLOY:{RESET}")
        for r in crit_failed:
            print(f"    - Feature {r['feature']}: {r['label']}")
        print(f"\n  {DIM}Scroll up for the [FMP] diagnostic line on each.")
        print(f"  'PATH BUG' = wrong URL in fmp_client.py.")
        print(f"  'KEY/PLAN' = key invalid, or endpoint not included in your plan.{RESET}")
        print("=" * 78)
        sys.exit(1)

    print(f"\n  {GREEN}All critical endpoints alive. Safe to deploy.{RESET}")
    print("=" * 78)
    sys.exit(0)


if __name__ == "__main__":
    main()
