#!/usr/bin/env python3
"""
test_xbrl_link_pipeline.py

Standalone tester: SEC XBRL → Financial Results (fr format) → GQuants Link → Formatted Alert

Run with:
    python test_xbrl_link_pipeline.py AAPL 320193
    python test_xbrl_link_pipeline.py MSFT 0000789019
    python test_xbrl_link_pipeline.py TSLA 1018724

Shows:
    1. XBRL raw data from SEC companyfacts
    2. Converted to GQuants "fr" format (readable financial results)
    3. GQuants link generation
    4. Final formatted alert with link embedded
"""

import sys
import json
import os
from unittest.mock import MagicMock
from datetime import datetime
from urllib.parse import urlencode

# Mock expensive deps that aren't needed for this test
sys.modules["supabase"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

import sec_financials
import gquants_format_converter as gq_fmt

def get_xbrl_data(ticker: str, cik: str):
    """Fetch SEC XBRL quarterly financial data."""
    print(f"\n{'='*70}")
    print(f"STEP 1: Fetch SEC XBRL Data")
    print(f"{'='*70}")
    print(f"Ticker: {ticker}")
    print(f"CIK: {cik}")

    quarters = sec_financials.get_income_statement_sync(cik, limit=8)

    if not quarters:
        print("❌ No XBRL data found. Falling back to FMP would happen here.")
        return None

    print(f"✅ Retrieved {len(quarters)} quarters from SEC XBRL")
    print(f"\nLatest 2 quarters (raw):")
    for i, q in enumerate(quarters[:2]):
        print(f"\n  Quarter {i+1} ({q.get('date')}):")
        print(f"    Revenue: {q.get('revenue'):,.0f}")
        print(f"    Net Income: {q.get('netIncome'):,.0f}")
        print(f"    EPS: {q.get('epsDiluted'):.2f}")
        print(f"    Source: {q.get('_source')}")

    return quarters


def convert_to_fr_format(ticker: str, cik: str, quarters, company_name: str = None):
    """Convert raw XBRL to GQuants 'fr' format (financial results)."""
    print(f"\n{'='*70}")
    print(f"STEP 2: Convert to GQuants 'fr' Format")
    print(f"{'='*70}")

    if not company_name:
        company_name = sec_financials.get_company_name(cik) or ticker

    payload = gq_fmt.xbrl_to_financial_results(
        ticker=ticker,
        cik=cik,
        quarters=quarters,
        company_name=company_name,
        form_type="10-Q"
    )

    if not payload:
        print("❌ Failed to convert to fr format")
        return None

    print(f"✅ Converted to 'fr' format")
    print(f"\nPayload structure:")
    print(f"  type: {payload.get('type')}")
    print(f"  name: {payload.get('name')}")
    print(f"  source: {payload.get('_source')}")

    print(f"\nFinancial overview (readable):")
    for row in payload.get('financial_overview', [])[:5]:  # Show first 5 metrics
        latest = row.get('latest_qtr_value', '-')
        prev = row.get('prev_qtr_value', '-')
        change = row.get('qoq_change', '-')
        item = row.get('item', '')
        print(f"  {item:30} {latest:>12} | Prev: {prev:>12} | {change}")

    return payload


def generate_frontend_link(payload: dict, alert_id: str = "test-alert-123"):
    """Generate the GQuants frontend link."""
    print(f"\n{'='*70}")
    print(f"STEP 3: Generate GQuants Frontend Link")
    print(f"{'='*70}")

    # Set a fake base URL for demo (in production this comes from env)
    os.environ["GQUANTS_ALERT_BASE_URL"] = "https://app.gquants.com/alerts"

    link = gq_fmt.make_frontend_link(payload, alert_id)

    print(f"Alert ID: {alert_id}")
    print(f"Base URL: https://app.gquants.com/alerts")
    print(f"\n✅ Frontend link generated:")
    print(f"   {link}")

    return link


def format_alert_with_link(payload: dict, link: str, summary_snippet: str = None):
    """Show how the alert looks with the link embedded."""
    print(f"\n{'='*70}")
    print(f"STEP 4: Final Formatted Alert (as sent to Telegram)")
    print(f"{'='*70}")

    ticker = payload.get("general_information", {}).get("ticker", "UNKNOWN")
    company = payload.get("name", "Company")

    # Sample summary that would come from the AI pipeline
    if not summary_snippet:
        summary_snippet = (
            f"{company} reported strong financials this quarter. "
            f"Revenue grew {payload.get('financial_overview', [{}])[0].get('qoq_change', 'N/A')}. "
            f"Net margins remained healthy."
        )

    alert_text = f"""📊 *Quarterly Results — ${ticker}*

📈 *Stock:* {ticker} 🟢 ${payload.get('general_information', {}).get('ticker', 'UNKNOWN')} (+0.5%)

{summary_snippet}

🔗 [View full report on GQuants]({link})

_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._
_Disclaimer: gquants.com/disclaimer_

📊 Manage your AI-powered watchlist: https://gquants.com/build

🏷 Feature 3 · SEC XBRL companyfacts"""

    print(alert_text)
    print(f"\n✅ Link is embedded and clickable in the alert")


def show_payload_json(payload: dict):
    """Show the raw JSON that gets stored in the alert."""
    print(f"\n{'='*70}")
    print(f"PAYLOAD JSON (stored in alert.extra['structured_payload'])")
    print(f"{'='*70}")

    # Show a truncated version (first 1000 chars)
    pretty = json.dumps(payload, indent=2)
    if len(pretty) > 1200:
        print(pretty[:1200])
        print(f"\n... ({len(pretty) - 1200} more characters)")
    else:
        print(pretty)


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_xbrl_link_pipeline.py TICKER [CIK]")
        print("\nExamples:")
        print("  python test_xbrl_link_pipeline.py AAPL 320193")
        print("  python test_xbrl_link_pipeline.py MSFT 0000789019")
        print("  python test_xbrl_link_pipeline.py TSLA 1018724")
        sys.exit(1)

    ticker = sys.argv[1].upper()
    cik = sys.argv[2] if len(sys.argv) > 2 else None

    if not cik:
        print(f"❌ CIK required for {ticker}")
        sys.exit(1)

    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║         XBRL → Readable Format → GQuants Link Test               ║
║                                                                  ║
║ This shows the full pipeline:                                   ║
║ 1. Fetch SEC XBRL data                                          ║
║ 2. Convert to 'fr' format (matches schema in docs)             ║
║ 3. Generate GQuants frontend link                               ║
║ 4. Show formatted alert with embedded link                      ║
╚══════════════════════════════════════════════════════════════════╝
""")

    # Step 1: Fetch XBRL data
    quarters = get_xbrl_data(ticker, cik)
    if not quarters:
        return

    # Step 2: Convert to fr format
    payload = convert_to_fr_format(ticker, cik, quarters)
    if not payload:
        return

    # Step 3: Generate link
    alert_id = f"alert-{ticker}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    link = generate_frontend_link(payload, alert_id)

    # Step 4: Show formatted alert
    format_alert_with_link(payload, link)

    # Bonus: Show raw payload JSON
    show_payload_json(payload)

    print(f"\n{'='*70}")
    print("✅ PIPELINE COMPLETE")
    print(f"{'='*70}")
    print(f"""
Key outputs:
  • XBRL source: SEC companyfacts API
  • Format: 'fr' (financial results) — matches XBRL_STRUCTURED_CONTENT_REFERENCE.md
  • Link: Ready for GQuants frontend to render
  • Alert: Ready for Telegram delivery

In production:
  • Payload stored in: alert.extra['structured_payload']
  • Link inserted before footer in formatted message
  • Frontend renders interactive financial tables with this JSON
""")


if __name__ == "__main__":
    main()
