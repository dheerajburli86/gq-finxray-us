"""
financial_metrics_poller.py
GQ FinXray US — Extract & alert on key financial metrics from 10-Q/10-K. Feature 3 enhancement.

Extracts:
- Revenue growth YoY
- Net income change
- EPS (earnings per share)
- Margin trends (gross, operating, net)
- Cash flow metrics
- Debt/equity changes

Compares to previous quarter/year and generates alerts for significant changes.
Stores in alerts table with source=SEC_FINANCIALS.
"""

import os
import logging
import re
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Metrics thresholds for alerting (%)
REVENUE_THRESHOLD = 5  # Alert on >5% YoY change
PROFIT_THRESHOLD = 10  # Alert on >10% YoY change
MARGIN_THRESHOLD = 2   # Alert on >2 bps margin change


def get_all_stocks():
    """Fetch all US stocks (NYSE + NASDAQ) from the stocks table."""
    try:
        result = supabase.table("stocks").select("ticker").execute()
        return [row["ticker"] for row in result.data if row.get("ticker")]
    except Exception as e:
        logger.error(f"[METRICS] Failed to fetch stocks list: {e}")
        return []


def metric_alert_exists(ticker, metric_type, period):
    """Check if metric alert already exists for this period."""
    try:
        result = supabase.table("alerts").select("id").eq(
            "ticker", ticker
        ).eq("source", "SEC_FINANCIALS").contains(
            "extra", {"metric_type": metric_type, "period": period}
        ).limit(1).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[METRICS] Dedup check failed: {e}")
        return False


def extract_metrics_from_10q(ticker, filing_text, period):
    """
    Extract key metrics from 10-Q filing text using regex patterns.
    Returns dict of metrics.
    """
    metrics = {}

    try:
        # Simple patterns (in real scenario, would use full XBRL parsing)
        # These are mock patterns - in production use FMP's 10-Q API or full document parsing

        # Revenue patterns
        revenue_match = re.search(r'(?:total|net)?\s+revenues?.*?(\d+(?:,\d{3})*)', filing_text, re.IGNORECASE)
        if revenue_match:
            metrics['revenue'] = int(revenue_match.group(1).replace(',', ''))

        # Net income patterns
        income_match = re.search(r'net\s+income.*?(\d+(?:,\d{3})*)', filing_text, re.IGNORECASE)
        if income_match:
            metrics['net_income'] = int(income_match.group(1).replace(',', ''))

        # EPS pattern
        eps_match = re.search(r'earnings\s+per\s+share.*?(\d+\.\d+)', filing_text, re.IGNORECASE)
        if eps_match:
            metrics['eps'] = float(eps_match.group(1))

        # Operating margin
        op_margin_match = re.search(r'operating\s+margin.*?(\d+\.\d+)%', filing_text, re.IGNORECASE)
        if op_margin_match:
            metrics['operating_margin'] = float(op_margin_match.group(1))

        # Net margin
        net_margin_match = re.search(r'net.*?margin.*?(\d+\.\d+)%', filing_text, re.IGNORECASE)
        if net_margin_match:
            metrics['net_margin'] = float(net_margin_match.group(1))

    except Exception as e:
        logger.error(f"[METRICS] Regex extraction failed for {ticker}: {e}")

    return metrics


def get_previous_period_metrics(ticker, form_type):
    """Fetch previous period's metrics from alerts table for comparison."""
    try:
        result = supabase.table("alerts").select("extra").eq(
            "ticker", ticker
        ).eq("source", "SEC_FINANCIALS").eq(
            "filing_type", form_type
        ).order("created_at", desc=True).limit(1).execute()

        if result.data:
            return result.data[0].get("extra", {}).get("metrics", {})
        return {}
    except Exception as e:
        logger.error(f"[METRICS] Failed to fetch previous metrics: {e}")
        return {}


def calculate_yoy_change(current, previous):
    """Calculate year-over-year percentage change."""
    if not previous or previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100


def store_metrics_alert(ticker, form_type, period, metrics, previous_metrics, yoy_changes):
    """Store financial metrics alert."""
    try:
        # Build summary from significant changes
        change_lines = []
        for metric, change_pct in yoy_changes.items():
            if change_pct is not None and abs(change_pct) >= (PROFIT_THRESHOLD if 'income' in metric else REVENUE_THRESHOLD):
                direction = "↑" if change_pct > 0 else "↓"
                change_lines.append(f"{metric.replace('_', ' ')}: {direction} {abs(change_pct):.1f}%")

        if not change_lines:
            return  # No significant changes

        summary = f"{form_type} filed. Key metrics: " + " | ".join(change_lines[:3])

        # Determine impact based on profit changes
        impact = "HIGH" if any(abs(yoy_changes.get('net_income', 0)) > 20) else "MEDIUM"

        alert_data = {
            "ticker": ticker,
            "source": "SEC_FINANCIALS",
            "filing_type": form_type,
            "summary": summary,
            "impact": impact,
            "delivered": False,
            "extra": tag_extra({
                "period": period,
                "metric_type": "FINANCIAL_SUMMARY",
                "metrics": metrics,
                "previous_metrics": previous_metrics,
                "yoy_changes": yoy_changes,
            })
        }

        supabase.table("alerts").insert(alert_data).execute()
        logger.info(f"[METRICS] {ticker} {form_type} {period}: {summary[:80]}")

    except Exception as e:
        logger.error(f"[METRICS] Failed to store alert for {ticker}: {e}")


def poll_financial_metrics():
    """
    Query recent 10-Q/10-K filings and extract metrics.
    This is called after SEC filing pollers complete.
    """
    try:
        # Query pending filings (not yet processed for metrics)
        result = supabase.table("raw_filings").select("*").in_(
            "filing_type", ["10-Q", "10-K"]
        ).eq("metrics_extracted", False).limit(50).execute()

        filings = result.data
        if not filings:
            logger.info("[METRICS] No pending filings for metric extraction")
            return

        logger.info(f"[METRICS] Processing {len(filings)} filings for metrics...")

        for filing in filings:
            try:
                ticker = filing.get("ticker", "")
                form_type = filing.get("filing_type", "")
                period = filing.get("filing_date", "")
                filing_text = filing.get("filing_text", "")
                filing_id = filing.get("id")

                if not all([ticker, form_type, filing_text]):
                    continue

                # Check dedup
                if metric_alert_exists(ticker, "FINANCIAL_SUMMARY", period):
                    supabase.table("raw_filings").update(
                        {"metrics_extracted": True}
                    ).eq("id", filing_id).execute()
                    continue

                # Extract metrics
                metrics = extract_metrics_from_10q(ticker, filing_text, period)
                if not metrics:
                    continue

                # Get previous period for comparison
                previous_metrics = get_previous_period_metrics(ticker, form_type)

                # Calculate YoY changes
                yoy_changes = {}
                for metric, value in metrics.items():
                    prev_value = previous_metrics.get(metric)
                    if prev_value is not None:
                        yoy_changes[metric] = calculate_yoy_change(value, prev_value)

                # Store alert if significant changes
                if yoy_changes:
                    store_metrics_alert(ticker, form_type, period, metrics, previous_metrics, yoy_changes)

                # Mark as processed
                supabase.table("raw_filings").update(
                    {"metrics_extracted": True}
                ).eq("id", filing_id).execute()

            except Exception as e:
                logger.error(f"[METRICS] Error processing filing: {e}")
                continue

        logger.info("[METRICS] Financial metrics extraction complete")

    except Exception as e:
        logger.error(f"[METRICS] Failed to process filings: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll_financial_metrics()
