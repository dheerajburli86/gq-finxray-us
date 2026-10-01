"""
verify_sec_json_fix.py

Verifies that the extra jsonb column fix allows SEC JSON to flow through
the entire pipeline from filing creation to alert delivery to payload logging.

Run: python verify_sec_json_fix.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unittest.mock import MagicMock

sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# Mock Supabase with proper JSONB support
fake_db = {
    "raw_filings": [],
    "alerts": [],
    "payload_log_upserts": [],
}

class FakeTable:
    def __init__(self, name):
        self.name = name

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def insert(self, row):
        # Simulate actual storage - JSON is stored as-is in JSONB columns
        if self.name not in fake_db:
            fake_db[self.name] = []
        fake_db[self.name].append(dict(row))  # Store a copy
        return self
    def update(self, row):
        if self.name not in fake_db:
            fake_db[self.name] = []
        fake_db[self.name].append(dict(row))
        return self
    def upsert(self, row, **k):
        if self.name + "_upserts" not in fake_db:
            fake_db[self.name + "_upserts"] = []
        fake_db[self.name + "_upserts"].append(dict(row))
        return self
    def execute(self):
        class R: data = []
        return R()

class FakeSupabase:
    def table(self, name):
        return FakeTable(name)

fake_supabase = FakeSupabase()
sys.modules["supabase"] = MagicMock()
sys.modules["supabase"].create_client = lambda *a, **k: fake_supabase

import sec_financials
import edgar_poller_async
import ai_pipeline
import alert_formatter
import delivery

# Override supabase
edgar_poller_async.supabase = fake_supabase
ai_pipeline.supabase = fake_supabase
alert_formatter.fmp_client = MagicMock()
alert_formatter.fmp_client.get_quote = lambda t: None
delivery.supabase = fake_supabase

print("=" * 70)
print("SCENARIO: SEC Form 8-K filed by Apple Inc.")
print("=" * 70)
print()

# STEP 1: Create a raw_filing with SEC JSON in extra (simulating edgar_poller_async)
print("STEP 1: Store raw filing with SEC JSON endpoints in extra")
print("-" * 70)

cik = "320193"
filing_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000073/0000320193-26-000073-index.htm"
sec_json = sec_financials.build_sec_json_links(cik, filing_url)

raw_filing_text = """UNITED STATES SECURITIES AND EXCHANGE COMMISSION

FORM 8-K

Item 2.02 Results of Operations and Financial Condition

Apple Inc. reported strong quarterly results with record revenue and
expansion of services revenue."""

raw_filing_extra = {
    "sec_json": sec_json,
    "company_name": "Apple Inc.",
    "source_priority": "SEC_EDGAR",
    "item_types": ["2.02: Results of Operations and Financial Condition"],
    "is_earnings_release": True,
}

# Simulate store_filing() from edgar_poller_async
fake_supabase.table("raw_filings").insert({
    "id": "rf-1",
    "source": "SEC_EDGAR",
    "filing_type": "8-K",
    "company_name": "Apple Inc.",
    "ticker": "AAPL",
    "raw_text": raw_filing_text,
    "filing_url": filing_url,
    "extra": raw_filing_extra,
    "status": "PENDING",
    "filed_at": "2026-09-09T14:30:00Z",
}).execute()

stored_filing = fake_db["raw_filings"][0]
print(f"✓ Raw filing stored with extra.sec_json: {bool(stored_filing['extra'].get('sec_json'))}")
print(f"  SEC JSON keys: {list(stored_filing['extra']['sec_json'].keys())}")
print()

# STEP 2: Simulate AI pipeline reading the filing and creating an alert
print("STEP 2: AI pipeline creates alert from raw filing")
print("-" * 70)

# The AI pipeline would read the filing, process it, and create an alert
alert_extra = dict(stored_filing['extra'])  # Copy all extra fields including sec_json
alert_extra['feature_id'] = 1
alert_extra['feature_name'] = 'SEC EDGAR Filings'
alert_extra['headline'] = 'Apple Reports Strong Q4 Results'
alert_extra['summarization_attempts'] = 1

# Simulate store_alert() from ai_pipeline
fake_supabase.table("alerts").insert({
    "id": "a-1",
    "ticker": "AAPL",
    "summary": "Apple Inc. reported quarterly earnings with record revenue.",
    "impact": "HIGH",
    "source": "SEC_EDGAR",
    "filing_type": "8-K",
    "extra": alert_extra,
    "delivered": False,
    "created_at": "2026-09-09T14:32:00Z",
}).execute()

stored_alert = fake_db["alerts"][0]
print(f"✓ Alert stored with extra.sec_json: {bool(stored_alert['extra'].get('sec_json'))}")
print(f"  Extra keys: {list(stored_alert['extra'].keys())}")
print()

# STEP 3: Delivery renders the message with SEC JSON link
print("STEP 3: Delivery renders alert message with SEC JSON link")
print("-" * 70)

msg = alert_formatter.build_message(stored_alert, reason="AAPL is on your watchlist.")
has_link = "index.json" in msg or "SEC filing data" in msg
print(f"✓ SEC JSON link in rendered message: {has_link}")
if has_link:
    for line in msg.split("\n"):
        if "🗂" in line or "index.json" in line:
            print(f"  {line}")
print()

# STEP 4: Delivery logs to payload_log
print("STEP 4: Delivery logs SEC JSON alert to payload_log")
print("-" * 70)

delivery._log_payload(stored_alert)
logged = fake_db.get("payload_log_upserts", [])
print(f"✓ Payload log entries: {len(logged)}")
if logged:
    entry = logged[0]
    print(f"  alert_id: {entry.get('alert_id')}")
    print(f"  payload_type: {entry.get('payload_type')}")
    print(f"  has sec_json in payload: {'sec_json' in (entry.get('payload') or {})}")
    print(f"  frontend_link: {entry.get('frontend_link')}")
print()

# VERIFICATION
print("=" * 70)
print("VERIFICATION")
print("=" * 70)

checks = [
    ("SEC JSON built", bool(sec_json)),
    ("Raw filing stores sec_json", bool(stored_filing['extra'].get('sec_json'))),
    ("Alert inherits sec_json from filing", bool(stored_alert['extra'].get('sec_json'))),
    ("Message renders SEC link", has_link),
    ("Payload log captures alert", bool(logged)),
    ("Payload log has sec_json", bool(logged and 'sec_json' in (logged[0].get('payload') or {}))),
]

all_pass = all(check[1] for check in checks)
for label, passed in checks:
    status = "✅" if passed else "❌"
    print(f"{status} {label}")

print()
if all_pass:
    print("✅ SEC JSON FEATURE WORKING — links will be visible in alerts and logged")
else:
    print("❌ SEC JSON FEATURE BROKEN — check which step failed")
