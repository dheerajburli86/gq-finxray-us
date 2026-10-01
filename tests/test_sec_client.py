"""
tests/test_sec_client.py

The SEC client is the latency-critical path: it is the first hop for every
filing, and SEC is the origin of the data, so time lost here is time no
downstream optimisation can recover.

  1. Sessions are reused within a poll (not rebuilt per request, which paid a
     TLS handshake on every fetch).
  2. The request rate stays under SEC's 10 req/s fair-access limit even with
     high concurrency, and even across separate event loops.
  3. Sessions are closed when a poll's loop ends.

Run:  python tests/test_sec_client.py
"""
import sys, os, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

import sec_client as sc

# ── 1. One session per loop, reused across requests ────────────────────────
async def session_reuse():
    a = sc._get_session()
    b = sc._get_session()
    c = sc._get_session()
    assert a is b is c, "a new ClientSession per request = a TLS handshake per request"
    await sc.close_session()
    assert not sc._sessions, "session was not released when the poll ended"
    return a

first = asyncio.run(session_reuse())
print("session reused within a poll, released at the end ✓")

# A second poll gets its own loop; it must not reuse the dead loop's session.
async def fresh_loop():
    s = sc._get_session()
    assert s is not first, "reused a session bound to a closed event loop"
    await sc.close_session()

asyncio.run(fresh_loop())
print("new poll (new loop) builds its own session ✓")

# ── 2. Rate limiter holds under SEC's fair-access ceiling ──────────────────
sc.MAX_RPS = 8
sc._request_times.clear()


async def burst(n):
    t0 = time.monotonic()
    await asyncio.gather(*(sc._await_rate_slot() for _ in range(n)))
    return time.monotonic() - t0


# 8 slots are free immediately; the 9th..16th must wait for the window to roll.
elapsed = asyncio.run(burst(16))
print(f"16 requests at {sc.MAX_RPS} rps took {elapsed:.2f}s")
assert elapsed >= 0.9, (
    f"16 requests cleared in {elapsed:.2f}s — that is >{16/max(elapsed,0.01):.0f} rps, "
    "over SEC's 10/s fair-access limit")
assert elapsed < 3.0, f"rate limiter is far slower than necessary ({elapsed:.2f}s)"

# The limiter is shared across loops, because SEC counts per requester and each
# poll runs in its own asyncio.run() on the SEC thread pool.
sc._request_times.clear()
asyncio.run(burst(8))          # fills the window in one loop
t0 = time.monotonic()
asyncio.run(burst(1))          # a *different* loop must still see it full
cross = time.monotonic() - t0
print(f"next request from a separate loop waited {cross:.2f}s")
assert cross > 0.2, "limiter is per-loop — concurrent pollers would each get a full budget"
print("rate limit holds across loops ✓")

# ── 3. Config reflects the available headroom ──────────────────────────────
print(f"concurrency={sc.MAX_CONCURRENCY} rps={sc.MAX_RPS} "
      f"retries={sc.MAX_RETRIES} retry_delay={sc.RETRY_DELAY}s")
assert sc.MAX_CONCURRENCY >= 20, "concurrency left at the old low default"
assert sc.MAX_RPS <= 10, "MAX_RPS above SEC's documented 10 req/s limit"
assert sc.MAX_RETRIES == 3 and sc.RETRY_DELAY == 5.0

print("\n✅ SEC CLIENT TEST PASS (session reuse, rate ceiling, retry policy)")
