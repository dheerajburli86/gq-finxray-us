import requests
import json

EODHD_API_KEY = "69c3c6e9122854.31751970"
BASE_URL = "https://eodhd.com/api"

def test_us_exchanges():
    """Get all US exchange tickers."""
    print("\n=== Testing US Exchange Coverage ===")
    
    exchanges = ["US", "NYSE", "NASDAQ", "AMEX"]
    
    for exchange in exchanges:
        url = f"{BASE_URL}/exchange-symbol-list/{exchange}"
        params = {"api_token": EODHD_API_KEY, "fmt": "json"}
        r = requests.get(url, params=params, timeout=30)
        
        if r.status_code == 200:
            data = r.json()
            print(f"{exchange}: {len(data)} instruments")
            if data:
                print(f"  Sample: {data[0]}")
        else:
            print(f"{exchange}: Failed — {r.status_code}")

def test_stock_quote():
    """Test live quote for a single stock."""
    print("\n=== Testing Live Quote ===")
    
    ticker = "AAPL"
    url = f"{BASE_URL}/real-time/{ticker}.US"
    params = {"api_token": EODHD_API_KEY, "fmt": "json"}
    r = requests.get(url, params=params, timeout=15)
    
    print(f"AAPL quote status: {r.status_code}")
    if r.status_code == 200:
        print(json.dumps(r.json(), indent=2))

def test_bulk_quotes():
    """Test bulk quotes for multiple stocks."""
    print("\n=== Testing Bulk Quotes ===")
    
    url = f"{BASE_URL}/real-time/AAPL.US"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "s": "MSFT.US,NVDA.US,TSLA.US,META.US,AMZN.US,GOOGL.US,JPM.US,NFLX.US,AMD.US"
    }
    r = requests.get(url, params=params, timeout=15)
    
    print(f"Bulk quote status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, list):
            print(f"Got {len(data)} quotes")
            for q in data[:3]:
                print(f"  {q.get('code')} — ${q.get('close')} ({q.get('change_p')}%)")
        else:
            print(json.dumps(data, indent=2))

def test_earnings_calendar():
    """Test earnings calendar."""
    print("\n=== Testing Earnings Calendar ===")
    
    url = f"{BASE_URL}/calendar/earnings"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-10",
        "to": "2026-06-17"
    }
    r = requests.get(url, params=params, timeout=15)
    
    print(f"Earnings calendar status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        earnings = data.get("earnings", [])
        print(f"Earnings this week: {len(earnings)}")
        for e in earnings[:5]:
            print(f"  {e.get('code')} — {e.get('report_date')} | EPS Est: {e.get('epsEstimate')}")

def test_fundamentals():
    """Test fundamentals for a stock."""
    print("\n=== Testing Fundamentals ===")
    
    url = f"{BASE_URL}/fundamentals/AAPL.US"
    params = {"api_token": EODHD_API_KEY, "fmt": "json", "filter": "General"}
    r = requests.get(url, params=params, timeout=15)
    
    print(f"Fundamentals status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Company: {data.get('Name')}")
        print(f"Sector: {data.get('Sector')}")
        print(f"Industry: {data.get('Industry')}")
        print(f"Market Cap: {data.get('MarketCapitalization')}")
        print(f"Description: {str(data.get('Description', ''))[:200]}")

def test_economic_events():
    """Test economic calendar."""
    print("\n=== Testing Economic Calendar ===")
    
    url = f"{BASE_URL}/calendar/economic-events"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-10",
        "to": "2026-06-17",
        "country": "US"
    }
    r = requests.get(url, params=params, timeout=15)
    
    print(f"Economic events status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        events = data.get("economic_events", [])
        print(f"US economic events this week: {len(events)}")
        for e in events[:5]:
            print(f"  {e.get('date')} — {e.get('event')} | Actual: {e.get('actual')} | Est: {e.get('estimate')}")

def test_news():
    """Test EODHD news feed."""
    print("\n=== Testing EODHD News ===")
    
    url = f"{BASE_URL}/news"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "s": "AAPL.US",
        "limit": 5
    }
    r = requests.get(url, params=params, timeout=15)
    
    print(f"News status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"News articles: {len(data)}")
        for n in data[:3]:
            print(f"  {n.get('date')} — {n.get('title', '')[:80]}")

def test_all_us_tickers():
    """Count total US tickers available."""
    print("\n=== Counting Total US Tickers ===")
    
    url = f"{BASE_URL}/exchange-symbol-list/US"
    params = {"api_token": EODHD_API_KEY, "fmt": "json"}
    r = requests.get(url, params=params, timeout=30)
    
    if r.status_code == 200:
        data = r.json()
        
        # Break down by type
        common = [x for x in data if x.get("Type") == "Common Stock"]
        etfs = [x for x in data if x.get("Type") == "ETF"]
        other = [x for x in data if x.get("Type") not in ("Common Stock", "ETF")]
        
        print(f"Total US instruments: {len(data)}")
        print(f"Common Stocks: {len(common)}")
        print(f"ETFs: {len(etfs)}")
        print(f"Other: {len(other)}")
        print(f"\nSample common stocks:")
        for s in common[:5]:
            print(f"  {s.get('Code')} — {s.get('Name')}")
    else:
        print(f"Failed — {r.status_code}: {r.text[:200]}")

if __name__ == "__main__":
    test_all_us_tickers()
    test_us_exchanges()
    test_stock_quote()
    test_bulk_quotes()
    test_earnings_calendar()
    test_fundamentals()
    test_economic_events()
    test_news()
    
    print("\n=== EODHD Test Complete ===")