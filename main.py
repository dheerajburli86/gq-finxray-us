import asyncio
import os
import time
import threading
import schedule
import hashlib
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
from datetime import datetime

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

import fmp_client
from feature_map import feature_footer

from edgar_poller import poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k, poll_sec_s1, load_cik_map
from news_poller import poll_all_news
from fmp_poller import poll_fmp_news, poll_fmp_events
from result_snapshot import process_pending_snapshots
from technical_poller import run_technical_poller
from ipo_poller import run_ipo_poller
from earnings_transcript_poller import run_earnings_transcript_poller
from news_roundup import run_etf_xray
from etf_flow_poller import run_etf_flow_poller
from heatmap_generator import run_sector_heatmap_daily, run_sector_heatmap_weekly, run_sector_heatmap_monthly


# ═════ STRICT DEDUPLICATION TRACKING ═════
_delivered_alert_cache = set()
_delivered_alert_hashes = set()
_CACHE_MAX_SIZE = 1000
_current_batch_hashes = set()


def generate_alert_content_hash(ticker, source, filing_type, summary):
    """Generate hash of alert content for semantic deduplication."""
    content_str = f"{ticker}#{source}#{filing_type}#{summary[:500]}"
    return hashlib.sha256(content_str.encode()).hexdigest()


def add_to_delivered_cache(alert_id, content_hash=None):
    """Track delivered alerts by ID and content hash."""
    global _delivered_alert_cache, _delivered_alert_hashes
    _delivered_alert_cache.add(alert_id)
    if content_hash:
        _delivered_alert_hashes.add(content_hash)
    
    if len(_delivered_alert_cache) > _CACHE_MAX_SIZE:
        _delivered_alert_cache = set(list(_delivered_alert_cache)[-500:])
    if len(_delivered_alert_hashes) > _CACHE_MAX_SIZE:
        _delivered_alert_hashes = set(list(_delivered_alert_hashes)[-500:])


def is_in_delivered_cache(alert_id):
    """Check if alert ID was recently delivered."""
    return alert_id in _delivered_alert_cache


def is_content_duplicate(content_hash):
    """Check if alert content was already delivered."""
    return content_hash in _delivered_alert_hashes


def is_duplicate_in_batch(content_hash):
    """Check if alert content already exists in current batch."""
    return content_hash in _current_batch_hashes


def add_to_batch(content_hash):
    """Track alert in current batch to prevent duplicate sends."""
    global _current_batch_hashes
    _current_batch_hashes.add(content_hash)


def clear_batch():
    """Clear batch tracker after delivery cycle completes."""
    global _current_batch_hashes
    _current_batch_hashes = set()


def load_delivered_cache():
    """Load recently delivered alerts from database on startup."""
    global _delivered_alert_cache, _delivered_alert_hashes
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=7)
        result = supabase.table("alerts") \
            .select("id, ticker, source, filing_type, summary") \
            .eq("delivered", True) \
            .gte("created_at", cutoff.isoformat()) \
            .execute()
        
        for row in result.data:
            alert_id = row.get("id")
            _delivered_alert_cache.add(alert_id)
            
            ticker = row.get("ticker")
            source = row.get("source")
            filing_type = row.get("filing_type")
            summary = row.get("summary", "")
            content_hash = generate_alert_content_hash(ticker, source, filing_type, summary)
            _delivered_alert_hashes.add(content_hash)
        
        print(f"[CACHE] Loaded {len(_delivered_alert_cache)} delivered alert IDs + {len(_delivered_alert_hashes)} content hashes")
    except Exception as e:
        print(f"[WARNING] Could not load delivered cache: {e}")


