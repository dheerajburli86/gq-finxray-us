"""
tests/test_freshness_and_8k_items.py

1. Queues must be newest-first, or a backlog delivers 12-hour-old news ahead
   of what just broke.
2. 8-K item classification, including the Item 2.02 earnings release.

Run:  python tests/test_freshness_and_8k_items.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# ── 1. Both queues order newest-first and expire the stale tail ────────────
calls = {"order": [], "expired": [], "or_": []}


class Q:
    def __init__(self, table):
        self.table = table
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def or_(self, filter_string):
        calls["or_"].append((self.table, filter_string))
        return self
    @property
    def not_(self): return self
    def limit(self, *a, **k): return self
    def order(self, col, desc=False):
        calls["order"].append((self.table, col, desc))
        return self
    def update(self, patch):
        calls["expired"].append((self.table, patch))
        return self
    def execute(self):
        class R: data = []
        return R()


import ai_pipeline as ap
ap.supabase = MagicMock()
ap.supabase.table = lambda name: Q(name)
ap.budget_exhausted = lambda: False
ap.run_pipeline()

orders = [o for o in calls["order"] if o[0] == "raw_filings"]
assert orders, "pipeline never ordered its queue"
assert orders[0][2] is True, \
    f"pipeline queue is still oldest-first {orders[0]} — backlog blocks fresh news"
assert any(t == "raw_filings" and p.get("status") == "EXPIRED"
           for t, p in calls["expired"]), "pipeline never expires its stale tail"
print("pipeline: newest-first + stale tail expired ✓")

# The priority tier must match on filing_type as well as source. PRIORITY_FILING_TYPES
# was defined and never referenced, so anything in it that does NOT arrive under a
# priority source was silently never prioritised — in practice INSIDER_FMP, which FMP
# writes as source="FMP_NEWS", queueing insider trades behind the news backlog they
# were listed to jump.
prio = [f for t, f in calls["or_"] if t == "raw_filings"]
assert prio, "pipeline no longer prioritises its queue at all"
assert "source.in." in prio[0], "priority tier stopped matching on source"
assert "filing_type.in." in prio[0], \
    "priority tier matches source only — PRIORITY_FILING_TYPES is dead again"
for ft in ap.PRIORITY_FILING_TYPES:
    assert f'"{ft}"' in prio[0], f"{ft} listed as priority but not in the query"
print(f"pipeline: priority tier matches source OR filing_type ✓")

calls["order"].clear(); calls["expired"].clear(); calls["or_"].clear()

import fmp_client
fmp_client.get_quote = lambda t: None
import delivery
delivery.supabase = MagicMock()
delivery.supabase.table = lambda name: Q(name)
delivery._expire_stale_alerts()
delivery._fetch_undelivered()

orders = [o for o in calls["order"] if o[0] == "alerts"]
assert orders and orders[0][2] is True, \
    f"delivery queue is still oldest-first {orders} — stale alerts go out first"
assert any(t == "alerts" and p.get("delivered") is True
           for t, p in calls["expired"]), "delivery never retires stale alerts"
print("delivery: newest-first + stale alerts retired ✓")

# ── 2. 8-K item classification ─────────────────────────────────────────────
sys.modules.setdefault("aiohttp", MagicMock())
from edgar_poller_async import extract_8k_items, is_earnings_8k

earnings = """UNITED STATES SECURITIES AND EXCHANGE COMMISSION
Item 2.02. Results of Operations and Financial Condition.
On October 30, 2026, Apple Inc. issued a press release announcing results.
Item 9.01. Financial Statements and Exhibits.
Exhibit 99.1 Press release dated October 30, 2026."""

items = extract_8k_items(earnings)
print(f"earnings 8-K -> {items}")
assert is_earnings_8k(items), "Item 2.02 earnings release not detected"
assert items[0].startswith("2.02"), "the meaningful item must lead, not 9.01"
assert any(i.startswith("9.01") for i in items), "9.01 should still be listed"

exec_change = """Item 5.02 Departure of Directors or Certain Officers.
On June 1, the Board appointed a new Chief Financial Officer.
Item 9.01 Financial Statements and Exhibits."""
items = extract_8k_items(exec_change)
print(f"exec-change 8-K -> {items}")
assert not is_earnings_8k(items), "a 5.02 must not be flagged as earnings"
assert items[0].startswith("5.02")

assert extract_8k_items("") == []
assert extract_8k_items("no item numbers anywhere in this text") == []
assert extract_8k_items("Item 99.99 Not A Real Item") == []
assert not is_earnings_8k([])
print("item extraction: earnings flagged, others not, junk ignored ✓")

# Case/spacing variations SEC actually uses.
for variant in ("ITEM 2.02.", "Item  2.02", "item 2.02"):
    assert is_earnings_8k(extract_8k_items(f"{variant} Results of Operations")), variant
print("case and spacing variants handled ✓")

print("\n✅ FRESHNESS + 8-K ITEM TEST PASS")
