"""
tests/test_feature8_ipo.py

Feature 8 (IPO Deep Dive) end to end, both halves.

WHY TWO SOURCES. EDGAR publishes IPO *documents* but no IPO *calendar*: an
initial S-1 carries no listing date, no price range and no final share count —
those appear later, as prose, in S-1/A amendments and the 424B4 pricing
prospectus. FMP's ipos-calendar carries exactly those as structured fields, but
only once a deal is scheduled, which is weeks to months after the S-1 is filed.
Neither source alone covers the event, so Feature 8 reads both:

    SEC_IPO / S-1          "X has filed to go public"   (EDGAR, minutes fresh)
    FMP_IPO / IPO_UPCOMING "X lists on DATE at $A-$B"   (FMP calendar, daily)

WHAT WAS BROKEN. poll_sec_s1 wrote raw_filings rows with status="IPO_PENDING"
and source="SEC_EDGAR", and nothing read them: ai_pipeline selects "PENDING",
and ipo_poller resolved its S-1 link live from FMP rather than from the table.
The poll fetched filing bodies market-wide, stored them, and no alert was ever
downstream of any of it.

THE ROUTING TRAP THIS GUARDS. These alerts CANNOT be emitted under SEC_EDGAR.
That source is in delivery.COMPANY_ONLY_SOURCES so a filing for an unwatched
company can never broadcast — correct for 8-K/10-Q/Form 4, fatal for an S-1,
whose registrant is pre-IPO and cannot be on anyone's watchlist. Under
SEC_EDGAR every IPO alert would resolve to an empty audience and be marked
delivered without being sent: the exact silent failure this file exists to
prevent.

Run:  python tests/test_feature8_ipo.py
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for m in ["supabase", "telegram", "telegram.constants", "telegram.error", "dotenv",
          "aiohttp", "requests"]:
    sys.modules.setdefault(m, MagicMock())
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "x")
os.environ.setdefault("TELEGRAM_TOKEN", "x")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


import feature_map
import delivery
import ai_pipeline
import edgar_poller_async as ep
import ipo_poller


# ── 1. Capture: S-1 rows land under the right source and status ───────────────
print("=== 1. S-1 CAPTURE REACHES THE PIPELINE, NOT A DEAD STATUS ===")

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>S-1 - Northwind Robotics Inc (0001900001)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1900001/000190000126000001/idx.htm"/>
    <summary>Registration statement</summary>
  </entry>
  <entry>
    <title>S-1/A - Northwind Robotics Inc (0001900001)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1900001/000190000126000002/idx.htm"/>
    <summary>Amendment No. 1</summary>
  </entry>
  <entry>
    <title>S-1 - Cobalt Health Sciences Corp (0001900002)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1900002/000190000226000001/idx.htm"/>
    <summary>Registration statement</summary>
  </entry>
</feed>"""

stored = []


