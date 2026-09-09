"""
result_snapshot.py
GQ FinXray US — Feature 3. SEC XBRL primary, FMP fallback.

Triggered when a 10-Q or 10-K lands in raw_filings. Pulls quarterly
financials from SEC XBRL companyfacts first (sec_financials.py) and only
falls back to FMP's income-statement endpoint when SEC has nothing
usable, then builds a Result Snapshot alert matching the original India
FinXray style.

Both sources return quarterly rows as a flat list (newest first) keyed by
symbol/date/period/fiscalYear, so the builder below is source-agnostic.
"""
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
import sec_financials
import gquants_format_converter as gq_fmt
from feature_map import tag_extra

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_value(val, prefix="$", suffix=""):
    if val is None:
        return "N/A"
    try:
        val = float(val)
        if abs(val) >= 1_000_000_000:
            return f"{prefix}{val/1_000_000_000:.2f}B{suffix}"
        elif abs(val) >= 1_000_000:
            return f"{prefix}{val/1_000_000:.2f}M{suffix}"
        elif abs(val) >= 1_000:
            return f"{prefix}{val/1_000:.2f}K{suffix}"
        else:
            return f"{prefix}{val:.2f}{suffix}"
    except Exception:
        return "N/A"


def change_arrow(current, previous):
    try:
        curr = float(current)
        prev = float(previous)
        if prev == 0:
            return "N/A"
        pct = ((curr - prev) / abs(prev)) * 100
        arrow = "▲" if pct >= 0 else "▼"
        return f"{arrow} {abs(pct):.1f}%"
    except Exception:
        return "N/A"


