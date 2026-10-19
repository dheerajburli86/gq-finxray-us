"""Offline tests: scope enforcement and query shape, no network or credentials."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gq_bot.data import GQData


class Q:
    def __init__(self, rows): self.rows, self.f = rows, []
    def select(self, *_): return self
    def eq(self, c, v): self.f.append(lambda r: str(r.get(c)) == str(v)); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def order(self, c, desc=False): self.rows = sorted(self.rows, key=lambda r: str(r.get(c)), reverse=desc); return self
    def limit(self, n): self.n = n; return self
    def execute(self):
        out = [r for r in self.rows if all(f(r) for f in self.f)]
        return type("R", (), {"data": out[:getattr(self, "n", None)]})()


class FakeSB:
    def __init__(self, tables): self.tables = tables
    def table(self, name): return Q(list(self.tables.get(name, [])))


def make():
    fx = FakeSB({
        "users": [{"id": "u1", "telegram_chat_id": "42", "is_active": True},
                  {"id": "u2", "telegram_chat_id": "43", "is_active": False}],
        "watchlists": [{"user_id": "u1", "ticker": "aapl"}, {"user_id": "u1", "ticker": "SPY"},
                       {"user_id": "u9", "ticker": "TSLA"}],
        "alerts": [{"id": 1, "ticker": "AAPL", "summary": "new", "created_at": "2999-01-01T00:00:00+00:00", "extra": {"feature_name": "News"}},
                   {"id": 2, "ticker": "TSLA", "summary": "other user's", "created_at": "2999-01-01T00:00:00+00:00"}],
    })
    hm = FakeSB({
        "instruments": [{"id": "i1", "symbol": "AAPL", "universe": "us_stocks", "is_active": True, "name": "Apple"},
                        {"id": "i2", "symbol": "SPY", "universe": "us_etfs", "is_active": True, "name": "SPDR"},
                        {"id": "i3", "symbol": "TSLA", "universe": "us_stocks", "is_active": True}],
        "returns_latest": [{"instrument_id": "i1", "price_usd": 100, "as_of": "2026-10-16", "ret_1d": 1.5}],
        "price_history": [{"instrument_id": "i1", "date": "2999-01-01", "close": 99}],
    })
    return GQData(fx, hm)


class Scope(unittest.TestCase):
    def test_watchlist_normalised(self):
        self.assertEqual(make().get_watchlist("u1"), ["AAPL", "SPY"])

    def test_inactive_user_rejected(self):
        self.assertIsNone(make().get_user(43))
        self.assertEqual(make().get_user(42)["id"], "u1")

    def test_every_lookup_refuses_unwatched_ticker(self):
        d = make()
        for call in (d.price_snapshot, d.price_history, d.fundamentals, d.events):
            r = call("u1", "TSLA")
            self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "not_on_watchlist")

    def test_events_never_leak_other_tickers(self):
        r = make().events("u1", "aapl", days=90)
        self.assertEqual([e["id"] for e in r["events"]], [1])
        self.assertEqual(r["events"][0]["feature"], "News")

    def test_snapshot_and_history(self):
        d = make()
        s = d.price_snapshot("u1", "$aapl")
        self.assertTrue(s["ok"]); self.assertEqual(s["returns_pct"]["ret_1d"], 1.5)
        self.assertEqual(d.price_snapshot("u1", "SPY")["reason"], "no_returns")
        self.assertEqual(d.price_history("u1", "AAPL", days=9999)["days"], 400)


if __name__ == "__main__":
    unittest.main()
