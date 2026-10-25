"""
Defects found by reading the 19 alerts delivered on 2026-10-20 (Tuesday).
Every case below is taken from real alert text, not invented.

Run: python tests/test_alert_quality_oct20.py
"""
import os
import sys
from datetime import datetime

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
import etf_data_poller as EDP        # noqa: E402
import alert_formatter as AF         # noqa: E402


def fresh_cache(levels=None, avg=None):
    TP._year_range_cache.update(date=datetime.now(TP.ET).date(), levels=levels or {},
                                avg_volume=avg or {}, level_dates={})


def snap(price, prev_close, vol, prev_vol, hi=None, lo=None):
    return {"price": price, "prev_close": prev_close, "day_volume": vol, "prev_volume": prev_vol,
            "day_high": hi or price, "day_low": lo or price,
            "change_pct": (price / prev_close - 1) * 100}


print("1. Treasury bills are collateral, not positions")
for n in ("TREASURY BILL 0 10/20/2026", "UNITED STATES TREASURY BILL 11/13/2026",
          "US T-BILL 0 12/04/26", "B 0 11/28/26 TBILL"):
    check(f"not alertable: {n}", not EDP.is_alertable_line({"security_name": n}))
for n in ("US TREASURY N/B 4.25% 2030", "TREASURY WINE ESTATES", "Exxon Mobil Corp"):
    check(f"still alertable: {n}", EDP.is_alertable_line({"security_name": n}))

print("2. Volume spike is measured against average volume, not one session")
# XLV: 2.74M by 10:21 (elapsed ~0.186); Monday 5.87M; 20-day average 8.4M.
fresh_cache(avg={"XLV": 8.4e6})
got = TP.check_volume_spikes({"XLV": snap(179.35, 175.65, 2.742e6, 5.87e6)}, set(), 0.186)
check("XLV 1.75x of average -> no alert", got == [], [a["summary"] for a in got])
fresh_cache(avg={})
got = TP.check_volume_spikes({"XLV": snap(179.35, 175.65, 2.742e6, 5.87e6)}, set(), 0.186)
check("no average cached -> previous session still used", len(got) == 1)
fresh_cache(avg={"ESGU": 320e3})
got = TP.check_volume_spikes({"ESGU": snap(168.19, 167.5, 1.73e6, 320e3)}, set(), 0.186)
check("ESGU real surge still alerts", len(got) == 1 and got[0]["extra"]["baseline"] == "avg_volume", got)
check("summary names its baseline", got and "average daily volume 320,000" in got[0]["summary"],
      got and got[0]["summary"])
check("no unfounded 'major news' claim", got and "major news" not in got[0]["summary"])

print("3. ETF heavy buying already covered the surge (Feature 7 then Feature 6)")


class _Q:
    def __init__(self, rows): self.rows = rows
    def __getattr__(self, _): return lambda *a, **k: self
    def execute(self):
        class R: pass
        r = R(); r.data = self.rows; return r


class _SB:
    def __init__(self, rows): self.rows = rows
    def table(self, _): return _Q(self.rows)


orig = TP.supabase
TP.supabase = _SB([{"ticker": "XLV", "filing_type": "ETF_MOMENTUM_UP", "source": "ETF_SIGNAL"},
                   {"ticker": "IWM", "filing_type": "ETF_OUTPERFORM", "source": "ETF_SIGNAL"},
                   {"ticker": "XLP", "filing_type": "RSI_OVERBOUGHT", "source": "TECHNICAL"}])
try:
    sent = TP.load_sent_today()
finally:
    TP.supabase = orig
check("XLV heavy buying blocks its volume spike", ("XLV", "VOLUME_SPIKE") in sent, sent)
check("relative-performance signal does not", ("IWM", "VOLUME_SPIKE") not in sent, sent)
check("own ledger still read", ("XLP", "RSI_OVERBOUGHT") in sent, sent)

print("4. 52-week high made before the first check is reported at the first check")
# Levels now come from completed sessions, so a high set at the open is a
# breakout on the first run, measured against the real previous high.
fresh_cache(levels={"XLV": (178.86, 140.0)})
got = TP.check_52_week({"XLV": snap(179.40, 175.65, 1e6, 1e6, hi=179.81)}, set())
check("breakout reported on the first run", len(got) == 1, got)
check("names the real previous high", got and "previous high of $178.86" in got[0]["summary"], got)

print("5. Technical alerts describe, they do not predict")
import inspect   # noqa: E402
import re        # noqa: E402
# Alert text only: the f-strings, not the comments explaining past bugs.
src = " ".join(re.findall(r'f"[^"]*"', inspect.getsource(TP)))
for phrase in ("mean reversion", "breakup momentum", "golden cross", "death cross", "bargain hunters",
               "major news or institutional"):
    check(f"gone: {phrase!r}", phrase not in src)

print("6. Technical header uses the body's price")
AF._quote_cache["XLI"] = (10 ** 12, {"price": 166.67, "changePercentage": -0.01})
line = AF._price_line("XLI", {"ticker": "XLI", "source": "TECHNICAL",
                               "extra": {"price": 166.45, "change_pct": -0.13}})
check("XLI header = body", "$166.45" in line and "-0.13%" in line, line)
AF._quote_cache.pop("XLI", None)

print("7. A watched mutual fund is re-checked every daily run, not every other one")
import mf_poller as MP                    # noqa: E402
from datetime import timezone, timedelta  # noqa: E402
now = datetime(2026, 10, 21, 1, 40, 0, tzinfo=timezone.utc)
st = {"checked_at": "2026-10-20T01:40:07.788892+00:00", "last_error": None}
check("OAKMX checked 23h59m53s ago is due", MP._due(st, True, now))
check("not due again at the 12:40 run (11h later)",
      not MP._due({"checked_at": now.isoformat()}, True, now + timedelta(hours=11)))
check("unwatched fund still waits ~3 days",
      not MP._due({"checked_at": now.isoformat()}, False, now + timedelta(days=2)))
check("errored fund still waits ~7 days",
      not MP._due({"checked_at": now.isoformat(), "last_error": "x"}, True, now + timedelta(days=5)))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
