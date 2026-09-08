"""
The deep link must appear in the message delivery.py ACTUALLY sends.

tests/test_format_alert.py asserts against main.format_alert(), which nothing
in the delivery path calls -- delivery.py renders via alert_formatter.
build_message(). That test passed while real alerts shipped no link at all.
This one pins the real renderer.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

for m in ["supabase", "dotenv", "fmp_client"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import fmp_client
fmp_client.get_quote = lambda t: None

import alert_formatter

ALERT = {
    "id": "9f1c-uuid", "ticker": "AAPL", "impact": "HIGH",
    "summary": "Revenue $83.03B, up 5.2% YoY", "source": "SEC_XBRL",
    "filing_type": "RESULT_SNAPSHOT",
    "extra": {"period": "Q3 FY2026", "form_type": "10-Q",
              "company_name": "Apple Inc.",
              "structured_payload": {"type": "fr", "ticker": "AAPL"}},
}

print("=== A. base URL unset -> no link, no crash ===")
os.environ.pop("GQUANTS_ALERT_BASE_URL", None)
a = alert_formatter.build_message(ALERT, reason="AAPL is on your watchlist.")
assert "GQuants" not in a, "link must not render without a configured base URL"
print("  no link ✓")

print("\n=== B. base URL set -> link renders in the DELIVERED message ===")
os.environ["GQUANTS_ALERT_BASE_URL"] = "https://app.gquants.com/alerts"
b = alert_formatter.build_message(ALERT, reason="AAPL is on your watchlist.")
print(b)
assert "View full report on GQuants" in b, "REGRESSION: deep link absent from real render path"
assert "type=fr" in b and "ticker=AAPL" in b
assert b.index("View full report on GQuants") < b.index("\U0001f3f7"), "link must sit above the footer"
assert "<a href=" in b and b.count("<a ") == b.count("</a>"), "unbalanced anchor tags"

print("\n=== C. legacy alert with no payload still renders ===")
legacy = dict(ALERT); legacy["extra"] = {"period": "Q3 FY2026"}
c = alert_formatter.build_message(legacy, reason="AAPL is on your watchlist.")
assert "GQuants" not in c
print("  renders normally ✓")

print("\n=== D. malformed payload must not break delivery ===")
bad = dict(ALERT); bad["extra"] = {"structured_payload": "not-a-dict"}
d = alert_formatter.build_message(bad, reason="x")
assert d, "a malformed payload must not raise -- it would drop the alert"
print("  survived ✓")

print("\n✅ DELIVERY LINK TEST PASS")
