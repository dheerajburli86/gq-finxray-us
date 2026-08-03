import requests
import json
from collections import Counter

key = "8fa10118bc784a2a843318de0b8238b1"

# ── 1. Explore crypto pair structure ─────────────────────────────────────────
print("=" * 60)
print("CRYPTO PAIRS — FULL ANALYSIS")
print("=" * 60)
r1 = requests.get(
    "https://api.twelvedata.com/cryptocurrencies",
    params={"apikey": key},
    timeout=30
)
print("Status:", r1.status_code)
cryptos = r1.json().get("data", [])
print("Total crypto pairs:", len(cryptos))

# Check what quote currencies exist
quotes = Counter(c.get("currency_quote", "") for c in cryptos)
print("\nQuote currencies available:")
for q, count in quotes.most_common(15):
    print(f"  {q:<10} {count} pairs")

# Check sample entries to understand structure
print("\nSample entries (first 5):")
for c in cryptos[:5]:
    print(f"  {json.dumps(c)}")

# Find USD pairs (might be stored differently)
usd_pairs = [c for c in cryptos if "USD" in c.get("currency_quote", "").upper()
             or "USD" in c.get("symbol", "").upper()]
print(f"\nPairs with USD in symbol or quote: {len(usd_pairs)}")
for c in usd_pairs[:20]:
    print(f"  {c.get('symbol'):<20} base={c.get('currency_base'):<15} quote={c.get('currency_quote')}")

# ── 2. Test direct price for known crypto symbols ─────────────────────────────
print("\n" + "=" * 60)
print("PRICE TEST — Known Crypto Symbols")
print("=" * 60)
test_symbols = ["BTC/USD", "ETH/USD", "BNB/USD", "SOL/USD", "XRP/USD",
                "ADA/USD", "DOGE/USD", "AVAX/USD", "DOT/USD", "MATIC/USD"]

r2 = requests.get(
    "https://api.twelvedata.com/price",
    params={"symbol": ",".join(test_symbols), "apikey": key},
    timeout=30
)
print("Status:", r2.status_code)
print("Response:", json.dumps(r2.json(), indent=2))

# ── 3. Test crypto ETFs by known tickers ─────────────────────────────────────
print("\n" + "=" * 60)
print("CRYPTO ETF PRICE TEST")
print("=" * 60)
crypto_etf_tickers = [
    "IBIT", "FBTC", "BITB", "ARKB", "BTCO",  # Bitcoin ETFs
    "ETHA", "FETH", "ETHW",                    # Ethereum ETFs
    "BITO", "BITI",                             # Futures-based
    "BLOK", "LEGR", "KOIN",                    # Blockchain ETFs
    "GBTC", "ETHE",                             # Grayscale
]
r3 = requests.get(
    "https://api.twelvedata.com/price",
    params={"symbol": ",".join(crypto_etf_tickers), "apikey": key},
    timeout=30
)
print("Status:", r3.status_code)
prices = r3.json()
print(f"\nCrypto ETFs with prices ({len([v for v in prices.values() if 'price' in v])} available):")
for sym, val in prices.items():
    if "price" in val:
        print(f"  ✅ {sym:<8} ${val['price']}")
    else:
        print(f"  ❌ {sym:<8} {val.get('message', 'no data')[:50]}")

