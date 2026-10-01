"""
diagnose_sec_json.py

Checks if SEC JSON endpoints are:
  1. Being built and attached to raw_filings.extra
  2. Being read and passed through to alerts.extra
  3. Being rendered in the alert message
  4. Being logged to payload_log

Run: python diagnose_sec_json.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unittest.mock import MagicMock

sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# Mock Supabase
fake_db = {
    "raw_filings": [],
    "alerts": [],
    "payload_log_upserts": [],
}

class FakeTable:
    def __init__(self, name):
        self.name = name
        self._data = []
        self._filters = {}

    def select(self, *a, **k): return self
    def eq(self, *a, **k):
        self._filters.update({a[0]: a[1]} if a else {})
        return self
    def insert(self, row):
        self._data.append(row)
        if self.name not in fake_db:
            fake_db[self.name] = []
        fake_db[self.name].append(row)
        return self
    def update(self, row):
        if self.name not in fake_db:
            fake_db[self.name] = []
        fake_db[self.name].append(row)
        return self
    def upsert(self, row, **k):
        if self.name + "_upserts" not in fake_db:
            fake_db[self.name + "_upserts"] = []
        fake_db[self.name + "_upserts"].append(row)
        return self
    def execute(self):
        class R: data = self._data or []
        return R()

class FakeSupabase:
    def table(self, name):
        return FakeTable(name)

fake_supabase = FakeSupabase()
sys.modules["supabase"] = MagicMock()
sys.modules["supabase"].create_client = lambda *a, **k: fake_supabase

import sec_financials
import ai_pipeline
import alert_formatter
import delivery

# Override supabase in all modules
ai_pipeline.supabase = fake_supabase
alert_formatter.fmp_client = MagicMock()
alert_formatter.fmp_client.get_quote = lambda t: None
delivery.supabase = fake_supabase

print("=" * 70)
print("STEP 1: Build SEC JSON endpoints")
print("=" * 70)

cik = "320193"
filing_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000073/0000320193-26-000073-index.htm"
sec_json = sec_financials.build_sec_json_links(cik, filing_url)
print(f"CIK: {cik}")
print(f"SEC JSON endpoints built:")
for k, v in sec_json.items():
    print(f"  {k}: {v}")
print()

print("=" * 70)
print("STEP 2: Simulate raw_filing storage with SEC JSON in extra")
print("=" * 70)

extra_with_json = {
    "sec_json": sec_json,
    "company_name": "Apple Inc.",
    "source_priority": "SEC_EDGAR",
}

raw_filing = {
    "id": "rf-1",
    "source": "SEC_EDGAR",
    "filing_type": "8-K",
    "company_name": "Apple Inc.",
    "ticker": "AAPL",
    "raw_text": "Item 2.02: Results of Operations",
    "filing_url": filing_url,
    "extra": extra_with_json,
    "status": "PENDING",
}

print(f"Raw filing extra.sec_json: {extra_with_json.get('sec_json') is not None}")
print(f"SEC JSON in extra: {list(extra_with_json.get('sec_json', {}).keys())}")
print()

print("=" * 70)
print("STEP 3: Simulate alert creation from raw_filing")
print("=" * 70)

# Simulate what ai_pipeline does
alert = {
    "id": "a-1",
    "ticker": "AAPL",
    "summary": "Apple reported quarterly results.",
    "impact": "HIGH",
    "source": "SEC_EDGAR",
    "filing_type": "8-K",
    "extra": extra_with_json,  # The extra should include sec_json
    "delivered": False,
}

print(f"Alert extra.sec_json: {alert.get('extra', {}).get('sec_json') is not None}")
print(f"SEC JSON in alert.extra: {list(alert.get('extra', {}).get('sec_json', {}).keys())}")
print()

print("=" * 70)
print("STEP 4: Check if alert_formatter renders the SEC JSON link")
print("=" * 70)

msg = alert_formatter.build_message(alert, reason="AAPL is on your watchlist.")
has_sec_link = "index.json" in msg or "SEC XBRL" in msg or "SEC filing data" in msg
print(f"SEC JSON link in rendered message: {has_sec_link}")
if has_sec_link:
    for line in msg.split("\n"):
        if "SEC" in line or "index.json" in line or "🗂" in line:
            print(f"  {line}")
print()

print("=" * 70)
print("STEP 5: Check if delivery._log_payload captures SEC JSON")
print("=" * 70)

delivery._log_payload(alert)
logged = fake_db.get("payload_log_upserts", [])
print(f"Payload log entries: {len(logged)}")
if logged:
    entry = logged[0]
    print(f"  alert_id: {entry.get('alert_id')}")
    print(f"  payload_type: {entry.get('payload_type')}")
    print(f"  frontend_link: {entry.get('frontend_link')}")
    payload = entry.get('payload', {})
    print(f"  payload has sec_json: {'sec_json' in payload}")
print()

print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"✓ SEC JSON endpoints built: {bool(sec_json)}")
print(f"✓ SEC JSON in raw_filing.extra: {extra_with_json.get('sec_json') is not None}")
print(f"✓ SEC JSON in alert.extra: {alert.get('extra', {}).get('sec_json') is not None}")
print(f"✓ SEC JSON link rendered: {has_sec_link}")
print(f"✓ SEC JSON logged to payload_log: {bool(logged)}")

if all([bool(sec_json), extra_with_json.get('sec_json'),
        alert.get('extra', {}).get('sec_json'), has_sec_link, bool(logged)]):
    print("\n✅ ALL CHECKS PASS — SEC JSON chain is working correctly")
else:
    print("\n❌ ISSUE DETECTED — check which step failed above")
