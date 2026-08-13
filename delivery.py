"""
delivery.py
GQ FinXray US — per-user alert fan-out.

FIXED (Aug 13 2026):
- Market-wide alerts now route to all opted-in users, not empty list
- resolve_audience() checks if alert is market-wide and returns all users if broadcast_enabled
- deliver_photo() routes heatmaps to market-wide users
- GQ_ENABLE_MARKET_WIDE defaults to "true" so features work out of box
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
from feature_map import resolve_feature

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

ADMIN_CHANNEL_ID = os.getenv("TELEGRAM_ADMIN_CHANNEL_ID") or None

ET = ZoneInfo("America/New_York")

# FIXED: Default to "true" so market-wide features work immediately
BROADCAST_ENABLED = (os.getenv("GQ_ENABLE_MARKET_WIDE", "true").strip().lower()
                     in ("1", "true", "yes"))


def broadcast_enabled():
    """True when market-wide (ticker='MARKET') alerts can actually be delivered."""
    return BROADCAST_ENABLED


# Market-wide features that bypass watchlist gating
MARKET_WIDE_FILING_TYPES = {
    "IPO_UPCOMING",
    "MACRO_BRIEFING",
    "ETF_XRAY",
    "MARKET_REPORT",
    "SECTOR_HEATMAP",
}
MARKET_WIDE_SOURCES = {
    "FMP_IPO",
    "MACRO_ROUNDUP",
    "MARKET_WATCHDOG",
    "ETF_XRAY",
}


def _is_market_wide(alert):
    """Check if alert is market-wide (not watchlist-scoped)."""
    ticker = alert.get("ticker", "").strip().upper()
    filing_type = alert.get("filing_type", "")
    source = alert.get("source", "")
    return (ticker == "MARKET" or
            filing_type in MARKET_WIDE_FILING_TYPES or
            source in MARKET_WIDE_SOURCES)


def resolve_audience(alert):
    """
    FIXED: Market-wide alerts now return all users (if broadcast enabled).
    Watchlist-scoped alerts return only users watching that ticker.
    """
    ticker = alert.get("ticker", "").strip().upper()

    # Market-wide alert
    if _is_market_wide(alert):
        if not broadcast_enabled():
            return []
        # Return all users who haven't opted out
        try:
            rows = supabase.table("users").select("id").execute()
            users = [r["id"] for r in (rows.data or []) if r.get("id")]
            return users
        except Exception as e:
            logger.error(f"[DELIVERY] Failed to load all users for market-wide: {e}")
            return []

    # Watchlist-scoped alert
    if not ticker or ticker == "MARKET":
        return []

    try:
        rows = (supabase.table("user_watchlist")
                .select("user_id")
                .eq("ticker", ticker)
                .execute())
        users = list(set(r["user_id"] for r in (rows.data or []) if r.get("user_id")))
        return users
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to resolve audience for {ticker}: {e}")
        return []


def deliver_photo(alert, user_id=None):
    """
    FIXED: Route heatmaps to market-wide users when user_id is None.
    """
    heatmap_id = alert.get("heatmap_id")
    if not heatmap_id:
        return

    # Determine target users
    if user_id:
        targets = [user_id]
    elif _is_market_wide(alert):
        # Market-wide heatmap
        if not broadcast_enabled():
            return
        targets = resolve_audience(alert)
    else:
        targets = []

    if not targets:
        return

    try:
        heatmap = supabase.table("heatmap_messages").select("*").eq("id", heatmap_id).single().execute()
        if not heatmap.data:
            return

        for target_user_id in targets:
            try:
                supabase.table("notification_heatmaps").insert({
                    "user_id": target_user_id,
                    "heatmap_id": heatmap_id,
                }).execute()
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    logger.error(f"[DELIVERY] Failed to queue heatmap for user {target_user_id}: {e}")
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to fetch heatmap {heatmap_id}: {e}")


async def deliver_to_user(user_id, alert):
    """
    Send alert to one user if they should receive it.
    """
    try:
        user_row = supabase.table("users").select("*").eq("id", user_id).single().execute()
        user = user_row.data if user_row.data else {}
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to load user {user_id}: {e}")
        return False

    chat_id = user.get("telegram_chat_id")
    if not chat_id:
        return False

    # Check min_impact threshold
    try:
        prefs = supabase.table("user_preferences").select("*").eq("user_id", user_id).single().execute()
        prefs_data = prefs.data if prefs.data else {}
    except:
        prefs_data = {}

    min_impact = prefs_data.get("min_impact", "LOW")
    impact_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    alert_impact = impact_order.get(alert.get("impact", "LOW"), 0)
    threshold_impact = impact_order.get(min_impact, 0)
    if alert_impact < threshold_impact:
        return False

    # Check muted features
    muted = prefs_data.get("muted_features", [])
    if alert.get("filing_type") in muted:
        return False

    # Check market-wide opt-out
    if _is_market_wide(alert) and not prefs_data.get("receive_market_wide", True):
        return False

    # Build and send message
    try:
        msg_text = build_message(alert, user_id)
        if not msg_text:
            return False

        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=chat_id,
            text=msg_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

        # Mark as delivered
        supabase.table("alert_deliveries").insert({
            "alert_id": alert["id"],
            "user_id": user_id,
            "delivered_at": datetime.now(ET).isoformat(),
        }).execute()

        return True
    except RetryAfter as e:
        logger.warning(f"[DELIVERY] Rate limited for user {user_id}: {e}")
        return False
    except Forbidden:
        logger.warning(f"[DELIVERY] User {user_id} blocked bot")
        return False
    except BadRequest as e:
        logger.error(f"[DELIVERY] Bad message format for user {user_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to send to user {user_id}: {e}")
        return False


async def fan_out_alert(alert):
    """
    Send one alert to all eligible users (watchlist-scoped or market-wide).
    Returns count of successful deliveries.
    """
    audience = resolve_audience(alert)
    if not audience:
        # Mark delivered even if nobody got it (e.g., unwatched ticker)
        supabase.table("alerts").update({"delivered": True}).eq("id", alert["id"]).execute()
        return 0

    tasks = [deliver_to_user(user_id, alert) for user_id in audience]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    sent = sum(1 for r in results if r is True)

    # Mark alert as fanned out
    supabase.table("alerts").update({"delivered": True}).eq("id", alert["id"]).execute()

    return sent


async def run_delivery():
    """
    Main delivery loop: fetch undelivered alerts and fan out to users.
    """
    try:
        alerts = supabase.table("alerts").select("*").eq("delivered", False).execute()
        alert_list = alerts.data or []
    except Exception as e:
        logger.error(f"[DELIVERY] Failed to fetch alerts: {e}")
        return

    if not alert_list:
        return

    tasks = [fan_out_alert(alert) for alert in alert_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    sent = sum(r for r in results if isinstance(r, int))

    logger.info(f"[DELIVERY] Fanned out {len(alert_list)} alerts to {sent} user-instances")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_delivery())
