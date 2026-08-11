"""
delivery.py
GQ FinXray US — per-user alert fan-out.

THE RULE THIS MODULE ENFORCES
-----------------------------
A user receives an alert about a company if, and only if, that company is on
their watchlist. Nothing else. There is no firehose channel, no "everyone gets
everything" path, no default subscription to the whole market.

The only exception is a small set of features that have no meaningful ticker to
match against — the sector heatmap, the IPO calendar (an IPO ticker cannot be on
a watchlist before it lists), the ETF Xray and the macro digest. Those are
flagged `market_wide=True` in feature_map.py and go to users who have not turned
them off (`user_preferences.receive_market_wide`). Everything else is strictly
watchlist-scoped.

WHY alerts.delivered IS NOT ENOUGH ANY MORE
-------------------------------------------
One alert now has many recipients, so a single boolean on the alert row cannot
express "sent to Dheeraj, failed for Priya, skipped for Raj (below their impact
floor)". `alert_deliveries` is the real ledger — one row per (alert, user) with
a UNIQUE constraint that doubles as the idempotency key, so a crash mid-fan-out
cannot double-send on restart. `alerts.delivered` now means only "this alert has
been fanned out", i.e. do not consider it again.

QUERY SHAPE
-----------
Everything is batched. For a cycle of N alerts touching M tickers the cost is a
fixed handful of queries, not N×M. The naive per-alert-per-user version would
issue thousands of round-trips per cycle at 6,300 tickers.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest

from alert_formatter import build_message, delivery_reason
from feature_map import is_market_wide, resolve_feature

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Optional admin firehose. Left UNSET by default — the whole point of this module
# is that nobody receives more than their watchlist asks for. Set it only if you
# want a private debug channel mirroring everything.
ADMIN_CHANNEL_ID = os.getenv("TELEGRAM_ADMIN_CHANNEL_ID") or None

ET = ZoneInfo("America/New_York")

IMPACT_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

# Telegram permits ~30 messages/second across all chats. Stay comfortably under.
SEND_GAP_SECONDS = 0.05
BATCH_LIMIT = 100


# ── Loading ───────────────────────────────────────────────────────────────────
def _fetch_undelivered(limit=BATCH_LIMIT):
    try:
        res = (supabase.table("alerts")
               .select("*")
               .eq("delivered", False)
               .order("created_at")
               .limit(limit)
               .execute())
        return res.data or []
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to fetch undelivered alerts: {e}")
        return []


def _fetch_active_users():
    """
    All active users with a chat_id, joined to their preferences.
    Returns {user_id: {...}}. Small table — one query per cycle is fine.
    """
    try:
        users = (supabase.table("users")
                 .select("id, telegram_chat_id, telegram_username, display_name, is_active")
                 .eq("is_active", True)
                 .execute()).data or []
        prefs = (supabase.table("user_preferences")
                 .select("user_id, min_impact, receive_market_wide, muted_features, max_alerts_per_day")
                 .execute()).data or []
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to load users/preferences: {e}")
        return {}

    pref_by_user = {p["user_id"]: p for p in prefs}
    out = {}
    for u in users:
        chat_id = u.get("telegram_chat_id")
        if not chat_id:
            # Registered but never opened a chat with the bot — nothing to send to.
            continue
        p = pref_by_user.get(u["id"], {})
        out[u["id"]] = {
            "user_id": u["id"],
            "chat_id": str(chat_id),
            "username": u.get("telegram_username"),
            "min_impact": (p.get("min_impact") or "MEDIUM").upper(),
            "receive_market_wide": p.get("receive_market_wide", True),
            "muted_features": set(p.get("muted_features") or []),
            "max_alerts_per_day": p.get("max_alerts_per_day") or 200,
        }
    return out


def _fetch_watchers(tickers):
    """
    {TICKER: {user_id, ...}} for the given tickers, in ONE query.

    Chunked at 200 tickers per request because PostgREST puts the `in.()` list in
    the URL and a 6,300-item list would exceed the server's URI length limit.
    """
    watchers = {}
    tickers = [t for t in tickers if t]
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            rows = (supabase.table("watchlists")
                    .select("user_id, ticker")
                    .in_("ticker", chunk)
                    .execute()).data or []
        except Exception as e:
            logger.error(f"[DELIVERY] Watchlist lookup failed for chunk {i}: {e}")
            continue
        for r in rows:
            watchers.setdefault((r.get("ticker") or "").upper(), set()).add(r["user_id"])
    return watchers


def _fetch_existing_deliveries(alert_ids):
    """{(alert_id, user_id)} already recorded — the idempotency guard."""
    seen = set()
    for i in range(0, len(alert_ids), 100):
        chunk = alert_ids[i:i + 100]
        try:
            rows = (supabase.table("alert_deliveries")
                    .select("alert_id, user_id")
                    .in_("alert_id", chunk)
                    .execute()).data or []
        except Exception as e:
            logger.error(f"[DELIVERY] Delivery-ledger lookup failed: {e}")
            continue
        for r in rows:
            seen.add((r["alert_id"], r["user_id"]))
    return seen


def _fetch_todays_counts(user_ids):
    """{user_id: alerts_sent_today} for the daily cap, measured from ET midnight."""
    if not user_ids:
        return {}
    midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {}
    try:
        rows = (supabase.table("alert_deliveries")
                .select("user_id")
                .eq("status", "SENT")
                .gte("created_at", midnight_et.isoformat())
                .execute()).data or []
        for r in rows:
            counts[r["user_id"]] = counts.get(r["user_id"], 0) + 1
    except Exception as e:
        logger.error(f"[DELIVERY] Daily-count lookup failed: {e}")
    return counts


# ── Routing ───────────────────────────────────────────────────────────────────
def resolve_audience(alert, users, watchers):
    """
    Who should receive this alert, and why.
    Returns a list of (user_dict, reason_string).

    This is the single place the watchlist rule is applied. Nothing bypasses it.
    """
    source      = alert.get("source")
    filing_type = alert.get("filing_type")
    ticker      = (alert.get("ticker") or "").upper()
    impact      = (alert.get("impact") or "LOW").upper()
    fid, _      = resolve_feature(source, filing_type)
    alert_rank  = IMPACT_RANK.get(impact, 1)

    if is_market_wide(source, filing_type):
        candidates = [u for u in users.values() if u["receive_market_wide"]]
        reason = "Market-wide alert. Turn these off any time with /settings."
    else:
        if not ticker or ticker in ("MARKET", "UNKNOWN"):
            # A ticker-scoped feature with no usable ticker cannot be routed to
            # anyone without violating the watchlist rule. Drop it rather than
            # guess — and say so in the ledger so it is visible, not silent.
            return []
        uids = watchers.get(ticker, set())
        candidates = [users[uid] for uid in uids if uid in users]
        reason = f"You're receiving this because {ticker} is on your watchlist."

    audience = []
    for u in candidates:
        if alert_rank < IMPACT_RANK.get(u["min_impact"], 2):
            continue                       # below this user's impact floor
        if fid in u["muted_features"]:
            continue                       # user muted this feature
        audience.append((u, reason))
    return audience


# ── Sending ───────────────────────────────────────────────────────────────────
async def _send_one(bot, chat_id, text):
    """Send with Telegram's own rate-limit signal honoured. Returns (ok, error)."""
    for attempt in range(3):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return True, None
        except RetryAfter as e:
            # Telegram telling us exactly how long to wait. Obey it.
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except Forbidden as e:
            # User blocked the bot or deleted the chat. Not retryable.
            return False, f"forbidden: {e}"
        except BadRequest as e:
            # Malformed HTML or chat not found. Not retryable — retrying just
            # burns quota on a message that can never send.
            return False, f"bad_request: {e}"
        except Exception as e:
            if attempt == 2:
                return False, str(e)[:300]
            await asyncio.sleep(2 ** attempt)
    return False, "exhausted retries"


