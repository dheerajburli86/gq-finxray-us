import sys; sys.path.insert(0, '.')
content = open('ipo_alert_standalone.py').read()

old_query = '''def generate_ipo_alert():
    """Get latest IPO from FMP and send alert with S-1 link."""
    from_date = date.today().isoformat()
    to_date = (date.today() + timedelta(days=30)).isoformat()
    
    ipos = fmp_client.get_ipo_calendar(from_date, to_date)'''

new_query = '''def generate_ipo_alert():
    """Get past IPOs from FMP (last 7 days) and send alerts with S-1 links."""
    # Query PAST 7 days, not upcoming
    from_date = (date.today() - timedelta(days=7)).isoformat()
    to_date = date.today().isoformat()
    
    ipos = fmp_client.get_ipo_calendar(from_date, to_date)'''

content = content.replace(old_query, new_query)
open('ipo_alert_standalone.py', 'w').write(content)
print('✓ Fixed: now pulls past IPOs (last 7 days) instead of upcoming')
