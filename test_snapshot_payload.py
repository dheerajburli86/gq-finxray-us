import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from unittest.mock import MagicMock
for m in ["supabase","dotenv","fmp_client","sec_financials","scraper_common","ai_pipeline"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a,**k: None

Q = [
 {"date":"2026-06-30","period":"Q3","calendarYear":"2026","revenue":83.03e9,"grossProfit":21.48e9,
  "operatingIncome":18.1e9,"netIncome":25.35e9,"eps":6.05,"costOfRevenue":61.5e9,
  "operatingExpenses":3.4e9,"_source":"SEC_XBRL"},
 {"date":"2026-03-31","period":"Q2","calendarYear":"2026","revenue":81.80e9,"grossProfit":21.11e9,
  "operatingIncome":17.5e9,"netIncome":-5.2e9,"eps":-1.24,"costOfRevenue":60.7e9,
  "operatingExpenses":3.6e9,"_source":"SEC_XBRL"},
]+[{"date":f"202{5-i}-12-31","period":"Q1","calendarYear":"2025","revenue":78.9e9,
    "grossProfit":20.0e9,"operatingIncome":16e9,"netIncome":22.0e9,"eps":5.75,
    "costOfRevenue":58.9e9,"operatingExpenses":3.5e9,"_source":"SEC_XBRL"} for i in range(4)]

import sec_financials, fmp_client
sec_financials.get_income_statement_sync = lambda cik, limit=8: Q
fmp_client.get_profile = lambda t: {"companyName":"Apple Inc."}

import result_snapshot as rs
snap = rs.build_result_snapshot("AAPL", "10-Q", cik="320193")
assert snap, "snapshot None"
print("keys present:", sorted(k for k in snap if k in ("quarters","cik","data_source")))
assert snap.get("quarters"), "REGRESSION: quarters missing"
assert snap.get("cik") == "320193", "REGRESSION: cik missing"

import gquants_format_converter as gq
payload = gq.xbrl_to_financial_results(
    ticker=snap["ticker"], cik=snap.get("cik",""), quarters=snap.get("quarters",[]),
    company_name=snap["company_name"], form_type=snap["form_type"])
assert payload, "PAYLOAD STILL EMPTY -- fix failed"
import json; print(json.dumps(payload, indent=2)[:900])
rows = {r["item"]: r for r in payload["financial_overview"]}
print("\nnet income prev (a loss):", rows["Net Income (USD)"]["prev_qtr_value"])
assert rows["Net Income (USD)"]["prev_qtr_value"] == "-$5.20B"
assert rows["Revenue (USD)"]["latest_qtr_value"] == "$83.03B"
assert payload["_source"] == "SEC_XBRL"
print("\n✅ RESULT SNAPSHOT PAYLOAD TEST PASS")