def _record(rows):
    """Write the delivery ledger. Conflicts are expected and harmless."""
    if not rows:
        return
    try:
        supabase.table("alert_deliveries").upsert(
            rows, on_conflict="alert_id,user_id", ignore_duplicates=True
        ).execute()
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to write alert_deliveries: {e}")


def _mark_fanned_out(alert_ids):
    if not alert_ids:
        return
    try:
        supabase.table("alerts").update({"delivered": True}).in_("id", alert_ids).execute()
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to mark alerts delivered: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
async def deliver_pending_alerts():
    """One fan-out cycle. Safe to call on a loop; safe to interrupt."""
    alerts = _fetch_undelivered()
    if not alerts:
        return

    users = _fetch_active_users()
    if not users:
        logger.warning("[DELIVERY] %d alerts pending but no active users with a chat_id. "
                       "Leaving them undelivered.", len(alerts))
        return

    tickers = {
        (a.get("ticker") or "").upper()
        for a in alerts
        if not is_market_wide(a.get("source"), a.get("filing_type"))
    }
    watchers = _fetch_watchers(sorted(t for t in tickers if t and t not in ("MARKET", "UNKNOWN")))

    alert_ids   = [a["id"] for a in alerts]
    already     = _fetch_existing_deliveries(alert_ids)
    sent_today  = _fetch_todays_counts(list(users.keys()))

    bot = Bot(token=TELEGRAM_TOKEN)
    ledger = []
    fanned = []
    stats = {"sent": 0, "failed": 0, "skipped": 0, "no_audience": 0}

    for alert in alerts:
        aid = alert["id"]
        audience = resolve_audience(alert, users, watchers)

        if not audience:
            stats["no_audience"] += 1
            fanned.append(aid)
            continue

        text = build_message(alert, reason=delivery_reason(alert))

        for user, reason in audience:
            uid = user["user_id"]
            if (aid, uid) in already:
                continue

            if sent_today.get(uid, 0) >= user["max_alerts_per_day"]:
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "SKIPPED", "reason": "daily_cap_reached"})
                stats["skipped"] += 1
                continue

            ok, err = await _send_one(bot, user["chat_id"], text)
            if ok:
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "SENT", "reason": reason})
                sent_today[uid] = sent_today.get(uid, 0) + 1
                stats["sent"] += 1
            else:
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "FAILED", "reason": reason, "error": err})
                stats["failed"] += 1

            await asyncio.sleep(SEND_GAP_SECONDS)

        # Optional admin mirror, off unless explicitly configured.
        if ADMIN_CHANNEL_ID:
            await _send_one(bot, ADMIN_CHANNEL_ID, text)

        fanned.append(aid)

        # Flush periodically so a crash loses at most a few ledger rows.
        if len(ledger) >= 50:
            _record(ledger)
            ledger = []

    _record(ledger)
    _mark_fanned_out(fanned)

    if any(stats.values()):
        logger.info("[DELIVERY] %d alerts fanned out — sent=%d failed=%d skipped=%d no_audience=%d",
                    len(fanned), stats["sent"], stats["failed"], stats["skipped"], stats["no_audience"])


