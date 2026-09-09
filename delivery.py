"""
delivery.py
GQ FinXray US — per-user alert fan-out.

THE RULE THIS MODULE ENFORCES
-----------------------------
A user receives an alert about a company if, and only if, that company is on
their watchlist. Nothing else. There is no firehose channel, no "everyone gets
everything" path, no default subscription to the whole market, and no exceptions.

All features — including IPO alerts, sector heatmaps, macro digests, and ETF
flows — are routed strictly by watchlist ticker. Alerts about unwatchlisted
symbols are silently skipped at delivery time.

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
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest

from alert_formatter import build_message, delivery_reason
from feature_map import resolve_feature
from gquants_format_converter import make_frontend_link

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Optional admin firehose. Left UNSET by default — the whole point of this module
# is that nobody receives more than their watchlist asks for. Set it only if you
# want a private debug channel mirroring everything.
ADMIN_CHANNEL_ID = os.getenv("TELEGRAM_ADMIN_CHANNEL_ID") or None

ET = ZoneInfo("America/New_York")

# ── Market-wide routing ───────────────────────────────────────────────────────
# There are two different things that were both being called "market-wide", and
# collapsing them into one switch is what broke half the product.
#
#   1. COMPANY news about a company nobody follows. This must never be sent.
#      That was the actual complaint ("every alert should only be from my
#      watchlist"), and it stays absolutely closed — enforced by the watchlist
#      gate in ai_pipeline Stage 0 and by ticker matching below.
#
#   2. WHOLE-MARKET products the user explicitly subscribed to as features:
#      sector heatmaps, the ETF X-ray, the macro digest, the market reports,
#      ETF flow, upcoming IPOs. These have no company ticker by construction —
#      they are filed as ticker='MARKET' — so a watchlist match is impossible
#      and blanket-blocking them silently disabled ten of the thirteen daily
#      scheduled jobs. They ran, wrote their rows, matched nobody, and were
#      marked delivered. That is why heatmaps stopped arriving.
#
# The allowlist below re-enables (2) without touching (1).
BROADCAST_ENABLED = (os.getenv("GQ_ENABLE_MARKET_WIDE", "false").strip().lower()
                     in ("1", "true", "yes"))


def broadcast_enabled():
    """True when market-wide (ticker='MARKET') alerts can actually be delivered."""
    return BROADCAST_ENABLED


# Feature-level products that are market-scoped BY CONSTRUCTION. Every one of
# these is a thing the user turned on as a feature, not incidental news about an
# unwatched company.
MARKET_WIDE_FILING_TYPES = {
    "SECTOR_HEATMAP", "HEATMAP_DAILY_MIDDAY", "HEATMAP_DAILY_AFTERNOON",
    "HEATMAP_WEEKLY", "HEATMAP_MONTHLY",
    "MARKET_REPORT", "MACRO_BRIEFING", "ETF_XRAY",
    "IPO_UPCOMING", "INFLOW", "OUTFLOW",
}
MARKET_WIDE_SOURCES = {
    "SECTOR_HEATMAP", "MARKET_REPORT", "MACRO_ROUNDUP", "ETF_FLOW", "FMP_IPO",
}


def _is_market_wide(alert):
    """
    True when this alert is a whole-market product rather than company news.

    Company news for an unwatched ticker is NOT market-wide and never becomes
    market-wide by falling through this function — it simply finds no audience.
    """
    if not BROADCAST_ENABLED:
        return False

    if (alert.get("filing_type") or "").upper() in MARKET_WIDE_FILING_TYPES:
        return True
    if (alert.get("source") or "").upper() in MARKET_WIDE_SOURCES:
        return True

    # A row with no company ticker cannot be watchlist-routed by definition.
    ticker = (alert.get("ticker") or "").upper()
    if ticker in ("", "MARKET", "UNKNOWN"):
        return True
    return False


IMPACT_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

# Telegram permits ~30 messages/second across all chats. Stay comfortably under.
SEND_GAP_SECONDS = 0.05
# Telegram's per-chat ceiling is about one message per second. The 50ms global
# gap above is a per-BOT courtesy; without a per-chat gap a user receiving
# several alerts in one cycle gets them 50ms apart, which reliably triggers 429
# RetryAfter and consumes the send-retry budget.
PER_CHAT_GAP_SECONDS = float(os.getenv("GQ_PER_CHAT_GAP_SECONDS", "1.05"))
BATCH_LIMIT = 100

# How long a transiently-failing alert stays eligible for another delivery
# attempt before it is settled and dropped. Matches the pipeline's freshness
# window: past this point the content is stale anyway.
MAX_RETRY_AGE_HOURS = float(os.getenv("MAX_CONTENT_AGE_HOURS", "24"))


# ── Loading ───────────────────────────────────────────────────────────────────
# Past this, an alert is no longer news to the reader -- they have seen it
# elsewhere, and delivering it makes the product look slow rather than
# thorough. Matches the pipeline's own content window.
MAX_ALERT_AGE_MINUTES = float(os.getenv("GQ_MAX_ALERT_AGE_MINUTES", "90"))


_last_expiry_at = [0.0]
EXPIRY_INTERVAL_SECONDS = float(os.getenv("GQ_EXPIRY_INTERVAL_SECONDS", "60"))


def _expire_stale_alerts(force=False):
    """
    Settle alerts too old to be worth sending, in one bulk UPDATE.

    Without this the undelivered queue is append-only whenever delivery falls
    behind: it never drains, and every cycle re-reads the same stale head.
    Marking them delivered retires them without sending -- the alert row and
    its summary stay in the table for review, they just stop being queued.
    """
    # Throttled: the delivery loop cycles every few seconds, and re-running a
    # bulk UPDATE that enforces a 90-minute window on every cycle is ~20
    # pointless writes a minute.
    now = time.monotonic()
    if not force and (now - _last_expiry_at[0]) < EXPIRY_INTERVAL_SECONDS:
        return 0
    _last_expiry_at[0] = now

    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=MAX_ALERT_AGE_MINUTES)).isoformat()
    try:
        stale = (supabase.table("alerts")
                 .update({"delivered": True})
                 .eq("delivered", False)
                 .lt("created_at", cutoff)
                 .execute()).data or []
        if stale:
            logger.info("[DELIVERY] Retired %d alert(s) older than %.0f minutes "
                        "without sending", len(stale), MAX_ALERT_AGE_MINUTES)
        return len(stale)
    except Exception as e:
        logger.error("[DELIVERY] Failed to expire stale alerts: %s", e)
        return 0


def _fetch_undelivered(limit=BATCH_LIMIT):
    try:
        # NEWEST FIRST. This was .order("created_at") -- strict FIFO -- so
        # whenever a backlog built up, fresh alerts queued BEHIND stale ones
        # and, because a cycle takes only BATCH_LIMIT, could not be reached
        # until the whole backlog drained. That is how an alert generated 12
        # hours ago went out ahead of news that broke seconds ago. The stale
        # tail is expired above rather than being allowed to block the head.
        res = (supabase.table("alerts")
               .select("*")
               .eq("delivered", False)
               .order("created_at", desc=True)
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
                 .select("user_id, min_impact, muted_features, max_alerts_per_day, "
                         "receive_market_wide")
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
            "muted_features": set(p.get("muted_features") or []),
            "max_alerts_per_day": p.get("max_alerts_per_day") or 200,
            # No preferences row at all means "defaults", and the column default
            # is TRUE. `p.get(...) is not False` keeps a missing row opted IN
            # instead of silently opting the user out of every market-wide alert.
            "receive_market_wide": p.get("receive_market_wide") is not False,
        }
    return out


def _fetch_watchers(tickers):
    """
    {TICKER: {user_id, ...}} for the given tickers, in ONE query.

    Chunked at 200 tickers per request because PostgREST puts the `in.()` list in
    the URL and a 6,300-item list would exceed the server's URI length limit.

    Returns (watchers, unresolved) where `unresolved` is the set of tickers whose
    lookup FAILED. That distinction matters: a failed chunk used to be silently
    skipped, which made every ticker in it look like it had no watchers. The
    caller then recorded "no_audience" and marked the alert delivered forever, so
    a momentary database blip permanently destroyed real alerts. Callers must
    leave unresolved tickers pending instead of concluding nobody wants them.

    TIGHTENED: Filter out empty/malformed tickers strictly.
    """
    watchers = {}
    unresolved = set()
    # Tightened: Only include valid tickers (non-empty, alphanumeric + dash/dot)
    tickers = [t.upper().strip() for t in (tickers or [])
               if t and isinstance(t, str) and t.strip() and
               all(c.isalnum() or c in '-.' for c in t.strip().upper())]

    if not tickers:
        return watchers, unresolved

    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            rows = (supabase.table("watchlists")
                    .select("user_id, ticker")
                    .in_("ticker", chunk)
                    .execute()).data or []
        except Exception as e:
            logger.error(f"[DELIVERY] Watchlist lookup failed for chunk {i}: {e}")
            unresolved.update(chunk)
            continue
        for r in rows:
            ticker = (r.get("ticker") or "").upper().strip()
            if ticker and r.get("user_id"):  # Double-check both fields exist
                watchers.setdefault(ticker, set()).add(r["user_id"])
    return watchers, unresolved


def _fetch_existing_deliveries(alert_ids):
    """
    {(alert_id, user_id)} already SETTLED — the idempotency guard.

    BUGFIX 2026-08-19: this returned every ledger row regardless of status, so a
    row written with status='FAILED' counted as "already delivered" and the
    retry path could never reach that user. Combined with the alert being marked
    delivered=True on the same pass, one transient Telegram error meant the user
    never received that alert — permanently, with no way to notice.

    Only terminal outcomes suppress a resend. FAILED rows are deliberately
    excluded so the next cycle tries again.
    """
    seen = set()
    for i in range(0, len(alert_ids), 100):
        chunk = alert_ids[i:i + 100]
        try:
            rows = (supabase.table("alert_deliveries")
                    .select("alert_id, user_id, status")
                    .in_("alert_id", chunk)
                    .execute()).data or []
        except Exception as e:
            logger.error(f"[DELIVERY] Delivery-ledger lookup failed: {e}")
            continue
        for r in rows:
            if (r.get("status") or "").upper() in ("SENT", "SKIPPED", "UNDELIVERABLE"):
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

    Two routes:

      * company-specific  -> users whose watchlist contains the alert's ticker
      * market-wide       -> users who have not opted out of market-wide alerts

    The market-wide route exists because several features have no company to
    route on. An IPO's ticker does not trade yet so it cannot be watchlisted; the
    macro digest, ETF Xray, sector heatmap and daily market reports carry
    ticker="MARKET". Routing those strictly on the watchlist is what silenced
    them on 2026-08-11: resolve_audience returned [] and the fan-out marked them
    delivered without anyone seeing them.

    Both routes still honour min_impact and muted_features.
    """
    source      = alert.get("source")
    filing_type = alert.get("filing_type")
    ticker      = (alert.get("ticker") or "").upper()
    impact      = (alert.get("impact") or "LOW").upper()
    fid, _      = resolve_feature(source, filing_type)
    alert_rank  = IMPACT_RANK.get(impact, 1)

    if _is_market_wide(alert):
        if not broadcast_enabled():
            return []
        candidates = [u for u in users.values() if u.get("receive_market_wide", True)]
        if ticker and ticker not in ("MARKET", "UNKNOWN"):
            # e.g. an IPO — name the company, it reads better than "market-wide".
            reason = (f"You're receiving this because {ticker} is a market-wide "
                      f"update. Turn these off any time with /settings.")
        else:
            reason = ("You're receiving this because it's a market-wide update. "
                      "Turn these off any time with /settings.")
    else:
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
    """
    Send with Telegram's own rate-limit signal honoured.

    Returns (ok, error, permanent). `permanent` distinguishes "this message can
    never send to this chat" (blocked bot, malformed HTML) from "this attempt
    failed" (network, 5xx, timeout). Only the former may settle the alert; the
    latter must leave it pending so a later cycle retries.
    """
    for attempt in range(3):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return True, None, False
        except RetryAfter as e:
            # Telegram telling us exactly how long to wait. Obey it.
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except Forbidden as e:
            # User blocked the bot or deleted the chat. Not retryable.
            return False, f"forbidden: {e}", True
        except BadRequest as e:
            # Malformed HTML or chat not found. Not retryable — retrying just
            # burns quota on a message that can never send.
            return False, f"bad_request: {e}", True
        except Exception as e:
            if attempt == 2:
                # Transient (network, 5xx, timeout). Retryable on a later cycle.
                return False, str(e)[:300], False
            await asyncio.sleep(2 ** attempt)
    return False, "exhausted retries", False


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


