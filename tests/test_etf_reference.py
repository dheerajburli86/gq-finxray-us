"""
ETF reference upkeep, offline:
  * Feature 14 resized positions (ETF_WEIGHT_SHIFT) net of fund flows
  * prospectus fee-table extraction (inline XBRL) + its use in expense alerts
  * fee-table backfill walking SEC submissions
  * ETF launches / closures vs Massive (Feature 16 ETF_DELISTED)
Run: python tests/test_etf_reference.py
"""
import os
import sys
from datetime import datetime as _dt, timedelta as _td

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x", "TELEGRAM_TOKEN": "1:x",
             "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

from fake_supabase import FakeDB  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import etf_fmp_client as F          # noqa: E402
import etf_data_poller as E         # noqa: E402
import etf_expense_extractor as X   # noqa: E402
import etf_listing_poller as L      # noqa: E402
import feature_map                  # noqa: E402
import delivery                     # noqa: E402
import alert_formatter              # noqa: E402

alert_formatter._cached_quote = lambda t: None

# ══════════════════════════════════════════════════════════════════════════════
print("=== 1. RESIZED POSITIONS (pure) ===")


def snap(items):
    """items: (key, weight, shares)"""
    return {k: {"security_key": k, "security_symbol": k, "security_name": f"{k} CORP",
                "portfolio_pct": w, "shares": sh} for k, w, sh in items}


base = [(f"S{i}", 10.0 - i * 0.5, 1000.0) for i in range(12)]
old = snap(base)
inflow = snap([(k, w * (1.03 if i % 2 else 0.97), sh * 1.10) for i, (k, w, sh) in enumerate(base)])
check("fund inflow + price drift -> nothing", E.weight_shifts(old, inflow) == [])
trim = [(k, w, sh * 1.10) for k, w, sh in base]
trim[0] = ("S0", 6.0, 1000.0 * 1.10 * 0.55)        # S0 cut to 55% beyond flow, 10% -> 6%
trim[3] = ("S3", 10.5, 1000.0 * 1.10 * 1.8)        # S3 added 80% beyond flow, 8.5% -> 10.5%
got = E.weight_shifts(old, snap(trim))
check("active trim + add detected, largest move first", [c["security_key"] for c in got] == ["S0", "S3"], got)
check("shares measured net of flow", got[0]["shares_vs_flow_pct"] == -45.0 and got[1]["shares_vs_flow_pct"] == 80.0, got)
split = [(k, w, sh) for k, w, sh in base]
split[1] = ("S1", 9.5, 2000.0)                     # 2:1 split, weight unchanged
check("stock split (shares x2, weight flat) ignored", E.weight_shifts(old, snap(split)) == [])
small = [(k, w, sh) for k, w, sh in base]
small[11] = ("S11", 4.5 + 0.3, 1500.0)             # +50% shares but only +0.3 pts
check("tiny weight move ignored", E.weight_shifts(old, snap(small)) == [])
check("too few paired positions -> nothing", E.weight_shifts(snap(base[:5]), snap(trim[:5])) == [])
units = snap([(k, w, sh * 1000) for k, w, sh in base])
check("vendor share-unit change cancels out", E.weight_shifts(old, units) == [])
cash = dict(old)
cash["CASH"] = {"security_key": "CASH", "security_symbol": None, "security_name": "USD CASH",
                "portfolio_pct": 1.0, "shares": 100.0}
cash2 = snap(trim)
cash2["CASH"] = dict(cash["CASH"], portfolio_pct=5.0, shares=1000.0)
check("cash lines never alert", "CASH" not in [c["security_key"] for c in E.weight_shifts(cash, cash2)])
s, imp, ft = E.shift_alert("Big Fund", "BIG", got, 12, "2026-10-12 10:00:00")
check("shift alert text + type", ft == "ETF_WEIGHT_SHIFT" and imp == "HIGH" and "S0 CORP (S0) 10.00% -> 6.00%" in s
      and "Decreased" in s and "Increased" in s, s)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 2. RESIZED POSITIONS through the poller (Feature 14) ===")
