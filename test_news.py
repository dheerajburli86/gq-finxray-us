import sys; sys.path.insert(0, '.')
import fmp_client
result = fmp_client.get_stock_news('AAPL', limit=3)
if result:
    print(f'Success! Got {len(result)} articles')
    print(f'First: {result[0].get("title", "N/A")[:60]}')
else:
    print('Still empty')
