"""
tests/test_duplicate_alert_guard.py

Regression for "same alert sent 4x in one minute": syndicated copies of one
story (same title/body, different URLs) processed concurrently must yield ONE
alert, and delivery must never fan out two alerts with the same summary_hash.

Run:  python tests/test_duplicate_alert_guard.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import ai_pipeline as ap

TITLE = "Anthropic Plans to Spend $518 Billion on Cloud and Data Centers"
BODY = TITLE + "\n\nAnthropic's draft prospectus, reported by Reuters, indicates plans for $518 billion in cloud obligations."
SUMMARY = "Anthropic's draft prospectus indicates $518 billion in cloud and infrastructure obligations, over $100 billion to Amazon."

def filing(i):
    return {"id": f"f{i}", "ticker": "AMZN", "company_name": "Amazon", "raw_text": BODY,
            "filing_type": "NEWS", "source": "FMP_NEWS", "filing_url": f"https://site{i}.com/a",
            "extra": {"title": TITLE}}

stored, statuses, calls = [], {}, []

ap.update_filing_status = lambda fid, st: statuses.__setitem__(fid, st)
ap._alert_exists = lambda field, value: False          # empty DB
def fake_inner(f):
    calls.append(f["id"])
    import time; time.sleep(0.05)                       # let siblings overlap
    stored.append(f["extra"])
    ap.update_filing_status(f["id"], "PROCESSED")
ap._process_filing = fake_inner

# 1. Four concurrent syndicated copies -> exactly one reaches the pipeline.
with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(ap.process_filing, [filing(i) for i in range(4)]))
assert len(calls) == 1, f"expected 1 pipeline run, got {len(calls)}"
assert sorted(v for v in statuses.values()) == ["DISCARDED"] * 3 + ["PROCESSED"]
assert stored[0].get("content_key") and stored[0].get("body_key")
print("PASS concurrent copies collapse to one")

# 2. A crash releases the claim so the row can be retried.
ap._claims.clear(); calls.clear()
def boom(f): raise RuntimeError("llm down")
ap._process_filing = boom
try: ap.process_filing(filing(9))
except RuntimeError: pass
ap._process_filing = fake_inner
ap.process_filing(filing(9))
assert calls == ["f9"], "retry after a crash must not be blocked"
print("PASS crash releases the claim")

# 3. A stored alert (e.g. after a restart) blocks a later copy.
ap._claims.clear(); calls.clear(); statuses.clear()
ap._alert_exists = lambda field, value: field == "content_key"
ap.process_filing(filing(1))
assert calls == [] and statuses["f1"] == "DISCARDED"
print("PASS stored alert blocks a later copy")

# 4. Different tickers / different stories are NOT merged.
ap._claims.clear(); calls.clear(); ap._alert_exists = lambda f, v: False
other = filing(2); other["ticker"] = "MSFT"
diff = filing(3); diff["raw_text"] = "Totally different story about chips and memory pricing in the datacenter market today."
diff["extra"] = {"title": "Micron revenue quadruples on HBM demand"}
for f in (filing(0), other, diff): ap.process_filing(f)
assert len(calls) == 3
print("PASS distinct stories untouched")

# 5. summary_hash is stable across whitespace/case/punctuation, ticker-scoped.
assert ap.summary_hash("AMZN", SUMMARY) == ap.summary_hash("amzn", "  " + SUMMARY.upper() + "!! ")
assert ap.summary_hash("AMZN", SUMMARY) != ap.summary_hash("MSFT", SUMMARY)
print("PASS summary_hash")

# 6. Delivery net: duplicates in one cycle -> only the oldest is kept.
import delivery as d
sent = []
d._mark_fanned_out = lambda ids: sent.extend(ids)
tbl = MagicMock(); d.supabase = MagicMock(); d.supabase.table.return_value = tbl
for m in ("select", "in_", "eq", "gte"): getattr(tbl, m).return_value = tbl
tbl.execute.return_value = MagicMock(data=[])
h = ap.summary_hash("AMZN", SUMMARY)
rows = [{"id": i, "created_at": f"2026-10-01T01:15:0{i}Z", "extra": {"summary_hash": h}} for i in (1, 2, 3, 4)]
rows.append({"id": 9, "created_at": "2026-10-01T01:15:09Z", "extra": {}})   # heatmap-style, no hash
keep, n = d._drop_duplicate_alerts(rows)
assert [r["id"] for r in keep] == [1, 9] and sorted(sent) == [2, 3, 4] and n == 3
print("PASS delivery keeps oldest, ignores hashless alerts")

# 7. Delivery net: already delivered inside the window -> suppressed.
sent.clear()
tbl.execute.return_value = MagicMock(data=[{"extra": {"summary_hash": h}}])
keep, n = d._drop_duplicate_alerts(rows[:2])
assert keep == [] and n == 2
print("PASS already-delivered duplicate suppressed")

# 8. Lookup failure fails open.
sent.clear(); tbl.execute.side_effect = RuntimeError("db down")
keep, n = d._drop_duplicate_alerts([rows[0]])
assert [r["id"] for r in keep] == [1] and n == 0
print("PASS lookup failure fails open")
print("ALL PASS")