def _past_retry_window(alert):
    """
    True once an alert is too old to be worth retrying.

    Bounds the retry loop introduced alongside the FAILED-send fix: a chat that
    is permanently unreachable for a non-permanent-looking reason would
    otherwise hold its alert pending forever and, because the queue is read
    oldest-first with a fixed limit, block every newer alert behind it.
    """
    stamp = alert.get("created_at")
    if not stamp:
        return True
    try:
        created = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
    return age_hours >= MAX_RETRY_AGE_HOURS


def _log_payload(alert):
    """
    Write the alert's structured XBRL/JSON payload to payload_log the moment it
    is about to reach Telegram — independent of whether GQUANTS_ALERT_BASE_URL
    is set, since the frontend link is no longer a prerequisite for logging.

    Upserts on alert_id so a retried delivery cycle (deferred alert, restarted
    process) never writes a duplicate row. Missing table or any DB error is
    swallowed to a warning: this is an audit trail, not part of the send path,
    and must never be the reason an alert fails to reach a user.
    """
    extra = alert.get("extra") if isinstance(alert.get("extra"), dict) else {}
    payload = extra.get("structured_payload")
    sec_json = extra.get("sec_json") or {}

    # Log anything carrying machine-readable data, not just a built payload:
    # an SEC filing alert's value here is the XBRL/JSON endpoints themselves,
    # which is exactly the "json/xbrl link based alert" this table is for.
    if not payload and not sec_json:
        return

    try:
        link = make_frontend_link(payload, str(alert.get("id") or "")) if payload else None
    except Exception:
        link = None
    # With no frontend link, the SEC endpoint IS the link worth keeping.
    link = link or sec_json.get("filing_index") or sec_json.get("companyfacts")

    record = dict(payload) if payload else {}
    if sec_json:
        record["sec_json"] = sec_json

    try:
        supabase.table("payload_log").upsert({
            "alert_id": alert.get("id"),
            "ticker": (alert.get("ticker") or "").upper(),
            "payload_type": (payload or {}).get("type") or "sec_json",
            "filing_type": alert.get("filing_type"),
            "source": alert.get("source"),
            "payload": record,
            "frontend_link": link,
        }, on_conflict="alert_id", ignore_duplicates=True).execute()
    except Exception as e:
        logger.warning("[DELIVERY] payload_log insert failed (run migrations/"
                       "2026-09-08_payload_log.sql?): %s", e)


