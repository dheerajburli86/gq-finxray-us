import requests
import json

EODHD_API_KEY = "69c3c6e9122854.31751970"
BASE_URL = "https://eodhd.com/api"

def test_economic_calendar_variants():
    """Test different economic calendar endpoints."""
    print("\n=== Economic Calendar Variants ===")
    
    # Variant 1
    url = f"{BASE_URL}/economic-events"
    params = {"api_token": EODHD_API_KEY, "fmt": "json", "country": "US"}
    r = requests.get(url, params=params, timeout=15)
    print(f"Variant 1 /economic-events: {r.status_code}")
    if r.status_code == 200:
        print(json.dumps(r.json(), indent=2)[:500])

    # Variant 2
    url2 = f"{BASE_URL}/macro-indicator/USA"
    params2 = {"api_token": EODHD_API_KEY, "fmt": "json", "indicator": "inflation_consumer_prices_annual"}
    r2 = requests.get(url2, params=params2, timeout=15)
    print(f"\nVariant 2 /macro-indicator: {r2.status_code}")
    if r2.status_code == 200:
        data = r2.json()
        print(f"Got {len(data)} data points")
        print(json.dumps(data[:3], indent=2))

    # Variant 3
    url3 = f"{BASE_URL}/calendar/economic-events"
    params3 = {"api_token": EODHD_API_KEY, "fmt": "json", "from": "2026-06-10", "to": "2026-06-20"}
    r3 = requests.get(url3, params3, timeout=15)
    print(f"\nVariant 3 /calendar/economic-events no country: {r3.status_code}")
    if r3.status_code == 200:
        data = r3.json()
        events = data.get("economic_events", data if isinstance(data, list) else [])
        print(f"Events: {len(events)}")
        for e in events[:5]:
            print(f"  {e}")

def test_earnings_deep():
    """Test earnings with estimates vs actuals."""
    print("\n=== Earnings Deep Test ===")
    
    # Earnings for specific US stock
    url = f"{BASE_URL}/calendar/earnings"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-01",
        "to": "2026-06-20",
        "symbols": "AAPL.US,MSFT.US,NVDA.US,TSLA.US,META.US,AMZN.US,GOOGL.US,JPM.US,NFLX.US,AMD.US"
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"Earnings with symbols status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        earnings = data.get("earnings", [])
        print(f"Earnings found: {len(earnings)}")
        for e in earnings[:10]:
            print(f"  {e}")

    # Historical earnings for AAPL
    url2 = f"{BASE_URL}/fundamentals/AAPL.US"
    params2 = {"api_token": EODHD_API_KEY, "fmt": "json", "filter": "Earnings"}
    r2 = requests.get(url2, params2, timeout=15)
    print(f"\nAAPL historical earnings status: {r2.status_code}")
    if r2.status_code == 200:
        data = r2.json()
        history = data.get("History", {})
        annual = data.get("Annual", {})
        print(f"Earnings history entries: {len(history)}")
        # Show last 3
        for k, v in list(history.items())[:3]:
            print(f"  {k}: {v}")

def test_fundamentals_deep():
    """Test full fundamentals data."""
    print("\n=== Fundamentals Deep Test ===")
    
    # Full financials
    url = f"{BASE_URL}/fundamentals/AAPL.US"
    params = {"api_token": EODHD_API_KEY, "fmt": "json"}
    r = requests.get(url, params=params, timeout=15)
    print(f"Full fundamentals status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Top level keys: {list(data.keys())}")
        
        # Highlights
        highlights = data.get("Highlights", {})
        print(f"\nHighlights:")
        print(f"  Market Cap: {highlights.get('MarketCapitalization')}")
        print(f"  PE Ratio: {highlights.get('PERatio')}")
        print(f"  EPS: {highlights.get('EarningsShare')}")
        print(f"  Revenue: {highlights.get('RevenueTTM')}")
        print(f"  Profit Margin: {highlights.get('ProfitMargin')}")
        print(f"  52W High: {highlights.get('52WeekHigh')}")
        print(f"  52W Low: {highlights.get('52WeekLow')}")
        print(f"  Analyst Target: {highlights.get('WallStreetTargetPrice')}")
        
        # Valuation
        valuation = data.get("Valuation", {})
        print(f"\nValuation:")
        print(f"  Forward PE: {valuation.get('ForwardPE')}")
        print(f"  PEG: {valuation.get('PEGRatio')}")
        print(f"  Price/Sales: {valuation.get('PriceSalesTTM')}")
        print(f"  Price/Book: {valuation.get('PriceBookMRQ')}")
        
        # Analyst ratings
        analyst = data.get("AnalystRatings", {})
        print(f"\nAnalyst Ratings:")
        print(f"  Rating: {analyst.get('Rating')}")
        print(f"  Target Price: {analyst.get('TargetPrice')}")
        print(f"  Strong Buy: {analyst.get('StrongBuy')}")
        print(f"  Buy: {analyst.get('Buy')}")
        print(f"  Hold: {analyst.get('Hold')}")
        print(f"  Sell: {analyst.get('Sell')}")
        print(f"  Strong Sell: {analyst.get('StrongSell')}")
        
        # Income statement
        financials = data.get("Financials", {})
        income = financials.get("Income_Statement", {}).get("quarterly", {})
        print(f"\nQuarterly Income Statement (last 2):")
        for k, v in list(income.items())[:2]:
            print(f"  {k}:")
            print(f"    Revenue: {v.get('totalRevenue')}")
            print(f"    Net Income: {v.get('netIncome')}")
            print(f"    EPS: {v.get('eps')}")

