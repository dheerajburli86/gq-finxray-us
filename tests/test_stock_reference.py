"""
Stock company master + ownership features (18-21), end to end with an
in-memory Supabase and stubbed SEC / FMP / Massive. No network.
Run: python tests/test_stock_reference.py
"""
import copy
import datetime as _dt
import os
import sys
import types
import uuid
from datetime import date, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ.update(SUPABASE_URL="https://fake.supabase.co", SUPABASE_KEY="k",
                  TELEGRAM_TOKEN="t", MASSIVE_API_KEY="m", FMP_API_KEY="f")
os.environ.pop("STOCK_ALERTS_ENABLED", None)
UTC = timezone.utc

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  <- {detail}"))
    if not cond:
        failures.append(label)


# ── Fake Supabase ─────────────────────────────────────────────────────────────
DB = {}
FAIL_INSERT = set()


class Q:
    def __init__(self, t):
        self.t, self.op, self.f, self.payload = t, "select", [], None
        self._order = self._range = self._limit = None
        self.on_conflict, self.ignore = None, False

    def select(self, *_a, **_k): self.op = "select"; return self
    def insert(self, rows): self.op, self.payload = "insert", rows; return self
    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload, self.on_conflict, self.ignore = "upsert", rows, on_conflict, ignore_duplicates
        return self
    def update(self, v): self.op, self.payload = "update", v; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.f.append(("eq", c, v)); return self
    def in_(self, c, v): self.f.append(("in", c, [str(x) for x in v])); return self
    def gte(self, c, v): self.f.append(("gte", c, v)); return self
    def order(self, c, desc=False): self._order = (c, desc); return self
    def range(self, a, b): self._range = (a, b); return self
    def limit(self, n): self._limit = n; return self

    def _m(self, r):
        for op, c, v in self.f:
            x = r.get(c)
            if op == "eq" and str(x) != str(v):
                return False
            if op == "in" and str(x) not in v:
                return False
            if op == "gte" and (x is None or x < v):
                return False
        return True

    def execute(self):
        rows = DB.setdefault(self.t, [])
        if self.op in ("insert", "upsert") and self.t in FAIL_INSERT:
            raise RuntimeError(f"fake write failure on {self.t}")
        items = self.payload if isinstance(self.payload, list) else [self.payload]
        if self.op == "insert":
            for r in items:
                r = copy.deepcopy(r)
                r.setdefault("id", len(rows) + 1 if self.t != "alerts" else str(uuid.uuid4()))
                r.setdefault("created_at", _dt.datetime.now(UTC).isoformat())
                rows.append(r)
            return types.SimpleNamespace(data=items)
        if self.op == "upsert":
            keys = (self.on_conflict or "id").split(",")
            for r in items:
                hit = next((x for x in rows if all(x.get(k) == r.get(k) for k in keys)), None)
                if hit and not self.ignore:
                    hit.update(copy.deepcopy(r))
                elif not hit:
                    rows.append(copy.deepcopy(r))
            return types.SimpleNamespace(data=items)
        hit = [r for r in rows if self._m(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return types.SimpleNamespace(data=hit)
        if self.op == "delete":
            DB[self.t] = [r for r in rows if r not in hit]
            return types.SimpleNamespace(data=hit)
        if self._order:
            c, desc = self._order
            hit.sort(key=lambda r: (r.get(c) is None, r.get(c) if not isinstance(r.get(c), str) else r.get(c)),
                     reverse=desc)
        if self._range:
            hit = hit[self._range[0]:self._range[1] + 1]
        if self._limit:
            hit = hit[:self._limit]
        return types.SimpleNamespace(data=copy.deepcopy(hit))


class FakeClient:
    def table(self, name):
        return Q(name)


import supabase  # noqa: E402
supabase.create_client = lambda *a, **k: FakeClient()

import feature_map  # noqa: E402
import stock_master_poller as smp  # noqa: E402
import stock_ownership_poller as sop  # noqa: E402
import delivery  # noqa: E402
import alert_formatter  # noqa: E402
import watchlist_util  # noqa: E402

smp.time.sleep = lambda *_: None
sop.time.sleep = lambda *_: None


def alerts(source=None):
    return [a for a in DB.get("alerts", []) if source is None or a["source"] == source]


# ── Synthetic market ──────────────────────────────────────────────────────────
def sec_base():
    return {
        "AAPL": {"cik": "320193", "name": "Apple Inc.", "exchange": "Nasdaq"},
        "MSFT": {"cik": "789019", "name": "MICROSOFT CORP", "exchange": "Nasdaq"},
        "BRK-B": {"cik": "1067983", "name": "BERKSHIRE HATHAWAY INC", "exchange": "NYSE"},
        "FB": {"cik": "1326801", "name": "Meta Platforms, Inc.", "exchange": "Nasdaq"},
        "XYZ": {"cik": "111", "name": "Xyz Holdings Inc", "exchange": "NYSE"},
        "MIDC": {"cik": "222", "name": "Midcap Co", "exchange": "NYSE"},
        "PENNY": {"cik": "333", "name": "Penny Corp", "exchange": "OTC"},      # never in universe
    }


def mas_base():
    return {t: {"primary_exchange": "XNAS", "type": "CS", "composite_figi": f"FIGI{t}"}
            for t in ("AAPL", "MSFT", "BRK-B", "FB", "XYZ", "MIDC")}


def fmp_base():
    return {
        "AAPL": {"sector": "Technology", "industry": "Consumer Electronics", "market_cap": 3.5e12},
        "MSFT": {"sector": "Technology", "industry": "Software", "market_cap": 3.1e12},
        "BRK-B": {"sector": "Financial Services", "industry": "Insurance", "market_cap": 9.5e11},
        "FB": {"sector": "Communication Services", "industry": "Internet", "market_cap": 1.4e12},
        "XYZ": {"sector": "Industrials", "industry": "Machinery", "market_cap": 1.5e9},
        "MIDC": {"sector": "Energy", "industry": "Oil", "market_cap": 9.0e9},
    }


SP = {f"S{i}" for i in range(495)} | {"AAPL", "MSFT", "BRK-B", "FB"}
NDX = {f"N{i}" for i in range(97)} | {"AAPL", "MSFT", "FB"}
DOW = {f"D{i}" for i in range(28)} | {"AAPL", "MSFT"}


def idx_base():
    return {"SP500": set(SP), "NASDAQ100": set(NDX), "DOW30": set(DOW)}


# ── 1. Pure helpers ───────────────────────────────────────────────────────────
print("\n=== 1. HELPERS ===")
check("ticker canonical form BRK.B -> BRK-B", smp.norm_ticker("brk.b") == "BRK-B")
check("name normaliser ignores suffix/case/punctuation",
      smp.norm_name("Apple Inc.") == smp.norm_name("APPLE INC") == "apple")
check("real rename is not normalised away", smp.norm_name("Facebook Inc") != smp.norm_name("Meta Platforms Inc"))
check("mcap classes", [smp.mcap_class(c) for c in (3e11, 5e10, 5e9, 1e9, 1e8, 1e7)] ==
      ["MEGA", "LARGE", "MID", "SMALL", "MICRO", "NANO"])
check("hysteresis: $10.2B stays MID", smp.sticky_mcap_class(10.2e9, "MID") == "MID")
check("hysteresis: $10.6B becomes LARGE", smp.sticky_mcap_class(10.6e9, "MID") == "LARGE")
check("hysteresis: $9.8B stays LARGE", smp.sticky_mcap_class(9.8e9, "LARGE") == "LARGE")
check("hysteresis: $9.4B becomes MID", smp.sticky_mcap_class(9.4e9, "LARGE") == "MID")
check("no current class -> plain class", smp.sticky_mcap_class(9.4e9, None) == "MID")
r = {"pending": {}}
c1 = smp.confirm(r, "name", "X", "2026-10-12")
c2 = smp.confirm(r, "name", "X", "2026-10-12")
c3 = smp.confirm(r, "name", "X", "2026-10-13")
check("confirm needs two SEPARATE days", (c1, c2, c3) == (False, False, True) and "name" not in r["pending"])
r = {"pending": {}}
smp.confirm(r, "name", "X", "2026-10-12")
check("a different value restarts the count", not smp.confirm(r, "name", "Y", "2026-10-13"))

# ── 2. Diff ───────────────────────────────────────────────────────────────────
print("\n=== 2. DIFF ===")
rows0, ev0 = smp.diff_master({}, sec_base(), mas_base(), fmp_base(), idx_base(), {}, "2026-10-12")
check("baseline: no events", ev0 == [], ev0)
check("baseline: OTC-only name kept out", "PENNY" not in rows0)
check("baseline: fields filled", rows0["AAPL"]["mcap_class"] == "MEGA" and rows0["AAPL"]["sector"] == "Technology"
      and rows0["AAPL"]["indexes"] == ["DOW30", "NASDAQ100", "SP500"] and rows0["AAPL"]["cik"] == "320193"
      and rows0["AAPL"]["composite_figi"] == "FIGIAAPL", rows0["AAPL"])
check("baseline: MIDC is MID", rows0["MIDC"]["mcap_class"] == "MID")
prev_idx = idx_base()


def day(prev, today, sec=None, mas="base", fmp=None, idx=None):
    return smp.diff_master(copy.deepcopy(prev), sec or sec_base(),
                           mas_base() if mas == "base" else mas, fmp or fmp_base(),
                           idx or idx_base(), prev_idx, today)


def kinds(evs):
    return sorted((e["ticker"], e["event_type"], e.get("new_value")) for e in evs)


# Symbol change: FB -> META, same CIK
s = sec_base(); s["META"] = s.pop("FB")
m = mas_base(); m["META"] = m.pop("FB")
r1, e1 = day(rows0, "2026-10-13", sec=s, mas=m)
check("symbol change detected same day", ("FB", "SYMBOL_CHANGE", "META") in kinds(e1), kinds(e1))
check("old row retired, new row carries data", r1["FB"]["renamed_to"] == "META" and not r1["FB"]["is_active"]
      and r1["META"]["sector"] == "Communication Services" and r1["META"]["mcap_class"] == "MEGA")
check("no DELISTED / LISTED noise for a rename",
      not any(e["event_type"] in ("DELISTED", "LISTED") for e in e1), kinds(e1))

# Rename: confirm over two days; formatting-only change is silent
s = sec_base(); s["XYZ"]["name"] = "Block Inc"; s["AAPL"]["name"] = "APPLE INC"
r1, e1 = day(rows0, "2026-10-13", sec=s)
check("rename day 1: pending only", not [e for e in e1 if e["event_type"] == "NAME_CHANGE"], kinds(e1))
check("formatting-only name change updates silently", r1["AAPL"]["name"] == "APPLE INC")
r2, e2 = day(r1, "2026-10-14", sec=s)
check("rename day 2: confirmed", ("XYZ", "NAME_CHANGE", "Block Inc") in kinds(e2), kinds(e2))
check("rename stored", r2["XYZ"]["name"] == "Block Inc")
r3, e3 = day(r2, "2026-10-15", sec=s)
check("no repeat on day 3", not e3, kinds(e3))

# Exchange transfer
s = sec_base(); s["XYZ"]["exchange"] = "OTC"
r1, e1 = day(rows0, "2026-10-13", sec=s)
r2, e2 = day(r1, "2026-10-14", sec=s)
check("exchange move to OTC confirmed on day 2 (kept in universe though OTC)",
      ("XYZ", "EXCHANGE_CHANGE", "OTC") in kinds(e2) and not e1, (kinds(e1), kinds(e2)))

# Delisting needs SEC + Massive both, on two days
s = sec_base(); s.pop("XYZ")
r1, e1 = day(rows0, "2026-10-13", sec=s)            # Massive still lists it
r2, e2 = day(r1, "2026-10-14", sec=s)
check("gone from SEC but still trading on Massive: no delisting", not e1 and not e2 and r2["XYZ"]["is_active"])
m = mas_base(); m.pop("XYZ")
r1, e1 = day(rows0, "2026-10-13", sec=s, mas=m)
check("delisting day 1: pending", not e1 and r1["XYZ"]["pending"].get("missing"))
r2, e2 = day(r1, "2026-10-14", sec=s, mas=m)
check("delisting day 2: confirmed", ("XYZ", "DELISTED", None) in kinds(e2) and not r2["XYZ"]["is_active"], kinds(e2))
r1b, e1b = day(rows0, "2026-10-13", sec=s, mas=None)
r2b, e2b = day(r1b, "2026-10-14", sec=s, mas=None)
check("Massive down: cannot confirm a delisting", not e2b and r2b["XYZ"]["is_active"])
r2c, e2c = day(r1, "2026-10-14")                    # reappears
check("reappearing clears the pending delisting", not e2c and "missing" not in r2c["XYZ"]["pending"])

# Market-cap class with hysteresis, 2-day confirm
f = fmp_base(); f["MIDC"]["market_cap"] = 1.06e10
r1, e1 = day(rows0, "2026-10-13", fmp=f)
r2, e2 = day(r1, "2026-10-14", fmp=f)
check("MID -> LARGE confirmed day 2", ("MIDC", "MCAP_CLASS_CHANGE", "LARGE") in kinds(e2) and not e1)
f["MIDC"]["market_cap"] = 1.02e10
r1, e1 = day(rows0, "2026-10-13", fmp=f)
r2, e2 = day(r1, "2026-10-14", fmp=f)
check("just over the line does not flip", not e2 and r2["MIDC"]["mcap_class"] == "MID")

# Sector
f = fmp_base(); f["XYZ"]["sector"] = "Technology"
r1, e1 = day(rows0, "2026-10-13", fmp=f)
r2, e2 = day(r1, "2026-10-14", fmp=f)
check("sector change confirmed day 2", ("XYZ", "SECTOR_CHANGE", "Technology") in kinds(e2) and not e1)
r1, e1 = day(rows0, "2026-10-13", fmp=None)
check("FMP down: no sector/mcap churn", not e1 and r1["XYZ"]["sector"] == "Industrials")

# Index membership
i = idx_base(); i["SP500"] = (i["SP500"] - {"S1"}) | {"XYZ"}
r1, e1 = day(rows0, "2026-10-13", idx=i)
check("S&P 500 addition", ("XYZ", "INDEX_ADDED", "SP500") in kinds(e1) and "SP500" in r1["XYZ"]["indexes"], kinds(e1))
i = idx_base(); i["DOW30"] = i["DOW30"] - {"MSFT"}
r1, e1 = day(rows0, "2026-10-13", idx=i)
check("Dow removal", ("MSFT", "INDEX_REMOVED", "DOW30") in kinds(e1) and "DOW30" not in r1["MSFT"]["indexes"])
i = idx_base(); i["SP500"] = set(list(i["SP500"])[:100])
r1, e1 = day(rows0, "2026-10-13", idx=i)
check("short index list ignored (vendor glitch)", not e1 and "SP500" in r1["AAPL"]["indexes"])
i = idx_base(); i["SP500"] = (i["SP500"] - {f"S{k}" for k in range(20)}) | {f"Z{k}" for k in range(20)}
r1, e1 = day(rows0, "2026-10-13", idx=i)
check("40-name swap in one run ignored", not e1)
i = idx_base(); i["SP500"] = None
r1, e1 = day(rows0, "2026-10-13", idx=i)
check("index fetch failed: membership kept", not e1 and "SP500" in r1["AAPL"]["indexes"])

# New listing
s = sec_base(); s["NEWCO"] = {"cik": "999", "name": "NewCo", "exchange": "Nasdaq"}
r1, e1 = day(rows0, "2026-10-13", sec=s)
check("new listing recorded as LISTED", ("NEWCO", "LISTED", "Nasdaq") in kinds(e1))

# ── 3. Alert rows ─────────────────────────────────────────────────────────────
print("\n=== 3. ALERT ROWS ===")
evs = [{"ticker": "FB", "cik": "1326801", "name": "Meta Platforms", "event_type": "SYMBOL_CHANGE",
        "old_value": "FB", "new_value": "META"},
       {"ticker": "BRK-B", "cik": "1067983", "name": "Berkshire", "event_type": "INDEX_ADDED",
        "old_value": None, "new_value": "SP500"},
       {"ticker": "MIDC", "cik": "222", "name": "Midcap", "event_type": "MCAP_CLASS_CHANGE",
        "old_value": "MID", "new_value": "LARGE"},
       {"ticker": "NEWCO", "cik": "9", "name": "NewCo", "event_type": "LISTED", "old_value": None,
        "new_value": "Nasdaq"},
       {"ticker": "MSFT", "cik": "789019", "name": "Microsoft", "event_type": "SECTOR_CHANGE",
        "old_value": "Technology", "new_value": "Software"}]
sent = set()
rows = smp.alert_rows(evs, {"MIDC": {"market_cap": 1.1e10}}, {"FB", "BRK.B", "MIDC", "NEWCO"}, "2026-10-13", sent)
by = {r["extra"]["event_type"]: r for r in rows}
check("only watched tickers alert (MSFT unwatched)", "SECTOR_CHANGE" not in by)
check("LISTED never alerts", "LISTED" not in by)
check("watchlist form kept (BRK.B)", by["INDEX_ADDED"]["ticker"] == "BRK.B")
check("symbol change alerts the OLD ticker, HIGH", by["SYMBOL_CHANGE"]["ticker"] == "FB"
      and by["SYMBOL_CHANGE"]["impact"] == "HIGH" and "META" in by["SYMBOL_CHANGE"]["summary"])
check("S&P add is HIGH, Feature 19", by["INDEX_ADDED"]["impact"] == "HIGH"
      and by["INDEX_ADDED"]["extra"]["feature_id"] == 19)
check("mcap up type + Feature 19 + cap text", by["MCAP_CLASS_CHANGE"]["filing_type"] == "STOCK_MCAP_CLASS_UP"
      and "$11.0B" in by["MCAP_CLASS_CHANGE"]["summary"])
check("symbol change is Feature 18", by["SYMBOL_CHANGE"]["extra"]["feature_id"] == 18)
check("dedup within a run", smp.alert_rows(evs, {}, {"FB"}, "2026-10-13", sent) == [])

# ── 4. Full run ───────────────────────────────────────────────────────────────
print("\n=== 4. FULL RUN ===")
FEED = {}


def install(sec, mas, fmp, idx):
    FEED.update(sec=sec, mas=mas, fmp=fmp, idx=idx)


smp.fetch_sec = lambda: copy.deepcopy(FEED["sec"])
smp.fetch_massive = lambda: copy.deepcopy(FEED["mas"])
smp.fetch_fmp = lambda: copy.deepcopy(FEED["fmp"])
smp.fetch_indexes = lambda: copy.deepcopy(FEED["idx"])
PROFILES = {"AAPL": {"isin": "US0378331005", "cusip": "037833100", "ipoDate": "1980-12-12"}}
PROFILE_CALLS = []


def fake_profile(t):
    PROFILE_CALLS.append(t)
    return PROFILES.get(t, {"isin": None})


smp.fmp_client.get_profile = fake_profile


def reset(watch=("FB", "XYZ", "AAPL")):
    DB.clear()
    DB["watchlists"] = [{"user_id": "u1", "ticker": t} for t in watch]
    DB["alerts"] = []
    watchlist_util._cache["watched"] = None


TODAY = {"d": "2026-10-12"}
smp._today_et = lambda: TODAY["d"]

reset()
install(sec_base(), mas_base(), fmp_base(), idx_base())
os.environ["STOCK_ALERTS_ENABLED"] = "true"
smp.run_stock_master()
check("baseline run stores master", len(DB["stock_master"]) == 6 and not alerts())
check("baseline stores index members", len(DB["stock_index_members"]) == len(SP) + len(NDX) + len(DOW))
aapl = next(r for r in DB["stock_master"] if r["ticker"] == "AAPL")
check("profile enrichment: ISIN/CUSIP/IPO date", aapl["isin"] == "US0378331005" and aapl["ipo_date"] == "1980-12-12")
check("watched tickers profiled first", PROFILE_CALLS[:3] == sorted(["AAPL", "FB", "XYZ"]), PROFILE_CALLS[:6])

s = sec_base(); s["XYZ"]["name"] = "Block Inc"; s["META"] = s.pop("FB")
m = mas_base(); m["META"] = m.pop("FB")
install(s, m, fmp_base(), idx_base())
TODAY["d"] = "2026-10-13"
smp.run_stock_master()
check("day 2: symbol change alerted, rename still pending",
      [a["filing_type"] for a in alerts()] == ["STOCK_SYMBOL_CHANGE"], [a["filing_type"] for a in alerts()])
xyz = next(r for r in DB["stock_master"] if r["ticker"] == "XYZ")
check("pending persisted in the table", xyz["pending"].get("name", {}).get("count") == 1, xyz["pending"])
smp.run_stock_master()
check("same-day re-run adds nothing", len(alerts()) == 1)
TODAY["d"] = "2026-10-14"
smp.run_stock_master()
check("day 3: rename alerted", sorted(a["filing_type"] for a in alerts()) ==
      ["STOCK_NAME_CHANGE", "STOCK_SYMBOL_CHANGE"])
check("events history kept", {e["event_type"] for e in DB["stock_master_events"]} >= {"SYMBOL_CHANGE", "NAME_CHANGE"})
check("Feature 18 footer", feature_map.feature_footer("STOCK_MASTER", "STOCK_NAME_CHANGE") ==
      "Feature 18 — Listing & Identity Changes")

# Switch off: data moves, no alerts
reset()
os.environ["STOCK_ALERTS_ENABLED"] = "false"
install(sec_base(), mas_base(), fmp_base(), idx_base()); TODAY["d"] = "2026-10-12"; smp.run_stock_master()
s = sec_base(); s["META"] = s.pop("FB"); m = mas_base(); m["META"] = m.pop("FB")
install(s, m, fmp_base(), idx_base()); TODAY["d"] = "2026-10-13"; smp.run_stock_master()
check("alerts off: no alert, change still recorded", not alerts() and
      any(e["event_type"] == "SYMBOL_CHANGE" for e in DB["stock_master_events"]))

# Alert write failure: master not advanced, no double send after recovery
reset()
os.environ["STOCK_ALERTS_ENABLED"] = "true"
install(sec_base(), mas_base(), fmp_base(), idx_base()); TODAY["d"] = "2026-10-12"; smp.run_stock_master()
install(s, m, fmp_base(), idx_base()); TODAY["d"] = "2026-10-13"
FAIL_INSERT.add("alerts"); smp.run_stock_master(); FAIL_INSERT.clear()
check("insert failed: master still on FB", any(r["ticker"] == "FB" and r["is_active"] for r in DB["stock_master"]))
smp.run_stock_master()
smp.run_stock_master()
check("recovered: exactly one symbol-change alert", len(alerts()) == 1)

# SEC down: nothing touched
before = copy.deepcopy(DB["stock_master"])
install(None, m, fmp_base(), idx_base()); smp.run_stock_master()
check("SEC unavailable: run skipped", DB["stock_master"] == before)

# ── 5. Feature 20: ETF ownership ──────────────────────────────────────────────
print("\n=== 5. ETF OWNERSHIP (Feature 20) ===")
funds = {"QQQ": {"name": "Invesco QQQ Trust", "aum": 3.0e11}, "TINY": {"name": "Tiny ETF", "aum": 2e7},
         "XLK": {"name": "Technology Select Sector SPDR", "aum": 7e10}}
ch = [{"id": 1, "ticker": "QQQ", "security_symbol": "AAPL", "action": "ADDED", "weight_new": 8.0},
      {"id": 2, "ticker": "TINY", "security_symbol": "AAPL", "action": "ADDED", "weight_new": 1.0},
      {"id": 3, "ticker": "XLK", "security_symbol": "MSFT", "action": "REMOVED", "weight_prev": 0.3},
      {"id": 4, "ticker": "TINY", "security_symbol": "BRK-B", "action": "REMOVED", "weight_prev": 2.0},
      {"id": 5, "ticker": "XLK", "security_symbol": "NVDA", "action": "ADDED", "weight_new": 9.0}]
wmap = sop.watched_map({"AAPL", "MSFT", "BRK.B"})
rows = sop.etf_holder_alerts(ch, funds, wmap, {}, set())
by = {r["ticker"]: r for r in rows}
check("unwatched NVDA ignored; sub-0.5% MSFT ignored", set(by) == {"AAPL", "BRK.B"}, set(by))
check("AAPL: two funds, HIGH ($24B QQQ position)", by["AAPL"]["impact"] == "HIGH"
      and "2 ETFs added AAPL" == by["AAPL"]["extra"]["headline"] and "≈$24.0B" in by["AAPL"]["summary"])
check("BRK.B: tiny fund exit is LOW ($400K)", by["BRK.B"]["impact"] == "LOW"
      and by["BRK.B"]["filing_type"] == "STOCK_ETF_REMOVED", by["BRK.B"]["summary"])
check("Feature 20 tag", by["AAPL"]["extra"]["feature_id"] == 20)
check("dedup key on max change id", by["AAPL"]["extra"]["dedup_key"] == "ETF_HOLDER:AAPL:2")

reset(watch=("AAPL",))
DB["etf_master"] = [{"ticker": k, **v} for k, v in funds.items()]
DB["etf_holdings_changes"] = [dict(c) for c in ch[:2]]
os.environ["STOCK_ALERTS_ENABLED"] = "true"
sop.run_stock_etf_holders()
check("first run only sets the watermark (no backlog)", not alerts() and
      DB["stock_ref_state"][0]["value"] == 2)
DB["etf_holdings_changes"].append({"id": 6, "ticker": "XLK", "security_symbol": "AAPL", "action": "ADDED",
                                   "weight_new": 12.0, "security_name": "Apple Inc"})
FAIL_INSERT.add("alerts"); sop.run_stock_etf_holders(); FAIL_INSERT.clear()
check("write failure keeps the watermark", DB["stock_ref_state"][0]["value"] == 2)
sop.run_stock_etf_holders(); sop.run_stock_etf_holders()
check("then one alert, watermark advanced", len(alerts("ETF_HOLDER")) == 1 and DB["stock_ref_state"][0]["value"] == 6)
check("XLK add ≈$8.4B", "≈$8.4B" in alerts("ETF_HOLDER")[0]["summary"])
os.environ["STOCK_ALERTS_ENABLED"] = "false"
DB["etf_holdings_changes"].append({"id": 7, "ticker": "QQQ", "security_symbol": "AAPL", "action": "REMOVED",
                                   "weight_prev": 8.0})
sop.run_stock_etf_holders()
check("alerts off: watermark moves, nothing sent", len(alerts("ETF_HOLDER")) == 1 and DB["stock_ref_state"][0]["value"] == 7)

# ── 6. Feature 21: 13F ────────────────────────────────────────────────────────
print("\n=== 6. 13F (Feature 21) ===")
check("Oct 12 2026: Q2 window closed, Q3 not ready", sop.reportable_quarter(date(2026, 10, 12)) is None)
check("Aug 19 2026 -> Q2 2026", sop.reportable_quarter(date(2026, 8, 19)) == (2026, 2))
check("Aug 18 2026 -> nothing yet", sop.reportable_quarter(date(2026, 8, 18)) is None)
check("Nov 19 2026 -> Q3 2026", sop.reportable_quarter(date(2026, 11, 19)) == (2026, 3))
check("Feb 20 2027 -> Q4 2026", sop.reportable_quarter(date(2027, 2, 20)) == (2026, 4))
check("May 20 2027 -> Q1 2027", sop.reportable_quarter(date(2027, 5, 20)) == (2027, 1))
RAW = {"AAPL": {"symbol": "AAPL", "investorsHolding": 5200, "investorsHoldingChange": -40,
                "numberOf13Fshares": 9.0e9, "numberOf13FsharesChange": -2.0e8, "ownershipPercent": 61.2,
                "ownershipPercentChange": -1.3, "newPositions": 180, "closedPositions": 220,
                "increasedPositions": 1800, "reducedPositions": 2100, "putCallRatio": 0.84},
       "MSFT": {"symbol": "MSFT", "investorsHolding": 6000, "investorsHoldingChange": 10,
                "numberOf13Fshares": 5.0e9, "numberOf13FsharesChange": 1.0e7, "ownershipPercent": 70.0,
                "ownershipPercentChange": 0.1}}
F13_CALLS = []


def fake_13f(t, y, q):
    F13_CALLS.append((t, y, q))
    return RAW.get(t)


sop.fetch_13f = fake_13f
s13 = sop.parse_13f(RAW["AAPL"])
a = sop.inst_alert("AAPL", "AAPL", "Apple Inc.", 2026, 3, s13, set())
check("13F: down, MEDIUM (-1.3 pts, -2.2% shares)", a["filing_type"] == "INST_OWNERSHIP_DOWN"
      and a["impact"] == "MEDIUM", (a["filing_type"], a["impact"]))
check("13F text", "5,200 (-40) vs Q2" in a["summary"] and "61.2% (-1.3 pts)" in a["summary"], a["summary"])
check("Feature 21 tag", a["extra"]["feature_id"] == 21)
m13 = sop.inst_alert("MSFT", "MSFT", None, 2026, 3, sop.parse_13f(RAW["MSFT"]), set())
check("13F: tiny change is LOW (filtered by default MEDIUM)", m13["impact"] == "LOW")

reset(watch=("AAPL", "MSFT", "QQQ"))
DB["etf_master"] = [{"ticker": "QQQ"}]
os.environ["STOCK_ALERTS_ENABLED"] = "true"
sop.run_stock_13f(today=date(2026, 10, 12))
check("outside window: no calls", not F13_CALLS)
sop.run_stock_13f(today=date(2026, 11, 20))
check("in window: stocks only (QQQ skipped)", sorted(t for t, _, _ in F13_CALLS) == ["AAPL", "MSFT"])
check("13F stored + 2 alerts", len(DB["stock_13f_summary"]) == 2 and len(alerts("INST_13F")) == 2)
sop.run_stock_13f(today=date(2026, 11, 21))
check("next day: no refetch, no repeat", len(F13_CALLS) == 2 and len(alerts("INST_13F")) == 2)
reset(watch=("AAPL",)); DB["etf_master"] = []; F13_CALLS.clear()
os.environ["STOCK_ALERTS_ENABLED"] = "false"
sop.run_stock_13f(today=date(2026, 11, 20)); sop.run_stock_13f(today=date(2026, 11, 21))
check("alerts off: data stored once, no alert", len(F13_CALLS) == 1 and len(DB["stock_13f_summary"]) == 1
      and not alerts())

# ── 7. Delivery + rendering ───────────────────────────────────────────────────
print("\n=== 7. DELIVERY + RENDERING ===")
for src, ft, fid in [("STOCK_MASTER", "STOCK_DELISTED", 18), ("STOCK_MASTER", "STOCK_INDEX_REMOVED", 19),
                     ("ETF_HOLDER", "STOCK_ETF_REMOVED", 20), ("INST_13F", "INST_OWNERSHIP_UP", 21)]:
    check(f"{src}/{ft} -> Feature {fid}, watchlist-only",
          feature_map.resolve_feature(src, ft)[0] == fid and not feature_map.is_market_wide(src, ft)
          and src in delivery.COMPANY_ONLY_SOURCES)
users = {"u1": {"user_id": "u1", "min_impact": "MEDIUM", "muted_features": set(), "receive_market_wide": True,
                "etf_signals_beta": False},
         "u2": {"user_id": "u2", "min_impact": "MEDIUM", "muted_features": {18}, "receive_market_wide": True,
                "etf_signals_beta": False},
         "u3": {"user_id": "u3", "min_impact": "MEDIUM", "muted_features": set(), "receive_market_wide": True,
                "etf_signals_beta": False}}
watch = {"FB": {"u1", "u2"}}
try:
    aud = delivery.resolve_audience({"source": "STOCK_MASTER", "filing_type": "STOCK_SYMBOL_CHANGE",
                                     "ticker": "FB", "impact": "HIGH", "extra": {}}, users, watch)
    got = sorted(u["user_id"] for u, _ in aud)
    check("symbol change reaches the watcher, honours mute, skips non-watcher", got == ["u1"], got)
except Exception as e:  # shape drift in delivery internals should fail loudly
    check("resolve_audience callable with test users", False, repr(e))
alert_formatter.fmp_client.get_quote = lambda t: {"price": 100.0, "changePercentage": 0.5}
msg = alert_formatter.build_message({"source": "STOCK_MASTER", "filing_type": "STOCK_SYMBOL_CHANGE",
                                     "ticker": "FB", "impact": "HIGH",
                                     "summary": "Meta Platforms changed its ticker from FB to META.",
                                     "extra": {"headline": "Meta Platforms now trades as META",
                                               "feature_id": 18, "feature_name": "Listing & Identity Changes"},
                                     "filing_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=1326801"})
text = msg if isinstance(msg, str) else str(msg)
check("renders headline, label and feature footer", "now trades as META" in text and "Ticker Change" in text
      and "Feature 18" in text, text[:400])

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
