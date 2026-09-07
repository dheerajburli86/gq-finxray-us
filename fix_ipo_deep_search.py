import sys; sys.path.insert(0, '.')
content = open('ipo_alert_standalone.py').read()

old_get_s1 = '''def get_s1_link(ticker, listing_date):
    """Get S-1 filing link from SEC EDGAR."""
    # Try exact date first
    s1_url = edgar_link.find_filing_url(
        ticker, form_type="S-1", target_date=listing_date, window_days=0
    )
    
    # If not found, try 120 days back (S-1 filed well before listing)
    if not s1_url:
        s1_url = edgar_link.find_filing_url(
            ticker, form_type="S-1",
            target_date=(date.fromisoformat(listing_date) - timedelta(days=120)).isoformat(),
            window_days=130
        )
    
    return s1_url'''

new_get_s1 = '''def get_s1_link(ticker, listing_date):
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
    
    return s1_url'''

content = content.replace(old_get_s1, new_get_s1)
open('ipo_alert_standalone.py', 'w').write(content)
print('✓ Fixed: searches 6+ months back, handles warrants/rights')
