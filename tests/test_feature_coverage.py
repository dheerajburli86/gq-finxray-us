"""
Regression cover for the four silent failures that suppressed 7 of 13 features.

Each of these failed invisibly in production — no exception, no error log, the
alert simply never arrived:

  1. FEATURE COVERAGE      main.py imported 9 modules and scheduled 6 features.
                           Analyst Ratings, Macro Digest and Watchlist Heatmap
                           were complete files that nothing ever called.
  2. MARKET-WIDE ROUTING   GQ_ENABLE_MARKET_WIDE defaulted false, so every
                           ticker="MARKET" alert resolved to an empty audience
                           and was marked delivered without being sent.
  3. FORM 4 WORD LADDER    the quality gate's floor sat ABOVE its own ceiling,
                           so no output could ever satisfy it.
  4. COMPANY LEAKAGE       the ticker catch-all would broadcast any alert whose
                           symbol failed to resolve.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "x")
os.environ.setdefault("TELEGRAM_TOKEN", "x")

import feature_map
import ai_pipeline
import delivery

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


print("=== 1. ALL 13 FEATURES ARE SCHEDULED IN main.py ===")
main_src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
# entry point -> the feature it serves
REQUIRED = {
    "poll_sec_8k": 1, "poll_sec_form4": 1, "poll_sec_10q": 1, "poll_sec_10k": 1,
    "poll_all_news": 2, "poll_fmp_news": 2,
    "process_pending_snapshots": 3,
    # Feature 4 has two halves: the heads-up that earnings are due
    # (poll_fmp_events) and the EPS surprise itself (poll_earnings_for_tickers).
    # feature_map listed EARNINGS_MISS/EARNINGS_BEAT as Feature 4 types
    # "emitted by earnings_alerts.py" while nothing ever scheduled that module.
    "poll_fmp_events": 4, "poll_earnings_for_tickers": 4,
    "run_large_trades_poller": 5,
    "run_technical_poller": 6,
    "run_etf_flow_poller": 7,
    "run_ipo_poller": 8,
    "run_sector_heatmap_midday": 9, "run_sector_heatmap_afternoon": 9,
    "run_sector_heatmap_weekly": 9, "run_sector_heatmap_monthly": 9,
    "run_earnings_transcript_poller": 10,
    "poll_analyst_ratings": 11,
    "run_macro_policy_roundup": 12, "send_premarket_report": 12,
    "send_market_close_report": 12,
    "run_watchlist_heatmap_midday": 13, "run_watchlist_heatmap_eod": 13,
}
covered = set()
for fn, fid in REQUIRED.items():
    scheduled = f"job({fn})" in main_src or f"sec_job({fn})" in main_src
    check(f"Feature {fid:>2} · {fn} scheduled", scheduled, "not wired into run_scheduler()")
    if scheduled:
        covered.add(fid)
check("every one of the 13 features has a scheduled entry point",
      covered == set(feature_map.FEATURES),
      f"missing {sorted(set(feature_map.FEATURES) - covered)}")

print("\n=== 2. MARKET-WIDE PRODUCTS CAN ACTUALLY BE DELIVERED ===")
check("broadcast enabled by default", delivery.broadcast_enabled(),
      "GQ_ENABLE_MARKET_WIDE default flipped back to false")
for src, ft in [("SECTOR_HEATMAP", "HEATMAP_DAILY_MIDDAY"), ("FMP_IPO", "IPO_UPCOMING"),
                ("MACRO_ROUNDUP", "MACRO_BRIEFING"), ("MARKET_REPORT", "MARKET_REPORT"),
                ("ETF_FLOW", "BULLISH_MOMENTUM")]:
    routed = delivery._is_market_wide({"source": src, "filing_type": ft, "ticker": "MARKET"})
    check(f"{src}/{ft} routes to an audience", routed, "would be dropped as no_audience")

# An S-1 registrant is pre-listing, so ticker_from_cik() cannot resolve it and
# the row carries ticker="UNKNOWN" BY CONSTRUCTION — not as a failure. That is
# exactly the shape the COMPANY_ONLY guard suppresses for SEC_EDGAR, which is
# why Feature 8's EDGAR half is emitted under its own source.
check("SEC_IPO/S-1 (ticker='UNKNOWN') routes to an audience",
      delivery._is_market_wide({"source": "SEC_IPO", "filing_type": "S-1",
                                "ticker": "UNKNOWN"}),
      "every IPO registration alert would be settled as delivered without being sent")

print("\n=== 3. COMPANY CONTENT NEVER BROADCASTS ===")
for src, ft, tk in [("FMP_NEWS", "NEWS", "MARKET"), ("SEC_EDGAR", "8-K", "UNKNOWN"),
                    ("SEC_EDGAR", "4", ""), ("TECHNICAL", "52W_HIGH", "UNKNOWN"),
                    ("WATCHLIST_HEATMAP", "HEATMAP_WATCHLIST_EOD", "WATCHLIST")]:
    leaked = delivery._is_market_wide({"source": src, "filing_type": ft, "ticker": tk})
    check(f"{src}/{ft} (ticker={tk!r}) stays watchlist-scoped", not leaked,
          "would be broadcast to every subscriber")

print("\n=== 4. EVERY WORD LADDER IS SATISFIABLE AT ITS FIRST RUNG ===")
for ft in ("4", "INSIDER_FMP", "NEWS", "8-K", "10-Q", "EARNINGS_TRANSCRIPT", ""):
    for chars in (200, 600, 1500, 4000, 40000):
        start, step, mx, floor = ai_pipeline.ladder_for(ft, "x" * chars)
        check(f"{ft or '(default)':<20} {chars:>6}ch  floor {floor} <= start {start}",
              floor <= start, f"floor {floor} above ceiling {start} — no output can pass")
        check(f"{ft or '(default)':<20} {chars:>6}ch  ladder reaches max {mx}",
              mx >= start, "max below start")

print("\n=== 5. NO POLLER EMITS AN UNMAPPED (source, filing_type) ===")
EMISSIONS = [
    ("SEC_EDGAR", "8-K"), ("SEC_EDGAR", "10-Q"), ("SEC_EDGAR", "10-K"), ("SEC_EDGAR", "4"),
    ("FMP_NEWS", "NEWS"), ("CNBC", "NEWS"), ("SEC_XBRL", "RESULT_SNAPSHOT"),
    ("FMP_NEWS", "EARNINGS_CALENDAR"), ("FMP_NEWS", "INSIDER_FMP"), ("FMP_NEWS", "BULK_DEAL"),
    ("FMP", "EARNINGS_MISS"), ("FMP", "EARNINGS_BEAT"),
    ("LARGE_TRADE", "LARGE_TRADE"), ("TECHNICAL", "RSI_OVERBOUGHT"), ("TECHNICAL", "52W_HIGH"),
    ("ETF_FLOW", "BULLISH_MOMENTUM"), ("ETF_FLOW", "BEARISH_MOMENTUM"),
    ("FMP_IPO", "IPO_UPCOMING"), ("SEC_IPO", "S-1"),
    ("SECTOR_HEATMAP", "HEATMAP_DAILY_MIDDAY"),
    ("SECTOR_HEATMAP", "HEATMAP_WEEKLY"), ("SECTOR_HEATMAP", "HEATMAP_MONTHLY"),
    ("FMP_TRANSCRIPT", "EARNINGS_TRANSCRIPT"), ("FMP_ANALYST", "ANALYST_RATING"),
    ("MACRO_ROUNDUP", "MACRO_BRIEFING"), ("MARKET_REPORT", "MARKET_REPORT"),
    ("WATCHLIST_HEATMAP", "HEATMAP_WATCHLIST_MIDDAY"), ("WATCHLIST_HEATMAP", "HEATMAP_WATCHLIST_EOD"),
]
tagged = set()
for src, ft in EMISSIONS:
    fid, name = feature_map.resolve_feature(src, ft)
    check(f"{src}/{ft} -> Feature {fid}", fid != 0, "resolves to Unmapped, cannot be muted or monitored")
    tagged.add(fid)
check("all 13 features receive at least one emission",
      tagged == set(feature_map.FEATURES),
      f"no emission maps to {sorted(set(feature_map.FEATURES) - tagged)}")

print("\n=== 6. NO LEGACY VENDORS ===")
import glob
offenders = []
for f in glob.glob(os.path.join(os.path.dirname(__file__), "..", "*.py")):
    try:
        body = open(f).read().lower()
    except Exception:
        continue
    if "eodhd" in body or "twelvedata" in body:
        offenders.append(os.path.basename(f))
check("no EODHD / TwelveData references remain", not offenders, f"found in {offenders}")

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL FEATURE COVERAGE TESTS PASS")