def get_stock_price(ticker: str):
    """Fetch live price and % change for a ticker from FMP."""
    try:
        q = fmp_client.get_quote(ticker)
        if not q or q.get("price") is None:
            return None
        price = float(q.get("price", 0))
        change_pct = float(q.get("changePercentage", 0) or 0)
        arrow = "🟢" if change_pct >= 0 else "🔴"
        sign = "+" if change_pct >= 0 else ""
        return {
            "price": f"${price:,.2f}",
            "change": f"{sign}{change_pct:.2f}%",
            "arrow": arrow
        }
    except Exception as e:
        print(f"[FMP] Price fetch failed for {ticker}: {e}")
        return None


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
        "REUTERS": "Reuters",
        "MARKETWATCH": "MarketWatch",
        "FMP_NEWS": "FMP",
        "TECHNICAL": "Technical (Massive/FMP)",
        "FMP_IPO": "FMP IPO Calendar",
        "FMP_TRANSCRIPT": "FMP Earnings Call Transcript",
        "ETF_FLOW": "ETF Flow (Massive)",
        "SECTOR_HEATMAP": "Sector Heatmap",
        "ETF_XRAY": "ETF Xray",
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_labels.get(source, source)
    time_str = datetime.now().strftime("%I:%M %p EST")
    footer = f"\n\n{feature_footer(source, filing_type)}"

    price_line = ""
    if ticker and ticker != "MARKET":
        price_data = get_stock_price(ticker)
        if price_data:
            price_line = f"\n📈 *Stock:* {ticker} {price_data['arrow']} {price_data['price']} ({price_data['change']})\n"

    if filing_type == "EARNINGS_CALENDAR":
        report_date = extra.get("report_date", "")
        timing_str = extra.get("timing", "")
        eps = extra.get("eps_estimate")
        eps_line = f"Analyst EPS Estimate: {eps}" if eps else "No EPS estimate available"
        return (
            f"📅 *Earnings Tomorrow — *\n"
            f"{price_line}\n"
            f"🕐 *When:* {timing_str} on {report_date}\n"
            f"📊 {eps_line}\n\n"
            f"Watch for potential volatility.\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "EARNINGS_TRANSCRIPT":
        year = extra.get("year", "")
        quarter = extra.get("quarter", "")
        return (
            f"📞 *Earnings Call Transcript — ${ticker}*"
            f"{price_line}\n"
            f"🗓 *Quarter:* Q{quarter} FY{year}\n\n"
            f"{summary}\n\n"
            f"📋 FMP Earnings Call Transcript · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "RESULT_SNAPSHOT":
        period = extra.get("period", "") if extra else ""
        form = extra.get("form_type", "") if extra else ""
        form_label = "Quarterly Results" if form == "10-Q" else "Annual Results"
        return (
            f"📊 *{form_label} — ${ticker}*"
            f"{price_line}\n"
            f"📅 *Period:* {period}\n\n"
            f"{summary}\n\n"
            f"📋 SEC {form} · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock\\'s news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "BULK_DEAL":
        insider = extra.get("insider_name", "Large investor") if extra else "Large investor"
        action = extra.get("action", "TRADE") if extra else "TRADE"
        value = extra.get("value", "N/A") if extra else "N/A"
        shares = extra.get("shares", "N/A") if extra else "N/A"
        trans_emoji = "🟢" if action == "BUY" else "🔴"
        return (
            f"{trans_emoji} *LARGE TRANSACTION — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"💰 Value: {value} · Shares: {shares}\n"
            f"👤 {insider}\n"
            f"📋 FMP Insider Data · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock\\'s news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "4":
        insider = extra.get("insider_name", "An insider")
        transaction = extra.get("transaction_type", "")
        trans_emoji = "🟢" if transaction == "BUY" else "🔴" if transaction == "SELL" else "📋"
        return (
            f"{trans_emoji} *INSIDER {transaction or 'TRADE'} — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"👤 {insider}\n"
            f"📋 SEC Form 4 · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "S-1":
        return (
            f"🚀 *IPO FILING — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"📋 SEC S-1 · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "NEWS":
        return (
            f"{emoji} *{source_name} — ${ticker}*"
            f"{price_line}\n"
            f"🔍 *Xray Intel:* {summary}\n\n"
            f"📰 {source_name} · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "TECHNICAL":
        return (
            f"{summary}\n\n"
            f"{price_line}"
            f"📊 Technical Alert · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "ETF_FLOW":
        return (
            f"{summary}\n\n"
            f"📊 ETF Flow Data · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor market flows._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "SECTOR_HEATMAP":
        return (
            f"{summary}\n\n"
            f"📊 Sector Heatmap · {time_str}\n\n"
            f"_You are receiving this notification based on your watchlist settings._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "ETF_XRAY":
        return (
            f"{summary}\n\n"
            f"📊 ETF Xray · {time_str}\n\n"
            f"_You are receiving this notification based on your watchlist settings._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    return (
        f"{emoji} *{source_name} — ${ticker}*"
        f"{price_line}\n"
        f"{summary}\n\n"
        f"📋 {source_name} · {time_str}\n\n"
        f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
        f"_Disclaimer: gquants.com/disclaimer_\n\n"
        f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
        f"{footer}"
    )


async def send_error_alert(message: str):
    """Send error alerts to the channel."""
    if not TELEGRAM_CHANNEL_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        time_str = datetime.now().strftime("%I:%M %p EST")
        msg = f"⚠️ *System Error*\n_{time_str}_\n\n{message}\n\n_GQ FinXray US · gquants.com_"
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"[ERROR] Failed to send error alert: {e}")


def log_alert_run(alert, telegram_success, telegram_error=None):
    """Log alert delivery attempt."""
    try:
        supabase.table("alert_run_log").insert({
            "alert_id": alert.get("id"),
            "ticker": alert.get("ticker"),
            "impact": alert.get("impact"),
            "source": alert.get("source"),
            "filing_type": alert.get("filing_type"),
            "summarization_attempts": alert.get("extra", {}).get("summarization_attempts"),
            "input_tokens": alert.get("extra", {}).get("input_tokens"),
            "output_tokens": alert.get("extra", {}).get("output_tokens"),
            "total_tokens": alert.get("extra", {}).get("total_tokens"),
            "llm_calls": alert.get("extra", {}).get("llm_calls"),
            "telegram_success": telegram_success,
            "telegram_error": (str(telegram_error)[:500] if telegram_error else None)
        }).execute()
    except Exception as e:
        print(f"[ERROR] Failed to write alert_run_log for {alert.get('ticker', 'UNKNOWN')}: {e}")


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
        sent_count = 0

        for alert in alerts:
            alert_id = alert.get("id")
            ticker = alert.get("ticker", "UNKNOWN")
            impact = alert.get("impact", "LOW")
            source = alert.get("source", "UNKNOWN")
            filing_type = alert.get("filing_type", "")
            summary = alert.get("summary", "")

            # ═════ CHECK #1: Alert ID ═════
            if is_in_delivered_cache(alert_id):
                print(f"[SKIP-ID] Alert {alert_id} already delivered (ID check)")
                supabase.table("alerts").update({"delivered": True}).eq("id", alert_id).execute()
                continue

            # ═════ CHECK #2: Content Hash (Historical) ═════
            content_hash = generate_alert_content_hash(ticker, source, filing_type, summary)
            if is_content_duplicate(content_hash):
                print(f"[SKIP-HASH] {ticker} {filing_type} duplicate (content hash match)")
                supabase.table("alerts").update({"delivered": True}).eq("id", alert_id).execute()
                add_to_delivered_cache(alert_id, content_hash)
                continue

            # ═════ CHECK #3: Batch Deduplication ═════
            if is_duplicate_in_batch(content_hash):
                print(f"[SKIP-BATCH] {ticker} {filing_type} duplicate (same delivery cycle)")
                supabase.table("alerts").update({"delivered": True}).eq("id", alert_id).execute()
                add_to_delivered_cache(alert_id, content_hash)
                continue

            # ═════ CHECK #4: Database Verification (2-hour window) - FIXED ═════
            skip_alert = False
            try:
                db_check = supabase.table("alerts") \
                    .select("id, summary") \
                    .eq("ticker", ticker) \
                    .eq("source", source) \
                    .eq("filing_type", filing_type) \
                    .eq("delivered", True) \
                    .gte("created_at", (datetime.now() - __import__('datetime').timedelta(hours=2)).isoformat()) \
                    .execute()
                
                if db_check.data:
                    for existing in db_check.data:
                        existing_summary = existing.get("summary", "")
                        if existing_summary[:300].lower() == summary[:300].lower():
                            print(f"[SKIP-DB] {ticker} duplicate found in DB (delivered 2h ago)")
                            supabase.table("alerts").update({"delivered": True}).eq("id", alert_id).execute()
                            add_to_delivered_cache(alert_id, content_hash)
                            skip_alert = True
                            break
            except Exception as e:
                print(f"[WARNING] DB verification failed: {e}")

            if skip_alert:
                continue

            # ═════ PASSED ALL CHECKS - SEND ALERT ═════
            if TELEGRAM_CHANNEL_ID:
                try:
                    msg = format_alert(alert)
                    await bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=msg,
                        parse_mode="Markdown"
                    )
                    print(f"[CHANNEL] {impact} — ${ticker} sent to channel")
                    log_alert_run(alert, telegram_success=True)
                    
                    add_to_delivered_cache(alert_id, content_hash)
                    add_to_batch(content_hash)
                    sent_count += 1
                    
                except Exception as e:
                    print(f"[ERROR] Channel post failed for {ticker}: {e}")
                    log_alert_run(alert, telegram_success=False, telegram_error=e)
            else:
                log_alert_run(alert, telegram_success=False, telegram_error="TELEGRAM_CHANNEL_ID not configured")

            supabase.table("alerts").update({"delivered": True}).eq("id", alert_id).execute()
            print(f"[DELIVERED] {impact} — ${ticker}")

        if sent_count > 0:
            print(f"[BATCH COMPLETE] Sent {sent_count} alerts")
        clear_batch()

    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")
        clear_batch()


def fetch_index_data():
    """Fetch S&P 500, NASDAQ, Dow from FMP."""
    indices = {"SPY": "S&P 500", "QQQ": "NASDAQ", "DIA": "Dow Jones"}
    lines = []
    for symbol, name in indices.items():
        try:
            q = fmp_client.get_quote(symbol)
            if q and q.get("price"):
                price = float(q["price"])
                chg = float(q.get("changePercentage", 0) or 0)
                arrow = "🟢" if chg >= 0 else "🔴"
                sign = "+" if chg >= 0 else ""
                lines.append(f"{arrow} *{name}:* ${price:,.2f} ({sign}{chg:.2f}%)")
        except Exception:
            pass
    return "\n".join(lines) if lines else "Index data unavailable"


def fetch_macro_data():
    """Fetch Gold, Crude Oil, Natural Gas from FMP commodities quotes."""
    instruments = {"GCUSD": "Gold", "CLUSD": "Crude Oil", "NGUSD": "Natural Gas"}
    lines = []
    for symbol, name in instruments.items():
        try:
            q = fmp_client.get_commodity_quote(symbol)
            if q and q.get("price"):
                price = float(q["price"])
                chg = float(q.get("changePercentage", 0) or 0)
                arrow = "🟢" if chg >= 0 else "🔴"
                sign = "+" if chg >= 0 else ""
                lines.append(f"{arrow} *{name}:* ${price:,.2f} ({sign}{chg:.2f}%)")
        except Exception:
            pass
    return "\n".join(lines) if lines else "Macro data unavailable"


def fetch_top_movers():
    """Fetch top 3 gainers and losers from a default watchlist via FMP."""
    tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "JPM", "BAC"]
    results = []
    for ticker in tickers:
        try:
            q = fmp_client.get_quote(ticker)
            if q and q.get("price"):
                results.append({
                    "ticker": ticker,
                    "change": float(q.get("changePercentage", 0) or 0),
                    "price": float(q["price"])
                })
        except Exception:
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
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=msg, parse_mode="Markdown")
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


def run_scheduler():
    print("[SCHEDULER] Starting...")
    load_cik_map()
    poll_sec_8k()
    poll_sec_form4()
    poll_sec_10q()
    poll_sec_10k()
    poll_sec_s1()
    poll_all_news()
    schedule.every(30).seconds.do(poll_sec_8k)
    schedule.every(30).seconds.do(poll_sec_form4)
    schedule.every(5).minutes.do(poll_sec_10q)
    schedule.every(5).minutes.do(poll_sec_10k)
    schedule.every(10).minutes.do(poll_sec_s1)
    schedule.every(30).minutes.do(process_pending_snapshots)
    schedule.every(30).minutes.do(run_earnings_transcript_poller)
    schedule.every(60).seconds.do(poll_all_news)

    poll_fmp_news()
    poll_fmp_events()
    schedule.every(10).minutes.do(poll_fmp_news)
    schedule.every(60).minutes.do(poll_fmp_events)

    run_technical_poller()
    run_ipo_poller()
    schedule.every(60).minutes.do(run_technical_poller)
    schedule.every().day.at("08:00").do(run_ipo_poller)

    schedule.every().day.at("09:00").do(run_etf_xray)
    run_etf_flow_poller()
    schedule.every(60).minutes.do(run_etf_flow_poller)

    schedule.every().day.at("09:25").do(send_premarket_report)
    schedule.every().day.at("09:30").do(send_market_open_report)
    schedule.every().day.at("09:30").do(run_sector_heatmap_daily)
    schedule.every().day.at("13:00").do(run_sector_heatmap_daily)
    schedule.every().day.at("16:00").do(run_sector_heatmap_weekly)
    schedule.every().day.at("16:30").do(run_sector_heatmap_monthly)
    schedule.every().day.at("13:00").do(send_midday_report)
    schedule.every().day.at("16:00").do(send_market_close_report)
    schedule.every().day.at("16:30").do(send_afterhours_report)

    print("[SCHEDULER] All pollers and market reports scheduled.")
    while True:
        schedule.run_pending()
        time.sleep(1)


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


async def delivery_loop():
    print("[DELIVERY] Starting...")
    load_delivered_cache()
    while True:
        await deliver_pending_alerts()
        await asyncio.sleep(30)


async def main():
    print("""
╔══════════════════════════════════════════════════════╗
║            GQ FinXray US — Starting Up               ║
║  SEC EDGAR + FMP + Massive + News + AI + Telegram    ║
║      WITH STRICT DEDUPLICATION PROTECTION            ║
╚══════════════════════════════════════════════════════╝
    """)
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
    pipeline_thread.start()
    await delivery_loop()


if __name__ == "__main__":
    asyncio.run(main())
