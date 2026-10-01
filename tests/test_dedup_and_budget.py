"""
tests/test_dedup_and_budget.py

The dedup prefilter must keep real duplicates (they still get an LLM call and
can still be discarded) while dropping unrelated pairs for free, and the daily
call budget must stop a batch before it starts rather than mid-filing.

Run:  python tests/test_dedup_and_budget.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import ai_pipeline as ap

NEW = ("Apple Inc. reported fourth-quarter revenue of $94.9 billion, up 6% "
       "year over year, with iPhone sales reaching $46.2 billion and services "
       "revenue setting a record at $24.9 billion.")

REPRINT = ("Apple reported fourth quarter revenue of $94.9 billion, a 6% "
           "increase year over year. iPhone sales were $46.2 billion while "
           "services revenue hit a record $24.9 billion.")

UNRELATED_1 = ("Apple named a new head of its automotive program following the "
               "departure of the previous lead, according to an internal memo.")
UNRELATED_2 = ("A federal judge denied Apple's motion to dismiss an antitrust "
               "suit brought by app developers over App Store commission rates.")
UNRELATED_3 = ("Apple announced a $110 billion share buyback authorization and "
               "raised its dividend by 4%.")

# ── 1. A near-identical reprint survives the prefilter ──────────────────────
kept = ap.rank_dedup_candidates(NEW, [UNRELATED_1, REPRINT, UNRELATED_2])
print(f"candidates kept for the reprint case: {len(kept)}")
assert REPRINT in kept, "prefilter dropped a real duplicate — it would be re-sent"
assert kept[0] == REPRINT, "real duplicate should rank first"

# ── 2. Unrelated summaries are dropped without an LLM call ─────────────────
kept = ap.rank_dedup_candidates(NEW, [UNRELATED_1, UNRELATED_2, UNRELATED_3])
print(f"candidates kept when nothing matches: {len(kept)}")
assert kept == [], f"unrelated summaries should cost zero calls, got {len(kept)}"

# ── 3. Comparisons are capped ───────────────────────────────────────────────
many = [REPRINT] * 10
kept = ap.rank_dedup_candidates(NEW, many)
print(f"candidates from 10 near-identical: {len(kept)} (cap {ap.DEDUP_MAX_LLM_COMPARISONS})")
assert len(kept) <= ap.DEDUP_MAX_LLM_COMPARISONS

# ── 4. Empty / missing history is safe ──────────────────────────────────────
assert ap.rank_dedup_candidates(NEW, []) == []
assert ap.rank_dedup_candidates(NEW, None) == []
assert ap.rank_dedup_candidates("", [REPRINT]) == [REPRINT]  # can't score: defer to LLM

# ── 5. Call-count saving on a realistic active-ticker history ──────────────
history = [UNRELATED_1, UNRELATED_2, UNRELATED_3,
           "Apple supplier Foxconn raised its outlook for the December quarter.",
           "Apple released iOS 18.2 with expanded Apple Intelligence features.",
           "Apple opened its first retail store in Malaysia.",
           "Apple faces a European Commission ruling on default browser choice.",
           "Apple's board approved the reappointment of its auditor.",
           "Apple began production of the M5 chip with TSMC.",
           REPRINT]
kept = ap.rank_dedup_candidates(NEW, history)
print(f"active ticker: {len(kept)} LLM call(s) instead of {len(history)}")
assert REPRINT in kept, "the one real duplicate must still be checked"
assert len(kept) < len(history), "prefilter saved nothing"

# ── 6. Budget guard stops a batch before it picks anything up ──────────────
ap.LLM_DAILY_CALL_BUDGET = 5
ap._budget_day[0] = None
ap._calls_today[0] = 0
assert not ap.budget_exhausted(), "fresh day should not be exhausted"

ap._budget_state()
ap._calls_today[0] = 5
assert ap.budget_exhausted(), "budget should be exhausted at the limit"

fetched = {"called": False}
def should_not_run(*a, **k):
    fetched["called"] = True
    raise AssertionError("run_pipeline fetched filings despite an exhausted budget")
ap.supabase.table = should_not_run

assert ap.run_pipeline() == 0
assert not fetched["called"]
print("exhausted budget: batch skipped, filings left PENDING ✓")

print("\n✅ DEDUP PREFILTER + DAILY BUDGET TEST PASS")
