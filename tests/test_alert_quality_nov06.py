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

if failures:
    print("\nFAILURES:", failures); sys.exit(1)
print("\nALL PASS")
