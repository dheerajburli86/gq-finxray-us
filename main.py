import asyncio
import os
import time
import threading
import schedule
import requests
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
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

    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
    source_labels = {
        "SEC_EDGAR": "SEC EDGAR",
        "CNBC": "CNBC",
        "BLOOMBERG": "Bloomberg",
        "MARKETWATCH": "MarketWatch"
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_labels.get(source, source)
    time_str = datetime.now().strftime("%I:%M %p EST")

    # Fetch live price (skip MARKET and UNKNOWN tickers)
    price_line = ""
    if ticker and ticker not in ("MARKET", "UNKNOWN"):
        price_data = get_stock_price(ticker)
        if price_data:
            price_line = f"\n📈 *Stock:* {ticker} {price_data['arrow']} {price_data['price']} ({price_data['change']})\n"

    footer = (
        f"\n_You are receiving this notification based on your request to "
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

    # S-1 IPO filing
    if filing_type == "S-1":
        return (
            f"🚀 *IPO FILING — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"📋 SEC S-1 · {time_str}"
            f"{footer}"
        )

    # News articles
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

            # Skip completely invalid alerts
            if ticker == "UNKNOWN":
                supabase.table("alerts") \
                    .update({"delivered": True}) \
                    .eq("id", alert["id"]) \
                    .execute()
                continue

            # MARKET ticker — skip user routing, post HIGH/MEDIUM to channel only
            if ticker == "MARKET":
                if impact in ("HIGH", "MEDIUM") and TELEGRAM_CHANNEL_ID:
                    try:
                        msg = format_alert(alert)
                        await bot.send_message(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            text=msg,
                            parse_mode="Markdown"
                        )
                        print(f"[CHANNEL] {impact} market alert posted")
                    except Exception as e:
                        print(f"[ERROR] Channel post failed: {e}")
                supabase.table("alerts") \
                    .update({"delivered": True}) \
                    .eq("id", alert["id"]) \
                    .execute()
                continue

            # Find users subscribed to this ticker
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
                    msg = format_alert(alert)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=msg,
                            parse_mode="Markdown"
                        )
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
                        supabase.table("delivery_logs").insert({
                            "alert_id": alert["id"],
                            "user_id": user_id,
                            "channel": "telegram",
                            "status": "failed",
                            "attempts": 1
                        }).execute()

            # Post HIGH and MEDIUM to channel
            if impact in ("HIGH", "MEDIUM") and TELEGRAM_CHANNEL_ID:
                try:
                    msg = format_alert(alert)
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
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
    tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "JPM", "BAC"]
    results = []
    for ticker in tickers:
        try:
            url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVEDATA_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if "close" in data:
                results.append({
                    "ticker": ticker,
                    "change": float(data.get("percent_change", 0)),
                    "price": float(data["close"])
                })
        except:
            pass
    if not results:
        return "Movers data unavailable", "Movers data unavailable"
    results.sort(key=lambda x: x["change"], reverse=True)
    gainers = "\n".join([f"🟢 *{r['ticker']}:* +{r['change']:.2f}%" for r in results[:3]])
    losers = "\n".join([f"🔴 *{r['ticker']}:* {r['change']:.2f}%" for r in results[-3:]])
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
    body = (
        f"*US Futures & Pre-Market Snapshot*\n\n"
        f"{indices}\n\n"
        f"*Macro*\n{macro}"
    )
    asyncio.run(send_market_report("🌅 Pre-Market Report", body))

def send_market_open_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    body = (
        f"*Markets are now open.*\n\n"
        f"*Indices at Open*\n{indices}\n\n"
        f"*Early Gainers*\n{gainers}\n\n"
        f"*Early Losers*\n{losers}"
    )
    asyncio.run(send_market_report("🔔 Market Open", body))

def send_midday_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    body = (
        f"*Midday Market Check*\n\n"
        f"*Indices*\n{indices}\n\n"
        f"*Top Gainers*\n{gainers}\n\n"
        f"*Top Losers*\n{losers}"
    )
    asyncio.run(send_market_report("⏱ Midday Pulse", body))

def send_market_close_report():
    indices = fetch_index_data()
    gainers, losers = fetch_top_movers()
    macro = fetch_macro_data()
    body = (
        f"*Markets have closed.*\n\n"
        f"*Final Index Levels*\n{indices}\n\n"
        f"*Top Gainers*\n{gainers}\n\n"
        f"*Top Losers*\n{losers}\n\n"
        f"*Macro*\n{macro}"
    )
    asyncio.run(send_market_report("📉 Market Close Report", body))

def send_afterhours_report():
    gainers, losers = fetch_top_movers()
    body = (
        f"*After-Hours Notable Movers*\n\n"
        f"*Gainers*\n{gainers}\n\n"
        f"*Losers*\n{losers}"
    )
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
        time.sleep(60)

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
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
    pipeline_thread.start()
    await delivery_loop()

if __name__ == "__main__":
    asyncio.run(main())