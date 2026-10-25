"""
Defect found in the alerts delivered on 2026-10-23 (Friday).

USO 12:21 ET: "hit a new 52-week high: $168.91 (up 0.6% from previous high of
$167.90)". Thursday's high was $163.66; $167.90 was Friday's own pre-market
print, which FMP's quote had already folded into yearHigh. The breakout above
$163.66 at the 10:07 run was missed, and the late alert misnamed the level.

Run: python tests/test_alert_quality_oct23.py
"""
import os
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x",
             "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import technical_poller as TP   # noqa: E402

ET = TP.ET
today = datetime.now(ET).date()


def ms(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


# 60 prior sessions topping out at 163.66 two days ago; today's partial bar
# (the pre-market-inflated one) must never become a "previous" level.
bars = []
for i in range(1, 61):
    d = today - timedelta(days=i)
    bars.append({"t": ms(d), "h": 163.66 if i == 2 else 150.0 + (i % 5),
                 "l": 140.0 if i == 30 else 145.0, "v": 5e6})
bars.append({"t": ms(today), "h": 167.90, "l": 160.0, "v": 1e6})

print("1. Levels come from completed sessions only")
TP._year_range_cache.update(date=None, levels={}, avg_volume={}, level_dates={})
real = TP.massive_client.get_aggregates
TP.massive_client.get_aggregates = lambda *a, **k: bars
try:
    n, left = TP.refresh_year_ranges({"USO"})
finally:
    TP.massive_client.get_aggregates = real
check("refreshed", (n, left) == (1, 0), (n, left))
check("52-week high is Thursday's 163.66, not today's 167.90",
      TP._year_range_cache["levels"]["USO"] == (163.66, 140.0), TP._year_range_cache["levels"])
check("dated", TP._year_range_cache["level_dates"]["USO"][0] == today - timedelta(days=2))
check("20-day average from the same call", TP._year_range_cache["avg_volume"]["USO"] == 5e6)

print("2. The breakout fires on the first run and names the real previous high")
idx = {"USO": {"price": 167.39, "prev_close": 162.51, "day_volume": 1e6, "prev_volume": 1e6,
               "day_high": 167.90, "day_low": 163.0, "change_pct": 3.0}}
got = TP.check_52_week(idx, set())
check("alert at 10:07, not 12:21", [a["filing_type"] for a in got] == ["52W_HIGH"], got)
check("previous high is $163.66", got and "previous high of $163.66" in got[0]["summary"],
      got and got[0]["summary"])
check("no '$167.90' quoted as previous", got and "of $167.90" not in got[0]["summary"])

print("3. Failed history fetch -> ticker retried next run, no alert from stale levels")
TP._year_range_cache.update(date=None, levels={}, avg_volume={}, level_dates={})
TP.massive_client.get_aggregates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
real_log = TP.log_poller_error
TP.log_poller_error = lambda *a, **k: None
try:
    n, left = TP.refresh_year_ranges({"USO"})
finally:
    TP.massive_client.get_aggregates = real
    TP.log_poller_error = real_log
check("nothing cached, still pending", (n, left) == (0, 1) and "USO" not in TP._year_range_cache["levels"],
      (n, left))
check("no 52-week alert without levels", TP.check_52_week(idx, set()) == [])

print("4. Leveraged / inverse funds do not get relative-performance alerts")
import etf_signals_poller as ES   # noqa: E402


def rel(beta, r, spy, resid=0.0138):
    base = {"sigma": 0.03, "adv": 5e7, "beta": beta, "resid_sigma": resid, "r2": 0.9,
            "rets": [0.0] * 300, "sessions": [{"date": today}] * 301}
    m = {"price": 100 * (1 + r), "prev_close": 100.0, "volume": 2e7}
    out = ES.evaluate_intraday("TQQQ", "TQQQ", base, m, 0.3, today.isoformat(), set(),
                               bench={"r": spy})
    return [a["filing_type"] for a in out if a["filing_type"] in ("ETF_OUTPERFORM", "ETF_UNDERPERFORM")]


check("TQQQ beta 4.78: no outperform", rel(4.78, 0.0015, -0.0057) == [])
check("SQQQ beta -4.80: no underperform", rel(-4.80, -0.0013, -0.0057) == [])
check("an ordinary fund (beta 1.6) still alerts", rel(1.61, 0.0010, -0.0057, 0.0046) == ["ETF_OUTPERFORM"],
      rel(1.61, 0.0010, -0.0057, 0.0046))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