def _mark_fanned_out(alert_ids):
    if not alert_ids:
        return
    try:
        supabase.table("alerts").update({"delivered": True}).in_("id", alert_ids).execute()
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to mark alerts delivered: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
async def deliver_pending_alerts():
    """
    One fan-out cycle. Safe to call on a loop; safe to interrupt.

    Sends are parallel ACROSS users. The previous version had one single loop
    that sent every recipient of every alert strictly one after another, so a
    cycle with 50 alerts x 10 recipients each did 500 sequential sends at
    ~1.05s apart (PER_CHAT_GAP_SECONDS) — over 8 minutes for a batch that
    should land within a couple of seconds. The 1-second-per-chat pacing is a
    real Telegram constraint, but it only applies to repeat sends to the SAME
    chat_id — it does not require serializing different chats behind each
    other. Every user's own queue is still sent to in order (so a user with
    three alerts this cycle gets them spaced out safely); different users'
    queues run concurrently via asyncio.gather, so N users drop their alerts
    at roughly the same moment instead of one after another.
    """
    _expire_stale_alerts()

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
    }
    watchers, unresolved = _fetch_watchers(
        sorted(t for t in tickers if t and t not in ("MARKET", "UNKNOWN")))

    alert_ids   = [a["id"] for a in alerts]
    already     = _fetch_existing_deliveries(alert_ids)
    sent_today  = _fetch_todays_counts(list(users.keys()))

    bot = Bot(token=TELEGRAM_TOKEN)
    ledger = []
    stats = {"sent": 0, "failed": 0, "skipped": 0, "no_audience": 0, "deferred": 0, "errored": 0}

    # aid -> {"alert": row, "retry_needed": bool, "fanned": bool}
    alert_state = {}
    # user_id -> [(aid, user, text, reason), ...], sent in order, one task/user
    per_user_queue = {}

    # ── Phase 1: resolve audience + build message text for every alert ────────
    # Cheap, synchronous, no network — safe to do inline before fanning out the
    # actual sends. Also the single choke point where every structured XBRL/JSON
    # payload gets logged, once per alert, regardless of which poller built it.
    for alert in alerts:
        aid = alert["id"]

        # A ticker whose watchlist lookup errored is UNKNOWN, not unwatched.
        # Leave the alert pending so the next cycle can route it properly rather
        # than burning it as "nobody wanted this".
        if (alert.get("ticker") or "").upper() in unresolved:
            stats["deferred"] += 1
            continue

        # One malformed alert must not take down the cycle. Without this, a bad
        # `extra` payload raised out of build_message() before _record() and
        # _mark_fanned_out() ever ran, so every already-sent message in the batch
        # was re-sent from scratch on the next 30-second pass — a duplicate storm
        # that repeated until the offending row was manually removed.
        try:
            audience = resolve_audience(alert, users, watchers)

            if not audience:
                stats["no_audience"] += 1
                alert_state[aid] = {"alert": alert, "retry_needed": False, "fanned": True}
                continue

            text = build_message(alert, reason=delivery_reason(alert))
            _log_payload(alert)

            alert_state[aid] = {"alert": alert, "retry_needed": False, "fanned": False}

            for user, reason in audience:
                uid = user["user_id"]
                if (aid, uid) in already:
                    continue

                if sent_today.get(uid, 0) >= user["max_alerts_per_day"]:
                    ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                                   "status": "SKIPPED", "reason": "daily_cap_reached"})
                    stats["skipped"] += 1
                    continue

                per_user_queue.setdefault(uid, []).append((aid, user, text, reason))
                # Reserve the slot now so two alerts to the same user in this
                # cycle both see the incremented count before either sends.
                sent_today[uid] = sent_today.get(uid, 0) + 1

            # Optional admin mirror, off unless explicitly configured. Fired
            # once per alert here rather than per recipient.
            if ADMIN_CHANNEL_ID:
                await _send_one(bot, ADMIN_CHANNEL_ID, text)
        except Exception as e:
            # Mark it fanned out anyway: it is structurally broken, and retrying
            # it forever would block the queue behind a row that can never send.
            logger.exception("[DELIVERY] Alert %s failed to process, skipping: %s", aid, e)
            stats["errored"] += 1
            alert_state[aid] = {"alert": alert, "retry_needed": False, "fanned": True}

    # ── Phase 2: fan out concurrently, one task per user ───────────────────────
    async def _drain_user_queue(uid, items):
        for aid, user, text, reason in items:
            ok, err, permanent = await _send_one(bot, user["chat_id"], text)
            if ok:
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "SENT", "reason": reason})
                stats["sent"] += 1
            elif permanent:
                # Nothing will ever make this send succeed. Record it as
                # terminal so it is not retried forever.
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "UNDELIVERABLE", "reason": reason, "error": err})
                stats["failed"] += 1
            else:
                ledger.append({"alert_id": aid, "user_id": uid, "chat_id": user["chat_id"],
                               "status": "FAILED", "reason": reason, "error": err})
                stats["failed"] += 1
                alert_state[aid]["retry_needed"] = True

            # Telegram allows roughly one message per second per chat. This gap
            # only serializes repeat sends to THIS chat_id — it no longer holds
            # up any other user's queue, which is what made fan-out slow.
            await asyncio.sleep(max(SEND_GAP_SECONDS, PER_CHAT_GAP_SECONDS))

    if per_user_queue:
        await asyncio.gather(*(
            _drain_user_queue(uid, items) for uid, items in per_user_queue.items()
        ))

    fanned = []
    for aid, state in alert_state.items():
        if state["fanned"]:
            fanned.append(aid)
        elif state["retry_needed"] and not _past_retry_window(state["alert"]):
            stats["deferred"] += 1
        else:
            fanned.append(aid)

    _record(ledger)
    _mark_fanned_out(fanned)

    if any(stats.values()):
        logger.info("[DELIVERY] %d alerts fanned out — sent=%d failed=%d skipped=%d "
                    "no_audience=%d deferred=%d errored=%d",
                    len(fanned), stats["sent"], stats["failed"], stats["skipped"],
                    stats["no_audience"], stats["deferred"], stats["errored"])


