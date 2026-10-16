"""
The mutual fund pollers, offline: managers (19), expense ratios (20), fund
actions (21), scheme & family register (22), company data, and the wiring that
keeps them separate from the ETF stack.

Run: python tests/test_mf_pollers.py
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "MASSIVE_API_KEY": "x",
             "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

from fake_supabase import FakeDB  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


import mf_common as mc              # noqa: E402
import mf_manager_poller as MGR     # noqa: E402
import mf_expense_poller as FEE     # noqa: E402
import mf_events_poller as EV       # noqa: E402
import mf_scheme_poller as SCH      # noqa: E402
import mf_company_data as CD        # noqa: E402
import sec_fee_tables as SFT        # noqa: E402
import feature_map                  # noqa: E402
import delivery                     # noqa: E402
import alert_formatter              # noqa: E402

alert_formatter._cached_quote = lambda t: None

db = FakeDB()
mc.sb = lambda: db
for m in (MGR, FEE, EV, SCH, CD):
    m.log = lambda *_a, **_k: None
mc.log = lambda *_a, **_k: None
mc.log_poller_error = lambda *_a, **_k: None

TODAY = date.today().isoformat()

# ── a small mutual fund register ─────────────────────────────────────────────
# One fund family (CIK 36405) running two funds. The first fund has two share
# classes, which is the thing ETFs do not have and these pollers must handle.
MASTER = [
    {"ticker": "VFIAX", "cik": "36405", "series_id": "S000002839", "class_id": "C000007773",
     "entity_name": "Vanguard Index Funds", "series_name": "Vanguard 500 Index Fund",
     "class_name": "Admiral Shares", "fund_type": "MUTUAL_FUND", "is_active": True},
    {"ticker": "VFINX", "cik": "36405", "series_id": "S000002839", "class_id": "C000007772",
     "entity_name": "Vanguard Index Funds", "series_name": "Vanguard 500 Index Fund",
     "class_name": "Investor Shares", "fund_type": "MUTUAL_FUND", "is_active": True},
    {"ticker": "VTSAX", "cik": "36405", "series_id": "S000002848", "class_id": "C000007806",
     "entity_name": "Vanguard Index Funds", "series_name": "Vanguard Total Stock Market Index Fund",
     "class_name": "Admiral Shares", "fund_type": "MUTUAL_FUND", "is_active": True},
    {"ticker": "VMFXX", "cik": "36405", "series_id": "S000002850", "class_id": "C000007810",
     "entity_name": "Vanguard Money Market Reserves", "series_name": "Vanguard Federal Money Market Fund",
     "class_name": "Investor Shares", "fund_type": "MONEY_MARKET", "is_active": True},
]
for r in MASTER:
    db.table("mf_master").insert(dict(r)).execute()
db.table("watchlists").insert({"ticker": "VFIAX", "user_id": 1}).execute()
db.table("watchlists").insert({"ticker": "VTSAX", "user_id": 1}).execute()

mc.watched_tickers = lambda: {"VFIAX", "VTSAX"}
U = mc.load_mf_universe(force=True)

print("\n1. Universe: mutual funds only, share classes grouped by fund")
check("4 share classes known, money market excluded from the alertable set",
      len(U["by_ticker"]) == 3 and "VMFXX" not in U["by_ticker"], sorted(U["by_ticker"]))
check("two classes of one fund resolve to one series",
      sorted(U["by_series"]["S000002839"]["tickers"]) == ["VFIAX", "VFINX"])
check("watched funds are the two on the watchlist", sorted(mc.watched_mf_funds(U)) == ["VFIAX", "VTSAX"])
check("money market funds ARE visible when asked for", "VMFXX" in
      mc.load_mf_universe(force=True, types=None)["by_ticker"])
mc.load_mf_universe(force=True)      # restore the alertable view for the rest

print("\n2. Feature registry: mutual funds have their own ids, separate from ETFs")
for src, ftype, want_fid in (("MF_MANAGER", "MF_PM_REMOVED", 19),
                             ("MF_FEE", "MF_EXPENSE_INCREASE", 20),
                             ("MF_EVENT", "MF_LIQUIDATION", 21),
                             ("MF_EVENT", "MF_CLOSED", 21),
                             ("MF_SCHEME", "MF_NEW_SHARE_CLASS", 22),
                             ("MF_PORTFOLIO", "MF_HOLDINGS_ADDED", 18)):
    fid, name = feature_map.resolve_feature(src, ftype)
    check(f"{src}/{ftype} -> feature {want_fid}", fid == want_fid, f"got {fid} {name}")
check("ETF manager changes stay on feature 15",
      feature_map.resolve_feature("FUND_MANAGER", "PM_REMOVED")[0] == 15)
check("ETF fund actions stay on feature 16",
      feature_map.resolve_feature("SEC_FUND", "ETF_LIQUIDATION")[0] == 16)
check("no mutual fund feature is market-wide",
      not any(feature_map.is_market_wide(s, t) for s, t in
              (("MF_MANAGER", "MF_PM_ADDED"), ("MF_FEE", "MF_EXPENSE_INCREASE"),
               ("MF_EVENT", "MF_MERGER"), ("MF_SCHEME", "MF_TICKER_RETIRED"))))

print("\n3. Delivery: every mutual fund alert is watchlist-routed")
for src, ftype in (("MF_MANAGER", "MF_PM_REMOVED"), ("MF_FEE", "MF_EXPENSE_INCREASE"),
                   ("MF_EVENT", "MF_LIQUIDATION"), ("MF_SCHEME", "MF_NEW_SHARE_CLASS")):
    check(f"{src} is ticker-scoped, never broadcast", src in delivery.COMPANY_ONLY_SOURCES)
    check(f"{src} is not market-wide", not delivery._is_market_wide(
        {"source": src, "filing_type": ftype, "ticker": "VFIAX"})
        if hasattr(delivery, "_is_market_wide") else True)
for ftype in ("MF_PM_REMOVED", "MF_LIQUIDATION", "MF_CLOSED", "MF_ADVISER_CHANGE",
              "MF_NEW_SHARE_CLASS", "MF_TICKER_RETIRED", "MF_SHARE_CLASS"):
    check(f"{ftype} has a human label", ftype in alert_formatter.FILING_TYPE_LABELS)

print("\n4. Managers (19): a departure alerts, and only for watched funds")
META = {"cik": "36405", "accession": "0000036405-26-000123", "form": "497",
        "filing_date": TODAY, "url": "https://sec.gov/x.htm", "key": "0000036405-26-000123:x.htm",
        "series_names": {"S000002839": "Vanguard 500 Index Fund",
                         "S000002848": "Vanguard Total Stock Market Index Fund"}}
MGR.save_roster("S000002839", [{"name": "Alice Chen"}, {"name": "Bob Diaz"}], META)
roster, _ = MGR.current_roster("S000002839")
check("roster stored for the fund", sorted(r["manager_name"] for r in roster) == ["Alice Chen", "Bob Diaz"])

ev = {"event_type": "REMOVED", "manager": "Bob Diaz", "manager_key": "bob diaz",
      "effective_date": TODAY, "effective_date_is_filing_date": True, "evidence": "left"}
MGR.store_event("S000002839", ev, META, "STICKER_497")
MGR.apply_event("S000002839", ev, META)
n = MGR.emit("S000002839", [ev], META, {"VFIAX", "VTSAX"})
alerts = db.table("alerts").select("*").execute().data
check("one alert per watched share class of that fund", n == 1 and len(alerts) == 1, n)
a = alerts[0]
check("it went to the watched class, not the unwatched one", a["ticker"] == "VFIAX")
check("source is the mutual fund one", a["source"] == "MF_MANAGER")
check("the wording says mutual fund, not ETF",
      "mutual fund" in a["summary"] and "ETF" not in a["summary"], a["summary"][:120])
check("a departure leaving one manager is MEDIUM, not silent", a["impact"] in ("MEDIUM", "HIGH"))
check("the remaining manager is named", "Alice Chen" in a["summary"])
check("feature 19 is tagged on the row", a["extra"].get("feature_id") == 19)

before = len(db.table("alerts").select("*").execute().data)
MGR.emit("S000002848", [ev], META, set())           # nobody watches -> collected, not alerted
check("an unwatched fund is never alerted",
      len(db.table("alerts").select("*").execute().data) == before)
dup = MGR.emit("S000002839", [ev], META, {"VFIAX"})  # same event, second filing
check("the same change filed twice alerts once", dup == 0)
check("the change is on file for the fund",
      len(db.table("mf_manager_events").select("*").eq("series_id", "S000002839").execute().data) == 1)

print("\n5. Expense ratios (20): a rise alerts with both numbers")
FEE_META = {"cik": "36405", "accession": "0000036405-26-000200", "form": "485BPOS",
            "filing_date": TODAY, "url": "https://sec.gov/fee.htm",
            "key": "0000036405-26-000200:fee.htm"}
PREV = {"ticker": "VFIAX", "series_id": "S000002839", "class_id": "C000007773",
        "management_fee": 0.03, "distribution_12b1": 0.0, "other_expenses": 0.01,
        "gross_expense_ratio": 0.04, "fee_waiver": 0.0, "net_expense_ratio": 0.04,
        "accession": "0000036405-25-000100", "form": "485BPOS",
        "filing_date": (date.today() - timedelta(days=365)).isoformat()}
db.table("mf_expense_breakdown").insert(dict(PREV)).execute()
NEW = {"VFIAX": {"series_id": "S000002839", "class_id": "C000007773", "management_fee": 0.05,
                 "distribution_12b1": 0.0, "other_expenses": 0.01, "gross_expense_ratio": 0.06,
                 "fee_waiver": 0.0, "net_expense_ratio": 0.06}}
sent = FEE.fee_alerts(NEW, FEE_META, {"VFIAX", "VTSAX"})
fa = [r for r in db.table("alerts").select("*").execute().data if r["source"] == "MF_FEE"]
check("a real fee change alerts once", sent == 1 and len(fa) == 1, sent)
check("both the old and the new number are quoted",
      "0.04%" in fa[0]["summary"] and "0.06%" in fa[0]["summary"], fa[0]["summary"][:160])
check("the cost on $10,000 is spelled out", "$10,000" in fa[0]["summary"])
check("it is an increase", fa[0]["filing_type"] == "MF_EXPENSE_INCREASE")
check("the share class is named", "Admiral" in fa[0]["summary"])
check("feature 20 is tagged", fa[0]["extra"].get("feature_id") == 20)

# The stored previous table is 0.04; a move to 0.0401 is a rounding artefact,
# not a fee change, and must stay silent.
n_before = len(db.table("alerts").select("*").execute().data)
FEE.fee_alerts({"VFIAX": dict(NEW["VFIAX"], net_expense_ratio=0.0401, gross_expense_ratio=0.0401)},
               dict(FEE_META, accession="0000036405-26-000201"), {"VFIAX"})
check("a rounding-sized move does not alert",
      len(db.table("alerts").select("*").execute().data) == n_before)

n_before = len(db.table("alerts").select("*").execute().data)
FEE.fee_alerts({"VTSAX": {"series_id": "S000002848", "net_expense_ratio": 0.04,
                          "gross_expense_ratio": 0.04}},
               dict(FEE_META, accession="0000036405-26-000202"), {"VTSAX"})
check("a first fee table is a baseline, not a change",
      len(db.table("alerts").select("*").execute().data) == n_before)

# A waiver expiring reads as a rise in the NET ratio, which is the number the
# holder pays — the poller must not compare a net against a gross and miss it.
n_before = len(db.table("alerts").select("*").execute().data)
FEE.fee_alerts({"VFIAX": {"series_id": "S000002839", "class_id": "C000007773",
                          "gross_expense_ratio": 0.10, "fee_waiver": 0.0,
                          "net_expense_ratio": 0.10}},
               dict(FEE_META, accession="0000036405-26-000203"), {"VFIAX"})
wa = [r for r in db.table("alerts").select("*").execute().data
      if r["extra"].get("accession") == "0000036405-26-000203"]
check("a real rise after a waiver ends does alert",
      len(db.table("alerts").select("*").execute().data) == n_before + 1 and len(wa) == 1)
check("and it compares net against net", wa and wa[0]["extra"].get("expense_field") == "net_expense_ratio",
      wa[0]["extra"].get("expense_field") if wa else None)

print("\n6. Expense ratios: the fee table is read out of inline XBRL")
XBRL = """
<xbrl><context id="c1"><entity><segment>
  <explicitMember dimension="rr:ProspectusShareClassAxis">cik:C000007773Member</explicitMember>
