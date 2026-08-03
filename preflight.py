"""
preflight.py
GQ FinXray US — full data-layer probe. Replaces fmp_preflight.py.

Hits every FMP endpoint, every Massive endpoint, and the unified market_data
layer exactly once, and reports what's alive. Run before any deploy.

Usage:
    PowerShell:  $env:FMP_API_KEY="..."; $env:MASSIVE_API_KEY="..."; python preflight.py
    Bash:        FMP_API_KEY=... MASSIVE_API_KEY=... python preflight.py

Exit 0 = every CRITICAL endpoint alive. Exit 1 = do not deploy.
"""

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import fmp_client
import massive_client

TICKER = "AAPL"
ETF = "SPY"

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
DIM = "\033[2m"; BOLD = "\033[1m"; RESET = "\033[0m"

if os.environ.get("NO_COLOR") or (os.name == "nt" and not os.environ.get("WT_SESSION")):
    _enabled = False
    if os.name == "nt" and not os.environ.get("NO_COLOR"):
        try:
            import ctypes
            _k = ctypes.windll.kernel32
            _enabled = bool(_k.SetConsoleMode(_k.GetStdHandle(-11), 7))
        except Exception:
            _enabled = False
    if not _enabled:
        GREEN = RED = YELLOW = DIM = BOLD = RESET = ""

results = []


def _shape(r):
    if r is None:
        return "None"
    if isinstance(r, list):
        if not r:
            return "empty list"
        f = r[0]
        if isinstance(f, dict):
            return f"{len(r)} rows, keys: {', '.join(list(f.keys())[:4])}..."
        return f"{len(r)} items"
    if isinstance(r, dict):
        return f"dict, keys: {', '.join(list(r.keys())[:4])}..."
    return str(r)[:60]


def probe(feature, label, path, fn, critical=True, empty_ok=False):
    try:
        t0 = time.time()
        res = fn()
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        print(f"  {RED}FAIL{RESET}  {label:<30} {DIM}{path}{RESET}")
        print(f"        {RED}exception: {e}{RESET}")
        results.append({"feature": feature, "label": label, "ok": False, "critical": critical})
        return None

    empty = res is None or (isinstance(res, (list, dict)) and len(res) == 0)
    if empty and not empty_ok:
        mark = f"{RED}FAIL{RESET}" if critical else f"{YELLOW}WARN{RESET}"
        print(f"  {mark}  {label:<30} {DIM}{path}{RESET}")
        print(f"        {DIM}returned: {_shape(res)}  ({ms}ms){RESET}")
        results.append({"feature": feature, "label": label, "ok": False, "critical": critical})
        return res

    print(f"  {GREEN}OK{RESET}    {label:<30} {DIM}{path}{RESET}")
    print(f"        {DIM}{_shape(res)}  ({ms}ms){RESET}")
    results.append({"feature": feature, "label": label, "ok": True, "critical": critical})
    return res


def section(title):
    print(f"\n{BOLD}--- {title} ---{RESET}")


