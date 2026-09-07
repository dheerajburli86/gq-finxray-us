import sys; sys.path.insert(0, '.')
import fmp_client
from datetime import datetime, timedelta

VOLUME_SPIKE_THRESHOLD = 1.1
PRICE_MOVE_THRESHOLD = 0.5

etfs = ['SPY', 'QQQ', 'XLY', 'GLD']

for ticker in etfs:
    quote = fmp_client.get_quote(ticker)
    volume = quote.get('volume', 0)
    change_pct = quote.get('changePercentage', 0)
    
    # Get yesterday's volume
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    hist = fmp_client.get_historical_prices(ticker, yesterday, yesterday)
    prev_volume = hist[0].get('volume', 0) if hist else 0
    
    if prev_volume > 0:
        vol_ratio = volume / prev_volume
    else:
        vol_ratio = 0
    
    print(f'{ticker}:')
    print(f'  Change: {change_pct:.2f}% (need ±{PRICE_MOVE_THRESHOLD}%)')
    print(f'  Volume: {volume} vs yesterday {prev_volume} (ratio: {vol_ratio:.2f}x, need {VOLUME_SPIKE_THRESHOLD}x)')
    
    meets_price = abs(change_pct) >= PRICE_MOVE_THRESHOLD
    meets_volume = vol_ratio >= VOLUME_SPIKE_THRESHOLD
    print(f'  TRIGGER: {meets_price and meets_volume} (price={meets_price}, volume={meets_volume})\n')
