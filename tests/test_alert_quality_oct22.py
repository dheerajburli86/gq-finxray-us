"""
Defects found by reading the alerts delivered on 2026-10-21 and 2026-10-22.
Every case below is taken from real alert text, not invented.

Run: python tests/test_alert_quality_oct22.py
"""
import os
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x",
             "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import technical_poller as TP        # noqa: E402
import etf_signals_poller as ES      # noqa: E402
import alert_formatter as AF         # noqa: E402

ET = TP.ET


def ms(d):
    return int(datetime(d.year, d.month, d.day, 0, 0, tzinfo=ET).timestamp() * 1000)


def snap(price, prev_close, hi=None, lo=None):
    return {"price": price, "prev_close": prev_close, "day_volume": 1e6, "prev_volume": 1e6,
            "day_high": hi or price, "day_low": lo or price,
            "change_pct": (price / prev_close - 1) * 100}


print("1. Volume baseline is a real 20-day average (FMP quote has no avgVolume)")
today = date(2026, 10, 22)
bars = [{"t": ms(today - timedelta(days=i)), "v": 1e6 * (1 if i % 2 else 3)} for i in range(1, 40)]
bars.append({"t": ms(today), "v": 99e6})            # today's partial bar must be ignored
real = TP.massive_client.get_aggregates
TP.massive_client.get_aggregates = lambda *a, **k: bars
try:
    adv = TP._avg_daily_volume("XLE", today)
finally:
    TP.massive_client.get_aggregates = real
check("20 complete sessions averaged, today's partial bar excluded", adv == 2e6, adv)
TP.massive_client.get_aggregates = lambda *a, **k: bars[:5]
try:
    check("too little history -> None (falls back to previous session)",
          TP._avg_daily_volume("XLE", today) is None)
finally:
    TP.massive_client.get_aggregates = real

# refresh_year_ranges fills the average even though the FMP quote lacks it.
TP._year_range_cache.update(date=None, levels={}, avg_volume={}, set_today={})
real_q, real_adv = TP.fmp_client.get_quotes, TP._avg_daily_volume
TP.fmp_client.get_quotes = lambda b: {t: {"yearHigh": 70, "yearLow": 50, "dayHigh": 68,
                                          "dayLow": 67} for t in b}
TP._avg_daily_volume = lambda t, d: 31.9e6
try:
    TP.refresh_year_ranges({"XLE"})
finally:
    TP.fmp_client.get_quotes, TP._avg_daily_volume = real_q, real_adv
check("average cached from bars", TP._year_range_cache["avg_volume"].get("XLE") == 31.9e6,
      TP._year_range_cache["avg_volume"])
# XLE Oct 22 10:00: 6.80M on the tape at ~12.4% elapsed. Previous session 26.5M -> 2.07x;
# against its 31.9M average it is 1.72x -- not a spike.
got = TP.check_volume_spikes({"XLE": {"price": 67.48, "prev_close": 68.16, "day_volume": 6.797e6,
                                      "prev_volume": 26.47e6, "day_high": 68.78, "day_low": 67.4,
                                      "change_pct": -1.0}}, set(), 0.124)
check("XLE 1.7x of its average no longer alerts", got == [], [a["summary"] for a in got])

print("2. RSI 'moves into' overbought only on a crossing")
y, t = date(2026, 10, 21), date(2026, 10, 22)
check("prior session found", TP._rsi_and_prior([{"timestamp": ms(t), "value": 70.8},
                                                 {"timestamp": ms(y), "value": 71.2}]) == (70.8, 71.2))
check("no timestamps -> second value is the prior",
      TP._rsi_and_prior([{"value": 70.8}, {"value": 69.0}]) == (70.8, 69.0))
check("nothing usable -> (None, None)", TP._rsi_and_prior([]) == (None, None))


def run_rsi(values):
    idx = {"XLP": snap(88.61, 87.10)}
    real_rsi, real_sma = TP.massive_client.get_rsi, TP.massive_client.get_sma
    TP.massive_client.get_rsi = lambda *a, **k: values
    TP.massive_client.get_sma = lambda *a, **k: []
    try:
        return TP.check_indicators(["XLP"], idx, set(), recent_crossovers=set())
    finally:
        TP.massive_client.get_rsi, TP.massive_client.get_sma = real_rsi, real_sma


