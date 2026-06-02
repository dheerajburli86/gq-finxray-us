import asyncio
import os
import time
import threading
import schedule
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
from datetime import datetime

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Import pollers ────────────────────────────────────────────────────────────
from edgar_poller import poll_sec_8k, poll_sec_form4, load_cik_map
from news_poller import poll_all_news

# ── Alert formatter ───────────────────────────────────────────────────────────
def format_alert(alert):
    impact = alert.get("impact", "LOW")
    ticker = alert.get("ticker", "UNKNOWN")
    summary = alert.get("summary", "")
    source = alert.get("source", "SEC_EDGAR")
    filing_type = alert.get("filing_type", "")

    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    source_labels = {
        "SEC_EDGAR": "SEC EDGAR",
        "CNBC": "CNBC",
        "BLOOMBERG": "Bloomberg",
        "MARKETWATCH": "MarketWatch"
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_labels.get(source, source)
    time_str = datetime.now().strftime("%I:%M %p IST")

    # Base message
    msg = f"{emoji} *{impact} — ${ticker}*\n\n{summary}\n\n📋 {source_name}"
    if filing_type and filing_type != "NEWS":
        msg += f" · {filing_type}"
    msg += f" · {time_str}"

    return msg

# ── Deliver pending alerts ────────────────────────────────────────────────────
async def deliver_pending_alerts():
    try:
        # Get undelivered alerts
        result = supabase.table("alerts") \
            .select("*") \
            .eq("delivered", False) \
            .order("created_at") \
            .limit(20) \
            .execute()

        alerts = result.data
        if not alerts:
            return

        bot = Bot(token=TELEGRAM_TOKEN)

        for alert in alerts:
            ticker = alert.get("ticker", "UNKNOWN")
            impact = alert.get("impact", "LOW")

            # Find users subscribed to this ticker
            # who want alerts at this impact level
            users_result = supabase.table("watchlists") \
                .select("user_id, users(telegram_chat_id)") \
                .eq("ticker", ticker) \
                .execute()

            # Also get user preferences
            delivered_to = []
            impact_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

            for watchlist_item in users_result.data:
                user_id = watchlist_item.get("user_id")
                user_data = watchlist_item.get("users", {})
                chat_id = user_data.get("telegram_chat_id") if user_data else None

                if not chat_id:
                    continue

                # Check user's minimum impact preference
                pref_result = supabase.table("user_preferences") \
                    .select("min_impact") \
                    .eq("user_id", user_id) \
                    .execute()

                min_impact = "LOW"
                if pref_result.data:
                    min_impact = pref_result.data[0].get("min_impact", "LOW")

                # Only send if alert meets user's minimum threshold
                if impact_order.get(impact, 1) >= impact_order.get(min_impact, 1):
                    msg = format_alert(alert)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=msg,
                            parse_mode="Markdown"
                        )
                        delivered_to.append(chat_id)

                        # Log delivery
                        supabase.table("delivery_logs").insert({
                            "alert_id": alert["id"],
                            "user_id": user_id,
                            "channel": "telegram",
                            "status": "sent",
                            "attempts": 1,
                            "delivered_at": datetime.now().isoformat()
                        }).execute()

                    except Exception as e:
                        print(f"[ERROR] Failed to deliver to {chat_id}: {e}")
                        supabase.table("delivery_logs").insert({
                            "alert_id": alert["id"],
                            "user_id": user_id,
                            "channel": "telegram",
                            "status": "failed",
                            "attempts": 1
                        }).execute()

            # Also post HIGH impact alerts to channel
            if impact == "HIGH" and TELEGRAM_CHANNEL_ID:
                try:
                    msg = format_alert(alert)
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
                    print(f"[CHANNEL] Posted HIGH alert for {ticker}")
                except Exception as e:
                    print(f"[ERROR] Failed to post to channel: {e}")

            # Mark alert as delivered
            supabase.table("alerts") \
                .update({"delivered": True}) \
                .eq("id", alert["id"]) \
                .execute()

            if delivered_to:
                print(f"[DELIVERED] {impact} — {ticker} → {len(delivered_to)} users")

    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")

# ── Scheduler thread ──────────────────────────────────────────────────────────
def run_scheduler():
    print("[SCHEDULER] Starting...")
    load_cik_map()

    # Initial polls
    poll_sec_8k()
    poll_sec_form4()
    poll_all_news()

    # Schedule recurring polls
    schedule.every(30).seconds.do(poll_sec_8k)
    schedule.every(30).seconds.do(poll_sec_form4)
    schedule.every(60).seconds.do(poll_all_news)

    print("[SCHEDULER] All pollers running.")
    while True:
        schedule.run_pending()
        time.sleep(1)

# ── AI pipeline thread ────────────────────────────────────────────────────────
def run_pipeline():
    print("[PIPELINE] Starting...")
    while True:
        try:
            from ai_pipeline import run_pipeline as process
            process()
        except Exception as e:
            print(f"[PIPELINE ERROR] {e}")
        time.sleep(60)

# ── Delivery loop ─────────────────────────────────────────────────────────────
async def delivery_loop():
    print("[DELIVERY] Starting...")
    while True:
        await deliver_pending_alerts()
        await asyncio.sleep(30)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print(f"""
╔══════════════════════════════════════════╗
║       GQ FinXray US — Starting Up        ║
║  SEC EDGAR + News + AI + Telegram        ║
╚══════════════════════════════════════════╝
    """)

    # Start scheduler in background thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Start AI pipeline in background thread
    pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
    pipeline_thread.start()

    # Run delivery loop in main async thread
    await delivery_loop()

if __name__ == "__main__":
    asyncio.run(main())