import sys; sys.path.insert(0, '.')
content = open('fmp_client.py').read()

# Remove the "stable/" prefix since BASE_URL already has it
old_func = '''def get_stock_news(ticker, limit=10):
    data = _get("stable/news/stock", {"symbols": ticker, "limit": limit})
    return data if isinstance(data, list) else []'''

new_func = '''def get_stock_news(ticker, limit=10):
    data = _get("news/stock", {"symbols": ticker, "limit": limit})
    return data if isinstance(data, list) else []'''

content = content.replace(old_func, new_func)
open('fmp_client.py', 'w').write(content)
print('Fixed: stable/news/stock → news/stock (BASE_URL already has /stable)')