db = FakeDB()
E.sb = lambda: db
E.ANY_DAY = True
E.get_watched_tickers = lambda force=False: {"BIG"}
DAY = [_dt(2026, 10, 1, 9, 0, tzinfo=E.ET)]
HOLD = {}
F.raw_info = lambda t: {"name": "Big Fund", "expenseRatio": 0.10, "assetClass": "Equity", "etfCompany": "X"}
F.raw_holdings = lambda t: HOLD.get(t, [])
F.raw_price_change = lambda t: None
F.raw_profile = lambda t: None
F.raw_sector_weights = lambda t: []
F.raw_country_weights = lambda t: []


def fmp_hold(stamp, items):
    return [{"asset": k, "name": f"{k} CORP", "weightPercentage": w, "sharesNumber": sh,
             "marketValue": w * 1e6, "updatedAt": stamp} for k, w, sh in items]


def run(mode="light"):
    DAY[0] += _td(days=1)
    E.run_etf_data_poller(mode, now=DAY[0])


db.tables["etf_master"] = [{"ticker": "BIG", "name": "Big Fund"}]
HOLD["BIG"] = fmp_hold("2026-10-01 10:00:00", base)
run("full")
check("baseline: no alert", db.rows("alerts") == [])
HOLD["BIG"] = fmp_hold("2026-10-02 10:00:00", trim)
run()
a = [x for x in db.rows("alerts") if x["filing_type"] == "ETF_WEIGHT_SHIFT"]
check("resize alert written once", len(a) == 1, db.rows("alerts"))
check("tagged Feature 14", a and a[0]["extra"]["feature_id"] == 14)
run()
check("same snapshot again: no repeat", len([x for x in db.rows("alerts") if x["filing_type"] == "ETF_WEIGHT_SHIFT"]) == 1)
E.get_watched_tickers = lambda force=False: set()
HOLD["BIG"] = fmp_hold("2026-10-03 10:00:00", base)
run()
check("unwatched fund: no resize alert", len([x for x in db.rows("alerts") if x["filing_type"] == "ETF_WEIGHT_SHIFT"]) == 1)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 3. FEE TABLES (inline XBRL) ===")


def ctx(cid, series=None, klass=None, axis="oef:ClassAxis"):
    seg = ""
    if series:
        seg += f'<xbrldi:explicitMember dimension="dei:LegalEntityAxis">trust:{series}Member</xbrldi:explicitMember>'
    if klass:
        seg += f'<xbrldi:explicitMember dimension="{axis}">trust:{klass}Member</xbrldi:explicitMember>'
    seg = f"<xbrli:segment>{seg}</xbrli:segment>" if seg else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
            f'0000000001</xbrli:identifier>{seg}</xbrli:entity><xbrli:period><xbrli:startDate>2026-01-01'
            f'</xbrli:startDate><xbrli:endDate>2026-12-31</xbrli:endDate></xbrli:period></xbrli:context>')


def fact(tag, cid, text, scale="-2", sign=None, fmt="ixt:num-dot-decimal"):
    sg = f' sign="{sign}"' if sign else ""
    return (f'<td><ix:nonFraction name="oef:{tag}" contextRef="{cid}" unitRef="pure" decimals="4" '
            f'scale="{scale}" format="{fmt}"{sg}>{text}</ix:nonFraction></td>')


