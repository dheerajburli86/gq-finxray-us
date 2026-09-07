import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Remove volume logic, use price only
old_logic = '''    volume = quote.get('volume', 0)
    change_pct = quote.get('changePercentage', 0)
    
    # Get yesterday's volume for comparison (FMP quote doesn't include avgVolume)
    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    hist = fmp_client.get_historical_prices(ticker, yesterday, yesterday)
    prev_volume = hist[0].get('volume', 0) if hist else 0
    
    if prev_volume == 0 or volume < VOLUME_SPIKE_THRESHOLD * prev_volume or abs(change_pct) < PRICE_MOVE_THRESHOLD:
        continue'''

new_logic = '''    change_pct = quote.get('changePercentage', 0)
    
    # Simplified: alert on price movement only (volume data unavailable from FMP)
    if abs(change_pct) < PRICE_MOVE_THRESHOLD:
        continue'''

content = content.replace(old_logic, new_logic)
open('etf_flow_poller.py', 'w').write(content)
print('Fixed: ETF Flow now triggers on price movement ±0.5%')
