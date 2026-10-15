"""
Mutual funds, offline: scheme master, /add, N-PORT holdings alerts, prospectus
fee-change alerts, and Features 15/16 covering mutual funds.
Run: python tests/test_mutual_funds.py
"""
import os
import sys
import types
from datetime import date, timedelta

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


import fund_manager_poller as FM     # noqa: E402
import mf_poller as MF               # noqa: E402
import etf_expense_extractor as X    # noqa: E402
import etf_fmp_client as F           # noqa: E402
import feature_map                   # noqa: E402
import delivery                      # noqa: E402
import alert_formatter               # noqa: E402

alert_formatter._cached_quote = lambda t: None
db = FakeDB()
FM.sb = lambda: db
WATCH = {"VFIAX", "VTSAX"}
FM.watched_tickers = lambda: set(WATCH)
FM.log = lambda *_a, **_k: None

U = {"by_ticker": {}, "by_series": {}, "ciks": set()}


def add_fund(t, cik, sid, cid, ftype="MF"):
    U["by_ticker"][t] = {"cik": cik, "series_id": sid, "class_id": cid, "fund_type": ftype}
    s = U["by_series"].setdefault(sid, {"cik": cik, "tickers": [], "fund_type": ftype})
    s["tickers"].append(t)
    U["ciks"].add(cik)


