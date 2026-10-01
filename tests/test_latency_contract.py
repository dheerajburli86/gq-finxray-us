"""
tests/test_latency_contract.py

Locks in the latency guarantees. Every one of these regressed silently before —
the alert still arrived, just minutes later than it should have, which no
existing test could see.

  1. DELIVERY DRAINS BACK-TO-BACK   delivery_loop slept DELIVERY_IDLE_SECONDS
     after EVERY cycle, including cycles that did work. delivery.py settles at
     most BATCH_LIMIT (100) alerts per call, so a backlog drained at 100 alerts
     per (cycle + gap) with the gap added at the exact moment the queue was
     known to be non-empty.

  2. SETTLED, NOT EXAMINED          the count driving that decision has to be
     alerts that LEFT the queue. Counting alerts examined would spin forever on
     rows deferred for a retry; counting nothing would restore the stall.

  3. POLL INTERVALS ARE THE DETECTION DELAY  for any source that returns current
     state rather than an event feed, the interval IS the latency. S-1 at 30
     minutes and analyst ratings at 2 hours were both far looser than their real
     cost justified.

Run:  python tests/test_latency_contract.py
"""
import asyncio
import os
import re
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for m in ["supabase", "telegram", "telegram.constants", "telegram.error", "dotenv",
          "aiohttp", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
          "pandas", "numpy", "feedparser", "bs4"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "x")
os.environ.setdefault("TELEGRAM_TOKEN", "x")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


import main
import delivery
import ai_pipeline

MAIN_SRC = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()


# ── 1. An empty queue reports 0, so the loop idles instead of spinning ────────
print("=== 1. deliver_pending_alerts REPORTS WHAT IT SETTLED ===")


class _EmptyTable:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def update(self, *a, **k): return self
    def insert(self, *a, **k): return self

    def execute(self):
        class R: data = []
        return R()


delivery.supabase = MagicMock()
delivery.supabase.table = lambda name: _EmptyTable()

settled = asyncio.run(delivery.deliver_pending_alerts())
check("empty queue returns a number, not None", settled is not None,
      "delivery_loop would treat None as idle by accident, not by contract")
check("empty queue returns 0", settled == 0, f"got {settled!r}")


# ── 2. The loop only sleeps when nothing was settled ──────────────────────────
print("\n=== 2. delivery_loop DOES NOT PAUSE MID-BACKLOG ===")


class _StopLoop(BaseException):
    """BaseException so delivery_loop's `except Exception` cannot swallow it."""


def run_loop_with(script):
    """
    Drive the real main.delivery_loop() through a scripted sequence of settled
    counts. Returns the number of sleeps it took along the way.
    """
    remaining = list(script)
    sleeps = []

    async def fake_deliver():
        if not remaining:
            raise _StopLoop
        return remaining.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    real_deliver, real_sleep = main.deliver_pending_alerts, asyncio.sleep
    main.deliver_pending_alerts = fake_deliver
    asyncio.sleep = fake_sleep
    try:
        asyncio.run(main.delivery_loop())
    except _StopLoop:
        pass
    finally:
        main.deliver_pending_alerts = real_deliver
        asyncio.sleep = real_sleep
    return sleeps


# Three full batches back-to-back, then the queue runs dry.
sleeps = run_loop_with([100, 100, 100, 0])
check("no sleep between batches while the backlog drains", sleeps.count(0) == 0 and len(sleeps) == 1,
      f"slept {len(sleeps)} time(s) for a 3-batch backlog: {sleeps}")
check("sleeps once the queue comes back empty", sleeps == [main.DELIVERY_IDLE_SECONDS],
      f"expected one {main.DELIVERY_IDLE_SECONDS}s idle sleep, got {sleeps}")

# A cycle that settles nothing (everything deferred for retry) must idle, not spin.
sleeps = run_loop_with([0, 0, 0])
check("a cycle that settles nothing idles instead of spinning", len(sleeps) == 3,
      f"expected 3 idle sleeps, got {sleeps}")


# ── 3. Intervals ──────────────────────────────────────────────────────────────
print("\n=== 3. POLL INTERVALS MATCH THE LATENCY BUDGET ===")

# S-1 IS a latency control now that Feature 8 consumes it: an S-1 is the first
# public signal a company intends to list, and EDGAR is the only source for that
# leading edge (FMP's calendar lists a deal once it is scheduled, which is a
# later event). It stays off the seconds-cadence fast lane because it is the one
# market-wide SEC poll and a cold start cannot finish inside a 15s tick.
check(f"S-1 polls every {main.SEC_S1_POLL_MINUTES}m (<=5)",
      main.SEC_S1_POLL_MINUTES <= 5,
      "Feature 8's early-warning half is only as fresh as this interval")
check("S-1 stays off the seconds-cadence SEC lane",
      "SEC_S1_POLL_MINUTES).minutes" in MAIN_SRC,
      "a market-wide poll on a 15s tick starves 8-K/Form 4 of request budget")

check(f"pipeline idles {main.PIPELINE_IDLE_SECONDS}s (<=1)",
      main.PIPELINE_IDLE_SECONDS <= 1, "idle gap adds straight to end-to-end latency")
check(f"delivery idles {main.DELIVERY_IDLE_SECONDS}s (<=1)",
      main.DELIVERY_IDLE_SECONDS <= 1, "idle gap adds straight to end-to-end latency")

m = re.search(r"schedule\.every\((\d+)\)\.(minutes|hours)\.do\(job\(poll_analyst_ratings\)\)",
              MAIN_SRC)
check("analyst ratings is scheduled at all", bool(m), "poll_analyst_ratings not wired")
if m:
    mins = int(m.group(1)) * (60 if m.group(2) == "hours" else 1)
    check(f"analyst ratings polls every {mins}m (<=30, was 120)", mins <= 30,
          "FMP returns current state, so this interval IS the detection delay")

check("SEC fast lane still 15s", main.SEC_FAST_POLL_SECONDS <= 15,
      "8-K / Form 4 are the latency-critical feeds")


# ── 4. The priority tier covers both axes ─────────────────────────────────────
print("\n=== 4. TIME-CRITICAL CONTENT JUMPS THE NEWS BACKLOG ===")

f = ai_pipeline._PRIORITY_OR_FILTER
check("priority tier matches on source", "source.in." in f, f)
check("priority tier matches on filing_type", "filing_type.in." in f,
      "PRIORITY_FILING_TYPES is dead again — INSIDER_FMP queues behind news")
for ft in ("4", "INSIDER_FMP", "8-K", "EARNINGS_TRANSCRIPT"):
    check(f"{ft} is prioritised", f'"{ft}"' in f, f"not present in {f}")

check("LLM concurrency is above the old 4", ai_pipeline.LLM_CONCURRENCY >= 8,
      "batch throughput is bounded by whichever of this and LLM_RPM binds first")

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL LATENCY CONTRACT TESTS PASS")
