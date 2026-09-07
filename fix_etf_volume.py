import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Replace the volume comparison logic to use historical data
old_logic = '''    volume = quote.get('volume', 0)
    change_pct = quote.get('changePercentage', 0)
    if volume < VOLUME_SPIKE_THRESHOLD * avg_volume or abs(change_pct) < PRICE_MOVE_THRESHOLD:'''

new_logic = '''    volume = quote.get('volume', 0)
    change_pct = quote.get('changePercentage', 0)
    
    # Get yesterday's volume for comparison (FMP quote doesn't include avgVolume)
    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    hist = fmp_client.get_historical_prices(ticker, yesterday, yesterday)
    prev_volume = hist[0].get('volume', 0) if hist else 0
    
    if prev_volume == 0 or volume < VOLUME_SPIKE_THRESHOLD * prev_volume or abs(change_pct) < PRICE_MOVE_THRESHOLD:'''

content = content.replace(old_logic, new_logic)
open('etf_flow_poller.py', 'w').write(content)
print('Fixed: using historical volume for comparison')
