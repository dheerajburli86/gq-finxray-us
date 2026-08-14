#!/usr/bin/env python3
"""
Expand watchlist from 10 to 20 stocks using REST API.

Current watchlist (10): AAPL, AMZN, CMCSA, GOOGL, META, MSFT, MU, NVDA, SPCX, TSLA
Adding (10): NFLX, CRWD, ADBE, CRM, DDOG, AMD, AVGO, INTC, PYPL, UBER
"""

import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY not set in .env")
    exit(1)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# New stocks to add
NEW_STOCKS = ["NFLX", "CRWD", "ADBE", "CRM", "DDOG", "AMD", "AVGO", "INTC", "PYPL", "UBER"]

try:
    # Get all users with watchlists
    url = f"{SUPABASE_URL}/rest/v1/watchlists?select=user_id"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"Error fetching users: {response.status_code} - {response.text}")
        exit(1)

    user_ids = list(set(row["user_id"] for row in response.json() if row.get("user_id")))
    print(f"Found {len(user_ids)} users with watchlists: {user_ids}")

    if not user_ids:
        print("No users found.")
        exit(1)

    # For each user, add new stocks
    added_count = 0
    for user_id in user_ids:
        for ticker in NEW_STOCKS:
            # Check if already exists
            check_url = f"{SUPABASE_URL}/rest/v1/watchlists?user_id=eq.{user_id}&ticker=eq.{ticker}"
            check_resp = requests.get(check_url, headers=headers)

            if check_resp.status_code == 200 and len(check_resp.json()) > 0:
                print(f"  {ticker} already in {user_id}'s watchlist")
                continue

            # Add to watchlist
            add_url = f"{SUPABASE_URL}/rest/v1/watchlists"
            payload = {"user_id": user_id, "ticker": ticker}

            add_resp = requests.post(add_url, headers=headers, json=payload)

            if add_resp.status_code in (201, 200):
                print(f"  ✓ Added {ticker} to {user_id}'s watchlist")
                added_count += 1
            else:
                print(f"  ✗ Failed to add {ticker} for {user_id}: {add_resp.status_code} - {add_resp.text}")

    print(f"\nCompleted! Added {added_count} ticker-user pairs")

    # Show updated counts
    print("\nUpdated watchlist sizes:")
    for user_id in user_ids:
        count_url = f"{SUPABASE_URL}/rest/v1/watchlists?user_id=eq.{user_id}&select=count"
        count_resp = requests.get(count_url, headers={**headers, "Prefer": "count=exact"})
        if "content-range" in count_resp.headers:
            count = int(count_resp.headers["content-range"].split("/")[1])
        else:
            count = len(count_resp.json())
        print(f"  {user_id}: {count} stocks")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
