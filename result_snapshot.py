"""
result_snapshot.py
GQ FinXray US — Feature 3. Rewritten on FMP (was EODHD).

Triggered when a 10-Q or 10-K lands in raw_filings. Fetches structured
quarterly financials from FMP's income-statement endpoint (instead of
EODHD's monolithic /fundamentals/ payload) and builds a Result Snapshot
alert matching the original India FinXray style.

FMP returns quarterly statements as a flat list (newest first), each row
keyed by symbol/date/period/fiscalYear — simpler to walk than EODHD's
nested {yearly: {...}, quarterly: {...}} dict-of-dicts shape.
"""
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
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
def build_result_snapshot(ticker, form_type):
    quarters = fmp_client.get_income_statement(ticker, period="quarter", limit=8)
    if not quarters or len(quarters) < 2:
        print(f"[SNAPSHOT] Not enough quarterly data for {ticker}")
        return None

    quarters = sorted(quarters, key=lambda r: r.get("date", ""), reverse=True)
    latest = quarters[0]
    prev = quarters[1]
    yoy = quarters[4] if len(quarters) >= 5 else None

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

    try:
        rev_qoq = float(revenue_latest) / float(revenue_prev) - 1 if revenue_latest and revenue_prev else 0
        if abs(rev_qoq) > 0.15:
            impact = "HIGH"
        elif abs(rev_qoq) > 0.05:
            impact = "MEDIUM"
        else:
            impact = "LOW"
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
        }
    }


def store_snapshot_alert(snapshot):
    try:
        supabase.table("alerts").insert({
            "ticker": snapshot["ticker"],
            "summary": snapshot["summary"],
            "impact": snapshot["impact"],
            "source": "FMP",
            "filing_type": "RESULT_SNAPSHOT",
            "extra": tag_extra({
                "period": snapshot["period"],
                "form_type": snapshot["form_type"],
                "company_name": snapshot["company_name"],
                "metrics": snapshot["metrics"],
                "source": "FMP Income Statement"
            }, "FMP", "RESULT_SNAPSHOT"),
            "delivered": False
        }).execute()
        print(f"[SNAPSHOT] {snapshot['impact']} — {snapshot['ticker']} {snapshot['period']} stored")
    except Exception as e:
        print(f"[SNAPSHOT] Failed to store: {e}")


def process_pending_snapshots():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking for pending Result Snapshots...")
    try:
        result = supabase.table("raw_filings") \
            .select("*") \
            .in_("filing_type", ["10-Q", "10-K"]) \
            .eq("status", "PROCESSED") \
            .eq("extra->>needs_result_snapshot", "true") \
            .limit(10) \
            .execute()

        filings = result.data
        if not filings:
            result2 = supabase.table("raw_filings") \
                .select("*") \
                .in_("filing_type", ["10-Q", "10-K"]) \
                .eq("status", "PENDING") \
                .limit(10) \
                .execute()
            filings = result2.data

        if not filings:
            print("[SNAPSHOT] No pending 10-Q/10-K filings found.")
            return

        print(f"[SNAPSHOT] Found {len(filings)} filing(s) to process.")

        for filing in filings:
            ticker = filing.get("ticker", "UNKNOWN")
            form_type = filing.get("filing_type", "10-Q")
            if ticker == "UNKNOWN":
                continue

            snapshot = build_result_snapshot(ticker, form_type)
            if snapshot:
                store_snapshot_alert(snapshot)
                try:
                    extra = filing.get("extra") or {}
                    extra["snapshot_done"] = True
                    supabase.table("raw_filings").update({"extra": extra}).eq("id", filing["id"]).execute()
                except Exception:
                    pass

            time.sleep(1)

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
