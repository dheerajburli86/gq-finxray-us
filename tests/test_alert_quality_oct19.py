"""
Defects found by reading the 22 alerts delivered on 2026-10-19 (Monday).
Every case below is taken from real alert text, not invented.

Run: python tests/test_alert_quality_oct19.py
"""
import datetime as dt
import os
import sys

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


import etf_data_poller as EDP        # noqa: E402
import etf_signals_poller as ESP     # noqa: E402
import alert_formatter as AF         # noqa: E402

print("1. Money-market sweep funds are cash, not positions")
for n in ("GOLDMAN FS TRSY OBLIG INST 468", "BLACKROCK LIQUIDITY FUNDS T-FUND",
          "FIDELITY GOVT CASH RESERVES", "Financial Square Treasury Obligations"):
    check(f"not alertable: {n}", not EDP.is_alertable_line({"security_name": n}))
for n in ("TESLA INC", "Prime Medicine Inc", "GOVERNMENT PROPERTIES INCOME TRUST",
          "TREASURY WINE ESTATES", "Primerica Inc", "US TREASURY N/B 4.25% 2030"):
    check(f"still alertable: {n}", EDP.is_alertable_line({"security_name": n}))

print("2. Expense alert says when the vendor ratio is pre-waiver")
note = EDP.fee_disagreement_note(0.94, {"gross_expense_ratio": 0.94, "net_expense_ratio": 0.78})
check("TQQQ: waiver note names the net 0.78%", "before the fund's fee waiver" in note and "0.78%" in note, note)
check("SQQQ: agreeing table adds nothing",
      EDP.fee_disagreement_note(0.94, {"gross_expense_ratio": 0.94, "net_expense_ratio": 0.94}) == "")
check("no net figure adds nothing", EDP.fee_disagreement_note(0.94, {"gross_expense_ratio": 0.94}) == "")
odd = EDP.fee_disagreement_note(0.94, {"gross_expense_ratio": 1.10, "net_expense_ratio": 0.80})
check("unexplained mismatch is flagged", "does not match" in odd, odd)

print("3. One message per ETF per session close")
def row(ft, impact, head, key):
    return {"ticker": "TQQQ", "summary": head + ".", "impact": impact, "source": "ETF_SIGNAL",
            "filing_type": ft, "extra": {"dedup_key": key, "headline": head}}
merged = ESP.merge_session_alerts([
    row("ETF_STREAK_DOWN", "MEDIUM", "5-Day Losing Streak", "k_streak"),
    row("ETF_DRAWDOWN_20", "HIGH", "Enters Bear Market Territory", "k_dd")])
check("two signals -> one alert", len(merged) == 1, merged)
m = merged[0]
check("led by the most severe", m["impact"] == "HIGH" and m["filing_type"] == "ETF_DRAWDOWN_20", m)
check("headline names both", m["extra"]["headline"] == "Enters Bear Market Territory · 5-Day Losing Streak",
      m["extra"]["headline"])
check("lead dedup key kept", m["extra"]["dedup_key"] == "k_dd")
check("merged key recorded", m["extra"]["merged_dedup_keys"] == ["k_streak"])
check("both bodies present", "Bear Market" in m["summary"] and "Losing Streak" in m["summary"])
check("single signal untouched", ESP.merge_session_alerts([row("X", "MEDIUM", "h", "k")])[0]["extra"]
      .get("merged_dedup_keys") is None)


class _Q:
    def __init__(self, rows): self.rows = rows
    def __getattr__(self, _): return lambda *a, **k: self
    def execute(self):
        class R: pass
        r = R(); r.data = self.rows; return r


rows = [{"id": 1, "extra": {"dedup_key": "k_dd", "merged_dedup_keys": ["k_streak"]}},
        {"id": 2, "extra": {"dedup_key": "k_other"}}]
_orig = ESP._paged
ESP._paged = lambda f: rows
try:
    sent = ESP.load_sent()
finally:
    ESP._paged = _orig
check("dedup ledger reads merged keys back", sent == {"k_dd", "k_streak", "k_other"}, sent)

print("4. Heavy selling then underperforming is one move, not two alerts")
day = dt.date(2026, 10, 19)
base = {"sigma": 0.0353, "adv": 9.3e6, "beta": -3.46, "r2": 0.5, "resid_sigma": 0.0261,
        "rets": [0.001] * 60, "sessions": [{"date": day - dt.timedelta(days=i)} for i in range(61, 0, -1)]}
prev = 18.50


def ev(r, vol, sent, bench=0.007):
    return ESP.evaluate_intraday("UVXY", "UVXY", base, {"price": prev * (1 + r), "prev_close": prev,
                                                       "volume": vol}, 0.4, day.isoformat(), sent,
                                 bench={"r": bench})


got = [a["filing_type"] for a in ev(-0.0843, 9.3e6 * 0.4 * 3.6, set())]
check("UVXY drop on heavy volume: momentum only", got == ["ETF_MOMENTUM_DOWN"], got)
s = {ESP.dedup_key("UVXY", "ETF_MOMENTUM_DOWN", day.isoformat())}
got = [a["filing_type"] for a in ev(-0.0843, 9.3e6 * 0.4 * 1.0, s)]
check("later run, same direction: still suppressed", got == [], got)
got = [a["filing_type"] for a in ev(-0.0843, 9.3e6 * 0.4 * 1.0, set())]
check("UVXY (beta -3.46) never gets a relative signal (Oct 23 rule)", got == [], got)
base["beta"] = 1.2
got = [a["filing_type"] for a in ev(-0.0843, 9.3e6 * 0.4 * 1.0, set())]
check("control: no momentum today -> underperform goes out", got == ["ETF_UNDERPERFORM"], got)
base["beta"] = -3.46

print("5. Header price is the price the body was computed from")
alert = {"ticker": "TQQQ", "source": "ETF_SIGNAL", "extra": {"price": 71.83, "change_pct": 5.30}}
AF._quote_cache["TQQQ"] = (10 ** 12, {"price": 71.68, "changePercentage": 5.09})
line = AF._price_line("TQQQ", alert)
check("snapshot price used", "$71.83" in line and "+5.30%" in line, line)
news = {"ticker": "TQQQ", "source": "FMP_NEWS", "extra": {"price": 1.0, "change_pct": 1.0}}
line = AF._price_line("TQQQ", news)
check("other sources keep the live quote", "$71.68" in line, line)
eod = {"ticker": "TQQQ", "source": "ETF_SIGNAL", "extra": {"close": 68.21}}
check("streak/drawdown alerts (no snapshot) keep the live quote", "$71.68" in AF._price_line("TQQQ", eod))
AF._quote_cache.pop("TQQQ", None)

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
