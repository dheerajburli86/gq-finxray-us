"""
Renders an alert through the REAL delivery path.

This used to exercise main.format_alert(). That function was dead code: delivery.py
has always rendered through alert_formatter.build_message(), and main's copy was
never called by anything except this test — so the test passed while the code that
actually reaches subscribers went untested. main.format_alert has since been
deleted; this now covers the renderer that ships.

Checks the three things that break silently in production:
  A. no GQUANTS_ALERT_BASE_URL  -> no deep link, alert still renders
  B. base URL set               -> deep link present, above the feature footer
  C. XBRL/JSON links            -> SEC companyfacts / filing-index reach the reader
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock

for m in ["supabase", "telegram", "telegram.constants", "telegram.error", "dotenv",
          "fmp_client", "massive_client"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import importlib
import gquants_format_converter
import alert_formatter

ALERT = {
    "id": "9f1c-uuid", "ticker": "AAPL", "impact": "HIGH",
    "summary": "Revenue $83.03B, up 5.2% YoY", "source": "SEC_XBRL",
    "filing_type": "RESULT_SNAPSHOT",
    "created_at": "2026-09-09T13:00:00+00:00",
    "extra": {"period": "Q3 FY2026", "form_type": "10-Q",
              "structured_payload": {"type": "fr", "ticker": "AAPL"}},
}


def render(alert):
    importlib.reload(gquants_format_converter)
    importlib.reload(alert_formatter)
    return alert_formatter.build_message(alert, reason="AAPL is on your watchlist.")


print("=== A. GQUANTS_ALERT_BASE_URL UNSET ===")
os.environ.pop("GQUANTS_ALERT_BASE_URL", None)
a = render(ALERT)
print(a)
assert "View full report on GQuants" not in a, "no base URL configured, so no deep link may be rendered"
assert "AAPL" in a and "83.03B" in a, "the summary body must survive rendering"

print("\n=== B. BASE URL SET ===")
os.environ["GQUANTS_ALERT_BASE_URL"] = "https://app.gquants.com/alerts"
b = render(ALERT)
print(b)
assert "View full report on GQuants" in b, "deep link must render once a base URL is configured"
assert b.index("View full report") < b.index("Feature 3"), "link must sit above the feature footer"

print("\n=== C. XBRL / JSON LINKS REACH THE READER ===")
# Feature 1 alerts carry SEC's own machine-readable endpoints. Those links are
# the interim deliverable until the frontend renders the payload properly, so
# losing them silently would be invisible and total.
with_json = dict(ALERT)
with_json["extra"] = dict(ALERT["extra"])
with_json["extra"]["sec_json"] = {
    "companyfacts": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
    "filing_index": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000070/index.json",
}
c = render(with_json)
print(c)
assert "data.sec.gov" in c or "sec.gov/Archives" in c, "SEC JSON link must be rendered"

print("\n=== D. NO PAYLOAD (legacy alert) ===")
legacy = dict(ALERT)
legacy["extra"] = {"period": "Q3 FY2026", "form_type": "10-Q"}
d = render(legacy)
assert "View full report on GQuants" not in d, "an alert with no payload must not fabricate a link"
print("  renders normally without a payload")

print("\nALL FORMAT TESTS PASS")
