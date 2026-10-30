"""
Oct 29: "Fund family: AB CAP FUND, INC.." -- SEC entity names ending in a
full stop got a second one from the alert template.
Run: python tests/test_alert_quality_oct30.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

import mf_common as mc            # noqa: E402

assert f"Fund family: {mc.family_text('AB CAP FUND, INC.')}." == "Fund family: AB CAP FUND, INC."
assert mc.family_text("Fidelity Puritan Trust") == "Fidelity Puritan Trust"
assert mc.family_text(None) == ""
print("  PASS  fund family ends with one full stop\n\nALL PASS")
