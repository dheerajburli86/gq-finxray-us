"""
Defects in the first mutual fund holdings alerts (Oct 26, FDGRX / FBALX / FSPTX).

  1. One issuer on several security lines read as an exit AND an add:
     "Added: SPACE EXPLORATION TECHNOLOGIES 1.16%; ... 0.84%. Exited: SPACE
     EXPLORATION TECHNOLOGIES 1.23%; ... 1.15%" for a stake that barely moved.
  2. N-PORT's free-text <ticker> leaked internal codes: "CEREBRAS SYSTEMS INC (2653800D)".
  3. A line grown from a placeholder read "+9129% beyond fund flows".

Run: python tests/test_alert_quality_oct26.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import mf_poller as MF   # noqa: E402

sys.modules["mf_company_data"] = type(sys)("mf_company_data")
sys.modules["mf_company_data"].describe = lambda keys: {}


def add(k, name, w):
    return {"security_key": k, "security_name": name, "weight_new": w, "weight_prev": None}


def rem(k, name, w):
    return {"security_key": k, "security_name": name, "weight_prev": w, "weight_new": None}


print("1. Issuer re-keyed across security lines")
d = {"added": [add("S3", "SPACE EXPLORATION TECHNOLOGIES CORP", 1.16),
               add("S4", "SPACE EXPLORATION TECHNOLOGIES CORP", 0.84),
               add("T2", "TAIWAN SEMICONDUCTOR MFG CO LTD", 1.66),
               add("I1", "INTEL CORP", 2.33)],
     "removed": [rem("S1", "SPACE EXPLORATION TECHNOLOGIES CORP", 1.23),
                 rem("S2", "SPACE EXPLORATION TECHNOLOGIES CORP", 1.15),
                 rem("T1", "Taiwan Semiconductor Mfg. Co. Ltd.", 0.74),
                 rem("C1", "CEREBRAS SYSTEMS INC", 0.62)],
     "resized": []}
text = MF.build_alert("Fund", "FDGRX", d, "2026-08-31", "2026-10-26", False)[0]
check("an unchanged re-keyed stake is not reported", "SPACE EXPLORATION" not in text, text)
check("a re-keyed stake that moved is one resize line",
      "TAIWAN SEMICONDUCTOR MFG CO LTD 0.74% -> 1.66% (now held through a different security line)" in text, text)
check("TSMC is no longer an exit", "previously 0.74%" not in text, text)
check("genuine add and exit kept", "INTEL CORP at 2.33%" in text and "CEREBRAS SYSTEMS INC, previously 0.62%" in text, text)

print("2. Only exchange-style tickers are shown")
rows = MF._as_etf_rows({"a": {"ticker": "2653800D"}, "b": {"ticker": "nvda"}, "c": {"ticker": "BRK.B"},
                        "d": {"ticker": "2330"}, "e": {"ticker": None}})
check("internal code dropped", rows["a"]["security_symbol"] is None)
check("local numeric code dropped", rows["d"]["security_symbol"] is None)
check("real symbols kept", rows["b"]["security_symbol"] == "NVDA" and rows["c"]["security_symbol"] == "BRK.B")
check("security_key untouched", MF._as_etf_rows({"2653800D": {"ticker": "2653800D"}}).keys() == {"2653800D"})

print("3. Token positions are described, not given absurd percentages")
d = {"added": [], "removed": [], "resized": [
    {"security_key": "M", "security_name": "MICRON TECHNOLOGY INC", "weight_prev": 0.01, "weight_new": 1.19,
     "shares_vs_flow_pct": 9129.0},
    {"security_key": "A", "security_name": "AMAZON.COM INC", "weight_prev": 5.30, "weight_new": 3.23,
     "shares_vs_flow_pct": -37.0}]}
text = MF.build_alert("Fund", "FDGRX", d, "2026-08-31", "2026-10-26", False)[0]
check("built up from a token position", "0.01% -> 1.19% (built up from a token position)" in text, text)
check("no 9129%", "9129" not in text, text)
check("ordinary resize unchanged", "5.30% -> 3.23% (position -37% beyond fund flows)" in text, text)

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