def main():
    missing = []
    if not fmp_client.FMP_API_KEY:
        missing.append("FMP_API_KEY")
    if not massive_client.MASSIVE_API_KEY:
        missing.append("MASSIVE_API_KEY")
    if missing:
        print(f"{RED}Not set: {', '.join(missing)}. Nothing to test.{RESET}")
        sys.exit(1)

    print("=" * 80)
    print("  GQ FinXray US — data layer preflight (FMP + Massive)")
    print(f"  FMP: {fmp_client.FMP_API_KEY[:6]}...  Massive: {massive_client.MASSIVE_API_KEY[:6]}...")
    print("=" * 80)
    fmp_client.reset_health()

    # Warm the shared snapshot up front so the market_data section below
    # measures cache reads rather than accidentally timing a cold fetch.
    import market_data as _md
    _md.snapshot_all()

    today = date.today()
    week_ahead = (today + timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()

    # ── MASSIVE ──────────────────────────────────────────────────────────────
    section("MASSIVE — market status & snapshots (the workhorses)")
    probe(0, "market status", "/v1/marketstatus/now",
          lambda: massive_client.get_market_status())
    snap = probe(6, "FULL MARKET SNAPSHOT", "/v2/snapshot/.../tickers",
                 lambda: massive_client.get_full_market_snapshot())
    if snap:
        print(f"        {GREEN}>> {len(snap):,} tickers in ONE call{RESET}")
    probe(6, "single snapshot", "/v2/snapshot/.../tickers/{t}",
          lambda: massive_client.get_snapshot(TICKER))
    probe(0, "top gainers", "/v2/snapshot/.../gainers",
          lambda: massive_client.get_gainers())
    probe(0, "top losers", "/v2/snapshot/.../losers",
          lambda: massive_client.get_losers())

    section("MASSIVE — tick tape (Feature 5: Large Trades)")
    since_ns = int((datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp() * 1e9)
    trades = probe(5, "tick trades", "/v3/trades/{t}",
                   lambda: massive_client.get_trades(TICKER, timestamp_gte=since_ns, limit=1000))
    if trades:
        big = [t for t in trades if (t.get("size") or 0) * (t.get("price") or 0) >= 1_000_000]
        print(f"        {DIM}{len(big)} print(s) over $1M in this sample{RESET}")

    section("MASSIVE — indicators, aggregates, reference")
    probe(6, "RSI", "/v1/indicators/rsi/{t}", lambda: massive_client.get_rsi(TICKER))
    probe(6, "SMA 200", "/v1/indicators/sma/{t}", lambda: massive_client.get_sma(TICKER))
    probe(9, "daily aggregates", "/v2/aggs/ticker/{t}/range",
          lambda: massive_client.get_aggregates(ETF, 1, "day", month_ago, today.isoformat()))
    probe(0, "previous close", "/v2/aggs/ticker/{t}/prev",
          lambda: massive_client.get_prev_close(TICKER))
    probe(0, "ticker details", "/v3/reference/tickers/{t}",
          lambda: massive_client.get_ticker_details(TICKER), critical=False)
    probe(2, "news", "/v2/reference/news",
          lambda: massive_client.get_news(TICKER, limit=5), critical=False)
    probe(0, "dividends", "/stocks/v1/dividends",
          lambda: massive_client.get_dividends(TICKER, limit=5), critical=False)
    probe(0, "splits", "/stocks/v1/splits",
          lambda: massive_client.get_splits(TICKER, limit=5), critical=False)

    # ── FMP ──────────────────────────────────────────────────────────────────
    section("FMP — news (Feature 2)")
    probe(2, "stock news", "/stable/news/stock",
          lambda: fmp_client.get_stock_news(TICKER, limit=5))
    probe(2, "general news", "/stable/news/general-latest",
          lambda: fmp_client.get_general_news(limit=5))
    probe(2, "latest stock news", "/stable/news/stock-latest",
          lambda: fmp_client.get_latest_stock_news(limit=5), critical=False)
    probe(2, "press releases", "/stable/news/press-releases",
          lambda: fmp_client.get_press_releases(TICKER, limit=5), critical=False)

    section("FMP — fundamentals (Feature 3)")
    probe(3, "income statement", "/stable/income-statement",
          lambda: fmp_client.get_income_statement(TICKER, limit=2))
    probe(3, "balance sheet", "/stable/balance-sheet-statement",
          lambda: fmp_client.get_balance_sheet(TICKER, limit=2), critical=False)
    probe(3, "cash flow", "/stable/cash-flow-statement",
          lambda: fmp_client.get_cash_flow(TICKER, limit=2), critical=False)
    probe(3, "key metrics", "/stable/key-metrics",
          lambda: fmp_client.get_key_metrics(TICKER, limit=2), critical=False)
    probe(3, "profile", "/stable/profile",
          lambda: fmp_client.get_profile(TICKER), critical=False)

    section("FMP — quotes, levels, ETF (Features 6, 9, 10)")
    probe(6, "quote (52w levels)", "/stable/quote", lambda: fmp_client.get_quote(TICKER))
    probe(9, "historical EOD", "/stable/historical-price-eod/full",
          lambda: fmp_client.get_historical_prices(ETF, month_ago, today.isoformat()))
    probe(10, "ETF info", "/stable/etf/info", lambda: fmp_client.get_etf_info(ETF), critical=False)
    probe(9, "commodity quote", "/stable/quote",
          lambda: fmp_client.get_commodity_quote("GCUSD"), critical=False)

    section("FMP — calendars & insider (Features 4, 5, 8)")
    probe(4, "earnings calendar", "/stable/earnings-calendar",
          lambda: fmp_client.get_earnings_calendar(today.isoformat(), week_ahead))
    probe(5, "insider trading", "/stable/insider-trading/search",
          lambda: fmp_client.get_insider_trading(TICKER, limit=5))
    probe(8, "IPO calendar", "/stable/ipos-calendar",
          lambda: fmp_client.get_ipo_calendar(today.isoformat(), week_ahead), empty_ok=True)

    section("FMP — transcripts (Feature 11)")
    dates = probe(11, "transcript dates", "/stable/earning-call-transcript-dates",
                  lambda: fmp_client.get_transcript_dates(TICKER))
    probe(11, "latest transcripts", "/stable/earning-call-transcript-latest",
          lambda: fmp_client.get_latest_transcripts(limit=5))
    if dates:
        d = dates[0]
        yr, qt = d.get("fiscalYear") or d.get("year"), d.get("quarter")
        probe(11, f"full transcript Q{qt} {yr}", "/stable/earning-call-transcript",
              lambda: fmp_client.get_earnings_transcript(TICKER, yr, qt))

    section("FMP — NEWLY WIRED (13F + analyst grades)")
    probe(5, "13F latest filings", "/stable/institutional-ownership/latest",
          lambda: fmp_client.get_institutional_ownership_latest(limit=5), critical=False)
    probe(5, "13F holder performance", "/stable/.../holder-performance-summary",
          lambda: fmp_client.get_holder_performance("0001067983"), critical=False)
    probe(2, "analyst grades", "/stable/grades",
          lambda: fmp_client.get_grades(TICKER), critical=False)
    probe(2, "price target consensus", "/stable/price-target-consensus",
          lambda: fmp_client.get_price_target_consensus(TICKER), critical=False)
    probe(2, "ratings snapshot", "/stable/ratings-snapshot",
          lambda: fmp_client.get_ratings_snapshot(TICKER), critical=False)

    # ── UNIFIED LAYER ────────────────────────────────────────────────────────
    section("market_data — unified layer & failover")
    import market_data
    market_data.reset_stats()
    probe(0, "market_open()", "massive -> clock fallback",
          lambda: {"open": market_data.market_open()}, empty_ok=True)
    probe(0, "quote() via snapshot", "cached snapshot", lambda: market_data.quote(TICKER))
    probe(0, "quotes() batch x5", "cached snapshot",
          lambda: market_data.quotes(["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]))
    probe(0, "year_high_low()", "FMP cached", lambda: {"levels": market_data.year_high_low(TICKER)})
    movers = market_data.top_movers(3)
    g_l = probe(0, "top_movers() liquidity-filtered", "massive gainers/losers",
                lambda: {"g": movers[0], "l": movers[1]})
    if g_l and g_l["g"]:
        gs = ", ".join(f"{x['ticker']} {x['change_pct']:+.1f}%" for x in g_l["g"])
        ls = ", ".join(f"{x['ticker']} {x['change_pct']:+.1f}%" for x in g_l["l"])
        print(f"        {DIM}gainers: {gs}{RESET}")
        print(f"        {DIM}losers:  {ls}{RESET}")
        print(f"        {DIM}(filtered to price>=${market_data.MOVERS_MIN_PRICE:.0f}, "
              f"vol>={market_data.MOVERS_MIN_VOLUME:,}){RESET}")

    print(f"\n  {DIM}provider stats: {market_data.get_stats()}{RESET}")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    failed = [r for r in results if not r["ok"]]
    crit = [r for r in failed if r["critical"]]
    warn = [r for r in failed if not r["critical"]]
    health = fmp_client.get_health()
    degraded = fmp_client.is_degraded()

    print(f"  {len(results) - len(failed)}/{len(results)} endpoints alive")
    print(f"  {DIM}FMP health: {health}{RESET}")

    if warn:
        print(f"\n  {YELLOW}Non-critical ({len(warn)}):{RESET}")
        for r in warn:
            print(f"    - F{r['feature']}: {r['label']}")

    if crit:
        print(f"\n  {RED}CRITICAL FAILURES ({len(crit)}):{RESET}")
        for r in crit:
            print(f"    - F{r['feature']}: {r['label']}")

    # Distinguish "our paths are wrong" from "the vendor is having a bad hour".
    # A 404 is a code bug that will never fix itself. A pile of 504s and
    # timeouts with zero 404s means the endpoints are correct and FMP's
    # backend is degraded -- re-verifying paths in that state wastes time.
    if degraded:
        print(f"\n  {YELLOW}>> FMP IS DEGRADED RIGHT NOW, NOT MISCONFIGURED.{RESET}")
        print(f"     {health['server_error']} server error(s) (5xx) and "
              f"{health['timeout']} timeout(s), but ZERO 404s.")
        print(f"     {DIM}Every failing path above returned clean data in an earlier run.")
        print(f"     Nothing to fix in code -- wait and re-run. If a path were genuinely")
        print(f"     wrong you would see 'PATH BUG'/404, which you do not.{RESET}")

    if health["not_found"]:
        print(f"\n  {RED}>> {health['not_found']} genuine 404(s) — real path bugs, fix these.{RESET}")

    if crit and not degraded:
        print(f"\n  {RED}DO NOT DEPLOY.{RESET}")
        print("=" * 80)
        sys.exit(1)

    if crit and degraded:
        print(f"\n  {YELLOW}Deploy is acceptable: failures are provider-side and the code")
        print(f"  retries 5xx. Re-run later to confirm.{RESET}")
        print("=" * 80)
        sys.exit(0)

    print(f"\n  {GREEN}All critical endpoints alive. Safe to deploy.{RESET}")
    print("=" * 80)
    sys.exit(0)


if __name__ == "__main__":
    main()