DOC = ("<html><body><div style='display:none'><ix:header><ix:resources>"
       + ctx("c1", "S000000001", "C000000011") + ctx("c2", "S000000002", "C000000022")
       + ctx("c3", "S000000003", "C000000033")
       + "</ix:resources></ix:header></div><table>"
       # Fund A: by class id. Waiver shown in parentheses, sign attribute.
       + fact("ManagementFeesOverAssets", "c1", "0.35")
       + fact("DistributionAndService12b1FeesOverAssets", "c1", "—", fmt="ixt:fixed-zero")
       + fact("OtherExpensesOverAssets", "c1", "0.05")
       + fact("ExpensesOverAssets", "c1", "0.40")
       + fact("FeeWaiverOrReimbursementOverAssets", "c1", "(0.10)", sign="-")
       + fact("NetExpensesOverAssets", "c1", "0.30")
       # Fund B: matched via dei:TradingSymbol (class id unknown to the universe)
       + '<ix:nonNumeric name="dei:TradingSymbol" contextRef="c2">bbb</ix:nonNumeric>'
       + fact("ManagementFeesOverAssets", "c2", "0.0009", scale="0")
       + fact("ExpensesOverAssets", "c2", "0.0009", scale="0")
       # Fund C: a mutual fund -- extracted but not stored (not an ETF)
       + fact("ManagementFeesOverAssets", "c3", "0.75")
       # Garbage value is dropped
       + fact("OtherExpensesOverAssets", "c3", "9999")
       # The expense EXAMPLE repeats numbers later; first value must win
       + fact("ManagementFeesOverAssets", "c1", "0.99")
       + "</table></body></html>")
by_class = {"C000000011": "AAA", "C000000033": "MFX"}
by_series = {"S000000001": ["AAA"], "S000000002": ["BBB"], "S000000003": ["MFX"]}
t = X.extract_fee_tables(DOC, by_class, by_series, ["S000000001", "S000000002", "S000000003"])
check("three funds found", set(t) == {"AAA", "BBB", "MFX"}, t)
check("AAA fee table", {k: t["AAA"].get(k) for k in X.FIELDS} == {
    "management_fee": 0.35, "distribution_12b1": 0.0, "other_expenses": 0.05, "acquired_fund_fees": None,
    "gross_expense_ratio": 0.4, "fee_waiver": -0.1, "net_expense_ratio": 0.3}, t["AAA"])
check("first value wins over the expense example", t["AAA"]["management_fee"] == 0.35)
check("BBB via TradingSymbol, scale 0 pure -> 0.09%", t["BBB"]["management_fee"] == 0.09
      and t["BBB"]["net_expense_ratio"] == 0.09, t["BBB"])
check("implausible value dropped", t["MFX"].get("other_expenses") is None)
check("ids carried", t["AAA"]["series_id"] == "S000000001" and t["AAA"]["class_id"] == "C000000011")
one = ("<html>" + ctx("d0") + fact("ManagementFeesOverAssets", "d0", "0.20")
       + fact("ExpensesOverAssets", "d0", "0.20") + "</html>")
t1 = X.extract_fee_tables(one, {}, {"S000000009": ["ONE"]}, ["S000000009"])
check("single-fund filing without dimensions", t1.get("ONE", {}).get("gross_expense_ratio") == 0.2, t1)
check("multi-series filing without dimensions is NOT guessed",
      X.extract_fee_tables(one, {}, {"S1": ["A"], "S2": ["B"]}, ["S1", "S2"]) == {})
check("no XBRL -> nothing", X.extract_fee_tables("<html>plain</html>", by_class, by_series) == {})
check("breakdown text", X.breakdown_text(t["AAA"]) ==
      "management fee 0.35%, other expenses 0.05%, total 0.40%, fee waiver -0.10%, net 0.30%",
      X.breakdown_text(t["AAA"]))

fdb = FakeDB()
fdb.tables["etf_master"] = [{"ticker": "AAA"}, {"ticker": "BBB"}]
n = X.store(fdb, t, {"accession": "0001-26-000001", "form": "497K", "filing_date": "2026-09-30",
                     "url": "https://www.sec.gov/x"})