stay = run_rsi([{"timestamp": ms(t), "value": 70.8}, {"timestamp": ms(y), "value": 71.5}])
check("already overbought yesterday -> no new alert", stay == [], [a["summary"] for a in stay])
cross = run_rsi([{"timestamp": ms(t), "value": 70.8}, {"timestamp": ms(y), "value": 68.4}])
check("crossing 70 today -> alert", [a["filing_type"] for a in cross] == ["RSI_OVERBOUGHT"], cross)
check("text names the prior reading", cross and "from 68.4 at the previous close" in cross[0]["summary"],
      cross and cross[0]["summary"])

print("3. A 52-week high that has already reversed says so")
TP._year_range_cache.update(date=datetime.now(ET).date(), levels={"XLE": (68.78, 50.0)},
                            avg_volume={}, set_today={"XLE": {"high": 68.78}})
got = TP.check_52_week({"XLE": snap(67.48, 68.16, hi=68.78)}, set())
check("one alert", len(got) == 1, got)
check("headline is not 'Breaks Out' on a fund down 1%",
      got and got[0]["extra"]["headline"] == "Hits New 52-Week High, Then Pulls Back",
      got and got[0]["extra"]["headline"])
check("summary gives the pull-back", got and "pulled back 1.9% from that high" in got[0]["summary"],
      got and got[0]["summary"])
TP._year_range_cache.update(levels={"XLE": (68.51, 50.0)}, set_today={})
got = TP.check_52_week({"XLE": snap(68.49, 67.97, hi=68.64)}, set())
check("a breakout still near its high keeps the breakout headline (XLE Oct 21)",
      got and got[0]["extra"]["headline"] == "Breaks Out To New 52-Week High"
      and "pulled back" not in got[0]["summary"], got and got[0]["summary"])

print("4. 200-SMA header shows the body's snapshot price")
real_rsi, real_sma = TP.massive_client.get_rsi, TP.massive_client.get_sma
TP.massive_client.get_rsi = lambda *a, **k: []
TP.massive_client.get_sma = lambda *a, **k: [{"value": 83.27}]
try:
    got = TP.check_indicators(["VXUS"], {"VXUS": snap(82.66, 84.00)}, set(), recent_crossovers=set())
finally:
    TP.massive_client.get_rsi, TP.massive_client.get_sma = real_rsi, real_sma
check("crossover alert built", [a["filing_type"] for a in got] == ["SMA200_CROSSOVER_DOWN"], got)
AF._quote_cache["VXUS"] = (10 ** 12, {"price": 82.64, "changePercentage": -1.61})
line = AF._price_line("VXUS", got[0] if got else None)
AF._quote_cache.pop("VXUS", None)
check("header uses $82.66 from the alert, not a later quote", "$82.66" in line, line)

print("5. Feature 7 momentum after a Feature 6 volume spike is dropped")


class _Q:
    def __init__(self, rows): self.rows = rows
    def __getattr__(self, _): return lambda *a, **k: self
    def execute(self):
        class R: pass
        r = R(); r.data = self.rows; return r


class _SB:
    def __init__(self, rows): self.rows = rows
    def table(self, _): return _Q(self.rows)


rows = [{"ticker": "GDX", "filing_type": "ETF_MOMENTUM_DOWN"},
        {"ticker": "UVXY", "filing_type": "ETF_MOMENTUM_UP"},
        {"ticker": "GDX", "filing_type": "ETF_BIG_MOVE_DOWN"}]
real_sb = ES._sb
ES._sb = _SB([{"ticker": "GDX"}])
try:
    kept = ES.drop_momentum_covered_by_technical(rows, date(2026, 10, 22))
finally:
    ES._sb = real_sb
kept_ft = [(a["ticker"], a["filing_type"]) for a in kept]
check("GDX heavy selling dropped (F6 spike at 10:00)", ("GDX", "ETF_MOMENTUM_DOWN") not in kept_ft, kept_ft)
check("UVXY momentum kept (no F6 spike)", ("UVXY", "ETF_MOMENTUM_UP") in kept_ft, kept_ft)
check("GDX big move kept -- it is an escalation", ("GDX", "ETF_BIG_MOVE_DOWN") in kept_ft, kept_ft)


class _Boom:
    def table(self, _): raise RuntimeError("down")


ES._sb = _Boom()
try:
    kept = ES.drop_momentum_covered_by_technical(rows, date(2026, 10, 22))
finally:
    ES._sb = real_sb
check("ledger unreadable -> nothing dropped", len(kept) == 3, kept)

print("6. Feature 6 re-checks the ledger before writing a volume spike")
import inspect   # noqa: E402
src = inspect.getsource(TP.run_technical_poller)
check("fresh ledger read sits between the checks and the write",
      src.index("fresh = load_sent_today()") < src.index("_write_alerts(alerts)"))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
