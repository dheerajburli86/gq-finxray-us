import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from unittest.mock import MagicMock
from datetime import datetime

for m in ["supabase","telegram","telegram.constants","dotenv","schedule",
          "fmp_client","massive_client","edgar_poller_async","fmp_poller",
          "news_poller","technical_poller","etf_flow_poller","ipo_poller",
          "earnings_transcript_poller","news_roundup","heatmap_generator",
          "result_snapshot","ai_pipeline","telegram_bot","scraper_common",
          "fmp_fundamentals","fmp_scraper","sec_client","sec_financials"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import feature_map
sys.modules["feature_map"] = feature_map
import main

main.get_stock_price = lambda t: None
alert = {
    "id": "9f1c-uuid", "ticker": "AAPL", "impact": "HIGH",
    "summary": "Revenue $83.03B, up 5.2% YoY", "source": "SEC_XBRL",
    "filing_type": "RESULT_SNAPSHOT",
    "extra": {"period": "Q3 FY2026", "form_type": "10-Q",
              "structured_payload": {"type": "fr", "ticker": "AAPL"}},
}

print("=== A. GQUANTS_ALERT_BASE_URL UNSET ===")
__import__("os").environ.pop("GQUANTS_ALERT_BASE_URL", None)
import importlib, gquants_format_converter
importlib.reload(gquants_format_converter); main.gq_fmt = gquants_format_converter
a = main.format_alert(alert); print(a); assert "🔗" not in a
print("\n=== B. BASE URL SET ===")
__import__("os").environ["GQUANTS_ALERT_BASE_URL"] = "https://app.gquants.com/alerts"
importlib.reload(gquants_format_converter); main.gq_fmt = gquants_format_converter
b = main.format_alert(alert); print(b)
assert "🔗" in b and b.index("🔗") < b.index("🏷"), "link must sit above footer"
print("\n=== C. NO PAYLOAD (legacy alert) ===")
legacy = dict(alert); legacy["extra"] = {"period":"Q3 FY2026","form_type":"10-Q"}
c = main.format_alert(legacy); assert "🔗" not in c
print("  no link, renders normally ✓")
print("\nALL FORMAT TESTS PASS")
