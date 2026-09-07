import sys; sys.path.insert(0, '.')
import fmp_client

# Try different endpoint names
endpoints_to_try = [
    ('stock-news', {'symbol': 'AAPL', 'limit': 1}),
    ('news', {'symbol': 'AAPL', 'limit': 1}),
    ('historical-news', {'symbol': 'AAPL', 'limit': 1}),
]

for endpoint, params in endpoints_to_try:
    result = fmp_client._get(endpoint, params)
    status = 'OK' if isinstance(result, list) and result else 'EMPTY/404'
    print(f'{endpoint:20s} → {status}')
