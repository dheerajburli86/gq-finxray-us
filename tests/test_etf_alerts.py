"""
Offline end-to-end cover for the ETF alert features.

  Feature 14  etf_data_poller        holdings added / removed / change, expense up / down
  Feature 16  etf_fund_events_poller liquidation, merger, strategy, fee  -> ai_pipeline
  Feature 15  is covered by tests/test_poller_e2e.py (fund_manager_poller)

Plus the integration points every ETF alert passes through: feature_map tagging,
delivery routing by watchlist, message rendering, ai_pipeline impact floor and
one-summary fan-out, and the bot accepting ETF tickers on /add.

No network: FMP, SEC and Supabase are all faked. Run: python tests/test_etf_alerts.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
for k, v in {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_KEY": "x",
             "TELEGRAM_TOKEN": "1:x", "FMP_API_KEY": "x", "DEEPINFRA_API_KEY": "x"}.items():
    os.environ.setdefault(k, v)

from fake_supabase import FakeDB  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# ══════════════════════════════════════════════════════════════════════════════
print("=== FEATURE 14 · etf_data_poller ===")
# ══════════════════════════════════════════════════════════════════════════════
import etf_fmp_client as F   # noqa: E402
import etf_data_poller as E  # noqa: E402
import alert_formatter       # noqa: E402
import delivery              # noqa: E402
import feature_map           # noqa: E402

db = FakeDB()
E.sb = lambda: db
E.ANY_DAY = True
WATCHED = {"MCHI", "AGG", "BIG"}
E.get_watched_tickers = lambda force=False: set(WATCHED)
alert_formatter._cached_quote = lambda t: None       # no live price in the render

from datetime import datetime as _dt, timedelta as _td  # noqa: E402
_DAY = [_dt(2026, 10, 1, 9, 0, tzinfo=E.ET)]


def run_e(mode, same_day=False):
    """Each poll is a new ET trading day unless same_day=True (expense
    confirmations count distinct days, so the clock must move in tests)."""
    if not same_day:
        _DAY[0] += _td(days=1)
    E.run_etf_data_poller(mode, now=_DAY[0])


db.tables["etf_master"] = [
    {"ticker": "MCHI", "name": "iShares MSCI China ETF"},
    {"ticker": "AGG", "name": "iShares Core US Aggregate Bond ETF"},
    {"ticker": "UNW", "name": "Unwatched Equity ETF"},
    {"ticker": "BIG", "name": "Big Portfolio ETF"},
]
FMP = {"info": {}, "holdings": {}}
CALLS = {"info": 0}


def _info(t):
    CALLS["info"] += 1
    return FMP["info"].get(t)


F.raw_info = _info
F.raw_holdings = lambda t: FMP["holdings"].get(t, [])
F.raw_price_change = lambda t: {"1M": 1.5, "1Y": 10.0, "ytd": 5.0, "3Y": 20.0}
F.raw_profile = lambda t: {"isin": "US4642871846", "beta": 1.1, "industry": "Asset Management"}
F.raw_sector_weights = lambda t: [{"sector": "Technology", "weightPercentage": 30},
                                  {"sector": "Consumer Cyclical", "weightPercentage": 25}]
F.raw_country_weights = lambda t: [{"country": "China", "weightPercentage": "95.50%"}]


def info(name, er, cls="Equity"):
    return {"name": name, "expenseRatio": er, "assetClass": cls, "etfCompany": "iShares",
            "assetsUnderManagement": 6123456789.0, "nav": 50.12, "navCurrency": "USD",
            "inceptionDate": "2011-03-29", "holdingsCount": 3, "isActivelyTrading": True,
            "website": f"https://www.example.com/{name.split()[1].lower()}"}


def hold(stamp, items):
    return [{"asset": s, "name": n, "weightPercentage": w, "updatedAt": stamp,
             "marketValue": w * 1e6, "sharesNumber": 1000} for s, n, w in items]


def alerts():
    return db.rows("alerts")


def changes(t=None):
    return [c for c in db.rows("etf_holdings_changes") if t is None or c["ticker"] == t]


def stored(t):
    return {r["security_key"]: r for r in db.rows("etf_holdings") if r["ticker"] == t}


def state(t):
    return next((s for s in db.rows("etf_snapshot_state") if s["ticker"] == t), {})


FMP["info"] = {"MCHI": info("iShares MSCI China ETF", 0.59),
               "AGG": info("iShares Core Aggregate", 0.03, "Fixed Income"),
               "UNW": info("Unwatched Equity ETF", 0.20),
               "BIG": info("Big Portfolio ETF", 0.10)}
FMP["holdings"] = {
    "MCHI": hold("2026-10-01 10:00:00", [("TCEHY", "TENCENT", 10.0), ("BABA", "ALIBABA", 5.0),
                                         ("NIO", "NIO INC", 1.0)]),
    "AGG": hold("2026-10-01 10:00:00", [("T1", "US TREASURY 2030", 3.0), ("T2", "US TREASURY 2031", 2.0)]),
    "UNW": hold("2026-10-01 10:00:00", [("P", "PCO", 4.0), ("Q", "QCO", 4.0)]),
    "BIG": hold("2026-10-01 10:00:00", [(f"S{i}", f"STOCK {i}", 5.0) for i in range(20)]),
}

# ── 1. Baseline: first sight of every fund stores silently ───────────────────
run_e("full")
check("baseline run writes no alerts", alerts() == [], alerts())
check("baseline run records no holdings changes", changes() == [], changes())
check("baseline stores MCHI's 3 holdings", set(stored("MCHI")) == {"TCEHY", "BABA", "NIO"})
check("baseline sets a holdings version", bool(state("MCHI").get("holdings_version")))
check("baseline sets the expense ratio", state("MCHI").get("expense_ratio") == 0.59, state("MCHI"))
m = next(r for r in db.rows("etf_master") if r["ticker"] == "MCHI")
check("etf_master.aum is an int (live column is BIGINT)", isinstance(m.get("aum"), int), m.get("aum"))
check("etf_master gets NAV and fund house", m.get("nav") == 50.12 and m.get("fund_house") == "iShares")
check("etf_master keeps the existing name", m.get("name") == "iShares MSCI China ETF")
comp = next((r for r in db.rows("etf_company_data") if r["ticker"] == "MCHI"), {})
check("company data: top country parsed from '95.50%'", comp.get("top_country") == "China"
      and comp.get("country_weights")[0]["weight"] == 95.5, comp)
check("company data: top sector by weight", comp.get("sector") == "Technology", comp)
perf = next((r for r in db.rows("etf_performance") if r["ticker"] == "MCHI"), {})
check("performance row stored", perf.get("return_1yr") == 10.0 and perf.get("return_1m") == 1.5, perf)
check("expense row stored", any(r["ticker"] == "MCHI" and r["net_expense_ratio"] == 0.59
                                for r in db.rows("etf_expenses")))

# ── 2. Same snapshot again: version gate, nothing happens ────────────────────
run_e("light")
check("unchanged snapshot -> no alert, no change rows", alerts() == [] and changes() == [])

# ── 3. Real portfolio change ──────────────────────────────────────────────────
FMP["holdings"]["MCHI"] = hold("2026-10-02 10:00:00", [
    ("TCEHY", "TENCENT", 10.0), ("BABA", "ALIBABA", 5.0),
    ("PDD", "PDD HOLDINGS", 2.0), ("XPEV", "XPENG", 0.1)])           # NIO out, PDD in, XPEV dust
FMP["holdings"]["AGG"] = hold("2026-10-02 10:00:00", [("T1", "US TREASURY 2030", 3.0),
                                                       ("T3", "US TREASURY 2033", 2.5)])
FMP["holdings"]["UNW"] = hold("2026-10-02 10:00:00", [("P", "PCO", 4.0), ("R", "RCO", 4.0)])
run_e("light")

a = alerts()
check("exactly one alert: MCHI (AGG is a bond fund, UNW is unwatched)",
      [x["ticker"] for x in a] == ["MCHI"], [(x["ticker"], x["filing_type"]) for x in a])
mchi = a[0] if a else {}
check("filing_type ETF_HOLDINGS_CHANGE (added and removed)", mchi.get("filing_type") == "ETF_HOLDINGS_CHANGE")
check("impact MEDIUM (largest move 2.0% < 5% high threshold)", mchi.get("impact") == "MEDIUM")
s = mchi.get("summary", "")
check("summary names the addition with its weight", "PDD HOLDINGS (PDD) at 2.00%" in s, s)
check("summary names the exit with its prior weight", "NIO INC (NIO), previously 1.00%" in s, s)
check("dust position is counted, not listed", "XPENG" not in s and "1 smaller change" in s, s)
check("alert is tagged Feature 14", (mchi.get("extra") or {}).get("feature_id") == 14)
check("alert links to the issuer page", mchi.get("filing_url") == "https://www.example.com/msci")
check("alert is queued for delivery", mchi.get("delivered") is False)
check("change rows stored for every fund, watched or not",
      {(c["ticker"], c["security_key"], c["action"]) for c in changes()} ==
      {("MCHI", "PDD", "ADDED"), ("MCHI", "XPEV", "ADDED"), ("MCHI", "NIO", "REMOVED"),
       ("AGG", "T3", "ADDED"), ("AGG", "T2", "REMOVED"), ("UNW", "R", "ADDED"), ("UNW", "Q", "REMOVED")},
      sorted((c["ticker"], c["security_key"], c["action"]) for c in changes()))
check("alerted flag set on the alerted changes only",
      {c["security_key"] for c in changes("MCHI") if c.get("alerted")} == {"PDD", "NIO"},
      [(c["security_key"], c.get("alerted")) for c in changes("MCHI")])
check("snapshot replaced: NIO deleted, PDD + XPEV stored", set(stored("MCHI")) == {"TCEHY", "BABA", "PDD", "XPEV"})

msg = alert_formatter.build_message(mchi, reason=alert_formatter.delivery_reason(mchi))
check("renders for Telegram with ticker, label and feature footer",
      "$MCHI" in msg and "ETF Holdings Change" in msg and "Feature 14" in msg, msg[:300])

users = {"u1": {"user_id": "u1", "chat_id": "1", "min_impact": "MEDIUM", "muted_features": set(),
                "max_alerts_per_day": 200, "receive_market_wide": True},
         "u2": {"user_id": "u2", "chat_id": "2", "min_impact": "MEDIUM", "muted_features": set(),
                "max_alerts_per_day": 200, "receive_market_wide": True}}
aud = delivery.resolve_audience(mchi, users, {"MCHI": {"u1"}})
check("delivered only to the user watching MCHI", [u["user_id"] for u, _ in aud] == ["u1"], aud)
users["u1"]["muted_features"] = {14}
check("muting Feature 14 silences it", delivery.resolve_audience(mchi, users, {"MCHI": {"u1"}}) == [])
users["u1"]["muted_features"] = set()

# ── 4. Re-run on the same new snapshot: no duplicate ─────────────────────────
run_e("light")
check("re-run on the same snapshot adds no alert", len(alerts()) == 1)
check("emit_alert refuses a repeated dedup_key",
      E.emit_alert("MCHI", E.SOURCE_PORTFOLIO, "ETF_HOLDINGS_CHANGE", "MEDIUM", "x",
                   {"dedup_key": mchi["extra"]["dedup_key"]}) is False)

# ── 5. A 6% new position is HIGH ──────────────────────────────────────────────
FMP["holdings"]["MCHI"] = hold("2026-10-03 10:00:00", [
    ("TCEHY", "TENCENT", 10.0), ("BABA", "ALIBABA", 5.0), ("PDD", "PDD HOLDINGS", 2.0),
    ("XPEV", "XPENG", 0.1), ("JD", "JD.COM", 6.0)])
run_e("light")
last = alerts()[-1]
check("6% addition -> HIGH, ETF_HOLDINGS_ADDED", last["impact"] == "HIGH"
      and last["filing_type"] == "ETF_HOLDINGS_ADDED" and len(alerts()) == 2,
      (last["impact"], last["filing_type"], len(alerts())))

# ── 6. Cash / collateral lines never alert ────────────────────────────────────
FMP["holdings"]["MCHI"] = hold("2026-10-04 10:00:00", [
    ("TCEHY", "TENCENT", 10.0), ("BABA", "ALIBABA", 5.0), ("PDD", "PDD HOLDINGS", 2.0),
    ("XPEV", "XPENG", 0.1), ("JD", "JD.COM", 6.0), ("USD", "CASH COLLATERAL USD", 3.0)])
run_e("light")
check("a 3% cash line is stored but not alerted", len(alerts()) == 2 and "USD" in stored("MCHI"))

# ── 7. Empty FMP response is a bad read, not "everything sold" ────────────────
FMP["holdings"]["MCHI"] = []
run_e("light")
check("empty holdings: no alert, snapshot untouched", len(alerts()) == 2 and len(stored("MCHI")) == 6)

# ── 8. Partial read (5 of 20) is ignored ──────────────────────────────────────
v_before = state("BIG").get("holdings_version")
FMP["holdings"]["BIG"] = hold("2026-10-02 10:00:00", [(f"S{i}", f"STOCK {i}", 20.0) for i in range(5)])
run_e("light")
check("partial read: no alert, 20 stored, version unchanged",
      len(alerts()) == 2 and len(stored("BIG")) == 20 and state("BIG").get("holdings_version") == v_before)

# ── 9. Wholesale re-keying is a vendor change, not 20 trades ─────────────────
FMP["holdings"]["BIG"] = hold("2026-10-03 10:00:00", [(f"ISIN{i}", f"STOCK {i}", 5.0) for i in range(20)])
run_e("light")
check("100% churn: re-baselined silently", len(alerts()) == 2 and not changes("BIG")
      and set(stored("BIG")) == {f"ISIN{i}" for i in range(20)})

# ── 10. Expense ratio: confirmation, flap, direction, impact ─────────────────
FMP["info"]["MCHI"] = info("iShares MSCI China ETF", 0.49)
run_e("light")
check("first sighting of a new ratio does not alert", len(alerts()) == 2)
run_e("light")
last = alerts()[-1]
check("confirmed on the 2nd run -> ETF_EXPENSE_DECREASE MEDIUM",
      len(alerts()) == 3 and last["filing_type"] == "ETF_EXPENSE_DECREASE" and last["impact"] == "MEDIUM",
      (len(alerts()), last.get("filing_type"), last.get("impact")))
check("expense summary states both ratios and the bp move",
      "from 0.59% to 0.49%" in last["summary"] and "-10.0 basis points" in last["summary"], last["summary"])
check("expense alert is Feature 14", last["extra"]["feature_id"] == 14)

FMP["info"]["MCHI"] = info("iShares MSCI China ETF", 0.55)
run_e("light")
FMP["info"]["MCHI"] = info("iShares MSCI China ETF", 0.49)
run_e("light")
check("one-run flap and back: no alert, pending cleared",
      len(alerts()) == 3 and state("MCHI").get("expense_pending") is None)

FMP["info"]["MCHI"] = info("iShares MSCI China ETF", 0.65)
run_e("light")
run_e("light")
last = alerts()[-1]
check("+16bp confirmed -> ETF_EXPENSE_INCREASE HIGH",
      last["filing_type"] == "ETF_EXPENSE_INCREASE" and last["impact"] == "HIGH", (last["filing_type"], last["impact"]))

# ── 11. Truncation boundary logic ─────────────────────────────────────────────
def rowset(d):
    return {k: {"portfolio_pct": w} for k, w in d.items()}


added, removed, kept = E.diff_holdings(rowset({"a": 10, "b": 5, "c": 1}),
                                       rowset({"a": 10, "b": 5, "c": 0.5, "d": 0.8, "e": 0.2}), max_rows=3)
check("position climbing into the stored top-N is not a new buy", added == [] and removed == [], (added, removed))
check("kept set is the heaviest N", set(kept) == {"a", "b", "d"})
added, removed, _ = E.diff_holdings(rowset({"a": 10, "b": 5, "c": 1}),
                                    rowset({"a": 10, "b": 5, "d": 3.0}), max_rows=3)
check("a genuinely new heavy position is added; a gone one removed", added == ["d"] and removed == ["c"])

# ── 12. Persistent FMP 429 stops the run instead of grinding ─────────────────
def _quota(t):
    CALLS["info"] += 1
    raise F.FMPError("persistent 429")


F.raw_info = _quota
CALLS["info"] = 0
E.WORKERS = 1
run_e("light")
check("quota error aborts after the first call", CALLS["info"] == 1, CALLS["info"])
F.raw_info = _info


# ══════════════════════════════════════════════════════════════════════════════
print("\n=== FEATURE 16 · etf_fund_events_poller ===")
# ══════════════════════════════════════════════════════════════════════════════
import fund_manager_poller as fm   # noqa: E402
import etf_fund_events_poller as EV  # noqa: E402

db2 = FakeDB()
fm.sb = lambda: db2
IS = "0001100663"     # iShares Trust
SOLO_CIK = "0000999999"
UNIVERSE = {
    "by_ticker": {
        "MCHI": {"cik": IS, "series_id": "S000004361", "class_id": "C1", "fund_type": "ETF"},
        "FXI": {"cik": IS, "series_id": "S000004362", "class_id": "C2", "fund_type": "ETF"},
        "OTHR": {"cik": IS, "series_id": "S000004363", "class_id": "C3", "fund_type": "ETF"},
        "SOLO": {"cik": SOLO_CIK, "series_id": "S000005555", "class_id": "C4", "fund_type": "ETF"},
    },
}
UNIVERSE["by_series"] = {v["series_id"]: {"cik": v["cik"], "tickers": [t], "fund_type": "ETF"}
                         for t, v in UNIVERSE["by_ticker"].items()}
UNIVERSE["ciks"] = {IS, SOLO_CIK}
fm.load_universe = lambda force=False: UNIVERSE
WATCHED2 = {"MCHI", "FXI", "SOLO"}
fm.watched_tickers = lambda: set(WATCHED2)

NAMES = {"S000004361": "iShares MSCI China ETF", "S000004362": "iShares China Large-Cap ETF",
         "S000004363": "iShares Other Markets Fund ETF", "S000005555": "Solo Strategy ETF"}


def series(*ids):
    return [{"series_id": i, "series_name": NAMES[i], "classes": []} for i in ids]


def hit(adsh, fname, cik, queries, form="497"):
    return {"_id": f"{adsh}:{fname}", "_q": set(queries),
            "_source": {"ciks": [cik], "form": form, "file_date": "2026-10-01",
                        "display_names": [f"Trust (CIK {cik})"]}}


FILLER = " Other information in this supplement is unchanged." * 30
DOCS = {
    # liquidation of an UNWATCHED fund in a watched trust — must not reach MCHI holders
    "d1.htm": "<p>Supplement dated October 1, 2026 to the Prospectus.</p>"
              "<p>The Board approved a plan of liquidation for the iShares Other Markets Fund ETF."
              "</p>" + FILLER,
    # liquidation naming BOTH watched funds
    "d2.htm": "<p>Supplement dated October 1, 2026 to the Prospectus.</p>"
              "<p>The Board of Trustees approved a plan of liquidation for the iShares "
              "China Large-Cap ETF and the iShares MSCI China ETF. The last day of trading will be "
              "November 13, 2026.</p>" + FILLER,
    # single-series trust: merger AND fee language — merger wins
    "d3.htm": "<p>Supplement dated October 1, 2026 to the Prospectus.</p>"
              "<p>The Board approved an Agreement and Plan of Reorganization under which the Solo "
              "Strategy ETF will be reorganized into a larger fund. Separately the adviser agreed to "
              "reduce its management fee.</p>" + FILLER,
    # EFTS said "new underlying index", the document does not — stemming false positive
    "d5.htm": "<p>Supplement dated October 1, 2026 to the Prospectus.</p>"
              "<p>Fund holdings disclosure for the iShares MSCI China ETF.</p>" + FILLER,
}
SERIES = {"0000000000-26-000001": series("S000004361", "S000004363"),
          "0000000000-26-000002": series("S000004361", "S000004362", "S000004363"),
          "0000000000-26-000003": series("S000005555"),
          "0000000000-26-000005": series("S000004361")}
HITS = [
    hit("0000000000-26-000001", "d1.htm", IS, ['"plan of liquidation"']),
    hit("0000000000-26-000002", "d2.htm", IS, ['"plan of liquidation"', '"last day of trading"']),
    hit("0000000000-26-000003", "d3.htm", SOLO_CIK, ['"agreement and plan of reorganization"',
                                                     '"reduce its management fee"']),
    hit("0000000000-26-000004", "d4.htm", "0000777777", ['"plan of liquidation"']),   # unwatched CIK
    hit("0000000000-26-000005", "d5.htm", IS, ['"new underlying index"']),
]
FETCH = {"header": 0, "doc": 0, "efts": 0}


def _efts(q, forms, start, end, max_pages=20):
    FETCH["efts"] += 1
    for h in HITS:
        if q in h["_q"]:
            yield {k: v for k, v in h.items() if k != "_q"}


def _header(cik, adsh):
    FETCH["header"] += 1
    return SERIES.get(adsh, [])


class _SEC:
    def text(self, url):
        FETCH["doc"] += 1
        return DOCS.get(url.rsplit("/", 1)[-1])


fm.efts_hits = _efts
fm.fetch_series_header = _header
fm.sec = _SEC()

queued = EV.run_etf_fund_events_poller()
raw = db2.rows("raw_filings")
ev = {r["doc_key"].split(":")[0][-1]: r for r in db2.rows("etf_fund_events")}

check("two filings queued (d2 liquidation, d3 merger)", queued == 2 and len(raw) == 2, (queued, len(raw)))
check("unwatched trust's filing never fetched (no header, no doc)",
      "4" not in ev and FETCH["header"] == 4, (sorted(ev), FETCH))
check("multi-fund filing naming only another fund -> NOT_WATCHED, no alert",
      ev.get("1", {}).get("status") == "NOT_WATCHED")
check("EFTS false positive -> NO_MATCH", ev.get("5", {}).get("status") == "NO_MATCH")
liq = next((r for r in raw if r["filing_type"] == "ETF_LIQUIDATION"), {})
check("liquidation: ONE row for two watched funds, the other via fan-out",
      liq.get("ticker") == "FXI" and liq.get("extra", {}).get("fanout_tickers") == ["MCHI"], liq.get("extra"))
check("liquidation: source SEC_FUND, floor HIGH, status PENDING",
      liq.get("source") == "SEC_FUND" and liq["extra"]["impact_floor"] == "HIGH" and liq["status"] == "PENDING")
check("liquidation excerpt carries the operative sentence",
      "plan of liquidation" in liq.get("raw_text", "").lower() and "November 13, 2026" in liq.get("raw_text", ""))
mer = next((r for r in raw if r["ticker"] == "SOLO"), {})
check("merger outranks fee change in the same filing",
      mer.get("filing_type") == "ETF_MERGER" and mer["extra"]["impact_floor"] == "HIGH", mer.get("filing_type"))
check("filing_url points at the SEC document", mer.get("filing_url", "").startswith("https://www.sec.gov/Archives/"))
check("raw rows resolve to Feature 16",
      all(feature_map.resolve_feature(r["source"], r["filing_type"])[0] == 16 for r in raw))

before = dict(FETCH)
EV.run_etf_fund_events_poller()
check("hourly re-run: nothing re-fetched, nothing re-queued",
      FETCH["header"] == before["header"] and FETCH["doc"] == before["doc"] and len(db2.rows("raw_filings")) == 2)

WATCHED2.clear()
FETCH["efts"] = 0
EV.run_etf_fund_events_poller()
check("no watched fund -> zero SEC search requests", FETCH["efts"] == 0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n=== ai_pipeline · impact floor + one-summary fan-out ===")
# ══════════════════════════════════════════════════════════════════════════════
import ai_pipeline as AP  # noqa: E402

db3 = FakeDB()
AP.supabase = db3
AP.store_summary = lambda **k: 77
SUMMARY = ("The board of trustees of iShares Trust approved a plan of liquidation for the iShares "
           "China Large-Cap ETF and the iShares MSCI China ETF. Both funds will stop accepting "
           "creation orders on November 14, 2026, and the last day of trading on NYSE Arca will be "
           "November 13, 2026. Shareholders who still hold shares on the liquidation date of "
           "November 20, 2026 will receive a cash distribution equal to the net asset value of their "
           "shares. The trust stated that the funds had not attracted sufficient assets to operate "
           "efficiently over the long term.")
check("fixture summary passes the real quality gate",
      AP.classify_failure(SUMMARY, 150, 70) is None, AP.classify_failure(SUMMARY, 150, 70))
LLM = {"n": 0}
REPLIES = ['{"is_gibberish": false}', '{"is_relevant": true}', SUMMARY,
           '{"issues_detected": false}', '{"impact": "LOW"}']


def _llm(prompt, retries=3, max_tokens=1000):
    LLM["n"] += 1
    return REPLIES[min(LLM["n"] - 1, len(REPLIES) - 1)]


AP.call_deepinfra = _llm
db3.tables["raw_filings"] = [dict(liq, id=501)]
AP.process_filing(dict(liq, id=501))
out = db3.rows("alerts")
check("one LLM pass (5 calls) for a filing covering two tickers", LLM["n"] == 5, LLM["n"])
check("two alert rows: primary + fan-out", sorted(a["ticker"] for a in out) == ["FXI", "MCHI"],
      [a["ticker"] for a in out])
check("LOW impact verdict raised to the HIGH floor on both", all(a["impact"] == "HIGH" for a in out))
check("both carry the same summary and the SEC link",
      len({a["summary"] for a in out}) == 1 and all(a.get("link") == liq["filing_url"] for a in out))
check("fan-out copy records its origin", any((a["extra"] or {}).get("fanout_of") == "FXI" for a in out))
check("both tagged Feature 16", all(a["extra"]["feature_id"] == 16 for a in out))
check("raw filing marked PROCESSED", db3.rows("raw_filings")[0]["status"] == "PROCESSED")
check("SEC_FUND jumps the pipeline queue", "SEC_FUND" in AP.PRIORITY_SOURCES)
check("fan-out list is sanitised", AP._fanout_tickers({"fanout_tickers": ["mchi", "FXI", "", "B@D", "MCHI"]},
                                                      "FXI") == ["MCHI"])


# ══════════════════════════════════════════════════════════════════════════════
print("\n=== telegram_bot · /add accepts ETFs ===")
# ══════════════════════════════════════════════════════════════════════════════
import telegram_bot as TB  # noqa: E402

db4 = FakeDB()
db4.tables["stocks"] = [{"ticker": "AAPL", "sector": "Technology", "exchange": "NASDAQ"}]
db4.tables["etf_master"] = [{"ticker": "MCHI", "name": "iShares MSCI China ETF", "category": "Equity",
                             "exchange": "NASDAQ"}]
TB.supabase = db4
etf = TB.get_stock_by_ticker("mchi")
check("ETF resolves through etf_master", etf and etf.get("is_etf") and etf["sector"] == "ETF · Equity", etf)
check("stock still resolves through stocks", (TB.get_stock_by_ticker("AAPL") or {}).get("sector") == "Technology")
check("unknown ticker is still refused", TB.get_stock_by_ticker("ZZZZ") is None)
check("prefix search includes ETFs", "MCHI" in [r["ticker"] for r in TB.search_stocks_by_prefix("MC")])
check("adding an ETF writes a watchlist row", TB.add_to_watchlist("user-1", "MCHI") is True
      and db4.rows("watchlists")[0]["ticker"] == "MCHI")


# ══════════════════════════════════════════════════════════════════════════════
print("\n=== Review regressions (independent review, 2026-10-02) ===")
# ══════════════════════════════════════════════════════════════════════════════
# R1. A supplement HEADED with two funds that liquidates only one must not tell
#     the other fund's holders it is closing.
db5 = FakeDB()
fm.sb = lambda: db5
WATCHED2.clear()
WATCHED2.update({"MCHI", "FXI"})
SERIES["0000000000-26-000010"] = series("S000004361", "S000004362")
DOCS["d10.htm"] = ("<p>SUPPLEMENT DATED OCTOBER 1, 2026 TO THE PROSPECTUS FOR THE iShares MSCI China ETF "
                   "AND THE iShares China Large-Cap ETF</p>"
                   "<p>Other information about the funds is unchanged.</p>"
                   "<p>The Board of Trustees approved a plan of liquidation for the iShares China Large-Cap "
                   "ETF. Its last day of trading will be November 13, 2026.</p>" + FILLER)
HITS[:] = [hit("0000000000-26-000010", "d10.htm", IS, ['"plan of liquidation"'])]
EV.run_etf_fund_events_poller()
raw5 = db5.rows("raw_filings")
check("R1 liquidation attributed only to the fund named with the phrase",
      len(raw5) == 1 and raw5[0]["ticker"] == "FXI" and raw5[0]["extra"]["fanout_tickers"] == [],
      [(r["ticker"], r["extra"]["fanout_tickers"]) for r in raw5])
check("R1 the header line names the liquidating fund, not the other",
      raw5 and raw5[0]["raw_text"].startswith("Fund: iShares China Large-Cap ETF (FXI)."))

# R2. Prospectus boilerplate is not a fee change; a full prospectus re-filing is
#     not an event at all.
db5.tables.clear()
SERIES["0000000000-26-000011"] = series("S000004361")
SERIES["0000000000-26-000012"] = series("S000004361")
DOCS["d11.htm"] = ("<p>Supplement dated October 1, 2026.</p><p>The fee waiver may not be terminated before "
                   "November 30, 2027, and the expense limitation agreement may not be terminated earlier.</p>" + FILLER)
DOCS["d12.htm"] = ("<p>PROSPECTUS. iShares MSCI China ETF.</p>" + FILLER * 3 +
                   "<p>A fund may adopt a plan of liquidation if its assets fall.</p>")
HITS[:] = [hit("0000000000-26-000011", "d11.htm", IS, list(next(r for r in EV.RULES if r[0] == "ETF_FEE_CHANGE")[3])),
           hit("0000000000-26-000012", "d12.htm", IS, ['"plan of liquidation"'])]
EV.run_etf_fund_events_poller()
st = {r["doc_key"].split(":")[1]: r["status"] for r in db5.rows("etf_fund_events")}
check("R2 'may not be terminated' boilerplate -> no fee-change alert",
      db5.rows("raw_filings") == [] and st.get("d11.htm") == "NO_MATCH", st)
check("R2 full prospectus re-filing (not a supplement) -> NO_MATCH", st.get("d12.htm") == "NO_MATCH", st)
check("R2 old generic 'terminate' pairs are gone from the rules",
      not any('"terminate"' in q for r in EV.RULES for q in r[3]))

# R5. A failed document fetch is retried on the next poll, not settled forever.
db5.tables.clear()
SERIES["0000000000-26-000013"] = series("S000004361")
DOCS.pop("d13.htm", None)
HITS[:] = [hit("0000000000-26-000013", "d13.htm", IS, ['"plan of liquidation"'])]
EV.run_etf_fund_events_poller()
check("R5 first attempt with no document -> NO_DOC, nothing queued",
      db5.rows("raw_filings") == [] and db5.rows("etf_fund_events")[0]["status"] == "NO_DOC")
DOCS["d13.htm"] = ("<p>Supplement dated October 2, 2026. The Board approved a plan of liquidation "
                   "for the iShares MSCI China ETF.</p>" + FILLER)
EV.run_etf_fund_events_poller()
check("R5 next poll retries and queues it", len(db5.rows("raw_filings")) == 1
      and db5.rows("etf_fund_events")[0]["status"] == "QUEUED")

# R3. A failed registry download must not be cached as "no funds".
import importlib  # noqa: E402
fm_real = importlib.reload(fm)
calls = {"n": 0}


class _MF:
    def __init__(self, payloads):
        self.payloads = payloads

    def json(self, url, params=None):
        calls["n"] += 1
        return self.payloads.pop(0) if self.payloads else None


fm_real._etf_symbols = lambda: set()
fm_real.sb = lambda: FakeDB()            # reload restored the real client; keep it offline
fm_real.sec = _MF([None, {"fields": ["cik", "seriesId", "classId", "symbol"],
                          "data": [[1100663, "S000004361", "C1", "MCHI"]]}])
fm_real._universe_cache = None
first = fm_real.load_universe()
second = fm_real.load_universe()
check("R3 failed download is not cached; next call refetches",
      first["by_ticker"] == {} and "MCHI" in second["by_ticker"] and calls["n"] == 2, (calls, second))
fm_real.sec = _MF([None])
kept = fm_real.load_universe(force=True)
check("R3 a failed forced refresh keeps the last good universe", "MCHI" in kept["by_ticker"])

# R4. No SGML header on a multi-series trust: no "every series" attribution.
db6 = FakeDB()
fm_real.sb = lambda: db6
fm_real._universe_cache = {"by_ticker": {}, "by_series": {f"S{i}": {"cik": IS, "tickers": [f"T{i}"],
                                                                     "fund_type": "ETF"} for i in range(5)},
                           "ciks": {IS}}
fm_real.fetch_series_header = lambda cik, adsh: []
fm_real.sec = type("S", (), {"text": lambda self, url: "<p>John Smith no longer serves as a portfolio "
                                                       "manager of the Fund.</p>"})()
meta = {"key": "0000000000-26-000020:s.htm", "accession": "0000000000-26-000020", "cik": IS,
        "form": "497", "filing_date": "2026-10-01", "url": "https://www.sec.gov/x/s.htm"}
n = fm_real.process_event_filing(dict(meta), {"T0", "T1", "T2", "T3", "T4"})
check("R4 no header on a 5-series trust -> no events, no alerts",
      n == 0 and db6.rows("fund_manager_events") == [] and db6.rows("alerts") == [])
check("R4 recorded NO_SERIES and left retryable",
      db6.rows("fund_filings_processed")[0]["status"] == "NO_SERIES"
      and fm_real.already_processed(meta["key"], "EVENT") is False)

# R6. An unreadable alerts ledger must not consume the change.
E.sb = lambda: db
db.fail_tables = set()
FMP["holdings"]["MCHI"] = hold("2026-10-05 10:00:00", [
    ("TCEHY", "TENCENT", 10.0), ("BABA", "ALIBABA", 5.0), ("PDD", "PDD HOLDINGS", 2.0),
    ("XPEV", "XPENG", 0.1), ("JD", "JD.COM", 6.0), ("USD", "CASH COLLATERAL USD", 3.0),
    ("BIDU", "BAIDU", 1.5)])
v_before = state("MCHI")["holdings_version"]
n_before = len(alerts())
db.fail_tables = {"alerts"}
run_e("light")
db.fail_tables = set()
check("R6 ledger outage: no alert, version NOT advanced",
      len(alerts()) == n_before and state("MCHI")["holdings_version"] == v_before)
run_e("light")
check("R6 next run delivers the change it could not before",
      len(alerts()) == n_before + 1 and alerts()[-1]["summary"].count("BAIDU (BIDU)") == 1)

# R7. Crash between an expense alert and the state flush cannot re-alert it.
n_before = len(alerts())
saved = dict(state("MCHI"))
FMP["info"]["MCHI"] = info("iShares MSCI China ETF", 0.45)
run_e("light")
run_e("light")
check("R7 expense change alerts once", len(alerts()) == n_before + 1)
check("R7 alerting ETF's state is written through immediately",
      state("MCHI").get("expense_ratio") == 0.45)
for s in db.rows("etf_snapshot_state"):          # simulate the lost flush
    if s["ticker"] == "MCHI":
        s.update(expense_ratio=saved["expense_ratio"], expense_pending=None, expense_pending_count=0)
run_e("light")
run_e("light")
check("R7 the same change re-confirming later does not alert again", len(alerts()) == n_before + 1)

# R9. One event fanned out to two watched tickers reaches a user once.
import asyncio  # noqa: E402
dbd = FakeDB()
delivery.supabase = dbd
dbd.tables["users"] = [{"id": "u1", "telegram_chat_id": 111, "is_active": True}]
dbd.tables["watchlists"] = [{"user_id": "u1", "ticker": "FXI"}, {"user_id": "u1", "ticker": "MCHI"}]
NOW = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
for i, t in enumerate(("FXI", "MCHI")):
    dbd.tables.setdefault("alerts", []).append({
        "id": 900 + i, "ticker": t, "summary": "Liquidation.", "impact": "HIGH", "source": "SEC_FUND",
        "filing_type": "ETF_LIQUIDATION", "delivered": False, "created_at": NOW,
        "extra": {"fanout_group": "SEC_FUND:501", "summary_hash": f"h{i}", "feature_id": 16}})
SENT = []


async def _send(bot, chat_id, text):
    SENT.append(chat_id)
    return True, None, False


delivery._send_one = _send
delivery.Bot = lambda token=None: None
delivery._log_payload = lambda a: None
delivery._log_alert_run = lambda *a, **k: None
delivery.PER_CHAT_GAP_SECONDS = 0
asyncio.run(delivery.deliver_pending_alerts())
led = dbd.rows("alert_deliveries")
check("R9 user watching both tickers gets ONE message", len(SENT) == 1, SENT)
check("R9 the other copy is settled as SKIPPED (terminal, not retried)",
      sorted(r["status"] for r in led) == ["SENT", "SKIPPED"], [(r["alert_id"], r["status"]) for r in led])
check("R9 both rows leave the queue", all(a["delivered"] for a in dbd.rows("alerts")))


# ══════════════════════════════════════════════════════════════════════════════
print("\n=== Audit regressions (second audit, 2026-10-02) ===")
# ══════════════════════════════════════════════════════════════════════════════
import fund_manager_extractor as fx  # noqa: E402

# A4. FMP's 0 is "not populated", never a baseline; same-day re-reads confirm nothing.
db.tables["etf_master"].append({"ticker": "NEWE", "name": "New ETF"})
WATCHED.add("NEWE")
FMP["holdings"]["NEWE"] = hold("2026-10-01 10:00:00", [("AAA", "AAA CO", 50.0), ("BBB", "BBB CO", 50.0)])
FMP["info"]["NEWE"] = info("New ETF", 0)
n0 = len(alerts())
run_e("light")
check("A4 expenseRatio 0 is not stored as a baseline", state("NEWE").get("expense_ratio") is None)
FMP["info"]["NEWE"] = info("New ETF", 0.35)
run_e("light"); run_e("light")
check("A4 real ratio arriving after a 0 does not alert", len(alerts()) == n0
      and state("NEWE").get("expense_ratio") == 0.35)
FMP["info"]["NEWE"] = info("New ETF", 0.40)
run_e("light")
run_e("light", same_day=True)
check("A4 two reads on the SAME day do not confirm", len(alerts()) == n0, len(alerts()) - n0)
run_e("light")
check("A4 a read on the next day confirms", len(alerts()) == n0 + 1
      and alerts()[-1]["filing_type"] == "ETF_EXPENSE_INCREASE")

# A7. Partial read of a fund larger than the stored cap is caught.
saved_max = E.MAX_ROWS
E.MAX_ROWS = 10
db.tables["etf_master"].append({"ticker": "HUGE", "name": "Huge Fund ETF"})
WATCHED.add("HUGE")
FMP["info"]["HUGE"] = info("Huge Fund ETF", 0.03)
FMP["holdings"]["HUGE"] = hold("2026-10-01 10:00:00", [(f"H{i}", f"HOLD {i}", 2.0) for i in range(40)])
run_e("light")
v0 = state("HUGE").get("holdings_version")
FMP["holdings"]["HUGE"] = hold("2026-10-02 10:00:00", [(f"H{i}", f"HOLD {i}", 2.0) for i in range(12)])
run_e("light")
check("A7 12-of-40 read of a capped fund is ignored, not re-baselined",
      state("HUGE").get("holdings_version") == v0 and len(stored("HUGE")) == 10, state("HUGE").get("last_note"))
E.MAX_ROWS = saved_max

# Quotes in holding names never reach an in() filter.
k = F.security_key({"asset": "", "name": 'FOO "A" SHARES, INC.'})
check("holding keys carry no double quotes or backslashes", '"' not in k and "\\" not in k, k)

# A3. Multi-fund sticker: "each Fund" and a fund-name heading both attribute.
db7 = FakeDB()
fm.sb = lambda: db7
fm.watched_tickers = lambda: {"MCHI", "FXI"}
fm.load_universe = lambda force=False: UNIVERSE
fm.efts_hits, fm.fetch_series_header, fm.sec = _efts, _header, _SEC()
SERIES["0000000000-26-000030"] = series("S000004361", "S000004362")
SERIES["0000000000-26-000031"] = series("S000004361", "S000004362", "S000004363")
DOCS["d30.htm"] = ("<p>Supplement dated October 1, 2026 to the Prospectus for the iShares MSCI China ETF "
                   "and the iShares China Large-Cap ETF (each, a Fund).</p><p>The Board approved a plan "
                   "of liquidation for each Fund.</p>" + FILLER)
DOCS["d31.htm"] = ("<p>Supplement dated October 1, 2026 to the Prospectuses of the iShares Trust.</p>"
                   "<p>iShares China Large-Cap ETF</p><p>The Board approved a plan of liquidation for "
                   "the Fund. The last day of trading will be November 13, 2026.</p>"
                   "<p>iShares Other Markets Fund ETF</p><p>No changes.</p>" + FILLER)
HITS[:] = [hit("0000000000-26-000030", "d30.htm", IS, ['"plan of liquidation"']),
           hit("0000000000-26-000031", "d31.htm", IS, ['"plan of liquidation"'])]
EV.run_etf_fund_events_poller()
by_doc = {r["extra"]["accession"][-2:]: r for r in db7.rows("raw_filings")}
check("A3 'for each Fund' -> both watched funds", "30" in by_doc
      and sorted([by_doc["30"]["ticker"]] + by_doc["30"]["extra"]["fanout_tickers"]) == ["FXI", "MCHI"],
      {k: (v["ticker"], v["extra"]["fanout_tickers"]) for k, v in by_doc.items()})
check("A3 fund-name heading above the paragraph -> that fund only", "31" in by_doc
      and by_doc["31"]["ticker"] == "FXI" and by_doc["31"]["extra"]["fanout_tickers"] == [])
check("A5 fan-out carries each copy's own fund name",
      by_doc.get("30", {}).get("extra", {}).get("fanout_names") == {"MCHI": "iShares MSCI China ETF"})
check("A5 header names every affected fund",
      "iShares China Large-Cap ETF (FXI); iShares MSCI China ETF (MCHI)" in by_doc.get("30", {}).get("raw_text", ""))

# A8. A UIT / grantor trust outside the fund registry is still searched.
db7.tables.clear()
db7.tables["etf_master"] = [{"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust"}]
fm.watched_tickers = lambda: {"SPY", "AAPL"}
EV._trust_cache.update(map=None, at=0.0)


class _SEC2(_SEC):
    def json(self, url, params=None):
        return {"0": {"cik_str": 884394, "ticker": "SPY", "title": "SPDR S&P 500 ETF TRUST"},
                "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


fm.sec = _SEC2()
f = EV.watched_funds()
check("A8 SPY resolved as a single-entity trust; AAPL (a stock) is not a fund",
      f.get("SPY", {}).get("cik") == "0000884394" and "AAPL" not in f, f)
DOCS["d40.htm"] = ("<p>Supplement dated October 1, 2026 to the Prospectus.</p><p>The Trust's sponsor "
                   "approved a plan of reorganization of the Trust.</p>" + FILLER)
HITS[:] = [{"_id": "0000000000-26-000040:d40.htm", "_q": {'"plan of reorganization"'},
            "_source": {"ciks": ["0000884394"], "form": "497", "file_date": "2026-10-01",
                        "display_names": ["SPDR S&P 500 ETF TRUST  (SPY)  (CIK 0000884394)"]}}]
EV.run_etf_fund_events_poller()
r = db7.rows("raw_filings")
check("A8 a trust's own filing is queued for SPY with the trust's name",
      len(r) == 1 and r[0]["ticker"] == "SPY" and r[0]["company_name"] == "SPDR S&P 500 ETF TRUST",
      [(x["ticker"], x["company_name"]) for x in r])

# A1 + A2. Fund manager: same sticker under two accessions alerts once; a
# non-supplement bio is not an event.
import importlib  # noqa: E402
fmm = importlib.reload(fm)
db8 = FakeDB()
fmm.sb = lambda: db8
VS = [{"series_id": "S000002500", "series_name": "Vanguard Explorer Fund", "classes": []}]
fmm._universe_cache = {"by_ticker": {"VEXPX": {"cik": "0000932471", "series_id": "S000002500",
                                               "class_id": "C1", "fund_type": "MF"}},
                       "by_series": {"S000002500": {"cik": "0000932471", "tickers": ["VEXPX"], "fund_type": "MF"}},
                       "ciks": {"0000932471"}}
fmm.fetch_series_header = lambda cik, adsh: VS
STICKER = ("<p>Supplement dated October 1, 2026 to the Prospectus for Vanguard Explorer Fund</p>"
           "<p>Effective immediately, John P. Smith no longer serves as a portfolio manager of "
           "Vanguard Explorer Fund.</p>")
BIO = ("<p>Vanguard Explorer Fund Summary Prospectus</p><p>Omar Haddad, CFA, joined the Fund as "
       "co-portfolio manager in 2021.</p>")
fmm.sec = type("S", (), {"text": lambda self, url: STICKER if "st" in url else BIO})()


def _m(adsh, name, form="497"):
    return {"key": f"{adsh}:{name}", "accession": adsh, "cik": "0000932471", "form": form,
            "filing_date": "2026-10-01", "url": f"https://www.sec.gov/x/{name}", "filer": "Vanguard"}


fmm.process_event_filing(_m("0000932471-26-000100", "st1.htm"), {"VEXPX"})
fmm.process_event_filing(_m("0000932471-26-000101", "st2.htm"), {"VEXPX"})
pm = [a for a in db8.rows("alerts") if a["source"] == "FUND_MANAGER"]
check("A1 same sticker under two accessions -> ONE alert", len(pm) == 1, [a["extra"]["accession"] for a in pm])
check("A1 fan-out group is independent of the accession",
      pm and "0000932471-26" not in pm[0]["extra"]["fanout_group"])
fmm.process_event_filing(_m("0000932471-26-000200", "bio.htm", "497K"), {"VEXPX"})
check("A2 manager bio in a summary prospectus -> no event, no alert",
      len(db8.rows("alerts")) == 1 and not any(e["manager_name"].startswith("Omar")
                                               for e in db8.rows("fund_manager_events")))
check("A2 recorded as NOT_SUPPLEMENT (terminal)",
      any(r["status"] == "NOT_SUPPLEMENT" for r in db8.rows("fund_filings_processed")))
ext = fx.extract_change_events("<p>Supplement dated Oct 1, 2026.</p><p>John Smith no longer serves as a "
                               "portfolio manager of the Fund.</p>",
                               [{"series_id": f"S{i}", "series_name": "", "classes": []} for i in range(5)],
                               "2026-10-01")
check("unnamed multi-series sticker is not attributed to every series",
      all(e["series_ids"] == [] for e in ext["events"]), [e["series_ids"] for e in ext["events"]])

# A6. Primary + fan-out copies are ONE insert.
db9 = FakeDB()
AP.supabase = db9
inserts = []
_orig_table = db9.table


def _spy_table(name):
    q = _orig_table(name)
    if name == "alerts":
        real_insert = q.insert
        q.insert = lambda rows: (inserts.append(rows), real_insert(rows))[1]
    return q


db9.table = _spy_table
LLM["n"] = 0
db9.tables["raw_filings"] = [dict(liq, id=601)]
AP.process_filing(dict(liq, id=601, raw_text="Second notice, filed separately. " + liq["raw_text"],
                       extra=dict(liq["extra"], title=liq["extra"]["title"] + " v2",
                                  fanout_names={"MCHI": "iShares MSCI China ETF"})))
check("A6 primary and fan-out copy written in a single insert",
      len(inserts) == 1 and isinstance(inserts[0], list) and len(inserts[0]) == 2, [type(i) for i in inserts])
cp = next((a for a in db9.rows("alerts") if a["ticker"] == "MCHI"), {})
check("A5 fan-out copy shows its own fund name",
      cp.get("extra", {}).get("company_name") == "iShares MSCI China ETF", cp.get("extra", {}).get("company_name"))


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASS")