check("only ETFs stored", n == 2 and {r["ticker"] for r in fdb.rows("etf_expense_breakdown")} == {"AAA", "BBB"})
X.store(fdb, t, {"accession": "0001-26-000001", "form": "497K", "filing_date": "2026-09-30"})
check("re-storing the same filing is idempotent", len(fdb.rows("etf_expense_breakdown")) == 2)
check("latest() returns the row", (X.latest(fdb, "AAA") or {}).get("net_expense_ratio") == 0.3)

# Expense alert carries the breakdown
db2 = FakeDB()
E.sb = lambda: db2
E.get_watched_tickers = lambda force=False: {"AAA"}
db2.tables["etf_master"] = [{"ticker": "AAA", "name": "Fund A"}]
db2.tables["etf_expense_breakdown"] = [dict(r) for r in fdb.rows("etf_expense_breakdown") if r["ticker"] == "AAA"]
ER = [0.40]
F.raw_info = lambda tk: {"name": "Fund A", "expenseRatio": ER[0], "assetClass": "Equity"}
HOLD["AAA"] = fmp_hold("2026-10-01 10:00:00", base)
run("full")
ER[0] = 0.30
run(); run()
ea = [x for x in db2.rows("alerts") if x["filing_type"] == "ETF_EXPENSE_DECREASE"]
check("expense alert includes the prospectus breakdown", ea and "management fee 0.35%" in ea[0]["summary"]
      and "fee waiver -0.10%" in ea[0]["summary"] and ea[0]["extra"]["fee_breakdown"]["net_expense_ratio"] == 0.3,
      ea and ea[0]["summary"])
db3 = FakeDB()
E.sb = lambda: db3
db3.tables["etf_master"] = [{"ticker": "AAA", "name": "Fund A"}]
db3.fail_tables = set()
ER[0] = 0.40
run("full"); ER[0] = 0.30; run(); run()
ea = [x for x in db3.rows("alerts") if x["filing_type"] == "ETF_EXPENSE_DECREASE"]
check("no breakdown stored: alert unchanged", ea and "prospectus" not in ea[0]["summary"]
      and ea[0]["extra"]["fee_breakdown"] is None)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 4. FEE-TABLE BACKFILL ===")
import fund_manager_poller as FM  # noqa: E402

bdb = FakeDB()
bdb.tables["etf_master"] = [{"ticker": "AAA"}, {"ticker": "BBB"}]
FM.sb = lambda: bdb
U = {"by_ticker": {"AAA": {"cik": "0000000001", "series_id": "S000000001", "class_id": "C000000011", "fund_type": "ETF"},
                   "BBB": {"cik": "0000000001", "series_id": "S000000002", "class_id": "C000000022", "fund_type": "ETF"},
                   "MFX": {"cik": "0000000002", "series_id": "S000000003", "class_id": "C000000033", "fund_type": "MF"}},
     "by_series": {"S000000001": {"cik": "0000000001", "tickers": ["AAA"], "fund_type": "ETF"},
                   "S000000002": {"cik": "0000000001", "tickers": ["BBB"], "fund_type": "ETF"},
                   "S000000003": {"cik": "0000000002", "tickers": ["MFX"], "fund_type": "MF"}},
     "ciks": {"0000000001", "0000000002"}}
FM.load_universe = lambda force=False: U
REQ = []
today = _dt.now().date().isoformat()


class FakeSEC:
    def json(self, url, params=None):
        REQ.append(url)
        return {"filings": {"recent": {
            "form": ["N-CSR", "497K", "485BPOS"], "filingDate": [today, today, today],
            "accessionNumber": ["0001-26-000009", "0001-26-000002", "0001-26-000001"],
            "primaryDocument": ["ncsr.htm", "k.htm", "pos.htm"]}}}

    def text(self, url):
        REQ.append(url)
        return DOC


FM.sec = FakeSEC()
FM.fetch_series_header = lambda cik, adsh: [{"series_id": "S000000001"}, {"series_id": "S000000002"}]
X.run_expense_backfill()
check("backfill stores both ETF fee tables", {r["ticker"] for r in bdb.rows("etf_expense_breakdown")} == {"AAA", "BBB"},
      bdb.rows("etf_expense_breakdown"))
