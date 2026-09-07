import sys; sys.path.insert(0, '.')
content = open('fmp_client.py').read()

# Fix the endpoint path from "stock-news" to "stable/news/stock"
content = content.replace(
    'def get_stock_news(ticker, limit=10):\n    data = _get("stock-news", {"symbols": ticker, "limit": limit})',
    'def get_stock_news(ticker, limit=10):\n    data = _get("stable/news/stock", {"symbols": ticker, "limit": limit})'
)

open('fmp_client.py', 'w').write(content)
print('Fixed: stock-news → stable/news/stock')
