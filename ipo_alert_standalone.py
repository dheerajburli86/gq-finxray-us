"""
IPO Alert Standalone — Feature 8
Generate and send a single IPO alert to Telegram with S-1 filing link.
Direct, no bullshit.
"""

import sys; sys.path.insert(0, '.')
import asyncio
from datetime import datetime, timezone, date, timedelta
from supabase import create_client
from dotenv import load_dotenv
import os

import fmp_client
import edgar_link
from feature_map import tag_extra
from main import deliver_pending_alerts

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def save_ipo_alert(ticker, name, exchange, listing_date, price_range, shares, s1_link):
    """Save IPO alert directly with S-1 link."""
    sb = get_supabase()
    
    summary = (
        f"🏦 *IPO Alert — Upcoming Listing*\n\n"
        f"*Company:* {name}\n"
        f"*Ticker:* ${ticker}\n"
        f"*Exchange:* {exchange}\n"
        f"*Listing Date:* {listing_date}\n"
        f"*Price Range:* {price_range}\n"
        f"*Shares Offered:* {shares}\n"
        f"_Source: FMP IPO Calendar | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )
    
    alert_dict = {
        "ticker": ticker,
        "summary": summary,
        "impact": "HIGH",
        "source": "FMP_IPO",
        "filing_type": "IPO_UPCOMING",
        "delivered": False,
        "link": s1_link,
        "extra": {
            "source": "FMP_IPO",
            "filing_type": "IPO_UPCOMING",
            "name": name,
            "exchange": exchange,
            "listing_date": listing_date
        }
    }
    
    sb.table("alerts").insert(alert_dict).execute()
    print(f'✓ Saved {ticker} IPO alert with link: {s1_link}')


def get_s1_link(ticker, listing_date):
    """Get S-1 filing link from SEC EDGAR."""
    from datetime import date, timedelta
    
    # Handle special securities: strip W (warrant), R (rights), etc.
    base_ticker = ticker.rstrip('WRCDEFGHIJKLMNOPQSTUVXYZ')
    if not base_ticker:
        base_ticker = ticker
    
    # Try base ticker first (for warrants/rights that have parent company)
    if base_ticker != ticker:
        print(f'    Stripped {ticker} -> {base_ticker} (special security)')
        s1_url = edgar_link.find_filing_url(
            base_ticker, form_type="S-1",
            target_date=(date.fromisoformat(listing_date) - timedelta(days=180)).isoformat(),
            window_days=200
        )
        if s1_url:
            return s1_url
    
    # Try original ticker - search 6 months back
    s1_url = edgar_link.find_filing_url(
        ticker, form_type="S-1",
        target_date=(date.fromisoformat(listing_date) - timedelta(days=180)).isoformat(),
        window_days=200
    )
    
    return s1_url


def generate_ipo_alert():
    """Get past IPOs from FMP (last 7 days) and send alerts with S-1 links."""
    # Query PAST 7 days, not upcoming
    from_date = (date.today() - timedelta(days=7)).isoformat()
    to_date = date.today().isoformat()
    
    ipos = fmp_client.get_ipo_calendar(from_date, to_date)
    
    if not ipos:
        print('No IPOs found in next 30 days')
        return
    
    print(f'Found {len(ipos)} upcoming IPOs, generating alerts...\n')
    
    for ipo in ipos:
        ticker = ipo.get("symbol", "").strip()
        name = ipo.get("company", "Unknown")
        exchange = ipo.get("exchange", "N/A")
        listing_date = ipo.get("date", "")
        price_range = ipo.get("priceRange", "TBD")
        shares = ipo.get("shares", 0)
        
        if not ticker or not listing_date:
            continue
        
        print(f'Processing {ticker}: {name}')
        
        # Get S-1 link
        s1_link = get_s1_link(ticker, listing_date)
        
        if s1_link:
            print(f'  ✓ Found S-1: {s1_link}')
        else:
            print(f'  ⚠ No S-1 found on EDGAR yet (might be recently filed)')
            # Use SEC EDGAR browse URL as fallback
            s1_link = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=S-1&dateb=&owner=exclude&count=10"
            print(f'  → Using fallback: {s1_link}')
        
        # Save alert with link
        save_ipo_alert(ticker, name, exchange, listing_date, price_range, shares, s1_link)
        print()


async def send_alerts():
    """Send all pending alerts to Telegram."""
    print('Sending to Telegram...')
    await deliver_pending_alerts()
    print('✓ Done!')


if __name__ == "__main__":
    print('=' * 60)
    print('Feature 8 — IPO Deep Dive Alert Generator')
    print('=' * 60 + '\n')
    
    generate_ipo_alert()
    
    asyncio.run(send_alerts())
