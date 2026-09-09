"""
tests/test_llm_rate_limit.py

Covers the DeepInfra concurrency semaphore, the sliding-window RPM limiter,
the shared 429 cooldown, and thread-local token accounting.

Run:  python tests/test_llm_rate_limit.py
"""
import sys, os, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor

sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import ai_pipeline as ap


# ── 1. Semaphore caps concurrent in-flight requests ──────────────────────────
in_flight = {"now": 0, "peak": 0}
peak_lock = threading.Lock()


def slow_post(url, headers=None, json=None, timeout=None):
    with peak_lock:
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
    time.sleep(0.05)
    with peak_lock:
        in_flight["now"] -= 1

    class R:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return R()


ap.requests.post = slow_post
ap.LLM_CONCURRENCY = 3
ap._llm_semaphore = threading.BoundedSemaphore(3)
ap.LLM_RPM = 10_000          # take the RPM limiter out of play for this check
ap._call_window.clear()

with ThreadPoolExecutor(max_workers=12) as pool:
    list(pool.map(lambda i: ap.call_deepinfra("p"), range(12)))

print(f"peak concurrent in-flight: {in_flight['peak']} (cap 3)")
assert in_flight["peak"] <= 3, f"semaphore breached: {in_flight['peak']} concurrent"
assert in_flight["peak"] > 1, "no concurrency at all — semaphore is serializing"


# ── 2. Sliding window blocks past LLM_RPM ────────────────────────────────────
ap._call_window.clear()
ap._cooldown_until[0] = 0.0
ap.LLM_RPM = 5

t0 = time.monotonic()
for _ in range(5):
    ap._acquire_call_slot()          # 5 slots available immediately
fast = time.monotonic() - t0
assert fast < 0.5, f"first {ap.LLM_RPM} calls should not block, took {fast:.2f}s"

blocked = {"done": False}


def try_sixth():
    ap._acquire_call_slot()
    blocked["done"] = True


t = threading.Thread(target=try_sixth, daemon=True)
t.start()
t.join(timeout=1.0)
print(f"6th call within window blocked: {not blocked['done']}")
assert not blocked["done"], "RPM window let a 6th call through immediately"


# ── 3. A 429 cooldown parks every thread, not just the one that hit it ──────
ap._call_window.clear()
ap.LLM_RPM = 10_000
ap._set_cooldown(30)

parked = {"done": False}


def try_during_cooldown():
    ap._acquire_call_slot()
    parked["done"] = True


t = threading.Thread(target=try_during_cooldown, daemon=True)
t.start()
t.join(timeout=1.0)
print(f"other thread parked during cooldown: {not parked['done']}")
assert not parked["done"], "cooldown did not park a second thread"
ap._cooldown_until[0] = 0.0


# ── 4. Token accounting stays per-thread under concurrency ──────────────────
ap._call_window.clear()
ap.LLM_RPM = 10_000
results = {}


def one_filing(n):
    ap._reset_token_usage()
    for _ in range(n):
        ap.call_deepinfra("p")
    results[n] = ap.get_token_usage()


with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(one_filing, [1, 2, 3, 4]))

for n, usage in sorted(results.items()):
    print(f"  thread making {n} call(s) -> {usage}")
    assert usage["calls"] == n, f"token bucket leaked across threads: {usage}"
    assert usage["input"] == 10 * n and usage["output"] == 5 * n

print("\n✅ LLM RATE-LIMIT TEST PASS "
      "(semaphore, RPM window, shared cooldown, thread-local tokens)")