</segment></entity></context>
<ix:nonFraction name="rr:ManagementFeesOverAssets" contextRef="c1" scale="-2">0.05</ix:nonFraction>
<ix:nonFraction name="rr:DistributionAndService12b1FeesOverAssets" contextRef="c1" scale="-2">0.25</ix:nonFraction>
<ix:nonFraction name="rr:ExpensesOverAssets" contextRef="c1" scale="-2">0.30</ix:nonFraction>
<ix:nonFraction name="rr:FeeWaiverOrReimbursementOverAssets" contextRef="c1" scale="-2">0.05</ix:nonFraction>
</xbrl>"""
tabs = SFT.extract_fee_tables(XBRL, by_class={"C000007773": "VFIAX"},
                              by_series={"S000002839": ["VFIAX", "VFINX"]},
                              filing_series=["S000002839"])
check("the share class is identified from its class id", "VFIAX" in tabs, list(tabs))
b = tabs.get("VFIAX", {})
check("management fee parsed", abs((b.get("management_fee") or 0) - 0.05) < 1e-6, b)
check("the 12b-1 fee is kept — it is what separates two classes of one fund",
      abs((b.get("distribution_12b1") or 0) - 0.25) < 1e-6, b)
check("the waiver is stored negative", (b.get("fee_waiver") or 0) < 0, b)
check("net = total + waiver", abs((b.get("net_expense_ratio") or 0) - 0.25) < 1e-6, b)
check("the breakdown reads as a sentence", "12b-1" in (SFT.breakdown_text(b) or ""))

print("\n7. Fund actions (21): mutual-fund-only events are recognised")
for et in ("MF_CLOSED", "MF_REOPENED", "MF_SHARE_CLASS", "MF_ADVISER_CHANGE"):
    check(f"{et} is a rule with an impact floor", et in EV.FLOOR and et in EV.LABEL)
check("liquidation outranks a fee change in the same document",
      EV.pick_event({"MF_FEE_CHANGE": ["x"], "MF_LIQUIDATION": ["y"]}) == "MF_LIQUIDATION")
check("a closing outranks a strategy change",
      EV.pick_event({"MF_STRATEGY_CHANGE": ["x"], "MF_CLOSED": ["y"]}) == "MF_CLOSED")
check("liquidation and merger are floored at HIGH",
      EV.FLOOR["MF_LIQUIDATION"] == "HIGH" and EV.FLOOR["MF_MERGER"] == "HIGH")
check("a fund closing to new investors is HIGH — the holder has days to act",
      EV.FLOOR["MF_CLOSED"] == "HIGH")
txt = "The Fund will be closed to new investors effective May 1."
check("the phrase is confirmed against the document we downloaded",
      "MF_CLOSED" in EV.verify_events(txt, {"MF_CLOSED": ['"closed to new investors"']}))
check("a phrase EFTS matched but the document does not contain is dropped",
      EV.verify_events(txt, {"MF_MERGER": ['"plan of reorganization"']}) == {})

print("\n8. Fund actions: attribution inside a multi-fund supplement")
two = ("Supplement to the Prospectus for the Alpha Fund and the Beta Fund\n"
       "The Board approved a plan of liquidation for the Alpha Fund.\n"
       "The Beta Fund is not affected.\n")
series = [{"series_id": "S1", "series_name": "Alpha Fund", "classes": []},
          {"series_id": "S2", "series_name": "Beta Fund", "classes": []}]
cands = {"AAAAX": {"series_id": "S1"}, "BBBBX": {"series_id": "S2"}}
about = EV.attribute_tickers(two, series, ["plan of liquidation"], cands)
check("only the fund named in the sentence is attributed", about == {"AAAAX"}, about)
both = ("Supplement to the Prospectus\n"
        "The Board approved a plan of liquidation for each Fund.\n")
check("'each Fund' does attribute to every fund in the filing",
      EV.attribute_tickers(both, series, ["plan of liquidation"], cands) == {"AAAAX", "BBBBX"})

print("\n9. Fund actions: queued for the AI pipeline, not alerted directly")
import ai_pipeline  # noqa: E402
check("MF_EVENT jumps the news backlog like the other SEC filings",
      "MF_EVENT" in ai_pipeline.PRIORITY_SOURCES)
check("ETF fund actions still have their own source there",
      "SEC_FUND" in ai_pipeline.PRIORITY_SOURCES)

print("\n10. Scheme register (22): new class, retirement, renames")
OLD = {r["ticker"]: dict(r, is_active=True) for r in MASTER}
NEW_REG = {t: dict(r) for t, r in OLD.items()}
NEW_REG["VFFSX"] = {"ticker": "VFFSX", "cik": "36405", "series_id": "S000002839",
                    "class_id": "C000111111", "entity_name": "Vanguard Index Funds",
                    "series_name": "Vanguard 500 Index Fund",
                    "class_name": "Institutional Select Shares", "fund_type": "MUTUAL_FUND"}
del NEW_REG["VTSAX"]
NEW_REG["VFINX"] = dict(NEW_REG["VFINX"], series_name="Vanguard 500 Index Fund II")
d = SCH.diff_register(OLD, NEW_REG)
check("the new share class is seen", [r["ticker"] for r in d["added"]] == ["VFFSX"])
check("the missing ticker is seen as retired", [r["ticker"] for r in d["retired"]] == ["VTSAX"])
check("the rename is seen", len(d["renamed"]) == 1 and d["renamed"][0][1]["ticker"] == "VFINX")

n0 = len(db.table("alerts").select("*").execute().data)
SCH._new_class_alert(NEW_REG["VFFSX"], ["VFIAX", "VFINX"], {"VFIAX"})
sa = [r for r in db.table("alerts").select("*").execute().data if r["source"] == "MF_SCHEME"]
check("a new share class alerts the holder of another class of the SAME fund",
      len(sa) == 1 and sa[0]["ticker"] == "VFIAX", [r["ticker"] for r in sa])
check("the new ticker is named", "VFFSX" in sa[0]["summary"])
check("it explains that classes differ by fee, not by portfolio",
      "same portfolio" in sa[0]["summary"] and "12b-1" in sa[0]["summary"])
check("feature 22 is tagged", sa[0]["extra"].get("feature_id") == 22)
SCH._retired_alert(OLD["VTSAX"], {"VTSAX"})
ra = [r for r in db.table("alerts").select("*").execute().data
      if r["filing_type"] == "MF_TICKER_RETIRED"]
check("a watched ticker leaving the register is HIGH",
      len(ra) == 1 and ra[0]["impact"] == "HIGH")
check("it says the money is not lost", "not lost" in ra[0]["summary"])
n1 = len(db.table("alerts").select("*").execute().data)
SCH._new_class_alert(NEW_REG["VFFSX"], ["VFIAX", "VFINX"], {"VFIAX"})
check("the same register change alerts once",
      len(db.table("alerts").select("*").execute().data) == n1)

print("\n11. Scheme register: guards")
db2 = FakeDB()
mc.sb = lambda: db2
for r in MASTER:
    db2.table("mf_master").insert(dict(r)).execute()
mc.load_mf_universe(force=True)
check("a register of 4 tickers is refused as partial — no diff is run",
      SCH.run_mf_scheme_poll() == 0)
big = {f"AAA{i:02d}X": {"ticker": f"AAA{i:02d}X", "series_id": f"S{i}", "cik": "1",
                        "series_name": f"Fund {i}", "entity_name": "Family",
                        "fund_type": "MUTUAL_FUND", "is_active": True} for i in range(100)}
d2 = SCH.diff_register(big, {k: v for k, v in list(big.items())[:50]})
check("half the book vanishing is caught by the diff", len(d2["retired"]) == 50)
check("the retirement guard trips above 10%", 50 / 100 > SCH.MAX_RETIRE_FRACTION)
mc.sb = lambda: db

print("\n12. Company data: the master behind the holdings")
check("market cap bands match the US convention",
      (CD.cap_class(300e9), CD.cap_class(50e9), CD.cap_class(5e9),
       CD.cap_class(900e6), CD.cap_class(100e6)) ==
      ("MEGA_CAP", "LARGE_CAP", "MID_CAP", "SMALL_CAP", "MICRO_CAP"))
check("no market cap means no band", CD.cap_class(None) is None and CD.cap_class(0) is None)
row = CD._row_from_holding(
    {"security_key": "US67066G1040", "ticker": "NVDA", "security_name": "NVIDIA Corp",
     "cusip": "67066G104", "isin": "US67066G1040", "asset_category": "EC"},
    stock={"ticker": "NVDA", "sector": "Technology", "exchange": "NASDAQ"},
    profile={"companyName": "NVIDIA Corporation", "sector": "Technology",
             "industry": "Semiconductors", "marketCap": 3.2e12, "cik": "0001045810",
             "exchangeShortName": "NASDAQ", "country": "US", "currency": "USD"})
check("sector, industry and cap class are filled",
      (row["sector"], row["industry"], row["cap_class"]) == ("Technology", "Semiconductors", "MEGA_CAP"), row)
check("the SEC CIK is carried — the US company code", row["cik"] == "0001045810")
check("the ISIN from the fund's own filing is kept", row["isin"] == "US67066G1040")
db.table("mf_security_master").insert(dict(row)).execute()
check("a holding is described for the alert text",
      CD.describe(["US67066G1040"]).get("US67066G1040") == "mega-cap Technology")
check("an unknown holding is simply not described", CD.describe(["NOTHING"]) == {})

print("\n13. The ETF pollers no longer handle mutual funds")
import fund_manager_poller as FM     # noqa: E402
import etf_fund_events_poller as EFE  # noqa: E402
import etf_expense_extractor as XEX  # noqa: E402
FM.sb = lambda: db
FM.watched_tickers = lambda: {"VFIAX", "VTSAX"}
FM.load_universe = lambda force=False: {
    "by_ticker": {"VFIAX": {"cik": "36405", "series_id": "S000002839", "fund_type": "MF"},
                  "SPY": {"cik": "884394", "series_id": "S000099999", "fund_type": "ETF"}},
    "by_series": {"S000002839": {"cik": "36405", "tickers": ["VFIAX"], "fund_type": "MF"},
                  "S000099999": {"cik": "884394", "tickers": ["SPY"], "fund_type": "ETF"}}}
n_before = len(db.table("alerts").select("*").execute().data)
FM.emit_alerts("S000002839", [{"event_type": "REMOVED", "manager": "X", "manager_key": "x"}],
               dict(META, series_names={}), {"VFIAX"})
check("the ETF manager poller does not alert on a mutual fund",
      len(db.table("alerts").select("*").execute().data) == n_before)
EFE._etf_master_tickers = lambda ts: set()
check("the ETF fund-action poller does not pick up a mutual fund",
      "VFIAX" not in EFE.watched_funds())
check("the ETF expense module's mutual fund path is off by default",
      XEX.MF_FEE_ALERTS_ENABLED is False)
check("and it writes nothing while off", XEX.mf_fee_alerts(db, {"VFIAX": {}}, FEE_META, {"VFIAX"}) == 0)

print("\n14. main.py schedules each mutual fund poller separately")
src = open(os.path.join(ROOT, "main.py")).read()
for fn in ("run_mf_manager_events", "run_mf_manager_rosters", "run_mf_manager_backfill",
           "run_mf_expense_poll", "run_mf_expense_backfill", "run_mf_fund_events_poller",
           "run_mf_scheme_poll", "run_mf_company_data"):
    check(f"{fn} is imported and scheduled",
          f"from mf_" in src and f"job({fn})" in src, fn)
check("the scheme diff runs after the register refresh",
      src.index('at("05:20").do(job(refresh_mf_master))') < src.index('do(job(run_mf_scheme_poll))'))

print("\n15. Regressions found by the 2026-10-16 deep debug")

# (a) The delta already carries its own sign. A literal "+" in front of a
# "+.2f" rendered every increase to the holder as "(++0.07 points)".
_s, _i, _t = FEE.build_alert(
    "American Balanced Fund", "American Funds", "ABALX", "Class A",
    {"net_expense_ratio": 0.55, "filing_date": "2025-10-01"},
    {"net_expense_ratio": 0.62}, "net_expense_ratio",
    {"accession": "a", "form": "485BPOS", "filing_date": "2026-10-16", "url": "u"})
check("a fee increase renders one sign, not two", "++" not in _s,
      [w for w in _s.split() if "++" in w])
check("the increase still shows the delta", "+0.07" in _s, _s[:90])
check("and the cost on $10,000 is right", "$55.00" in _s and "$62.00" in _s)

# (b) The effective date must come from an event that actually HAS one.
# Testing the flag alone picked the first event whose flag was merely absent,
# whose effective_date is None, so "effective November 1" was announced to
# holders as effective on the filing date.
mc._uni_cache.clear()
mc._uni_at.clear()
_db = FakeDB()
mc.sb = lambda: _db
_db.tables["mf_master"] = [
    {"ticker": "FGRIX", "cik": "35315", "series_id": "S000007080", "class_id": "C1",
     "entity_name": "Fidelity", "series_name": "Growth & Income", "class_name": "",
     "fund_type": "MUTUAL_FUND", "is_active": True}]
_meta = {"accession": "0000035315-26-000123", "cik": "35315", "form": "497",
         "filing_date": "2026-10-16", "url": "https://sec.gov/x.htm",
         "series_names": {"S000007080": "Growth & Income"}, "detection": "STICKER_497"}
MGR.emit("S000007080",
         # The addition sorts first and carries NO effective date at all.
         [{"event_type": "ADDED", "manager": "Alice Ng", "manager_key": "alice ng"},
          {"event_type": "REMOVED", "manager": "John Doe", "manager_key": "john doe",
           "effective_date": "2026-11-01", "effective_date_is_filing_date": False}],
         _meta, {"FGRIX"})
_rows = _db.table("alerts").select("*").execute().data
check("a stated effective date survives an undated event sorting first",
      len(_rows) == 1 and _rows[0]["extra"].get("effective_date") == "2026-11-01",
      _rows[0]["extra"].get("effective_date") if _rows else "no alert")
check("and the holder reads that date, not the filing date",
      _rows and "Nov 01, 2026" in _rows[0]["summary"],
      _rows[0]["summary"][:140] if _rows else "")

# (c) A cosmetic edit between two vintages of SEC's register must not reach a
# holder as "your fund was renamed" to what reads as the same name.
for _a, _b in (("Gamma Advisors, Inc.", "Gamma Advisors Inc"),
               ("Alpha-Beta Fund", "Alpha Beta Fund"),
               ("A.B.C. Fund", "ABC Fund"),
               ("ABC Fund", "abc  fund")):
    check(f"cosmetic register edit is not a rename: {_a!r} -> {_b!r}",
          SCH._same_name(_a, _b))
for _a, _b in (("Alpha Growth Fund", "Alpha Value Fund"),
               ("Growth Fund", "Growth Fund II"),
               ("Income Fund A", "Income Fund B"),
               ("Gamma Advisors", "Gamma Capital LLC")):
    check(f"a real rename is still caught: {_a!r} -> {_b!r}",
          not SCH._same_name(_a, _b))
check("a punctuation-only fund rename produces no diff",
      SCH.diff_register(
          {"X": {"ticker": "X", "series_id": "S", "series_name": "A.B.C. Fund",
                 "entity_name": "Advisors, Inc."}},
          {"X": {"ticker": "X", "series_id": "S", "series_name": "ABC Fund",
                 "entity_name": "Advisors Inc"}}) == {
          "added": [], "retired": [], "renamed": [], "families": []})

# (d) The universe cache is keyed on the fund-type filter. mf_scheme_poller
# asks for every type; a single-slot cache then handed that whole-register
# result to the next poller that wanted ordinary funds only, making
# money-market and insurance-dedicated funds alertable.
mc._uni_cache.clear()
mc._uni_at.clear()
_db2 = FakeDB()
mc.sb = lambda: _db2
_db2.tables["mf_master"] = [
    {"ticker": "ORDIN", "cik": "1", "series_id": "S1", "class_id": "C1", "entity_name": "E",
     "series_name": "Ordinary", "class_name": "", "fund_type": "MUTUAL_FUND", "is_active": True},
    {"ticker": "MMFXX", "cik": "1", "series_id": "S2", "class_id": "C2", "entity_name": "E",
     "series_name": "Cash Reserves", "class_name": "", "fund_type": "MONEY_MARKET",
     "is_active": True}]
check("the default universe is ordinary funds only",
      set(mc.load_mf_universe(force=True)["by_ticker"]) == {"ORDIN"})
check("types=None deliberately sees the whole register",
      set(mc.load_mf_universe(force=True, types=None)["by_ticker"]) == {"ORDIN", "MMFXX"})
check("and that does NOT leak into the next default call",
      set(mc.load_mf_universe()["by_ticker"]) == {"ORDIN"},
      sorted(mc.load_mf_universe()["by_ticker"]))

# (e) fanout_group identifies one event. Falling back to filing_type made
# delivery drop the second of two unrelated funds in the same cycle as
# "same_event_other_ticker".
_db3 = FakeDB()
mc.sb = lambda: _db3
mc.emit_alerts("MF_FEE", ["AAA"], "s1", "HIGH", "MF_EXPENSE_INCREASE", {})
mc.emit_alerts("MF_FEE", ["BBB"], "s2", "HIGH", "MF_EXPENSE_INCREASE", {})
_g = [r["extra"].get("fanout_group") for r in _db3.table("alerts").select("*").execute().data]
check("no event key -> no fanout_group, so two funds are not collapsed",
      _g == [None, None], _g)
_db4 = FakeDB()
mc.sb = lambda: _db4
mc.emit_alerts("MF_FEE", ["AAA", "BBB"], "s", "HIGH", "MF_EXPENSE_INCREASE",
               {}, dedup_key="evt-1")
_g4 = {r["extra"].get("fanout_group")
       for r in _db4.table("alerts").select("*").execute().data}
check("one event across two share classes shares one fanout_group",
      _g4 == {"evt-1"}, _g4)

print()
if failures:
    print(f"FAILURES ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("ALL PASS")
