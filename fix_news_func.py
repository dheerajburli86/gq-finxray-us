import sys; sys.path.insert(0, '.')
content = open('fmp_client.py').read()

# Replace the entire get_stock_news function
old_func = '''def get_stock_news(ticker, limit=10):
    data = _get("stock-news", {"symbols": ticker, "limit": limit})
    return data if isinstance(data, list) else []'''

new_func = '''def get_stock_news(ticker, limit=10):
    data = _get("stable/news/stock", {"symbols": ticker, "limit": limit})
    return data if isinstance(data, list) else []'''

content = content.replace(old_func, new_func)
open('fmp_client.py', 'w').write(content)
print('Fixed get_stock_news: stock-news → stable/news/stock')