check("MF registrant not walked", not any("0000000002" in u for u in REQ), REQ)
check("stops once the registrant is covered (one prospectus read)",
      sum(1 for u in REQ if u.endswith(".htm")) == 1, REQ)
check("filing ledger records it", any(r["purpose"] == "EXPENSE" and r["status"] == "OK"
                                      for r in bdb.rows("fund_filings_processed")))
REQ.clear()
X.run_expense_backfill()
check("second run: nothing left to do, no SEC calls", REQ == [], REQ)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 5. LAUNCHES / CLOSURES (etf_listing_poller) ===")
ldb = FakeDB()
L.sb = lambda: ldb
WATCH = {"OLD1", "GONE"}
L.get_watched_tickers = lambda force=False: set(WATCH)
L.MIN_ACTIVE = 10
ACTIVE = {}


def listed(*items):
    """items: (ticker, type)"""
    ACTIVE.clear()
    for i in range(20):
        ACTIVE[f"CS{i}"] = {"ticker": f"CS{i}", "name": f"Stock {i}", "type": "CS", "primary_exchange": "XNYS"}
    for tk, ty in items:
        ACTIVE[L.norm(tk)] = {"ticker": tk, "name": f"{tk} ETF", "type": ty, "primary_exchange": "ARCX"}


L.fetch_active = lambda: dict(ACTIVE) if ACTIVE else None
ldb.tables["etf_master"] = [{"ticker": t, "name": f"{t} Fund", "is_actively_trading": True}
                            for t in ("OLD1", "GONE", "NEVER")]
ldb.tables["alerts"] = []


def state(t):
    return next((r for r in ldb.rows("etf_listing_state") if r["ticker"] == t), {})


def master(t):
    return next((r for r in ldb.rows("etf_master") if r["ticker"] == t), None)


listed(("OLD1", "ETF"), ("GONE", "ETF"), ("MISSED", "ETF"), ("NOTE1", "ETN"))
L.run_etf_listing_poller("2026-10-12")
check("baseline: missing ETFs/ETNs added to etf_master silently",
      master("MISSED") and master("NOTE1") and not ldb.rows("etf_listing_events") and not ldb.rows("alerts"))
check("baseline: MIC mapped to exchange", master("MISSED")["exchange"] == "NYSE ARCA")
check("baseline: never-listed master ticker parked", state("NEVER")["status"] == "NOT_LISTED")

listed(("OLD1", "ETF"), ("MISSED", "ETF"), ("NOTE1", "ETN"), ("NEWB", "ETF"))   # GONE missing, NEWB new
L.run_etf_listing_poller("2026-10-13")
check("day 1 missing: pending, no alert", state("GONE")["status"] == "PENDING_CLOSE" and not ldb.rows("alerts"))
check("day 1 new: pending launch, not in master yet", state("NEWB")["status"] == "PENDING_LAUNCH" and not master("NEWB"))
L.run_etf_listing_poller("2026-10-13")
check("same-day re-run does not confirm", state("GONE")["status"] == "PENDING_CLOSE" and not master("NEWB"))
L.run_etf_listing_poller("2026-10-14")
check("day 2: closure confirmed", state("GONE")["status"] == "CLOSED" and master("GONE")["is_actively_trading"] is False)
al = ldb.rows("alerts")
check("watched closure alerts once, Feature 16, HIGH", len(al) == 1 and al[0]["ticker"] == "GONE"
      and al[0]["filing_type"] == "ETF_DELISTED" and al[0]["extra"]["feature_id"] == 16 and al[0]["impact"] == "HIGH", al)
