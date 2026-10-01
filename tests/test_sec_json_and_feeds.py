"""
tests/test_sec_json_and_feeds.py

  1. SEC JSON endpoints are built correctly and reach the alert + payload_log.
  2. Dead publisher feeds fail over to an alternate URL and are eventually
     quarantined instead of being retried forever.

Run:  python tests/test_sec_json_and_feeds.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# ── 1. SEC JSON link construction ──────────────────────────────────────────
import sec_financials as sf

FILING = ("https://www.sec.gov/Archives/edgar/data/320193/000032019326000073/"
          "0000320193-26-000073-index.htm")
links = sf.build_sec_json_links("320193", FILING)
for k, v in links.items():
    print(f"  {k:<14} {v}")

assert links["companyfacts"] == \
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", links
assert links["submissions"] == \
    "https://data.sec.gov/submissions/CIK0000320193.json", links
assert links["filing_index"] == (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000073/index.json")
assert sf.build_sec_json_links("") == {}
assert "filing_index" not in sf.build_sec_json_links("320193")   # no filing URL
assert sf.build_sec_json_links("CIK320193")["companyfacts"].endswith("CIK0000320193.json")
print("SEC JSON endpoints built correctly ✓")

# ── 2. The links reach the delivered message ───────────────────────────────
import fmp_client
fmp_client.get_quote = lambda t: None
from alert_formatter import build_message

msg = build_message({
    "id": "a1", "ticker": "AAPL", "impact": "HIGH", "source": "SEC_EDGAR",
    "filing_type": "8-K", "summary": "Apple reported quarterly results.",
    "extra": {"company_name": "Apple Inc.", "sec_json": links,
              "item_types": ["2.02: Results of Operations and Financial Condition"]},
}, reason="AAPL is on your watchlist.")
assert "index.json" in msg, "SEC JSON link missing from the delivered alert"
assert "SEC filing data" in msg
print("SEC JSON link renders in the alert ✓")

# ── 3. payload_log records SEC-JSON alerts, not just built payloads ────────
import delivery
logged = {}
class T:
    def upsert(self, row, **k):
        logged.update(row); return self
    def execute(self):
        class R: data = []
        return R()
delivery.supabase = MagicMock()
delivery.supabase.table = lambda name: T()

delivery._log_payload({"id": "a1", "ticker": "AAPL", "source": "SEC_EDGAR",
                       "filing_type": "8-K", "extra": {"sec_json": links}})
assert logged, "an SEC-JSON alert was not logged at all"
assert logged["payload_type"] == "sec_json", logged
assert logged["payload"]["sec_json"]["companyfacts"].endswith("CIK0000320193.json")
assert "index.json" in (logged["frontend_link"] or "")
print("payload_log captures SEC JSON alerts ✓")

logged.clear()
delivery._log_payload({"id": "a2", "ticker": "MSFT", "extra": {}})
assert not logged, "an alert with no machine-readable data should not be logged"
print("alerts with no JSON/XBRL data are not logged ✓")

# ── 4. Feed failover + quarantine ──────────────────────────────────────────
sys.modules.setdefault("feedparser", MagicMock())
import news_poller as np

mw = [s for s in np.NEWS_SOURCES if s["name"] == "MarketWatch Banking"][0]
assert mw.get("alt_urls"), "dead MarketWatch feed has no alternate configured"
print(f"MarketWatch Banking alternates: {mw['alt_urls']}")

class Resp:
    def __init__(self, code): self.status_code = code; self.text = "<rss/>"

attempts = []
def fake_get(url, headers=None, timeout=None):
    attempts.append(url)
    return Resp(200 if "dowjones" in url else 403)

np._session = MagicMock()
np._session.get = fake_get
r, reason = np._fetch_feed(mw)
assert r is not None and reason is None, f"failover did not recover: {reason}"
assert any("dowjones" in u for u in attempts), attempts
print("dead feed fails over to its alternate ✓")

# A feed whose every URL is dead gets quarantined rather than retried forever.
np._session.get = lambda url, headers=None, timeout=None: Resp(404)
np._source_health.clear()
name = mw["name"]
for _ in range(np.FEED_QUARANTINE_AFTER):
    np._note_source_failure(name, mw["url"], "HTTP 404")
assert np._source_health[name].get("quarantined"), "permanently dead feed never quarantined"
assert np._source_is_paused(name), "quarantined feed is still being polled"
print(f"feed quarantined after {np.FEED_QUARANTINE_AFTER} failures ✓")

np._note_source_recovery(name)
assert not np._source_is_paused(name), "recovery did not clear quarantine"
print("recovery clears quarantine ✓")

print("\n✅ SEC JSON + FEED FAILOVER TEST PASS")
