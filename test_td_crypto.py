import requests
import json

key = "8fa10118bc784a2a843318de0b8238b1"

# ── 1. All crypto pairs ───────────────────────────────────────────────────────
print("=" * 60)
print("CRYPTO PAIRS")
print("=" * 60)
r1 = requests.get("https://api.twelvedata.com/cryptocurrencies", params={"apikey": key}, timeout=15)
print("Status:", r1.status_code)
cryptos = r1.json().get("data", [])
print("Total crypto pairs:", len(cryptos))

usd_pairs = [c for c in cryptos if c.get("currency_quote") == "USD"]
print("USD-quoted pairs:", len(usd_pairs))

print("\nFirst 30 USD pairs:")
for c in usd_pairs[:30]:
    print(f"  {c.get('symbol'):<20} {c.get('currency_base'):<20} Exchange: {c.get('exchange')}")

# ── 2. Crypto ETFs ────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("CRYPTO ETFs")
print("=" * 60)
r2 = requests.get("https://api.twelvedata.com/etf", params={"apikey": key}, timeout=15)
print("Status:", r2.status_code)
etfs = r2.json().get("data", [])
print("Total ETFs:", len(etfs))

keywords = ["crypto", "bitcoin", "ethereum", "blockchain", "digital asset", "btc", "eth", "web3", "defi"]
crypto_etfs = [
    e for e in etfs
    if any(kw in e.get("name", "").lower() for kw in keywords)
]
print("Crypto-related ETFs found:", len(crypto_etfs))

print("\nAll crypto ETFs:")
for e in crypto_etfs:
    print(f"  {e.get('symbol'):<10} {e.get('name')}")

# ── 3. Test price fetch for top cryptos ──────────────────────────────────────
print("\n" + "=" * 60)
print("PRICE TEST — Top 10 USD Pairs")
print("=" * 60)
top10 = [c.get("symbol") for c in usd_pairs[:10]]
symbols_str = ",".join(top10)
r3 = requests.get(
    "https://api.twelvedata.com/price",
    params={"symbol": symbols_str, "apikey": key},
    timeout=15
)
print("Status:", r3.status_code)
prices = r3.json()
for sym, val in prices.items():
    print(f"  {sym:<20} {val}")

# Save full crypto list
with open("crypto_pairs_full.json", "w") as f:
    json.dump(usd_pairs, f, indent=2)
print("\nFull USD crypto list saved to crypto_pairs_full.json")