def test_insider_transactions():
    """Test insider transactions from EODHD."""
    print("\n=== Insider Transactions ===")
    
    url = f"{BASE_URL}/insider-transactions"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "code": "AAPL.US",
        "limit": 5
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"Insider transactions status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Transactions: {len(data)}")
        for t in data[:3]:
            print(f"  {t}")

def test_analyst_ratings():
    """Test analyst ratings and upgrades/downgrades."""
    print("\n=== Analyst Ratings ===")
    
    url = f"{BASE_URL}/calendar/analyst-ratings"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-10",
        "to": "2026-06-16"
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"Analyst ratings status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        ratings = data.get("analyst_ratings", data if isinstance(data, list) else [])
        print(f"Ratings this week: {len(ratings)}")
        for rat in ratings[:5]:
            print(f"  {rat}")

def test_ipos():
    """Test IPO calendar."""
    print("\n=== IPO Calendar ===")
    
    url = f"{BASE_URL}/calendar/ipos"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-01",
        "to": "2026-06-30"
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"IPO calendar status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        ipos = data.get("ipos", data if isinstance(data, list) else [])
        print(f"IPOs this month: {len(ipos)}")
        for ipo in ipos[:5]:
            print(f"  {ipo}")

def test_splits_dividends():
    """Test splits and dividends calendar."""
    print("\n=== Splits & Dividends Calendar ===")
    
    # Splits
    url = f"{BASE_URL}/calendar/splits"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-01",
        "to": "2026-06-30"
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"Splits calendar status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        splits = data.get("splits", data if isinstance(data, list) else [])
        print(f"Splits this month: {len(splits)}")
        for s in splits[:3]:
            print(f"  {s}")

    # Dividends
    url2 = f"{BASE_URL}/calendar/dividends"
    params2 = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "from": "2026-06-10",
        "to": "2026-06-20"
    }
    r2 = requests.get(url2, params2, timeout=15)
    print(f"\nDividends calendar status: {r2.status_code}")
    if r2.status_code == 200:
        data = r2.json()
        divs = data.get("dividends", data if isinstance(data, list) else [])
        print(f"Dividends this week: {len(divs)}")
        for d in divs[:3]:
            print(f"  {d}")

def test_macro_indicators():
    """Test US macro indicators."""
    print("\n=== US Macro Indicators ===")
    
    indicators = [
        "inflation_consumer_prices_annual",
        "gdp_growth_rate",
        "unemployment_rate",
        "interest_rate",
        "retail_sales_mom",
        "industrial_production_mom"
    ]
    
    for indicator in indicators:
        url = f"{BASE_URL}/macro-indicator/USA"
        params = {"api_token": EODHD_API_KEY, "fmt": "json", "indicator": indicator}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data:
                latest = data[-1]
                print(f"  {indicator}: {latest.get('value')} ({latest.get('date')})")
        else:
            print(f"  {indicator}: {r.status_code}")

def test_news_general():
    """Test general US market news without ticker filter."""
    print("\n=== General US Market News ===")
    
    url = f"{BASE_URL}/news"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "t": "US",
        "limit": 10,
        "offset": 0
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"General news status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Articles: {len(data)}")
        for n in data[:5]:
            print(f"  {n.get('date', '')[:10]} — {n.get('title', '')[:80]}")
            print(f"    Tickers: {n.get('symbols', [])}")

def test_sentiment():
    """Test sentiment data."""
    print("\n=== Sentiment Data ===")
    
    url = f"{BASE_URL}/sentiments"
    params = {
        "api_token": EODHD_API_KEY,
        "fmt": "json",
        "s": "AAPL.US"
    }
    r = requests.get(url, params=params, timeout=15)
    print(f"Sentiment status: {r.status_code}")
    if r.status_code == 200:
        print(json.dumps(r.json(), indent=2)[:500])

if __name__ == "__main__":
    test_economic_calendar_variants()
    test_earnings_deep()
    test_fundamentals_deep()
    test_insider_transactions()
    test_analyst_ratings()
    test_ipos()
    test_splits_dividends()
    test_macro_indicators()
    test_news_general()
    test_sentiment()
    
    print("\n=== Deep EODHD Test Complete ===")