check("day 2: launch confirmed and added", master("NEWB") and state("NEWB")["status"] == "ACTIVE")
ev = {(e["ticker"], e["event_type"]) for e in ldb.rows("etf_listing_events")}
check("events recorded", {("GONE", "CLOSED"), ("NEWB", "LAUNCHED")} <= ev, ev)
check("never-listed ticker never 'closes'", state("NEVER")["status"] == "NOT_LISTED")
L.run_etf_listing_poller("2026-10-15")
check("no repeat alert", len(ldb.rows("alerts")) == 1)
listed(("OLD1", "ETF"), ("MISSED", "ETF"), ("NOTE1", "ETN"), ("NEWB", "ETF"), ("GONE", "ETF"))
L.run_etf_listing_poller("2026-10-16")
check("reappearing fund: RELISTED, trading again", ("GONE", "RELISTED") in
      {(e["ticker"], e["event_type"]) for e in ldb.rows("etf_listing_events")} and master("GONE")["is_actively_trading"])

# Glitch guard + failures
gdb = FakeDB()
L.sb = lambda: gdb
gdb.tables["etf_master"] = [{"ticker": f"E{i}", "name": f"E{i}", "is_actively_trading": True} for i in range(40)]
gdb.tables["alerts"] = []
listed(*[(f"E{i}", "ETF") for i in range(40)])
L.run_etf_listing_poller("2026-10-12")
listed(("E0", "ETF"))
L.run_etf_listing_poller("2026-10-13")
L.run_etf_listing_poller("2026-10-14")
check("39 funds vanishing at once is held as a glitch",
      all(r["status"] != "CLOSED" for r in gdb.rows("etf_listing_state")))
ACTIVE.clear()
before = [dict(r) for r in gdb.rows("etf_listing_state")]
L.run_etf_listing_poller("2026-10-15")
check("Massive down: nothing touched", gdb.rows("etf_listing_state") == before)

fdb2 = FakeDB()
L.sb = lambda: fdb2
fdb2.tables["etf_master"] = [{"ticker": "GONE", "name": "Gone", "is_actively_trading": True}]
fdb2.tables["alerts"] = []
listed(("GONE", "ETF")); L.run_etf_listing_poller("2026-10-12")
listed(); L.run_etf_listing_poller("2026-10-13")
fdb2.fail_tables = {"alerts"}
L.run_etf_listing_poller("2026-10-14")
fdb2.fail_tables = set()
check("alert ledger down: closure not committed", next(r for r in fdb2.rows("etf_listing_state"))["status"] != "CLOSED")
L.run_etf_listing_poller("2026-10-15")
check("then closes and alerts once", len(fdb2.rows("alerts")) == 1
      and next(r for r in fdb2.rows("etf_listing_state"))["status"] == "CLOSED")

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 6. ROUTING + RENDERING ===")
for src, ft, fid in [("ETF_LISTING", "ETF_DELISTED", 16), ("ETF_PORTFOLIO", "ETF_WEIGHT_SHIFT", 14)]:
    check(f"{src}/{ft} -> Feature {fid}, watchlist-only",
          feature_map.resolve_feature(src, ft)[0] == fid and not feature_map.is_market_wide(src, ft)
          and src in delivery.COMPANY_ONLY_SOURCES)
users = {u: {"user_id": u, "min_impact": "MEDIUM", "muted_features": m, "receive_market_wide": True,
             "etf_signals_beta": False} for u, m in (("u1", set()), ("u2", {16}), ("u3", set()))}
aud = delivery.resolve_audience({"source": "ETF_LISTING", "filing_type": "ETF_DELISTED", "ticker": "GONE",
                                 "impact": "HIGH", "extra": {}}, users, {"GONE": {"u1", "u2"}})
check("closure reaches the watcher only, mute honoured", sorted(u["user_id"] for u, _ in aud) == ["u1"])
msg = alert_formatter.build_message(dict(fdb2.rows("alerts")[0]))
check("renders label + footer", "Fund Stopped Trading" in str(msg) and "Feature 16" in str(msg), str(msg)[:300])

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
