import sys; sys.path.insert(0, '.')
import fmp_client

# Monkey-patch _get to print the URL before calling
original_get = fmp_client._get

def debug_get(path, params=None, timeout=20, retries=2):
    api_key = fmp_client.API_KEY
    url = f'https://financialmodelingprep.com/api/v3/{path}?apikey={api_key}'
    if params:
        for k, v in params.items():
            url += f'&{k}={v}'
    print(f'DEBUG URL: {url}')
    return original_get(path, params, timeout, retries)

fmp_client._get = debug_get

result = fmp_client.get_stock_news('AAPL', limit=1)
print(f'Result: {result}')
