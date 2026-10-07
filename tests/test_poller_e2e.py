"""
End-to-end test of fund_manager_poller with a fake SEC and a fake Supabase.
Proves: EFTS hit -> SGML header -> sticker parse -> event rows -> roster update
-> watchlist-gated alert, plus idempotency and roster-diff detection.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# stub third-party imports so the test runs without network deps
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("supabase", types.SimpleNamespace(create_client=lambda *a, **k: None))

import fund_manager_poller as P  # noqa: E402


# ── fake Supabase ────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, db, table):
        self.db, self.t, self.f, self.op, self.payload, self.lim = db, table, [], "select", None, None
        self.conflict = None

    def select(self, *_):
        self.op = "select"; return self

    def insert(self, row):
        self.op, self.payload = "insert", row; return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.payload, self.conflict = "upsert", rows, on_conflict; return self

    def update(self, vals):
        self.op, self.payload = "update", vals; return self

    def eq(self, k, v):
        self.f.append(("eq", k, v)); return self

    def in_(self, k, v):
        self.f.append(("in", k, set(v))); return self

    def gte(self, k, v):
        self.f.append(("gte", k, v)); return self

    def limit(self, n):
        self.lim = n; return self

    def _get(self, row, k):
        if "->>" in k:
            col, key = k.split("->>")
            return str((row.get(col) or {}).get(key))
        return row.get(k)

    def _match(self, row):
        for op, k, v in self.f:
            val = self._get(row, k)
            if op == "eq" and val != v and str(val) != str(v):
                return False
            if op == "in" and val not in v:
                return False
            if op == "gte" and (val or "") < v:
                return False
        return True

    def execute(self):
        rows = self.db.setdefault(self.t, [])
        if self.op == "select":
            out = [r for r in rows if self._match(r)]
            return types.SimpleNamespace(data=out[: self.lim] if self.lim else out)
        if self.op == "insert":
            # supabase-py accepts one row or a list of rows (emit_alerts inserts
            # every watched share class in a single request).
            new_rows = self.payload if isinstance(self.payload, list) else [self.payload]
            for p in new_rows:
                if self.t == "fund_manager_events":
                    k = ("accession", "series_id", "manager_key", "event_type")
                    if any(all(r[x] == p[x] for x in k) for r in rows):
                        raise Exception("duplicate key value violates unique constraint")
                rows.append(dict(p, created_at="2099-01-01T00:00:00+00:00"))
            return types.SimpleNamespace(data=new_rows)
        if self.op == "upsert":
            keys = self.conflict.split(",")
            for new in self.payload if isinstance(self.payload, list) else [self.payload]:
                hit = next((r for r in rows if all(r.get(k) == new.get(k) for k in keys)), None)
                if hit:
                    hit.update(new)
                else:
                    rows.append(dict(new))
            return types.SimpleNamespace(data=[])
        if self.op == "update":
            for r in rows:
                if self._match(r):
                    r.update(self.payload)
            return types.SimpleNamespace(data=[])


class FakeSB:
    def __init__(self):
        self.db = {"watchlists": [{"ticker": "VEXPX"}]}      # user watches Explorer Investor

    def table(self, t):
        return _Q(self.db, t)


# ── fake SEC ─────────────────────────────────────────────────────────────────
HDR = """<SERIES>
<OWNER-CIK>0000036405
<SERIES-ID>S000002500
<SERIES-NAME>Vanguard Explorer Fund
<CLASS-CONTRACT>
<CLASS-CONTRACT-ID>C000006789
<CLASS-CONTRACT-NAME>Investor Shares
<CLASS-CONTRACT-TICKER-SYMBOL>VEXPX
</CLASS-CONTRACT>
<CLASS-CONTRACT>
<CLASS-CONTRACT-ID>C000006790
<CLASS-CONTRACT-NAME>Admiral Shares
<CLASS-CONTRACT-TICKER-SYMBOL>VEXRX
</CLASS-CONTRACT>
</SERIES>"""

PROSPECTUS = """<p>Vanguard Explorer Fund</p><p>Portfolio Managers</p>
<p>Michelle Louie, CFA, Principal of Vanguard. She has co-managed the Fund since 2017.</p>
<p>John P. Smith, CFA, Portfolio Manager at Vanguard. He has co-managed the Fund since 2012.</p>
<p>Purchase and Sale of Fund Shares</p>"""

PROSPECTUS_NEXT_YEAR = """<p>Vanguard Explorer Fund</p><p>Portfolio Managers</p>
<p>Michelle Louie, CFA, Principal of Vanguard. She has co-managed the Fund since 2017.</p>
<p>Omar Haddad, Portfolio Manager at Vanguard. He has co-managed the Fund since 2027.</p>
<p>Purchase and Sale of Fund Shares</p>"""

STICKER = """<p>Vanguard Explorer Fund</p><p>Supplement Dated September 8, 2026</p>
<p>Effective October 3, 2026, John P. Smith, CFA, will no longer serve as a portfolio manager of the Fund.</p>"""


class FakeSEC:
    def __init__(self):
        self.docs = {
            "prosp.htm": PROSPECTUS, "prosp2.htm": PROSPECTUS_NEXT_YEAR, "sticker.htm": STICKER,
        }

    def json(self, url, params=None):
        if url == P.URL_MF_TICKERS:
            return {"fields": ["cik", "seriesId", "classId", "symbol"],
                    "data": [[36405, "S000002500", "C000006789", "VEXPX"],
                             [36405, "S000002500", "C000006790", "VEXRX"],
                             [1100663, "S000090001", "C000200001", "TENY"]]}
        if url == P.URL_EFTS:
            if "no longer serve" in params["q"] and "a portfolio manager" in params["q"]:
                return {"hits": {"total": {"value": 1}, "hits": [{
                    "_id": "0000932471-26-000100:sticker.htm",
                    "_source": {"ciks": ["0000036405"], "form": "497", "file_date": "2026-09-08",
                                "display_names": ["VANGUARD WORLD FUND"]}}]}}
            return {"hits": {"total": {"value": 0}, "hits": []}}
        return None

    def text(self, url):
        if url.endswith(".hdr.sgml"):
            return HDR
        return self.docs.get(url.rsplit("/", 1)[-1])


def setup():
    fake = FakeSB()
    P._sb = fake
    P.sec = FakeSEC()
    P._universe_cache = None
    P.FMP_API_KEY = None
    return fake


def _roster_meta(doc, adsh, fdate):
    return {"accession": adsh, "cik": "0000036405", "form": "485BPOS", "filing_date": fdate,
            "filename": doc, "url": P._archive("0000036405", adsh, doc)}


def test_full_flow():
    fake = setup()
    u = P.load_universe()
    assert u["by_ticker"]["VEXPX"]["fund_type"] == "MF"
    assert u["by_ticker"]["TENY"]["fund_type"] == "ETF"

    # 1. backfill roster (no alerts on first roster)
    P.process_roster_filing(_roster_meta("prosp.htm", "0000932471-25-000001", "2025-12-20"),
                            P.watched_tickers(), emit_diffs=False)
    cur = {r["manager_name"] for r in fake.db["fund_managers"] if r["is_current"]}
    assert cur == {"Michelle Louie", "John P. Smith"}
    assert not fake.db.get("alerts")

    # 2. sticker: Smith leaves -> event + roster update + ONE alert for watched VEXPX only
    P.run_event_poll(days_back=30)
    evs = fake.db["fund_manager_events"]
    assert [(e["event_type"], e["manager_name"]) for e in evs] == [("REMOVED", "John P. Smith")]
    assert evs[0]["effective_date"] == "2026-10-03"
    alerts = fake.db["alerts"]
    assert [a["ticker"] for a in alerts] == ["VEXPX"]           # VEXRX not watched -> no alert
    a = alerts[0]
    assert a["filing_type"] == "PM_REMOVED" and a["impact"] == "MEDIUM"
    assert "John P. Smith is leaving" in a["summary"] and "Michelle Louie" in a["summary"]
    assert a["extra"]["current_team"] == ["Michelle Louie"]
    assert {r["manager_name"] for r in fake.db["fund_managers"] if r["is_current"]} == {"Michelle Louie"}

    # 3. idempotent: re-running the poll creates nothing new
    P.run_event_poll(days_back=30)
    assert len(fake.db["fund_manager_events"]) == 1 and len(fake.db["alerts"]) == 1

    # 4. next prospectus adds Haddad with no sticker -> ROSTER_DIFF catches it;
    #    Smith's removal is NOT re-alerted (sticker already reported it)
    P.process_roster_filing(_roster_meta("prosp2.htm", "0000932471-26-000900", "2026-12-20"),
                            P.watched_tickers(), emit_diffs=True)
    evs = [(e["event_type"], e["manager_name"], e["detection_source"]) for e in fake.db["fund_manager_events"]]
    assert ("ADDED", "Omar Haddad", "ROSTER_DIFF") in evs
    assert len(evs) == 2
    assert fake.db["alerts"][-1]["filing_type"] == "PM_ADDED"

    # 5. an older prospectus never overwrites a newer roster
    P.process_roster_filing(_roster_meta("prosp.htm", "0000932471-24-000001", "2024-12-20"),
                            P.watched_tickers(), emit_diffs=True)
    assert {r["manager_name"] for r in fake.db["fund_managers"] if r["is_current"]} == {"Michelle Louie", "Omar Haddad"}


if __name__ == "__main__":
    # run_all.sh executes each file as a script, which never calls pytest-style
    # test_ functions on its own — without this block the file "passes" while
    # running nothing.
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
        print(f"  PASS  {_fn.__name__}")
    print(f"{len(_fns)} tests passed")
