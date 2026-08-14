#!/usr/bin/env python3
"""
Expand watchlist from 10 to 20 stocks.

Current watchlist (10): AAPL, AMZN, CMCSA, GOOGL, META, MSFT, MU, NVDA, SPCX, TSLA
Adding (10): NFLX, CRWD, ADBE, CRM, DDOG, AMD, AVGO, INTC, PYPL, UBER

These stocks have high SEC filing activity, insider trading, earnings calendar,
and analyst coverage to activate features 1, 3, 4-12.
"""

import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# New stocks to add
NEW_STOCKS = ["NFLX", "CRWD", "ADBE", "CRM", "DDOG", "AMD", "AVGO", "INTC", "PYPL", "UBER"]

# Get all current users who have watchlists
try:
    result = supabase.table("watchlists").select("user_id").execute()
    user_ids = list(set(row["user_id"] for row in result.data if row.get("user_id")))
    print(f"Found {len(user_ids)} users with watchlists")

    if not user_ids:
        print("No users found. Skipping watchlist expansion.")
        exit(1)

    # For each user, add the new stocks
    added_count = 0
    for user_id in user_ids:
        for ticker in NEW_STOCKS:
            try:
                # Check if already exists
                check = supabase.table("watchlists").select("id").eq("user_id", user_id).eq("ticker", ticker).execute()
                if check.data and len(check.data) > 0:
                    print(f"  {ticker} already in {user_id}'s watchlist, skipping")
                    continue

                # Add to watchlist
                supabase.table("watchlists").insert({
                    "user_id": user_id,
                    "ticker": ticker
                }).execute()
                print(f"  ✓ Added {ticker} to {user_id}'s watchlist")
                added_count += 1
            except Exception as e:
                print(f"  ✗ Failed to add {ticker} for {user_id}: {e}")

    print(f"\nCompleted! Added {added_count} ticker-user pairs to watchlist")

    # Show updated watchlist count per user
    print("\nUpdated watchlist sizes:")
    for user_id in user_ids:
        count = supabase.table("watchlists").select("ticker", count="exact").eq("user_id", user_id).execute()
        print(f"  {user_id}: {count.count} stocks")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
