"""
Nov 5: GDX "Added: Torex Resources Inc (TXR.TO) at 0.55%. Removed: Other,
previously 0.55%." -- the data feed naming its catch-all line, not a trade.
Run: python tests/test_alert_quality_nov06.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import etf_data_poller as EDP   # noqa: E402

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)

other = {"action": "REMOVED", "security_name": "Other", "weight_prev": 0.55}
torex = {"action": "ADDED", "security_name": "Torex Resources Inc", "security_symbol": "TXR.TO", "weight_new": 0.55}
newco = {"action": "ADDED", "security_name": "Newmont Corp", "security_symbol": "NEM", "weight_new": 3.10}
check("'Other' is not alertable", not EDP.is_alertable_line(other))
for n in ("Others", "OTHER SECURITIES", "Unclassified", "-", ""):
    check(f"placeholder: {n!r}", EDP.is_placeholder({"security_name": n}))
for n in ("Otherworld Inc", "Other Wordly Mining", "Torex Resources Inc", "OTIS WORLDWIDE"):
    check(f"real name kept: {n!r}", EDP.is_alertable_line({"security_name": n}))
out = EDP.drop_named_placeholders([torex, newco], [other, torex, newco])
check("Torex absorbed by 'Other', Newmont kept", out == [newco], out)
check("no placeholder removed -> nothing dropped",
      EDP.drop_named_placeholders([torex], [torex]) == [torex])
check("weight far apart -> real add kept",
      EDP.drop_named_placeholders([newco], [other, newco]) == [newco])

print("Crash day: repeats need a widening step and stop after 2 (VTV, Nov 5)")
import datetime as _d
import etf_signals_poller as ESP   # noqa: E402
_day = _d.date(2026, 11, 5)
def _ev(z, sent, lv, ct):
    sigma = 0.0085
    base = {"sigma": sigma, "adv": 3.0e6, "beta": None, "rets": [0.0] * 300,
            "sessions": [{"date": _day}] * 301, "last_close": 180.0}
    m = {"price": 180.0 * (1 - z * sigma), "prev_close": 180.0, "volume": 3.0e6 * 0.5 * 2.0}
    return ESP.evaluate_intraday("VTV", "VTV", base, m, 0.5, _day.isoformat(), sent,
                                 levels=lv, counts=ct)
kinds = lambda out: [a["filing_type"] for a in out]
sent, lv, ct = set(), {}, {}
check("2.6x: first big drop sent", kinds(_ev(2.6, sent, lv, ct)) == ["ETF_BIG_MOVE_DOWN"])
check("3.15x: first repeat sent (+0.5)", kinds(_ev(3.15, sent, lv, ct)) == ["ETF_BIG_MOVE_DOWN"])
check("3.9x: second repeat needs +1.0 -> not sent", _ev(3.9, sent, lv, ct) == [])
check("4.2x: second repeat sent", kinds(_ev(4.2, sent, lv, ct)) == ["ETF_BIG_MOVE_DOWN"])
check("6.0x: cap reached, nothing more today", _ev(6.0, sent, lv, ct) == [])
check("count tracked", ct.get(("VTV", "DOWN")) == 3, ct)
rows = [{"ticker": "VTV", "filing_type": "ETF_BIG_MOVE_DOWN", "extra": {}},
        {"ticker": "VTV", "filing_type": "52W_LOW", "extra": {"merged_filing_types": ["ETF_BIG_MOVE_DOWN"]}},
        {"ticker": "VB", "filing_type": "ETF_MOMENTUM_DOWN", "extra": {}}]
check("counts read back from stored rows (merged included)",
      ESP.counts_from_rows(rows) == {("VTV", "DOWN"): 2, ("VB", "DOWN"): 1}, ESP.counts_from_rows(rows))

if failures:
    print("\nFAILURES:", failures); sys.exit(1)
print("\nALL PASS")