def _run_capture(existing_ciks=()):
    stored.clear()
    ep.sec_client = MagicMock()

    async def _get_text(url):
        return FEED

    async def _gather_limited(coros, limit=None):
        out = []
        for c in coros:
            c.close()          # we are not exercising the real fetch here
            out.append("Northwind Robotics has filed a registration statement "
                       "covering an initial public offering of common stock. " * 12)
        return out

    ep.sec_client.get_text = _get_text
    ep.sec_client.gather_limited = _gather_limited
    ep.known_filing_urls = lambda urls: set()
    ep.known_registrant_ciks = lambda ciks, source: set(existing_ciks)
    ep.ticker_from_cik = lambda cik: "UNKNOWN"
    ep.sec_financials = MagicMock()
    ep.sec_financials.build_sec_json_links = lambda cik, url: {
        "companyfacts": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"}
    ep.store_filing = lambda ft, company, ticker, text, url, extra=None, \
        status="PENDING", source="SEC_EDGAR": stored.append(
            {"filing_type": ft, "company": company, "ticker": ticker, "url": url,
             "extra": extra, "status": status, "source": source})
    return asyncio.run(ep.poll_sec_s1_async())


_run_capture()
check("S-1 filings are captured at all", len(stored) > 0, "nothing stored")
check("stored under source=SEC_IPO, not SEC_EDGAR",
      all(r["source"] == "SEC_IPO" for r in stored),
      f"got {[r['source'] for r in stored]} — SEC_EDGAR would never reach an audience")
check("stored status=PENDING so the AI pipeline picks them up",
      all(r["status"] == "PENDING" for r in stored),
      f"got {[r['status'] for r in stored]} — IPO_PENDING is read by nothing")
check("CIK is carried for the ipo_poller merge",
      all((r["extra"] or {}).get("cik") for r in stored), "no cik in extra")

# The amendment must not become a second alert for the same company.
ciks = [(r["extra"] or {}).get("cik") for r in stored]
check("one row per registrant, not one per amendment",
      len(ciks) == len(set(ciks)) == 2,
      f"got CIKs {ciks} — S-1/A would fire a duplicate broadcast")

# A registrant we already captured is skipped entirely on the next poll.
_run_capture(existing_ciks=("0001900001", "0001900002"))
check("an already-captured registrant is not re-stored", stored == [],
      f"re-stored {len(stored)} row(s) — every poll would re-alert")


# ── 2. Routing: the alert actually reaches somebody ───────────────────────────
print("\n=== 2. ROUTING ===")

fid, fname = feature_map.resolve_feature("SEC_IPO", "S-1")
check(f"SEC_IPO/S-1 -> Feature {fid} ({fname})", fid == 8,
      "an Unmapped alert cannot be muted or monitored per feature")
check("FMP_IPO/IPO_UPCOMING still -> Feature 8",
      feature_map.resolve_feature("FMP_IPO", "IPO_UPCOMING")[0] == 8)
check("Feature 8 is declared market-wide",
      feature_map.FEATURES[8].get("market_wide") is True,
      "a pre-listing registrant has no ticker to match a watchlist on")

check("SEC_IPO/S-1 with ticker='UNKNOWN' routes to an audience",
      delivery._is_market_wide({"source": "SEC_IPO", "filing_type": "S-1",
                                "ticker": "UNKNOWN"}),
      "would be settled as delivered without being sent")

# The guard that made this necessary must still hold for ordinary filings.
check("SEC_EDGAR/8-K with ticker='UNKNOWN' still does NOT broadcast",
      not delivery._is_market_wide({"source": "SEC_EDGAR", "filing_type": "8-K",
                                    "ticker": "UNKNOWN"}),
      "an unresolved 8-K reaching every subscriber is worse than reaching none")
check("a legacy SEC_EDGAR/S-1 row stays company-scoped",
      not delivery._is_market_wide({"source": "SEC_EDGAR", "filing_type": "S-1",
                                    "ticker": "UNKNOWN"}),
      "routing on filing_type would retroactively broadcast old sync-poller rows")

check("SEC_IPO jumps the news backlog",
      "SEC_IPO" in ai_pipeline.PRIORITY_SOURCES,
      "being early is the entire value of the EDGAR half")
check("S-1 uses the long-form ladder, not the Form 4 one",
      "S-1" not in ai_pipeline.SHORT_FORM_TYPES,
      "a registration statement is not a one-line insider trade")
check("S-1 is not treated as news for expiry",
      "S-1" not in ai_pipeline.NEWS_LIKE,
      "would expire on the 90-minute news window instead of the filing window")


# ── 3. The merge: FMP's calendar entry finds our captured S-1 ─────────────────
print("\n=== 3. FMP CALENDAR <-> CAPTURED S-1 MERGE ===")

CAPTURED = [{
    "company_name": "Northwind Robotics Inc",
    "filing_url": "https://www.sec.gov/Archives/edgar/data/1900001/x/idx.htm",
    "filed_at": "2026-07-02T10:00:00",
    "extra": {"cik": "0001900001",
              "sec_json": {"companyfacts": "https://data.sec.gov/x.json"}},
}]


class _Rows:
    def __init__(self, rows): self.rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def ilike(self, col, pattern):
        token = pattern.strip("%").lower()
        return _Rows([r for r in self.rows if token in (r["company_name"] or "").lower()])
    def execute(self):
        class R: pass
        R.data = self.rows
        return R()


ipo_poller.get_supabase = lambda: MagicMock(table=lambda n: _Rows(CAPTURED))

hit = ipo_poller.find_captured_s1("Northwind Robotics, Inc.")
check("punctuation/suffix differences still match", hit is not None,
      "'Northwind Robotics, Inc.' vs 'Northwind Robotics Inc'")
if hit:
    check("the merge carries the real EDGAR filing URL",
          hit["url"].startswith("https://www.sec.gov/Archives/"), hit["url"])
    check("the merge carries the CIK", hit["cik"] == "0001900001", str(hit))

check("an unrelated company does not match",
      ipo_poller.find_captured_s1("Cobalt Health Sciences Corp") is None,
      "a wrong match attaches the wrong prospectus to a live IPO alert")

# Name matching is the whole join, so its edges are worth pinning down.
for a, b, want in [
    ("Northwind Robotics Inc", "Northwind Robotics, Inc.", True),
    ("The Northwind Robotics Company", "Northwind Robotics", True),
    ("Northwind Robotics", "Northwind Robotics Holdings", True),
    ("Acme", "Acme Biosciences", False),          # single-token prefix
    ("Acme Bio", "Acme Robotics", False),
    ("", "Northwind Robotics", False),
]:
    got = ipo_poller._names_match(a, b)
    check(f"names_match({a!r}, {b!r}) is {want}", got is want, f"got {got}")


# ── 4. A merged FMP alert carries everything the merge is for ─────────────────
print("\n=== 4. THE MERGED ALERT ===")

ipo_poller.already_sent = lambda t, s: False
ipo_poller.edgar_link = MagicMock()
ipo_poller.edgar_link.find_filing_url = lambda *a, **k: None   # force the merge path

saved = {}
ipo_poller.save_alert = lambda t, su, i, e=None, link=None: saved.update(
    ticker=t, summary=su, impact=i, extra=e or {}, link=link)

ipo_poller.process_ipo({
    "symbol": "NWR", "company": "Northwind Robotics, Inc.", "exchange": "NASDAQ",
    "date": "2026-09-20", "shares": 10_000_000, "marketCap": 1_400_000_000,
    "priceRange": "18.00-20.00", "actions": "expected",
})

extra = saved.get("extra", {})
check("the alert links to the real filing, not a guessed directory",
      (saved.get("link") or "").startswith("https://www.sec.gov/Archives/"),
      f"got {saved.get('link')!r} — FMP's ticker lookup returned nothing here, "
      f"which is the normal case for a pre-listing symbol")
check("the merge is recorded as the link's provenance",
      extra.get("s1_source") == "edgar_capture", str(extra.get("s1_source")))
check("CIK reaches the alert", extra.get("cik") == "0001900001", str(extra.get("cik")))
check("SEC JSON links reach the alert (so payload_log gets a row)",
      bool(extra.get("sec_json")),
      "delivery._log_payload writes nothing without a payload or sec_json")

payload = extra.get("structured_payload") or {}
check("a structured `ipo` payload is attached", payload.get("type") == "ipo",
      str(payload)[:120])
timeline = payload.get("ipo_details", {}).get("general_details_timeline", {})
check("payload filing_date is the S-1's real date, not today",
      timeline.get("filing_date") == "2026-07-02",
      f"got {timeline.get('filing_date')!r} — s1_to_ipo used datetime.now()")
check("payload expected_listing_date comes from FMP's calendar",
      timeline.get("expected_listing_date") == "2026-09-20",
      str(timeline))
finance = payload.get("ipo_details", {}).get("financial_report", {})
check("payload carries FMP's deal terms (EDGAR has none of these)",
      "18.00" in str(finance.get("price_range")) and "10.0M" in str(finance.get("shares_offered")),
      str(finance))

check("a billion-dollar deal is HIGH impact", saved.get("impact") == "HIGH",
      str(saved.get("impact")))

print()
if failures:
    print(f"FAILURES ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL FEATURE 8 TESTS PASS")
