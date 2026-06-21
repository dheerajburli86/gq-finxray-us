"""
Result Snapshot — GQ FinXray US
Triggered when a 10-Q or 10-K lands in raw_filings.
Fetches structured financial data from EODHD fundamentals API
and builds a Result Snapshot alert matching India FinXray style.
"""
import os
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
EODHD_API_KEY = os.getenv("EODHD_API_KEY")
BASE_URL = "https://eodhd.com/api"

# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_value(val, prefix="$", suffix=""):
    """Format large numbers into readable strings."""
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
    except:
        return "N/A"

def fmt_pct(val):
    """Format percentage values."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val)*100:.1f}%"
    except:
        return "N/A"

def change_arrow(current, previous):
    """Return up/down arrow + percentage change between two values."""
    try:
        curr = float(current)
        prev = float(previous)
        if prev == 0:
            return "N/A"
        pct = ((curr - prev) / abs(prev)) * 100
        arrow = "▲" if pct >= 0 else "▼"
        return f"{arrow} {abs(pct):.1f}%"
    except:
        return "N/A"

def snapshot_already_sent(ticker, period):
    """Check if we already sent a snapshot for this ticker + period."""
    try:
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "RESULT_SNAPSHOT") \
            .eq("extra->>period", str(period)) \
            .execute()
        return len(result.data) > 0
    except:
        return False

# ── EODHD Fundamentals Fetch ──────────────────────────────────────────────────
def fetch_fundamentals(ticker):
    """Fetch full fundamentals from EODHD for a ticker."""
    try:
        url = f"{BASE_URL}/fundamentals/{ticker}.US"
        r = requests.get(url, params={"api_token": EODHD_API_KEY, "fmt": "json"}, timeout=20)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[SNAPSHOT] EODHD fundamentals returned {r.status_code} for {ticker}")
            return None
    except Exception as e:
        print(f"[SNAPSHOT] Failed to fetch fundamentals for {ticker}: {e}")
        return None

# ── Result Snapshot Builder ───────────────────────────────────────────────────
def build_result_snapshot(ticker, form_type):
    """
    Build a Result Snapshot alert from EODHD fundamentals.
    Covers: Revenue, Gross Profit, Operating Income, EBITDA, Net Income,
    EPS — with QoQ and YoY comparisons.
    """
    data = fetch_fundamentals(ticker)
    if not data:
        return None

    financials = data.get("Financials", {})
    income = financials.get("Income_Statement", {})
    highlights = data.get("Highlights", {})
    general = data.get("General", {})

    company_name = general.get("Name", ticker)
    currency = general.get("CurrencyCode", "USD")

    # Get quarterly data
    quarterly = income.get("quarterly", {})
    if not quarterly:
        print(f"[SNAPSHOT] No quarterly data for {ticker}")
        return None

    sorted_quarters = sorted(quarterly.keys(), reverse=True)
    if len(sorted_quarters) < 2:
        print(f"[SNAPSHOT] Not enough quarters for {ticker}")
        return None

    # Latest quarter vs previous quarter (QoQ)
    latest_q = sorted_quarters[0]
    prev_q = sorted_quarters[1]
    latest = quarterly[latest_q]
    prev = quarterly[prev_q]

    # Same quarter last year (YoY) — find it
    yoy_q = None
    yoy = None
    if len(sorted_quarters) >= 5:
        yoy_q = sorted_quarters[4]
        yoy = quarterly[yoy_q]

    period = latest_q

    if snapshot_already_sent(ticker, period):
        print(f"[SNAPSHOT] Already sent for {ticker} {period}")
        return None

    # Extract metrics
    revenue_latest = latest.get("totalRevenue")
    revenue_prev = prev.get("totalRevenue")
    revenue_yoy = yoy.get("totalRevenue") if yoy else None

    gross_profit_latest = latest.get("grossProfit")
    gross_profit_prev = prev.get("grossProfit")

    op_income_latest = latest.get("operatingIncome")
    op_income_prev = prev.get("operatingIncome")

    ebitda_latest = latest.get("ebitda")
    ebitda_prev = prev.get("ebitda")

    net_income_latest = latest.get("netIncome")
    net_income_prev = prev.get("netIncome")
    net_income_yoy = yoy.get("netIncome") if yoy else None

    eps_latest = latest.get("dilutedEPS")
    eps_prev = prev.get("dilutedEPS")
    eps_yoy = yoy.get("dilutedEPS") if yoy else None

    # Margin calculations
    def margin(income_val, revenue_val):
        try:
            return (float(income_val) / float(revenue_val)) * 100
        except:
            return None

    gross_margin = margin(gross_profit_latest, revenue_latest)
    op_margin = margin(op_income_latest, revenue_latest)
    net_margin = margin(net_income_latest, revenue_latest)

    # Get YoY revenue growth from highlights if available
    rev_growth_yoy = highlights.get("QuarterlyRevenueGrowthYOY")
    earn_growth_yoy = highlights.get("QuarterlyEarningsGrowthYOY")

    # Build the summary text
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

        f"EPS: {eps_latest} | QoQ: {change_arrow(eps_latest, eps_prev)}" +
        (f" | YoY: {change_arrow(eps_latest, eps_yoy)}" if eps_yoy else ""),
    ]

    summary = "\n".join([l for l in lines if l is not None])

    # Determine impact
    try:
        rev_qoq = float(revenue_latest) / float(revenue_prev) - 1 if revenue_latest and revenue_prev else 0
        if abs(rev_qoq) > 0.15:
            impact = "HIGH"
        elif abs(rev_qoq) > 0.05:
            impact = "MEDIUM"
        else:
            impact = "LOW"
    except:
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
            "eps": str(eps_latest) if eps_latest else "N/A",
            "gross_margin": f"{gross_margin:.1f}%" if gross_margin else "N/A",
            "net_margin": f"{net_margin:.1f}%" if net_margin else "N/A",
            "period": period
        }
    }

def store_snapshot_alert(snapshot):
    """Store Result Snapshot as a direct alert."""
    try:
        supabase.table("alerts").insert({
            "ticker": snapshot["ticker"],
            "summary": snapshot["summary"],
            "impact": snapshot["impact"],
            "source": "EODHD",
            "filing_type": "RESULT_SNAPSHOT",
            "extra": {
                "period": snapshot["period"],
                "form_type": snapshot["form_type"],
                "company_name": snapshot["company_name"],
                "metrics": snapshot["metrics"],
                "source": "EODHD Fundamentals"
            },
            "delivered": False
        }).execute()
        print(f"[SNAPSHOT] {snapshot['impact']} — {snapshot['ticker']} {snapshot['period']} stored")
    except Exception as e:
        print(f"[SNAPSHOT] Failed to store: {e}")

# ── Main processor ────────────────────────────────────────────────────────────
def process_pending_snapshots():
    """
    Find 10-Q and 10-K filings that need Result Snapshots and process them.
    Runs every 30 minutes from main.py scheduler.
    """
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
            # Also check PENDING ones that haven't been processed yet
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

                # Mark as snapshot done so we don't re-process
                try:
                    extra = filing.get("extra") or {}
                    extra["snapshot_done"] = True
                    supabase.table("raw_filings") \
                        .update({"extra": extra}) \
                        .eq("id", filing["id"]) \
                        .execute()
                except:
                    pass

            time.sleep(1)  # Rate limit EODHD calls

    except Exception as e:
        print(f"[SNAPSHOT] Processing failed: {e}")

if __name__ == "__main__":
    # Test with a known ticker
    print("Testing Result Snapshot with AAPL...")
    snapshot = build_result_snapshot("AAPL", "10-Q")
    if snapshot:
        print("\n" + "="*60)
        print(snapshot["summary"])
        print("="*60)
        print(f"Impact: {snapshot['impact']}")
    else:
        print("No snapshot generated.")
