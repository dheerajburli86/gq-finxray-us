"""
Oct 29: XLV and SCHD each got three separate messages in one 10:21 run
(heavy buying, RSI overbought, 52-week high). One message per ETF per run now.
Run: python tests/test_alert_quality_oct30.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import mf_common as mc                  # noqa: E402
import etf_signals_poller as ESP        # noqa: E402

failures = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)

assert f"Fund family: {mc.family_text('AB CAP FUND, INC.')}." == "Fund family: AB CAP FUND, INC."
assert mc.family_text("Fidelity Puritan Trust") == "Fidelity Puritan Trust"
assert mc.family_text(None) == ""
print("  PASS  fund family ends with one full stop")

def a(t, ft, impact, head, key):
    return {"ticker": t, "summary": head + ".", "impact": impact, "source": "ETF_SIGNAL",
            "filing_type": ft, "extra": {"dedup_key": key, "headline": head}}

rows = [a("XLV", "ETF_MOMENTUM_UP", "HIGH", "Heavy Buying", "k1"),
        a("XLV", "TA_RSI_OVERBOUGHT", "MEDIUM", "RSI Overbought", "k2"),
        a("XLV", "TA_52W_HIGH", "HIGH", "52-Week High", "k3"),
        a("SCHD", "ETF_MOMENTUM_UP", "MEDIUM", "Heavy Buying", "k4"),
        a("VB", "ETF_OUTPERF", "MEDIUM", "Outperforming", "k5")]
out = ESP.merge_alerts_by_ticker(rows)
check("five alerts, three tickers -> three messages", len(out) == 3, [o["ticker"] for o in out])
xlv = next(o for o in out if o["ticker"] == "XLV")
check("XLV led by HIGH", xlv["impact"] == "HIGH", xlv["impact"])
check("XLV keeps all three keys", sorted([xlv["extra"]["dedup_key"]] + xlv["extra"]["merged_dedup_keys"])
      == ["k1", "k2", "k3"], xlv["extra"])
check("VB untouched", next(o for o in out if o["ticker"] == "VB")["extra"].get("merged_dedup_keys") is None)
check("empty run", ESP.merge_alerts_by_ticker([]) == [])

if failures:
    print("\nFAILURES:", failures); sys.exit(1)
print("\nALL PASS")
