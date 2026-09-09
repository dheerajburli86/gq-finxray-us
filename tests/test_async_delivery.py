"""
tests/test_async_delivery.py

Verifies deliver_pending_alerts() fans out concurrently across users instead
of serially, and that _log_payload() writes the payload_log row exactly once
per alert that carries a structured payload.

Run:  python tests/test_async_delivery.py
"""
import sys, os, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

# ── Fake Supabase ────────────────────────────────────────────────────────────
class FakeTable:
    def __init__(self, name, db):
        self.name = name
        self.db = db
        self._filters = []

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def execute(self):
        class R: data = self.db.get(self.name, [])
        return R()

    def insert(self, rows):
        self.db.setdefault(self.name, [])
        self.db[self.name].append(rows)
        return self

    def upsert(self, rows, **k):
        self.db.setdefault(self.name + "_upserts", [])
        self.db[self.name + "_upserts"].append(rows)
        return self

    def update(self, *a, **k): return self


class FakeSupabase:
    def __init__(self):
        self.db = {
            "users": [
                {"id": "u1", "telegram_chat_id": "111", "telegram_username": "a", "is_active": True},
                {"id": "u2", "telegram_chat_id": "222", "telegram_username": "b", "is_active": True},
                {"id": "u3", "telegram_chat_id": "333", "telegram_username": "c", "is_active": True},
            ],
            "user_preferences": [],
            "watchlists": [
                {"user_id": "u1", "ticker": "AAPL"},
                {"user_id": "u2", "ticker": "AAPL"},
                {"user_id": "u3", "ticker": "MSFT"},
            ],
            "alert_deliveries": [],
        }

    def table(self, name):
        return FakeTable(name, self.db)


fake_supabase = FakeSupabase()
sys.modules["supabase"] = MagicMock()
sys.modules["supabase"].create_client = lambda *a, **k: fake_supabase

import fmp_client
fmp_client.get_quote = lambda ticker: None  # no live price lookups in this test

import delivery

delivery.supabase = fake_supabase

# ── Fake Telegram Bot: records send order/timing, no real network ──────────
SEND_LOG = []

class FakeBot:
    def __init__(self, token=None):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        SEND_LOG.append((chat_id, time.monotonic()))
        await asyncio.sleep(0.01)  # simulate network latency
        return True

delivery.Bot = FakeBot
delivery.SEND_GAP_SECONDS = 0.01
delivery.PER_CHAT_GAP_SECONDS = 0.05

alerts = [
    {"id": "a1", "ticker": "AAPL", "impact": "HIGH", "summary": "Revenue up.",
     "source": "SEC_EDGAR", "filing_type": "8-K", "extra": {}, "delivered": False,
     "created_at": "2026-09-08T00:00:00Z"},
    {"id": "a2", "ticker": "MSFT", "impact": "HIGH", "summary": "Cloud growth.",
     "source": "SEC_XBRL", "filing_type": "RESULT_SNAPSHOT",
     "extra": {"structured_payload": {"type": "fr", "ticker": "MSFT", "name": "Microsoft"}},
     "delivered": False, "created_at": "2026-09-08T00:00:00Z"},
]
fake_supabase.db["alerts"] = alerts


async def run():
    t0 = time.monotonic()
    await delivery.deliver_pending_alerts()
    elapsed = time.monotonic() - t0

    print(f"Sends recorded: {len(SEND_LOG)}")
    print(f"Elapsed: {elapsed:.3f}s")

    # 3 recipients total (u1+u2 for AAPL, u3 for MSFT). If sends ran fully
    # serially at PER_CHAT_GAP_SECONDS=0.05 apart regardless of chat, this
    # would take >= 0.10s beyond the per-send latency. Concurrent fan-out
    # across the 3 distinct chats should land well under that.
    assert len(SEND_LOG) == 3, f"expected 3 sends, got {len(SEND_LOG)}"
    # Serial would be >= 3 * PER_CHAT_GAP_SECONDS (0.15s) plus per-send
    # latency; concurrent measures ~0.06s. 0.10 sits clearly between the two,
    # far enough from the observed value to not flake on a loaded machine while
    # still failing outright if the sends ever go back to being serialized.
    assert elapsed < 0.10, f"fan-out took {elapsed:.3f}s — sends are not concurrent"

    upserts = fake_supabase.db.get("payload_log_upserts", [])
    print(f"payload_log upserts: {len(upserts)} (expected 1 — only a2 carries a structured_payload)")
    assert len(upserts) == 1, upserts
    assert upserts[0]["alert_id"] == "a2"
    assert upserts[0]["payload_type"] == "fr"

    print("\n✅ ASYNC DELIVERY TEST PASS — sends ran concurrently across users")

asyncio.run(run())
