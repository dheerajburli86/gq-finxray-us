"""
One-off: assign N random covered tickers to an already-registered user.

Usage:  python assign_random_watchlist.py <telegram_username> [count]

Requires the user to have already sent /start to the bot (that's what
creates the users row this looks up by username) and real SUPABASE_URL /
SUPABASE_KEY in .env -- this script does not run in a sandbox with no DB.
"""
import os
import random
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    username = sys.argv[1].lstrip("@")
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    user = (sb.table("users").select("*")
            .ilike("telegram_username", username).limit(1).execute()).data
    if not user:
        print(f"No user row for @{username} yet -- they must send /start to the "
              f"bot first (and be present in AUTHORIZED_USERNAMES).")
        sys.exit(1)
    user_id = user[0]["id"]

    existing = {r["ticker"] for r in
                sb.table("watchlists").select("ticker").eq("user_id", user_id).execute().data}

    pool = [r["ticker"] for r in sb.table("stocks").select("ticker").execute().data
            if r["ticker"] not in existing]
    if len(pool) < count:
        print(f"Only {len(pool)} uncovered-and-not-already-watched tickers available.")
    picks = random.sample(pool, min(count, len(pool)))

    for t in picks:
        sb.table("watchlists").insert({"user_id": user_id, "ticker": t}).execute()

    print(f"Assigned {len(picks)} tickers to @{username}: {', '.join(sorted(picks))}")


if __name__ == "__main__":
    main()
