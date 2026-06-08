import asyncio
import os
import time
import threading
import schedule
import requests
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from telegram import Update
from datetime import datetime

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

from edgar_poller import poll_sec_8k, poll_sec_form4, load_cik_map
from news_poller import poll_all_news

# ── TwelveData price fetch ────────────────────────────────────────────────────
def get_stock_price(ticker: str):
    try:
        url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVEDATA_API_KEY}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "error" or "close" not in data:
            return None
        price = float(data.get("close", 0))
        change_pct = float(data.get("percent_change", 0))
        arrow = "🟢" if change_pct >= 0 else "🔴"
        sign = "+" if change_pct >= 0 else ""
        return {
            "price": f"${price:,.2f}",
            "change": f"{sign}{change_pct:.2f}%",
            "arrow": arrow
        }
    except Exception as e:
        print(f"[TWELVEDATA] Price fetch failed for {ticker}: {e}")
        return None

# ── Alert formatter ───────────────────────────────────────────────────────────
def format_alert(alert):
    impact = alert.get("impact", "LOW")
    ticker = alert.get("ticker", "UNKNOWN")
    summary = alert.get("summary", "")
    source = alert.get("source", "SEC_EDGAR")
    filing_type = alert.get("filing_type", "")
    extra = alert.get("extra") or {}
    filing_url = alert.get("filing_url", "")

    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    source_labels = {
        "SEC_EDGAR": "SEC EDGAR",
        "CNBC": "CNBC",
        "BLOOMBERG": "Bloomberg",
        "MARKETWATCH": "MarketWatch",
        "REUTERS": "Reuters"
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_labels.get(source, source)
    time_str = datetime.now().strftime("%I:%M %p EST")

    # Live price
    price_line = ""
    if ticker and ticker not in ("MARKET", "UNKNOWN"):
        price_data = get_stock_price(ticker)
        if price_data:
            price_line = f"\n📈 *Stock:* {ticker} {price_data['arrow']} {price_data['price']} ({price_data['change']})\n"

    # Source link
    source_link = ""
    if filing_url:
        if source == "SEC_EDGAR":
            source_link = f"\n🔗 [View SEC Filing]({filing_url})"
        elif source == "CNBC":
            source_link = f"\n🔗 [Read on CNBC]({filing_url})"
        elif source == "BLOOMBERG":
            source_link = f"\n🔗 [Read on Bloomberg]({filing_url})"
        elif source == "MARKETWATCH":
            source_link = f"\n🔗 [Read on MarketWatch]({filing_url})"
        elif source == "REUTERS":
            source_link = f"\n🔗 [Read on Reuters]({filing_url})"
        else:
            source_link = f"\n🔗 [Read Full Article]({filing_url})"

    footer = (
        f"{source_link}\n\n"
        f"_You are receiving this notification based on your request to "
        f"monitor this stock's news, updates and transactions._\n"
        f"_Disclaimer: gquants.com/disclaimer_\n\n"
        f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
    )

    # Form 4 insider trade
    if filing_type == "4":
        insider = extra.get("insider_name", "An insider")
        transaction = extra.get("transaction_type", "")
        trans_emoji = "🟢" if transaction == "BUY" else "🔴" if transaction == "SELL" else "📋"
        return (
            f"{trans_emoji} *INSIDER {transaction or 'TRADE'} — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"👤 {insider}\n"
            f"📋 SEC Form 4 · {time_str}"
            f"{footer}"
        )

    # S-1 IPO
    if filing_type == "S-1":
        return (
            f"🚀 *IPO FILING — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"📋 SEC S-1 · {time_str}"
            f"{footer}"
        )

    # News
    if filing_type == "NEWS":
        ticker_display = f"${ticker}" if ticker != "MARKET" else "Market News"
        return (
            f"{emoji} *{source_name} — {ticker_display}*"
            f"{price_line}\n"
            f"🔍 *Xray Intel:* {summary}\n\n"
            f"📰 {source_name} · {time_str}"
            f"{footer}"
        )

    # Default 8-K
    item_types = extra.get("item_types", [])
    items_str = ""
    if item_types:
        first_item = item_types[0].split(":")[0].strip()
        items_str = f" · {first_item}"

    return (
        f"{emoji} *{impact} — ${ticker}*"
        f"{price_line}\n"
        f"🔍 *Xray Intel:* {summary}\n\n"
        f"📋 {source_name}{items_str} · {time_str}"
        f"{footer}"
    )

def get_feedback_keyboard(alert_id: str):
    keyboard = [[
        InlineKeyboardButton("✅ Useful", callback_data=f"useful_{alert_id}"),
        InlineKeyboardButton("❌ Not Useful", callback_data=f"notuseful_{alert_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

# ── Send alert with optional image ───────────────────────────────────────────
async def send_telegram_message(bot, chat_id, alert):
    msg = format_alert(alert)
    extra = alert.get("extra") or {}
    image_url = extra.get("image_url", "")
    alert_id = str(alert.get("id", ""))
    keyboard = get_feedback_keyboard(alert_id)

    if image_url:
        caption = msg[:1024]
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return True
        except Exception:
            pass

    await bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return True

# ── Error alerting ────────────────────────────────────────────────────────────
async def send_error_alert(message: str):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=f"⚠️ *GQ FinXray US — System Alert*\n\n{message}\n\n🕐 {datetime.now().strftime('%I:%M %p IST')}",
            parse_mode="Markdown"
        )
    except:
        pass

# ── Feedback callback handler ─────────────────────────────────────────────────
async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = str(query.from_user.id)

    if data.startswith("useful_"):
        alert_id = data.replace("useful_", "")
        feedback = "useful"
    elif data.startswith("notuseful_"):
        alert_id = data.replace("notuseful_", "")
        feedback = "not_useful"
    else:
        return

    try:
        supabase.table("feedback").insert({
            "alert_id": alert_id,
            "telegram_chat_id": chat_id,
            "feedback": feedback,
            "created_at": datetime.now().isoformat()
        }).execute()
        await query.edit_message_reply_markup(reply_markup=None)
        print(f"[FEEDBACK] {feedback} — alert {alert_id} from {chat_id}")
    except Exception as e:
        print(f"[ERROR] Feedback logging failed: {e}")

# ── Deliver pending alerts ────────────────────────────────────────────────────
async def deliver_pending_alerts():
    try:
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
        impact_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

        for alert in alerts:
            ticker = alert.get("ticker", "UNKNOWN")
            impact = alert.get("impact", "LOW")

            if ticker == "UNKNOWN":
                supabase.table("alerts") \
                    .update({"delivered": True}) \
                    .eq("id", alert["id"]) \
                    .execute()
                continue

            # MARKET ticker — HIGH/MEDIUM to channel only
            if ticker == "MARKET":
                if impact in ("HIGH", "MEDIUM") and TELEGRAM_CHANNEL_ID:
                    try:
                        await send_telegram_message(bot, TELEGRAM_CHANNEL_ID, alert)
                        print(f"[CHANNEL] {impact} market alert posted")
                    except Exception as e:
                        print(f"[ERROR] Channel post failed: {e}")
                supabase.table("alerts") \
                    .update({"delivered": True}) \
                    .eq("id", alert["id"]) \
                    .execute()
                continue

            # Find subscribed users
            watchlist_result = supabase.table("watchlists") \
                .select("user_id") \
                .eq("ticker", ticker) \
                .execute()

            delivered_to = []

            for item in watchlist_result.data:
                user_id = item.get("user_id")
                if not user_id:
                    continue

                user_result = supabase.table("users") \
                    .select("telegram_chat_id") \
                    .eq("id", user_id) \
                    .execute()

                if not user_result.data:
                    continue

                chat_id = user_result.data[0].get("telegram_chat_id")
                if not chat_id:
                    continue

                pref_result = supabase.table("user_preferences") \
                    .select("min_impact") \
                    .eq("user_id", user_id) \
                    .execute()

                min_impact = "LOW"
                if pref_result.data:
                    min_impact = pref_result.data[0].get("min_impact", "LOW")

                if impact_order.get(impact, 1) >= impact_order.get(min_impact, 1):
                    try:
                        await send_telegram_message(bot, chat_id, alert)
                        delivered_to.append(chat_id)
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

            # Post HIGH and MEDIUM to channel
            if impact in ("HIGH", "MEDIUM") and TELEGRAM_CHANNEL_ID:
                try:
                    await send_telegram_message(bot, TELEGRAM_CHANNEL_ID, alert)
                    print(f"[CHANNEL] {impact} alert posted — ${ticker}")
                except Exception as e:
                    print(f"[ERROR] Channel post failed: {e}")

            # Mark delivered
            supabase.table("alerts") \
                .update({"delivered": True}) \
                .eq("id", alert["id"]) \
                .execute()

            if delivered_to:
                print(f"[DELIVERED] {impact} — ${ticker} → {len(delivered_to)} users")

    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")

# ── Market report helpers ─────────────────────────────────────────────────────
def fetch_index_data():
    indices = {"SPY": "S&P 500", "QQQ": "NASDAQ", "DIA": "Dow Jones"}
    lines = []
    for symbol, name in indices.items():
        try:
            url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVEDATA_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if "close" in data:
                price = float(data["close"])
                chg = float(data.get("percent_change", 0))
                arrow = "🟢" if chg >= 0 else "🔴"
                sign = "+" if chg >= 0 else ""
                lines.append(f"{arrow} *{name}:* ${price:,.2f} ({sign}{chg:.2f}%)")
        except:
            pass
    return "\n".join(lines) if lines else "Index data unavailable"

def fetch_macro_data():
    instruments = {"GLD": "Gold ETF", "USO": "Oil ETF", "UUP": "USD Index ETF"}
    lines = []
    for symbol, name in instruments.items():
        try:
            url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVEDATA_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if "close" in data:
                price = float(data["close"])
                chg = float(data.get("percent_change", 0))
                arrow = "🟢" if chg >= 0 else "🔴"
                sign = "+" if chg >= 0 else ""
                lines.append(f"{arrow} *{name}:* ${price:,.2f} ({sign}{chg:.2f}%)")
        except:
            pass
    return "\n".join(lines) if lines else "Macro data unavailable"

def fetch_top_movers():
    tickers = ["AAPL", "NVDA", "TSLA", "MSFT", "META", "AMZN", "GOOGL", "JPM", "NFLX", "AMD"]
    results = []
    for ticker in tickers:
        try:
            url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVEDATA_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if "close" in data:
                chg = float(data.get("percent_change", 0))
                results.append({
                    "ticker": ticker,
                    "change": chg,
                    "price": float(data["close"])
                })
        except:
            pass
    if not results:
        return "Movers data unavailable", "Movers data unavailable"

    results.sort(key=lambda x: x["change"], reverse=True)
    actual_gainers = [r for r in results if r["change"] > 0]
    actual_losers = [r for r in results if r["change"] < 0]

    gainers = "\n".join([f"🟢 *{r['ticker']}:* +{r['change']:.2f}%" for r in actual_gainers[:3]]) if actual_gainers else "_No gainers_"
    losers = "\n".join([f"🔴 *{r['ticker']}:* {r['change']:.2f}%" for r in actual_losers[-3:]]) if actual_losers else "_No losers_"
    return gainers, losers

async def send_market_report(title: str, body: str):
    if not TELEGRAM_CHANNEL_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        time_str = datetime.now().strftime("%I:%M %p EST")
        msg = f"📊 *{title}*\n_{time_str}_\n\n{body}\n\n_GQ FinXray US · gquants.com_"
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=msg,
            parse_mode="Markdown"
        )
        print(f"[REPORT] Sent: {title}")
    except Exception as e:
        print(f"[ERROR] Failed to send market report: {e}")

def send_premarket_report():
    indices = fetch_index_data()
    macro = fetch_macro_data()
    body = f"*US Futures & Pre-Market Snapshot*\n\n{indices}\n\n*Macro*\n{macro}"
    asyncio.run(send_market_report("🌅 Pre-Market Report", body))

def send_market_open_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    body = f"*Markets are now open.*\n\n*Indices at Open*\n{indices}\n\n*Early Gainers*\n{gainers}\n\n*Early Losers*\n{losers}"
    asyncio.run(send_market_report("🔔 Market Open", body))

def send_midday_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    body = f"*Midday Market Check*\n\n*Indices*\n{indices}\n\n*Top Gainers*\n{gainers}\n\n*Top Losers*\n{losers}"
    asyncio.run(send_market_report("⏱ Midday Pulse", body))

def send_market_close_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    macro = fetch_macro_data()
    body = f"*Markets have closed.*\n\n*Final Index Levels*\n{indices}\n\n*Top Gainers*\n{gainers}\n\n*Top Losers*\n{losers}\n\n*Macro*\n{macro}"
    asyncio.run(send_market_report("📉 Market Close Report", body))

def send_afterhours_report():
    gainers, losers = fetch_top_movers()
    body = f"*After-Hours Notable Movers*\n\n*Gainers*\n{gainers}\n\n*Losers*\n{losers}"
    asyncio.run(send_market_report("🌙 After-Hours Movers", body))

# ── Scheduler thread ──────────────────────────────────────────────────────────
def run_scheduler():
    print("[SCHEDULER] Starting...")
    load_cik_map()
    poll_sec_8k()
    poll_sec_form4()
    poll_all_news()
    schedule.every(30).seconds.do(poll_sec_8k)
    schedule.every(30).seconds.do(poll_sec_form4)
    schedule.every(60).seconds.do(poll_all_news)
    schedule.every().day.at("09:25").do(send_premarket_report)
    schedule.every().day.at("09:30").do(send_market_open_report)
    schedule.every().day.at("13:00").do(send_midday_report)
    schedule.every().day.at("16:00").do(send_market_close_report)
    schedule.every().day.at("16:30").do(send_afterhours_report)
    print("[SCHEDULER] All pollers and market reports scheduled.")
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
            asyncio.run(send_error_alert(f"Pipeline error: {str(e)}"))
        time.sleep(30)

# ── Delivery loop ─────────────────────────────────────────────────────────────
async def delivery_loop():
    print("[DELIVERY] Starting...")
    while True:
        await deliver_pending_alerts()
        await asyncio.sleep(30)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("""
╔══════════════════════════════════════════╗
║       GQ FinXray US — Starting Up        ║
║  SEC EDGAR + News + AI + Telegram        ║
╚══════════════════════════════════════════╝
    """)

    # Start feedback handler
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern="^useful_|^notuseful_"))

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
    pipeline_thread.start()

    # Run both delivery loop and telegram app simultaneously
    async with app:
        await app.start()
        await app.updater.start_polling()
        await delivery_loop()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())