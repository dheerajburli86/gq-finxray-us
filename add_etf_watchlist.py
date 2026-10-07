"""
add_etf_watchlist.py
Put ETFs (or stocks) on one user's watchlist and check that user can actually
receive alerts.

    python add_etf_watchlist.py <telegram_username> MCHI SPY QQQ
    python add_etf_watchlist.py <telegram_username> --remove SPY
    python add_etf_watchlist.py <telegram_username>            # just show status

Every ticker is checked against etf_master (ETFs) and stocks before it is
written, so a typo cannot silently sit on a watchlist producing nothing.
"""

import os
import sys

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "50"))


def sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def find_user(db, username):
    u = username.lstrip("@")
    # ilike for case-insensitivity, then an exact compare: "_" is a LIKE
    # wildcard, so "dheeraj_x" would otherwise also match "dheerajax".
    rows = db.table("users").select("id, telegram_username, telegram_chat_id, is_active") \
        .ilike("telegram_username", u).execute().data or []
    rows = [r for r in rows if (r.get("telegram_username") or "").lower() == u.lower()]
    return rows[0] if rows else None


def classify(db, ticker):
    """'ETF', 'STOCK' or None."""
    if db.table("etf_master").select("ticker").eq("ticker", ticker).limit(1).execute().data:
        return "ETF"
    if db.table("stocks").select("ticker").eq("ticker", ticker).limit(1).execute().data:
        return "STOCK"
    return None


def watchlist(db, user_id):
    rows = db.table("watchlists").select("ticker").eq("user_id", user_id).execute().data or []
    return sorted({r["ticker"].upper() for r in rows if r.get("ticker")})


def main(argv, db=None):
    if not argv:
        print(__doc__)
        return 1
    db = db or sb()
    user = find_user(db, argv[0])
    if not user:
        print(f"No user with telegram_username '{argv[0]}'. Create the user first (see the steps).")
        return 1
    uid = user["id"]
    remove = "--remove" in argv
    tickers = [t.upper() for t in argv[1:] if not t.startswith("--")]

    current = watchlist(db, uid)
    for t in tickers:
        if remove:
            db.table("watchlists").delete().eq("user_id", uid).eq("ticker", t).execute()
            print(f"  removed {t}")
            continue
        kind = classify(db, t)
        if not kind:
            print(f"  SKIP  {t}: not in etf_master or stocks (typo, or not a covered ticker)")
            continue
        if t in current:
            print(f"  ok    {t}: already on the watchlist ({kind})")
            continue
        if len(current) >= MAX_WATCHLIST:
            print(f"  SKIP  {t}: watchlist is full ({MAX_WATCHLIST})")
            continue
        db.table("watchlists").insert({"user_id": uid, "ticker": t}).execute()
        current.append(t)
        print(f"  added {t} ({kind})")

    final = watchlist(db, uid)
    etfs = [t for t in final if classify(db, t) == "ETF"]
    print(f"\n@{user['telegram_username']}: {len(final)} tickers, {len(etfs)} ETFs -> {', '.join(final) or '(empty)'}")
    if not user.get("is_active"):
        print("  WARNING: user is_active = false — delivery skips inactive users.")
    if not user.get("telegram_chat_id"):
        print("  WARNING: no telegram_chat_id — the user must send /start to the bot before anything arrives.")
    prefs = db.table("user_preferences").select("min_impact, muted_features") \
        .eq("user_id", uid).limit(1).execute().data or []
    if prefs:
        p = prefs[0]
        if (p.get("min_impact") or "MEDIUM").upper() == "HIGH":
            print("  NOTE: min_impact is HIGH — MEDIUM ETF alerts (most holdings/fee/manager changes) are filtered out.")
        muted = set(p.get("muted_features") or []) & {14, 15, 16}
        if muted:
            print(f"  NOTE: ETF features muted for this user: {sorted(muted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