def g(row, *keys, default=None):
    """Try several possible key spellings — FMP has renamed fields across versions."""
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def snapshot_already_sent(ticker, period):
    try:
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "RESULT_SNAPSHOT") \
            .eq("extra->>period", str(period)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


# ── Result Snapshot Builder ───────────────────────────────────────────────────
def build_result_snapshot(ticker, form_type, cik=None):
    """
    SEC XBRL first, FMP second.

    SEC publishes the same facts FMP resells, without the ingestion lag
    and without the per-call cost, so companyfacts is the primary source
    whenever the filing gave us a CIK. FMP is the fallback for filers
    whose XBRL is missing, malformed, or too sparse to compare quarters.
    """
    quarters = []
    if cik:
        try:
            quarters = sec_financials.get_income_statement_sync(cik, limit=8)
            if quarters and len(quarters) >= 2:
                print(f"[SNAPSHOT] {ticker}: {len(quarters)} quarters from SEC XBRL")
            else:
                quarters = []
        except Exception as e:
            print(f"[SNAPSHOT] SEC XBRL failed for {ticker}: {e}")
            quarters = []

    if not quarters:
        print(f"[SNAPSHOT] {ticker}: falling back to FMP")
        quarters = fmp_client.get_income_statement(ticker, period="quarter", limit=8)

    if not quarters or len(quarters) < 2:
        print(f"[SNAPSHOT] Not enough quarterly data for {ticker}")
        return None

    quarters = sorted(quarters, key=lambda r: r.get("date", ""), reverse=True)
    data_source = quarters[0].get("_source") or "FMP"
    latest = quarters[0]
    prev = quarters[1]
    yoy = quarters[4] if len(quarters) >= 5 else None

    # SEC-over-FMP means SEC over FMP everywhere in this function, not just
    # for the quarters -- calling fmp_client.get_profile() unconditionally
    # here spent one FMP call per snapshot purely for a company name, even
    # when companyfacts (already fetched and cached above) carries the same
    # name under "entityName". Only fall back to FMP when SEC XBRL wasn't
    # the source or didn't have a usable name.
    company_name = None
    if data_source == "SEC_XBRL" and cik:
        company_name = sec_financials.get_company_name(cik)
    if not company_name:
        profile = fmp_client.get_profile(ticker)
        company_name = (profile or {}).get("companyName", ticker)

    # g() tries several key spellings since FMP has renamed fields across API
    # versions (older v3-style responses used all-lowercase keys; current
    # /stable responses use camelCase) — applied to every financial-statement
    # field read here, not just EPS, so a version mismatch degrades to "N/A"
    # instead of a KeyError-shaped None silently propagating into the alert.
    period = f"{g(latest, 'period', default='')} FY{g(latest, 'fiscalYear', 'calendarYear', default='')}".strip()
    if not period.strip():
        period = latest.get("date", "latest quarter")

    if snapshot_already_sent(ticker, period):
        print(f"[SNAPSHOT] Already sent for {ticker} {period}")
        return None

    revenue_latest = g(latest, "revenue", "totalRevenue")
    revenue_prev = g(prev, "revenue", "totalRevenue")
    revenue_yoy = g(yoy, "revenue", "totalRevenue") if yoy else None

    gross_profit_latest = g(latest, "grossProfit", "grossprofit")
    gross_profit_prev = g(prev, "grossProfit", "grossprofit")

    op_income_latest = g(latest, "operatingIncome", "operatingincome")
    op_income_prev = g(prev, "operatingIncome", "operatingincome")

    ebitda_latest = g(latest, "ebitda", "EBITDA")
    ebitda_prev = g(prev, "ebitda", "EBITDA")

    net_income_latest = g(latest, "netIncome", "netincome")
    net_income_prev = g(prev, "netIncome", "netincome")
    net_income_yoy = g(yoy, "netIncome", "netincome") if yoy else None

    eps_latest = g(latest, "epsDiluted", "epsdiluted", "eps")
    eps_prev = g(prev, "epsDiluted", "epsdiluted", "eps")
    eps_yoy = g(yoy, "epsDiluted", "epsdiluted", "eps") if yoy else None

    def margin(income_val, revenue_val):
        try:
            return (float(income_val) / float(revenue_val)) * 100
        except Exception:
            return None

    gross_margin = margin(gross_profit_latest, revenue_latest)
    op_margin = margin(op_income_latest, revenue_latest)
    net_margin = margin(net_income_latest, revenue_latest)
    # EPS is a per-share decimal (e.g. 1.25), not an aggregate dollar amount —
    # fmt_value()'s B/M/K suffix logic is for totals like revenue/net income,
    # so it was deliberately not used here. Instead, just guarantee a plain
    # "N/A" instead of the literal string "None" leaking into a customer-
    # facing alert when a field name doesn't resolve.
    try:
        eps_display = f"${float(eps_latest):.2f}" if eps_latest is not None else "N/A"
    except Exception:
        eps_display = "N/A"

    lines = [
        f"{company_name} has published financial results for {period}.",
        "",
        f"📊 Key Highlights:",
        f"Revenue: {fmt_value(revenue_latest)} | QoQ: {change_arrow(revenue_latest, revenue_prev)}" +
        (f" | YoY: {change_arrow(revenue_latest, revenue_yoy)}" if revenue_yoy else ""),

        f"Gross Profit: {fmt_value(gross_profit_latest)}" +
        (f" | Margin: {gross_margin:.1f}%" if gross_margin else "") +
        f" | QoQ: {change_arrow(gross_profit_latest, gross_profit_prev)}",

        f"Operating Income: {fmt_value(op_income_latest)}" +
        (f" | Margin: {op_margin:.1f}%" if op_margin else "") +
        f" | QoQ: {change_arrow(op_income_latest, op_income_prev)}",

        f"EBITDA: {fmt_value(ebitda_latest)} | QoQ: {change_arrow(ebitda_latest, ebitda_prev)}",

        f"Net Income: {fmt_value(net_income_latest)}" +
        (f" | Margin: {net_margin:.1f}%" if net_margin else "") +
        f" | QoQ: {change_arrow(net_income_latest, net_income_prev)}" +
        (f" | YoY: {change_arrow(net_income_latest, net_income_yoy)}" if net_income_yoy else ""),

        f"EPS: {eps_display} | QoQ: {change_arrow(eps_latest, eps_prev)}" +
        (f" | YoY: {change_arrow(eps_latest, eps_yoy)}" if eps_yoy else ""),
    ]

    summary = "\n".join([l for l in lines if l is not None])

    # Floor is MEDIUM, not LOW. A published quarterly result is material to
    # anyone holding the stock by definition, and the default user preference
    # (min_impact=MEDIUM in delivery.py) drops LOW outright — so grading a flat
    # quarter as LOW meant the Result Snapshot was built, stored, and then
    # silently filtered out of every subscriber's feed.
    try:
        rev_qoq = float(revenue_latest) / float(revenue_prev) - 1 if revenue_latest and revenue_prev else 0
        impact = "HIGH" if abs(rev_qoq) > 0.15 else "MEDIUM"
    except Exception:
        impact = "MEDIUM"

    return {
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "period": period,
        "form_type": form_type,
        "company_name": company_name,
        "metrics": {
            "revenue": fmt_value(revenue_latest),
            "gross_profit": fmt_value(gross_profit_latest),
            "net_income": fmt_value(net_income_latest),
            "eps": eps_display,
            "gross_margin": f"{gross_margin:.1f}%" if gross_margin else "N/A",
            "net_margin": f"{net_margin:.1f}%" if net_margin else "N/A",
            "period": period
        },
        # Carried for the structured-payload builder in store_snapshot_alert().
        # Without these the builder receives an empty quarter list and silently
        # returns {}, which is exactly how the payload feature shipped broken.
        "quarters": quarters,
        "cik": cik or "",
        "data_source": data_source
    }


def store_snapshot_alert(snapshot):
    # Label the alert with the source the numbers actually came from, so a
    # silent slide back to FMP is visible in the channel and in Supabase
    # rather than hidden behind a hardcoded "FMP".
    src = snapshot.get("data_source") or "FMP"
    label = ("SEC XBRL companyfacts" if src == "SEC_XBRL"
             else "FMP Income Statement")
    
    # Build structured GQuants payload for the frontend
    structured_payload = gq_fmt.xbrl_to_financial_results(
        ticker=snapshot["ticker"],
        cik=snapshot.get("cik", ""),
        quarters=snapshot.get("quarters", []),
        company_name=snapshot["company_name"],
        form_type=snapshot["form_type"]
    )
    
    try:
        extra = tag_extra({
            "period": snapshot["period"],
            "form_type": snapshot["form_type"],
            "company_name": snapshot["company_name"],
            "metrics": snapshot["metrics"],
            "source": label,
            "structured_payload": structured_payload,
            "data_source": src,
        }, src, "RESULT_SNAPSHOT")
        
        supabase.table("alerts").insert({
            "ticker": snapshot["ticker"],
            "summary": snapshot["summary"],
            "impact": snapshot["impact"],
            "source": src,
            "filing_type": "RESULT_SNAPSHOT",
            "extra": extra,
            "delivered": False
        }).execute()
        print(f"[SNAPSHOT] {snapshot['impact']} — {snapshot['ticker']} {snapshot['period']} stored (source: {label})")
    except Exception as e:
        print(f"[SNAPSHOT] Failed to store: {e}")


# An 8-K carrying Item 2.02 IS the quarterly earnings release — filed by the
# company itself the moment it announces, hours ahead of any vendor and typically
# WEEKS ahead of the 10-Q. edgar_poller_async already detects it and sets
# extra["is_earnings_release"], but nothing consumed that flag, so the fastest
# result signal SEC publishes was tagged and then ignored while Feature 3 waited
# for the slow filing. It is a first-class trigger here now.
SNAPSHOT_FORM_TYPES = ["10-Q", "10-K", "8-K"]


def _mark_done(filing):
    """Stamp the row so the next cycle does not re-fetch financials for it."""
    try:
        extra = dict(filing.get("extra") or {})
        extra["snapshot_done"] = True
        supabase.table("raw_filings").update({"extra": extra}).eq("id", filing["id"]).execute()
    except Exception as e:
        print(f"[SNAPSHOT] Could not mark {filing.get('ticker')} done: {e}")


def process_pending_snapshots():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for pending Result Snapshots...")
    try:
        # THE STARVATION BUG THIS FIXES. The old query selected PROCESSED 10-Q/10-K
        # rows with needs_result_snapshot=true and NEVER filtered on snapshot_done,
        # which this function itself writes. So every 30 seconds it re-selected the
        # same rows and re-fetched SEC XBRL / FMP financials for all of them. Worse,
        # `.limit(10)` with no `.order()` meant that once ten already-processed rows
        # existed, PostgREST kept returning those same ten and a newly filed 10-Q
        # could never be reached — Feature 3 starved permanently.
        base = (supabase.table("raw_filings")
                .select("*")
                .in_("filing_type", SNAPSHOT_FORM_TYPES)
                .eq("status", "PROCESSED")
                .is_("extra->>snapshot_done", "null")
                .order("filed_at", desc=True)
                .limit(10))
        filings = base.execute().data or []

        # 8-Ks only qualify when they actually carry Item 2.02.
        filings = [f for f in filings
                   if f.get("filing_type") != "8-K"
                   or (f.get("extra") or {}).get("is_earnings_release")]

        if not filings:
            print("[SNAPSHOT] No pending filings found.")
            return

        print(f"[SNAPSHOT] Found {len(filings)} filing(s) to process.")

        for filing in filings:
            ticker = filing.get("ticker", "UNKNOWN")
            form_type = filing.get("filing_type", "10-Q")
            if ticker == "UNKNOWN":
                _mark_done(filing)
                continue

            cik = (filing.get("extra") or {}).get("cik")
            snapshot = build_result_snapshot(ticker, form_type, cik=cik)
            if snapshot:
                store_snapshot_alert(snapshot)
            # Marked done either way. A filer whose XBRL is unusable will still be
            # unusable in thirty seconds, and retrying it forever is exactly what
            # blocked the queue before.
            _mark_done(filing)

    except Exception as e:
        print(f"[SNAPSHOT] Processing failed: {e}")


if __name__ == "__main__":
    print("Testing Result Snapshot with AAPL...")
    snapshot = build_result_snapshot("AAPL", "10-Q")
    if snapshot:
        print("\n" + "=" * 60)
        print(snapshot["summary"])
        print("=" * 60)
        print(f"Impact: {snapshot['impact']}")
    else:
        print("No snapshot generated.")