async def deliver_photo(image_path, caption, source, filing_type,
                        user_id=None, alert_id=None):
    """
    Send an image (heatmap) to the correct audience.

    The text fan-out above cannot carry an image, so heatmap modules call this
    directly — but they still go through the same audience rules:

      user_id given  -> personal heatmap (watchlist heatmap), that one user only
      user_id None   -> market-wide heatmap (sector heatmap), every user who has
                        not opted out of market-wide alerts

    The `user_id is None` branch used to be a hardcoded `targets = []`, which is
    why sector heatmaps stopped being delivered on 2026-08-11 — the image was
    rendered every day and then thrown away.

    Returns (sent_count, failed_count).
    """
    users = _fetch_active_users()
    if user_id is not None:
        targets = [users[user_id]] if user_id in users else []
    elif broadcast_enabled():
        targets = [u for u in users.values() if u.get("receive_market_wide", True)]
    else:
        targets = []

    if not targets:
        logger.info("[DELIVERY] Photo %s: no eligible recipients", filing_type)
        return 0, 0

    bot = Bot(token=TELEGRAM_TOKEN)
    sent = failed = 0
    ledger = []

    for user in targets:
        ok = False
        try:
            with open(image_path, "rb") as fh:
                await bot.send_photo(
                    chat_id=user["chat_id"],
                    photo=fh,
                    caption=caption[:1024],   # Telegram caps captions at 1024 chars
                    parse_mode=ParseMode.HTML,
                )
            ok = True
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                with open(image_path, "rb") as fh:
                    await bot.send_photo(chat_id=user["chat_id"], photo=fh,
                                         caption=caption[:1024], parse_mode=ParseMode.HTML)
                ok = True
            except Exception as e2:
                logger.warning("[DELIVERY] Photo retry failed for %s: %s", user["chat_id"], e2)
        except Exception as e:
            logger.warning("[DELIVERY] Photo send failed for %s: %s", user["chat_id"], e)

        sent += 1 if ok else 0
        failed += 0 if ok else 1

        if alert_id:
            # Status must reflect THIS user's send. It previously read the
            # running `sent` counter, so once any user succeeded every later
            # recipient was logged SENT even when their delivery had failed.
            ledger.append({
                "alert_id": alert_id, "user_id": user["user_id"], "chat_id": user["chat_id"],
                "status": "SENT" if ok else "FAILED",
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
