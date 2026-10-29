"""
Oct 28: "Fidelity Balanced Fund Fidelity Balanced Fund (FBALX) lowered ..." --
the retail class is named after the fund, and the alert printed both.
Run: python tests/test_alert_quality_oct28.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import mf_common as mc            # noqa: E402
import mf_expense_poller as E     # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


for fund, cls, want in (("Fidelity Balanced Fund", "Fidelity Balanced Fund", ""),
                        ("Fidelity Contrafund", "Contrafund", ""),
                        ("Fidelity Balanced Fund", "Class K", " Class K"),
                        ("The Growth Fund of America", "Class A", " Class A"),
                        ("Any Fund", None, "")):
    check(f"class_suffix({fund!r}, {cls!r})", mc.class_suffix(fund, cls) == want, mc.class_suffix(fund, cls))

s = E.build_alert("Fidelity Balanced Fund", "FIDELITY PURITAN TRUST", "FBALX", "Fidelity Balanced Fund",
                  {"net_expense_ratio": 0.46, "filing_date": "2025-10-27"}, {"net_expense_ratio": 0.44},
                  "net_expense_ratio", {"form": "485BPOS", "filing_date": "2026-10-27"})[0]
check("fund named once", s.startswith("Fidelity Balanced Fund (FBALX) lowered its net expense ratio"), s[:80])

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
