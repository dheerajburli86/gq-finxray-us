import requests

api_key = 'H36sGjQSwQlhiqfEfbj329udWy8nbQ8v'

# Test the correct endpoint
url = f'https://financialmodelingprep.com/stable/news/stock?symbols=AAPL&limit=5&apikey={api_key}'
print(f'Testing: /stable/news/stock')
print(f'URL: https://financialmodelingprep.com/stable/news/stock?symbols=AAPL&limit=5&apikey=***')

r = requests.get(url, timeout=10)
print(f'Status: {r.status_code}')

if r.status_code == 200:
    data = r.json()
    print(f'SUCCESS! Got {len(data)} articles')
    if data:
        print(f'First: {data[0].get("title", "N/A")[:70]}')
else:
    print(f'ERROR: {r.text[:200]}')
