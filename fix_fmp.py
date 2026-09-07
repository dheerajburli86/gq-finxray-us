import sys; sys.path.insert(0, '.')
content = open('fmp_client.py').read()
content = content.replace(
    'data = _get("stock-news", {"symbols": ticker, "limit": limit})',
    'data = _get("stock-news", {"symbol": ticker, "limit": limit})'
)
open('fmp_client.py', 'w').write(content)
print('Fixed: symbols → symbol')
