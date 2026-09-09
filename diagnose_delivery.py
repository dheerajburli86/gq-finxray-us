#!/usr/bin/env python3
"""
diagnose_delivery.py — can each user actually receive alerts, and did they?

Answers, per user, the only question that matters: if an alert fires for a
ticker on their watchlist right now, does it reach their Telegram?

Every stage that can silently drop an alert is checked in the order delivery.py
applies them, because a failure at any one of them looks identical from the
outside (no alerts arriving):

  registered -> chat_id captured -> active -> watchlist non-empty
  -> impact floor -> muted features -> daily cap -> actually delivered

Run where SUPABASE_URL / SUPABASE_KEY are set (Railway shell, or locally with
a populated .env):

    python diagnose_delivery.py
    python diagnose_delivery.py dj_12312 diptam shlok999999
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

URL, KEY = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
if not URL or not KEY:
    sys.exit("SUPABASE_URL / SUPABASE_KEY not set — run this where the app runs.")

sb = create_client(URL, KEY)

DEFAULT_USERS = ["dj_12312", "diptam", "shlok999999"]
usernames = [u.lstrip("@").lower() for u in (sys.argv[1:] or DEFAULT_USERS)]

AUTHORIZED = {u.strip().lstrip("@").lower()
              for u in os.getenv("AUTHORIZED_USERNAMES", "").split(",") if u.strip()}
BROADCAST = os.getenv("GQ_ENABLE_MARKET_WIDE", "false").strip().lower() in ("1", "true", "yes")
IMPACT_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def rows(table, **q):
    try:
        req = sb.table(table).select(q.pop("select", "*"))
        for col, val in q.items():
            req = req.eq(col, val)
        return req.execute().data or []
    except Exception as e:
        print(f"    ! query {table} failed: {e}")
        return []


print("=" * 72)
print("GQ FinXray — delivery diagnosis")
print("=" * 72)
print(f"AUTHORIZED_USERNAMES : {sorted(AUTHORIZED) or 'UNSET (bot will reject /start)'}")
print(f"market-wide delivery : {'ON' if BROADCAST else 'OFF — ticker=MARKET alerts reach nobody'}")

for name in usernames:
    print(f"\n{'-' * 72}\n@{name}\n{'-' * 72}")

    if AUTHORIZED and name not in AUTHORIZED:
        print("  BLOCKED: not in AUTHORIZED_USERNAMES — /start is refused, so this")
        print("           user can never register or receive anything.")

    user = None
    try:
        r = sb.table("users").select("*").ilike("telegram_username", name).limit(1).execute()
        user = (r.data or [None])[0]
    except Exception as e:
        print(f"  ! user lookup failed: {e}")

    if not user:
        print("  NOT REGISTERED — no row in `users`. They must send /start to the bot.")
        continue

    uid = user["id"]
    chat_id = user.get("telegram_chat_id")
    print(f"  user_id   : {uid}")
    print(f"  chat_id   : {chat_id or 'MISSING — never sent /start; nothing can be delivered'}")
    print(f"  is_active : {user.get('is_active')}")

    wl = [w["ticker"] for w in rows("watchlists", user_id=uid, select="ticker")]
    print(f"  watchlist : {len(wl)} ticker(s){' — EMPTY, nothing can match' if not wl else ''}")
    if wl:
        print(f"              {', '.join(sorted(wl)[:15])}{' …' if len(wl) > 15 else ''}")

    prefs = (rows("user_preferences", user_id=uid) or [{}])[0]
    floor = (prefs.get("min_impact") or "MEDIUM").upper()
    muted = prefs.get("muted_features") or []
    cap = prefs.get("max_alerts_per_day") or 200
    print(f"  min_impact: {floor}" + ("  <-- LOW-impact alerts are filtered out"
                                      if IMPACT_RANK.get(floor, 2) > 1 else ""))
    print(f"  muted     : {muted or 'none'}")
    print(f"  daily cap : {cap}")

    if not chat_id or not user.get("is_active") or not wl:
        print("  => CANNOT RECEIVE until the above is fixed.")
        continue

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    try:
        dels = (sb.table("alert_deliveries").select("status, reason, error, created_at")
                .eq("user_id", uid).gte("created_at", since).execute().data or [])
    except Exception as e:
        dels = []
        print(f"  ! delivery-ledger query failed: {e}")

    by_status = Counter((d.get("status") or "?").upper() for d in dels)
    print(f"  last 24h  : {dict(by_status) or 'NO DELIVERY ATTEMPTS AT ALL'}")
    for d in dels:
        if (d.get("status") or "").upper() in ("FAILED", "UNDELIVERABLE"):
            print(f"              {d['status']}: {str(d.get('error'))[:110]}")

    if by_status.get("SENT"):
        print("  => RECEIVING.")
    elif dels:
        print("  => matched alerts, but none sent — see the statuses above.")
    else:
        print("  => nothing even attempted. Either no alert fired for their tickers,")
        print("     or alerts are dying before delivery (check flagged_summaries).")

# ── Where alerts are dying upstream of delivery ─────────────────────────────
print(f"\n{'=' * 72}\nPIPELINE HEALTH (last 24h)\n{'=' * 72}")
since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

try:
    flagged = (sb.table("flagged_summaries").select("ticker, failure_reason, created_at")
               .gte("created_at", since).execute().data or [])
    if flagged:
        print(f"flagged, NOT sent: {len(flagged)}")
        for reason, n in Counter(f.get("failure_reason") for f in flagged).most_common():
            print(f"   {n:>4}  {reason}")
    else:
        print("flagged, NOT sent: 0")
except Exception as e:
    print(f"! flagged_summaries query failed: {e}")

try:
    alerts = (sb.table("alerts").select("source, filing_type, delivered, created_at")
              .gte("created_at", since).execute().data or [])
    print(f"\nalerts created: {len(alerts)}")
    per_feature = Counter()
    for a in alerts:
        per_feature[(a.get("source"), a.get("filing_type"))] += 1
    for (src, ft), n in per_feature.most_common():
        print(f"   {n:>4}  {src} / {ft}")
    undelivered = [a for a in alerts if not a.get("delivered")]
    if undelivered:
        print(f"\n{len(undelivered)} still undelivered (delivery loop backlog or no audience)")
except Exception as e:
    print(f"! alerts query failed: {e}")

print("\nDone.")
