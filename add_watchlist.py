from supabase import create_client
from dotenv import load_dotenv
import os

load_dotenv()
sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

tickers = ["NVDA", "GOOGL", "CMCSA", "AAPL", "MSFT", "META", "AMZN", "TSLA", "SPCX", "MU"]
existing = [r["ticker"] for r in sb.table("watchlists").select("ticker").execute().data]

added = 0
for t in tickers:
    if t not in existing:
        sb.table("watchlists").insert({"ticker": t}).execute()
        print(f"Added: {t}")
        added += 1
    else:
        print(f"Already exists: {t}")

print(f"\nDone. Added {added} new tickers.")
current = [r["ticker"] for r in sb.table("watchlists").select("ticker").execute().data]
print("Current watchlist:", current)