add_fund("VFIAX", "0000036405", "S000002839", "C000007773")
add_fund("VFINX", "0000036405", "S000002839", "C000007772")
add_fund("VTSAX", "0000036405", "S000002848", "C000007806")
add_fund("SPY", "0000884394", "S000099999", "C000099999", "ETF")
def _tk(i):                       # realistic mutual fund ticker: 4 letters + X
    a = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return a[i // 17576 % 26] + a[i // 676 % 26] + a[i // 26 % 26] + a[i % 26] + "X"


for i in range(6000):
    add_fund("Q" + _tk(i)[1:], "0000000777", f"S1{i:08d}", f"C1{i:08d}")
FM.load_universe = lambda force=False: U

# ══════════════════════════════════════════════════════════════════════════════
print("=== 1. SCHEME MASTER ===")
CSV = ("﻿Reporting File Number,CIK Number,Entity Name,Entity Org Type,Series ID,Series Name,"
       "Class ID,Class Name,Class Ticker,Address_1,Address_2,City,State,Zip Code\n"
       "811-02652,36405,VANGUARD INDEX FUNDS,30,S000002839,Vanguard 500 Index Fund,C000007773,"
       "Admiral Shares,VFIAX,PO BOX 2600,,VALLEY FORGE,PA,19482\n"
       "811-02652,36405,VANGUARD INDEX FUNDS,30,S000002839,Vanguard 500 Index Fund,C000007772,"
       "Investor Shares,VFINX,PO BOX 2600,,VALLEY FORGE,PA,19482\n"
       "811-02652,36405,VANGUARD INDEX FUNDS,30,S000002848,Vanguard Total Stock Market Index Fund,"
       "C000007806,Admiral Shares,VTSAX,PO BOX 2600,,VALLEY FORGE,PA,19482\n")
names = MF.parse_series_class_csv(CSV)
check("CSV parsed by class id (BOM + loose headers)", names["C000007773"]["series_name"] == "Vanguard 500 Index Fund"
      and names["C000007773"]["class_name"] == "Admiral Shares" and names["C000007773"]["entity_name"] == "VANGUARD INDEX FUNDS")
check("series fallback key present", names["S000002848"]["series_name"].startswith("Vanguard Total"))
rows = MF.master_rows(U, names)
vf = next(r for r in rows if r["ticker"] == "VFIAX")
check("ETFs excluded, MFs in", "SPY" not in {r["ticker"] for r in rows} and len(rows) == 6003)
check("master row joins names", vf["series_name"] == "Vanguard 500 Index Fund" and vf["class_name"] == "Admiral Shares")


class Resp:
    def __init__(self, text):
        self.content, self.status_code = text.encode(), 200


FETCHED = []


class FakeSEC:
    docs = {}

    def get(self, url, params=None, timeout=30, retries=4):
        FETCHED.append(url)
        if "series-class" in url and "2026" in url and "investment-company-series-class-information/investment-company" in url:
            return Resp(CSV + ("," * 13 + "\n") * 20000)
        return None

    def json(self, url, params=None):
        FETCHED.append(url)
        return self.docs.get(url)

    def text(self, url):
        FETCHED.append(url)
        return self.docs.get(url)


SEC = FakeSEC()
FM.sec = SEC
MF.date = types.SimpleNamespace(today=lambda: date(2026, 10, 12))
F.raw_info = lambda t: {"fundFamily": "Vanguard", "assetClass": "Equity", "expenseRatio": 0.04,
                        "assetsUnderManagement": 1.4e12, "inceptionDate": "2000-11-13"} if t == "VFIAX" else None
db.tables["mf_master"] = [{"ticker": "OLDMF", "cik": "1", "series_id": "S0", "is_active": True}]
MF.refresh_mf_master()
m = {r["ticker"]: r for r in db.rows("mf_master")}
check("mf_master filled", len([r for r in m.values() if r.get("is_active")]) == 6003, len(m))
check("names from SEC CSV", m["VFIAX"]["series_name"] == "Vanguard 500 Index Fund")
check("closed share class retired", m["OLDMF"]["is_active"] is False)
check("watched MF enriched from FMP", m["VFIAX"].get("fund_family") == "Vanguard" and m["VFIAX"]["expense_ratio"] == 0.04
      and m["VFIAX"]["aum"] == 1.4e12, m["VFIAX"])
check("unwatched MF not sent to FMP", m["VFINX"].get("fmp_checked_at") is None)
U_small = {"by_ticker": {"A": U["by_ticker"]["VFIAX"]}, "by_series": {}, "ciks": set()}
FM.load_universe = lambda force=False: U_small
before = len(db.rows("mf_master"))
MF.refresh_mf_master()
check("short registry read: nothing retired", all(r["is_active"] for r in db.rows("mf_master") if r["ticker"] != "OLDMF"))
FM.load_universe = lambda force=False: U

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 2. /add ACCEPTS MUTUAL FUNDS ===")
import telegram_bot as TB  # noqa: E402

TB.supabase = db
db.tables.setdefault("stocks", [])
db.tables.setdefault("etf_master", [])
got = TB.get_stock_by_ticker("vfiax")
check("/add finds a mutual fund", got and got["ticker"] == "VFIAX" and got["sector"] == "Mutual Fund · Admiral Shares"
      and got["name"] == "Vanguard 500 Index Fund", got)
check("retired class refused", TB.get_stock_by_ticker("OLDMF") is None)
check("unknown ticker still refused", TB.get_stock_by_ticker("ZZZZZ") is None)
check("prefix search includes mutual funds", "VFIAX" in [r["ticker"] for r in TB.search_stocks_by_prefix("VFI")])

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 3. N-PORT PARSING ===")


def nport(series, rep, items, net=1e9):
    sec = "".join(
        f"<invstOrSec><name>{n}</name><lei>X</lei><title>{n} COM</title><cusip>{c}</cusip>"
        f"<identifiers>{'<isin value=\"' + i + '\"/>' if i else ''}{'<ticker value=\"' + tk + '\"/>' if tk else ''}</identifiers>"
        f"<balance>{b}</balance><units>NS</units><curCd>USD</curCd><valUSD>{b * 10}</valUSD><pctVal>{p}</pctVal>"
        f"<payoffProfile>Long</payoffProfile><assetCat>{cat}</assetCat></invstOrSec>"
        for n, c, i, tk, b, p, cat in items)
    return ('<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/nport">'
            "<headerData><submissionType>NPORT-P</submissionType></headerData><formData>"
            f"<genInfo><regName>VANGUARD INDEX FUNDS</regName><seriesName>Vanguard 500</seriesName>"
            f"<seriesId>{series}</seriesId><repPdEnd>{rep}</repPdEnd><repPdDate>{rep}</repPdDate></genInfo>"
            f"<fundInfo><totAssets>{net}</totAssets><netAssets>{net}</netAssets></fundInfo>"
            f"<invstOrSecs>{sec}</invstOrSecs></formData></edgarSubmission>")


BASE = [(f"CO{i}", f"00000000{i}", f"US00000000{i:02d}", f"T{i}", 1000.0, 9.0 - i * 0.5, "EC") for i in range(12)]
x1 = nport("S000002839", "2026-06-30", BASE + [("CO0", "000000000", "US0000000000", "T0", 100.0, 0.2, "EC"),
                                               ("CASH", "N/A", None, None, 1.0, 1.0, "STIV")])
p = MF.parse_nport(x1)
check("namespace stripped, header read", p["series_id"] == "S000002839" and p["rep_date"] == "2026-06-30"
      and p["net_assets"] == 1e9)
check("duplicate lines merged", round(p["holdings"]["US0000000000"]["portfolio_pct"], 2) == 9.2
      and p["holdings"]["US0000000000"]["balance"] == 1100.0)
check("keys: ISIN first, N/A cusip ignored", "US0000000005" in p["holdings"] and "CASH|CASH COM" in p["holdings"])
check("bad XML -> None", MF.parse_nport("<not xml") is None)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 4. HOLDINGS DIFF + ALERT TEXT ===")
old = MF.parse_nport(nport("S", "2026-03-31", BASE))["holdings"]
q2 = [list(r) for r in BASE]
q2[1][4], q2[1][5] = 1700.0, 12.0          # CO1 bought up: shares +70%, 8.5% -> 12%
q2 = [tuple(r) for r in q2 if r[0] != "CO11"] + [("NEWCO", "999999999", "US9999999999", "NEW", 500.0, 2.5, "EC")]
new = MF.parse_nport(nport("S", "2026-06-30", q2))["holdings"]
d = MF.diff_snapshot(old, new)
check("added / exited / resized found", [c["security_key"] for c in d["added"]] == ["US9999999999"]
      and [c["security_key"] for c in d["removed"]] == ["US0000000011"]
      and [c["security_key"] for c in d["resized"]] == ["US0000000001"], d)
s, imp, ft, lists = MF.build_alert("Vanguard 500 Index Fund", "VFIAX", d, "2026-06-30", "2026-08-28", False)
check("combined alert", ft == "MF_HOLDINGS_CHANGE" and "Added: NEWCO (NEW) at 2.50%" in s and "Exited: CO11" in s
      and "Resized: CO1 (T1) 8.50% -> 12.00%" in s and "SEC Form N-PORT" in s, s)
check("resize of 3.5 pts is HIGH", imp == "HIGH")
check("bond fund: adds/exits suppressed", MF.build_alert("Bond", "B", dict(d, resized=[]), "x", "y", True) is None)
check("partial filing ignored", MF.diff_snapshot(old, dict(list(new.items())[:3]))["rebaseline"])
churn = MF.parse_nport(nport("S", "2026-06-30", [(f"Z{i}", f"9{i:08d}", f"ZZ{i:010d}", None, 10.0, 5.0, "EC")
                                                 for i in range(15)]))["holdings"]
old25 = MF.parse_nport(nport("S", "2026-03-31", [(f"A{i}", f"8{i:08d}", f"AA{i:010d}", None, 10.0, 4.0, "EC")
                                                  for i in range(25)]))["holdings"]
check("full turnover treated as re-keying", "churn" in (MF.diff_snapshot(old25, churn)["rebaseline"] or ""))
check("debt fund detection", MF.is_debt_fund(MF.parse_nport(nport("S", "x", [("B", "1", None, None, 1.0, 90.0, "DBT"),
                                                                              ("E", "2", None, None, 1.0, 10.0, "EC")]))["holdings"]))

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 5. N-PORT POLLER END TO END (Feature 18) ===")
CIK = "0000036405"
SUBS = FM.URL_SUBMISSIONS.format(cik10=CIK)
HDR = {}
FM.fetch_series_header = lambda cik, adsh: [{"series_id": s} for s in HDR.get(adsh, [])]


def filings(*rows):
    """rows: (accession, filing_date, form)"""
    SEC.docs[SUBS] = {"filings": {"recent": {
        "form": [r[2] for r in rows], "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[0] for r in rows], "primaryDocument": ["primary_doc.xml"] * len(rows)}}}


def doc(adsh, xml):
    SEC.docs[FM._archive(CIK, adsh, "primary_doc.xml")] = xml


db.tables["alerts"] = []
# the 6,000 synthetic funds were only needed for the master refresh; keep the real ones here
db.tables["mf_master"] = [r for r in db.rows("mf_master") if r["ticker"] in ("VFIAX", "VFINX", "VTSAX")]
MF.RECHECK_WATCHED_DAYS = MF.RECHECK_DAYS = 0        # no waiting between runs in this section
HDR.update({"A-Q1-500": ["S000002839"], "A-Q1-TSM": ["S000002848"], "A-Q2-500": ["S000002839"],
            "A-Q2-500-AMEND": ["S000002839"]})
filings(("A-Q1-500", "2026-05-28", "NPORT-P"), ("A-Q1-TSM", "2026-05-28", "NPORT-P"), ("X", "2026-05-01", "N-CSR"))
doc("A-Q1-500", nport("S000002839", "2026-03-31", BASE))
doc("A-Q1-TSM", nport("S000002848", "2026-03-31", BASE))
MF.run_mf_holdings()
check("first filing per fund is a silent baseline", db.rows("alerts") == [] and
      {r["series_id"] for r in db.rows("mf_holdings_state") if r.get("accession")} == {"S000002839", "S000002848"})
check("headers read once and cached", len(db.rows("mf_nport_filings")) == 2)
n_hdr = len(FETCHED)
MF.run_mf_holdings()
check("nothing new: no header or document re-reads",
      not any(u.endswith("primary_doc.xml") or "hdr" in u for u in FETCHED[n_hdr:]), FETCHED[n_hdr:])
WATCH.add("VFINX")
filings(("A-Q2-500-AMEND", "2026-08-29", "NPORT-P/A"), ("A-Q2-500", "2026-08-28", "NPORT-P"),
        ("A-Q1-500", "2026-05-28", "NPORT-P"), ("A-Q1-TSM", "2026-05-28", "NPORT-P"))
doc("A-Q2-500", nport("S000002839", "2026-06-30", q2))
MF.run_mf_holdings()
al = db.rows("alerts")
check("Q2 filing alerts each watched share class of the fund", sorted(a["ticker"] for a in al) == ["VFIAX", "VFINX"], al)
check("Feature 18, MF_PORTFOLIO", al and al[0]["extra"]["feature_id"] == 18 and al[0]["source"] == "MF_PORTFOLIO")
check("fund name from mf_master", al and al[0]["summary"].startswith("Vanguard 500 Index Fund"))
check("amendments ignored", not any("AMEND" in (a["extra"].get("accession") or "") for a in al))
check("changes recorded", {c["action"] for c in db.rows("mf_holdings_changes")} == {"ADDED", "REMOVED", "RESIZED"})
MF.run_mf_holdings()
check("re-run: no repeat", len(db.rows("alerts")) == 2)
st = next(r for r in db.rows("mf_holdings_state") if r["series_id"] == "S000002839")
check("state advanced to Q2", st["rep_date"] == "2026-06-30" and st["accession"] == "A-Q2-500")
db.fail_tables = {"alerts"}
filings(("A-Q3-500", "2026-11-25", "NPORT-P"))
HDR["A-Q3-500"] = ["S000002839"]
q3 = [r for r in q2 if r[0] != "CO10"]
doc("A-Q3-500", nport("S000002839", "2026-09-30", q3))
MF.run_mf_holdings()
db.fail_tables = set()
st = next(r for r in db.rows("mf_holdings_state") if r["series_id"] == "S000002839")
check("alert ledger down: state not advanced", st["rep_date"] == "2026-06-30")
MF.run_mf_holdings()
check("then alerts once", len([a for a in db.rows("alerts") if a["extra"]["rep_date"] == "2026-09-30"]) == 2)

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 6. PROSPECTUS FEE CHANGES ===")


def fees(net, gross=None):
    return {"VFIAX": {"management_fee": net, "gross_expense_ratio": gross or net, "net_expense_ratio": net,
                      "series_id": "S000002839", "class_id": "C000007773"}}


fdb = FakeDB()
fdb.tables["mf_master"] = [{"ticker": "VFIAX", "series_name": "Vanguard 500 Index Fund", "class_name": "Admiral Shares"}]
fdb.tables["etf_master"] = []
fdb.tables["alerts"] = []
m1 = {"accession": "P1", "form": "485BPOS", "filing_date": "2025-04-28", "url": "u1"}
check("first prospectus: baseline, no alert", X.mf_fee_alerts(fdb, fees(0.04), m1, {"VFIAX"}) == 0)
X.store(fdb, fees(0.04), m1)
check("MF row stored with fund_type", fdb.rows("etf_expense_breakdown")[0].get("fund_type") == "MF")
m2 = {"accession": "P2", "form": "497K", "filing_date": "2026-04-28", "url": "u2"}
check("same fees next year: no alert", X.mf_fee_alerts(fdb, fees(0.04), m2, {"VFIAX"}) == 0)
X.store(fdb, fees(0.04), m2)
m3 = {"accession": "P3", "form": "497K", "filing_date": "2026-09-01", "url": "u3"}
n = X.mf_fee_alerts(fdb, fees(0.20), m3, {"VFIAX"})
a = fdb.rows("alerts")
check("fee rise alerts once, HIGH, Feature 18", n == 1 and a[0]["filing_type"] == "MF_EXPENSE_INCREASE"
      and a[0]["impact"] == "HIGH" and a[0]["extra"]["feature_id"] == 18, a)
check("text", "from 0.04% to 0.20%" in a[0]["summary"] and "Admiral Shares" in a[0]["summary"], a[0]["summary"])
check("dedup on the same prospectus", X.mf_fee_alerts(fdb, fees(0.20), m3, {"VFIAX"}) == 0)
check("unwatched class: nothing", X.mf_fee_alerts(fdb, fees(0.50), {"accession": "P4", "form": "497K",
                                                                    "filing_date": "2026-10-01"}, set()) == 0)
edb = FakeDB()
edb.tables["etf_master"] = [{"ticker": "AAA"}]
X.store(edb, {"AAA": {"management_fee": 0.1, "series_id": "S1"}}, {"accession": "E1"})
check("ETF-only store unchanged when no MF rows (no fund_type key)", "fund_type" not in edb.rows("etf_expense_breakdown")[0])

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 7. FEATURES 15 / 16 COVER MUTUAL FUNDS ===")
s15, _, _ = FM.build_alert("Vanguard 500 Index Fund", "MF", [{"manager": "Jane Doe"}], [], ["Jane Doe"], None,
                           {"form": "497", "filing_date": "2026-10-01"})
check("Feature 15 names it a mutual fund", "(mutual fund)" in s15, s15)
import etf_fund_events_poller as EV  # noqa: E402
check("Feature 16 picks up watched mutual funds", "VFIAX" in EV.watched_funds())
check("Feature 16 renamed for both", feature_map.FEATURES[16]["name"] == "Fund Actions (ETF & Mutual Fund)")
for src, ft in [("MF_PORTFOLIO", "MF_WEIGHT_SHIFT"), ("MF_EXPENSE", "MF_EXPENSE_DECREASE")]:
    check(f"{src}/{ft} -> Feature 18, watchlist-only", feature_map.resolve_feature(src, ft)[0] == 18
          and src in delivery.COMPANY_ONLY_SOURCES and not feature_map.is_market_wide(src, ft))
users = {u: {"user_id": u, "min_impact": "MEDIUM", "muted_features": mm, "receive_market_wide": True,
             "etf_signals_beta": False} for u, mm in (("u1", set()), ("u2", {18}), ("u3", set()))}
aud = delivery.resolve_audience({"source": "MF_PORTFOLIO", "filing_type": "MF_HOLDINGS_CHANGE", "ticker": "VFIAX",
                                 "impact": "MEDIUM", "extra": {}}, users, {"VFIAX": {"u1", "u2"}})
check("reaches watcher, mute honoured", sorted(u["user_id"] for u, _ in aud) == ["u1"])
msg = str(alert_formatter.build_message(dict(db.rows("alerts")[0])))
check("renders", "Feature 18" in msg and "Fund Portfolio Changes" in msg, msg[:300])

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 8. FUND TYPES: WHICH FUNDS CAN ALERT ===")
check("ordinary fund", MF.classify_mf("VFIAX", "VANGUARD INDEX FUNDS", "Vanguard 500 Index Fund") == "MUTUAL_FUND")
check("insurance portfolio ticker shape", MF.classify_mf("VPVLDVV", "PIMCO VARIABLE INSURANCE TRUST", "PIMCO Low Duration") == "INSURANCE")
check("variable insurance trust", MF.classify_mf("IAVGX", "VOYA VARIABLE INSURANCE TRUST", "Growth and Income") == "INSURANCE")
check("money market", MF.classify_mf("ACTXX", "JANUS INVESTMENT FUND", "Janus Henderson Government Money Market Fund") == "MONEY_MARKET")
check("sector fund named Insurance stays a mutual fund",
      MF.classify_mf("FSPCX", "FIDELITY SELECT PORTFOLIOS", "Insurance Portfolio") == "MUTUAL_FUND")
check("master rows carry fund_type", all(r["fund_type"] in ("MUTUAL_FUND", "MONEY_MARKET", "INSURANCE")
                                          for r in MF.master_rows(U, {})))

db.tables["mf_master"].extend([
    {"ticker": "MMKTX", "cik": "9", "series_id": "SMM", "fund_type": "MONEY_MARKET", "is_active": True},
    {"ticker": "INSVX", "cik": "9", "series_id": "SIN", "fund_type": "INSURANCE", "is_active": True},
    {"ticker": "NEWWX", "cik": "9", "series_id": "SNEW", "is_active": True}])
for t_, sid_ in (("MMKTX", "SMM"), ("INSVX", "SIN"), ("NEWWX", "SNEW")):
    add_fund(t_, "0000000009", sid_, "C" + sid_)
WATCH |= {"MMKTX", "INSVX", "NEWWX"}
w = MF.watched_mfs(U)
check("money market and insurance funds are not polled", "MMKTX" not in w and "INSVX" not in w, sorted(w))
check("ordinary and not-yet-listed funds are polled", {"VFIAX", "VTSAX", "NEWWX"} <= set(w), sorted(w))

# retirement safety: a short registry read must not wipe the list
db.tables["mf_master"] = [r for r in db.rows("mf_master") if r["ticker"] not in ("OLDMF2",)]
db.tables["mf_master"].append({"ticker": "OLDMF2", "cik": "1", "series_id": "S9", "fund_type": "MUTUAL_FUND", "is_active": True})
db.tables["mf_master"].append({"ticker": "OLDINX", "cik": "1", "series_id": "S8", "fund_type": "INSURANCE", "is_active": True})
FM.load_universe = lambda force=False: U
MF.refresh_mf_master()
m2 = {r["ticker"]: r for r in db.rows("mf_master")}
check("closed ordinary fund retired", m2["OLDMF2"]["is_active"] is False)
check("insurance fund absent from registry is not retired", m2["OLDINX"]["is_active"] is True)
part = {"by_ticker": dict(list(U["by_ticker"].items())[:5100]), "by_series": {}, "ciks": set()}
for r in db.rows("mf_master"):
    r["is_active"] = True
FM.load_universe = lambda force=False: part
MF.refresh_mf_master()
live = sum(1 for r in db.rows("mf_master") if r["is_active"] and r.get("fund_type") != "INSURANCE")
check("registry missing >15% of funds: retires none", live == sum(1 for r in db.rows("mf_master") if r.get("fund_type") != "INSURANCE"), live)
FM.load_universe = lambda force=False: U
WATCH -= {"MMKTX", "INSVX", "NEWWX"}

# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 9. SCRAPE EVERY ORDINARY FUND, ALERT ONLY WATCHED ===")
CIK2 = "0000555555"
SUBS2 = FM.URL_SUBMISSIONS.format(cik10=CIK2)
MF.RECHECK_WATCHED_DAYS = MF.RECHECK_DAYS = 0
db.tables["alerts"] = []
db.tables["mf_master"] = [r for r in db.rows("mf_master") if r["ticker"] in ("VFIAX", "VFINX", "VTSAX")]
for t_, sid_, ft_ in (("UAAAX", "SU1", "MUTUAL_FUND"), ("UAABX", "SU2", "MUTUAL_FUND"), ("UAACX", "SU3", "INSURANCE")):
    db.tables["mf_master"].append({"ticker": t_, "cik": "555555", "series_id": sid_, "fund_type": ft_,
                                   "is_active": True, "series_name": "Fund " + sid_})
rows9 = []


def f2(*rows):
    SEC.docs[SUBS2] = {"filings": {"recent": {
        "form": ["NPORT-P"] * len(rows), "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[0] for r in rows], "primaryDocument": ["primary_doc.xml"] * len(rows)}}}


def d2(adsh, xml):
    SEC.docs[FM._archive(CIK2, adsh, "primary_doc.xml")] = xml


HDR.update({"B-Q1-SU1": ["SU1"], "B-Q1-SU2": ["SU2"], "B-Q1-SU3": ["SU3"]})
f2(("B-Q1-SU1", "2026-05-28"), ("B-Q1-SU2", "2026-05-28"), ("B-Q1-SU3", "2026-05-28"))
for sid_ in ("SU1", "SU2", "SU3"):
    d2(f"B-Q1-{sid_}", nport(sid_, "2026-03-31", BASE))
alerts_before = len(db.rows("alerts"))
MF.run_mf_holdings()
have = {r["series_id"] for r in db.rows("mf_holdings_state") if r.get("accession")}
check("unwatched funds are scraped and baselined", {"SU1", "SU2"} <= have, sorted(have))
check("their holdings are stored", len([r for r in db.rows("mf_holdings") if r["series_id"] == "SU1"]) == len(BASE))
check("insurance-only fund never downloaded", not any("BQ1SU3" in u for u in FETCHED))
check("no alerts for unwatched funds", len(db.rows("alerts")) == alerts_before)

q2b = [r for r in BASE if r[0] != "CO10"] + [("NEWCO", "123456789", "US1234567890", "NW", 500.0, 3.0, "EC")]
HDR["B-Q2-SU1"] = ["SU1"]
f2(("B-Q2-SU1", "2026-08-28"), ("B-Q1-SU1", "2026-05-28"), ("B-Q1-SU2", "2026-05-28"), ("B-Q1-SU3", "2026-05-28"))
d2("B-Q2-SU1", nport("SU1", "2026-06-30", q2b))
MF.run_mf_holdings()
check("unwatched change: recorded for display, no alert",
      any(c["series_id"] == "SU1" for c in db.rows("mf_holdings_changes")) and len(db.rows("alerts")) == alerts_before)
add_fund("UAAAX", CIK2, "SU1", "CSU1")          # the registry lists it; now somebody watches it
WATCH.add("UAAAX")
HDR["B-Q3-SU1"] = ["SU1"]
f2(("B-Q3-SU1", "2026-11-25"), ("B-Q2-SU1", "2026-08-28"), ("B-Q1-SU2", "2026-05-28"), ("B-Q1-SU3", "2026-05-28"))
d2("B-Q3-SU1", nport("SU1", "2026-09-30", [r for r in q2b if r[0] != "CO9"]))
MF.run_mf_holdings()
got = [a for a in db.rows("alerts") if a["ticker"] == "UAAAX"]
check("once watched, the next filing alerts", len(got) == 1 and got[0]["extra"]["feature_id"] == 18, got)
WATCH.discard("UAAAX")

# budgets: one document per run, the rest wait for the next run
for t_, sid_ in (("UAADX", "SU4"), ("UAAEX", "SU5")):
    db.tables["mf_master"].append({"ticker": t_, "cik": "555555", "series_id": sid_, "fund_type": "MUTUAL_FUND",
                                   "is_active": True, "series_name": "Fund " + sid_})
    HDR[f"B-Q1-{sid_}"] = [sid_]
    d2(f"B-Q1-{sid_}", nport(sid_, "2026-03-31", BASE))
f2(("B-Q3-SU1", "2026-11-25"), ("B-Q1-SU2", "2026-05-28"), ("B-Q1-SU3", "2026-05-28"),
   ("B-Q1-SU4", "2026-05-28"), ("B-Q1-SU5", "2026-05-28"))
MF.DOC_BUDGET = 1


def stored(sid_):
    return any(r["series_id"] == sid_ and r.get("accession") for r in db.rows("mf_holdings_state"))


MF.run_mf_holdings()
check("document budget: one new fund this run", sum(stored(x) for x in ("SU4", "SU5")) == 1)
MF.run_mf_holdings()
check("the rest on the next run", stored("SU4") and stored("SU5"))
MF.DOC_BUDGET = 1500

# an unreadable filing is remembered and not hammered
db.tables["mf_master"].append({"ticker": "UAAFX", "cik": "555555", "series_id": "SU6", "fund_type": "MUTUAL_FUND",
                               "is_active": True, "series_name": "Fund SU6"})
HDR["B-Q1-SU6"] = ["SU6"]
d2("B-Q1-SU6", "this is not xml")
f2(("B-Q3-SU1", "2026-11-25"), ("B-Q1-SU2", "2026-05-28"), ("B-Q1-SU3", "2026-05-28"),
   ("B-Q1-SU4", "2026-05-28"), ("B-Q1-SU5", "2026-05-28"), ("B-Q1-SU6", "2026-05-28"))
MF.run_mf_holdings()
st6 = next(r for r in db.rows("mf_holdings_state") if r["series_id"] == "SU6")
check("unreadable filing: error remembered", st6.get("last_error") and not st6.get("accession"), st6)
n_f = len(FETCHED)
MF.run_mf_holdings()
check("and not downloaded again", not any("BQ1SU6" in u for u in FETCHED[n_f:]))

# recheck windows: after a full pass nothing is re-listed until they elapse
MF.RECHECK_WATCHED_DAYS, MF.RECHECK_DAYS = 1, 3
n_f = len(FETCHED)
MF.run_mf_holdings()
check("inside the recheck window: no requests to SEC",
      not [u for u in FETCHED[n_f:] if SUBS2 in u or "primary_doc" in u], FETCHED[n_f:])
MF.RECHECK_WATCHED_DAYS = MF.RECHECK_DAYS = 0

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