async def deliver_photo(image_path, caption, source, filing_type,
                        user_id=None, alert_id=None):
    """
    Send an image (heatmap) to the correct audience.

    The text fan-out above cannot carry an image, so heatmap modules call this
    directly — but they still go through the same audience rules rather than
    blasting a shared channel:

      user_id given  -> personal heatmap, that one user only
      user_id None   -> market-wide heatmap, every active user who has not set
                        `receive_market_wide = false`

    Returns (sent_count, failed_count).
    """
    users = _fetch_active_users()
    if user_id is not None:
        targets = [users[user_id]] if user_id in users else []
    else:
        targets = [u for u in users.values() if u["receive_market_wide"]]

    if not targets:
        logger.info("[DELIVERY] Photo %s: no eligible recipients", filing_type)
        return 0, 0

    bot = Bot(token=TELEGRAM_TOKEN)
    sent = failed = 0
    ledger = []

    for user in targets:
        try:
            with open(image_path, "rb") as fh:
                await bot.send_photo(
                    chat_id=user["chat_id"],
                    photo=fh,
                    caption=caption[:1024],   # Telegram caps captions at 1024 chars
                    parse_mode=ParseMode.HTML,
                )
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                with open(image_path, "rb") as fh:
                    await bot.send_photo(chat_id=user["chat_id"], photo=fh,
                                         caption=caption[:1024], parse_mode=ParseMode.HTML)
                sent += 1
            except Exception as e2:
                logger.warning("[DELIVERY] Photo retry failed for %s: %s", user["chat_id"], e2)
                failed += 1
        except Exception as e:
            logger.warning("[DELIVERY] Photo send failed for %s: %s", user["chat_id"], e)
            failed += 1

        if alert_id:
            ledger.append({
                "alert_id": alert_id, "user_id": user["user_id"], "chat_id": user["chat_id"],
                "status": "SENT" if sent else "FAILED",
                "reason": "personal watchlist heatmap" if user_id else "market-wide heatmap",
            })
        await asyncio.sleep(SEND_GAP_SECONDS)

    if ledger:
        _record(ledger)

    logger.info("[DELIVERY] Photo %s -> sent=%d failed=%d", filing_type, sent, failed)
    return sent, failed


def deliver_photo_sync(image_path, caption, source, filing_type, user_id=None, alert_id=None):
    """
    Blocking wrapper, for heatmap jobs running on the synchronous scheduler
    thread. Uses its own event loop so it cannot interfere with the main
    delivery loop running on the primary thread.
    """
    try:
        return asyncio.run(deliver_photo(image_path, caption, source, filing_type,
                                         user_id=user_id, alert_id=alert_id))
    except Exception as e:
        logger.error("[DELIVERY] deliver_photo_sync failed: %s", e)
        return 0, 0


def list_users_with_watchlists():
    """
    [(user_dict, [tickers])] for every active user who has at least one ticker.
    Used by the per-user watchlist heatmap so it renders only real watchlists.
    """
    users = _fetch_active_users()
    if not users:
        return []
    try:
        rows = supabase.table("watchlists").select("user_id, ticker").execute().data or []
    except Exception as e:
        logger.error("[DELIVERY] Failed to load watchlists: %s", e)
        return []

    by_user = {}
    for r in rows:
        uid = r.get("user_id")
        if uid in users and r.get("ticker"):
            by_user.setdefault(uid, []).append(r["ticker"].upper())

    return [(users[uid], sorted(set(tickers))) for uid, tickers in by_user.items() if tickers]


async def delivery_loop(interval_seconds=30):
    logger.info("[DELIVERY] Loop started (watchlist-scoped fan-out, %ss interval)", interval_seconds)
    while True:
        try:
            await deliver_pending_alerts()
        except Exception as e:
            logger.exception("[DELIVERY] Cycle failed, continuing: %s", e)
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(deliver_pending_alerts())
