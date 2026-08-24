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

import fmp_client
from feature_map import feature_footer

from edgar_poller import poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k, poll_sec_s1, load_cik_map
from news_poller import poll_all_news
from fmp_poller import poll_eodhd_news, poll_eodhd_events
from result_snapshot import process_pending_snapshots
from technical_poller import run_technical_poller
from ipo_poller import run_ipo_poller
from earnings_transcript_poller import run_earnings_transcript_poller
from news_roundup import run_etf_xray
from etf_flow_poller import run_etf_flow_poller
from heatmap_generator import run_sector_heatmap_daily, run_sector_heatmap_weekly, run_sector_heatmap_monthly


# ── FMP price fetch (replaces TwelveData) ─────────────────────────────────────
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


# ── Link rendering ───────────────────────────────────────────────────────────
# Only these (source, filing_type) pairs have underlying documents with URLs:
FEATURES_WITH_LINKS = {
    ("SEC_EDGAR", "8-K"),
    ("SEC_EDGAR", "10-Q"),
    ("SEC_EDGAR", "10-K"),
    ("SEC_EDGAR", "S-1"),
    ("SEC_EDGAR", "4"),
    ("FMP_NEWS", "NEWS"),
    ("CNBC", "NEWS"),
    ("REUTERS", "NEWS"),
    ("MARKETWATCH", "NEWS"),
    ("NASDAQ", "NEWS"),
    ("IBD", "NEWS"),
    ("FORTUNE", "NEWS"),
    ("CNN", "NEWS"),
    ("BLOOMBERG", "NEWS"),
    ("FMP", "RESULT_SNAPSHOT"),
    ("FMP_NEWS", "BULK_DEAL"),
    ("FMP", "INSIDER_FMP"),
    ("FMP_IPO", "IPO_UPCOMING"),
}

def should_have_link(source, filing_type):
    """True only for features with real underlying documents."""
    return (source, filing_type) in FEATURES_WITH_LINKS


def format_link_line(link, time_str):
    """Format link as markdown for Telegram. Returns empty string if no link."""
    if not link or not isinstance(link, str) or not link.startswith(("http://", "https://")):
        return ""
    return f"\n📎 [View source]({link}) · {time_str}"


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

    # Fetch live price from FMP (skip MARKET ticker)
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
        # No link line: earnings dates are data-only, no underlying document
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
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"📊 *{form_label} — ${ticker}*"
            f"{price_line}\n"
            f"📅 *Period:* {period}\n\n"
            f"{summary}\n\n"
            f"📋 SEC {form} · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock\\'s news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "BULK_DEAL":
        insider = extra.get("insider_name", "Large investor") if extra else "Large investor"
        action = extra.get("transaction_type", "TRADE") if extra else "TRADE"
        value = extra.get("value", "N/A") if extra else "N/A"
        shares = extra.get("shares", "N/A") if extra else "N/A"
        trans_emoji = "🟢" if action == "BUY" else "🔴"
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"{trans_emoji} *LARGE TRANSACTION — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"💰 Value: {value} · Shares: {shares}\n"
            f"👤 {insider}\n"
            f"📋 SEC Form 4 · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock\\'s news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "S-1":
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"🚀 *IPO FILING — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"📋 SEC S-1 · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "NEWS":
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"{emoji} *{source_name} — ${ticker}*"
            f"{price_line}\n"
            f"🔍 *Xray Intel:* {summary}\n\n"
            f"📰 {source_name} · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "TECHNICAL":
        # No link line: technical signals are computed, not sourced from a document
        return (
            f"{summary}\n\n"
            f"{price_line}"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "FMP_IPO":
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"{summary}\n\n"
            f"📋 SEC S-1 Filing · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "ETF_FLOW":
        # No link line: ETF flow signals are computed from price/volume, not sourced
        return (
            f"{summary}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "SECTOR_HEATMAP":
        # No link line: heatmap is visualization, no source document
        return (
            f"{summary}\n\n"
            f"_Heatmap image shown above._\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "ETF_XRAY" or source == "NEWS_ROUNDUP":
        # No link line: digest/summary, no single source document
        return (
            f"{summary}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "FMP_TRANSCRIPT":
        # No link line: FMP doesn't provide public URLs for transcripts
        return (
            f"📞 *Earnings Call Transcript — ${ticker}*"
            f"{price_line}\n"
            f"🗓 *Quarter:* Q{extra.get('quarter', '?')} FY{extra.get('year', '?')}\n\n"
            f"{summary}\n\n"
            f"📋 FMP Earnings Call Transcript · {time_str}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if filing_type == "4":
        insider = extra.get("insider_name", "An insider")
        transaction = extra.get("transaction_type", "")
        trans_emoji = "🟢" if transaction == "BUY" else "🔴" if transaction == "SELL" else "📋"
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
        return (
            f"{trans_emoji} *INSIDER {transaction or 'TRADE'} — ${ticker}*"
            f"{price_line}\n"
            f"{summary}\n\n"
            f"👤 {insider}\n"
            f"📋 SEC Form 4 · {time_str}{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    # Default fallback for 8-K, 10-Q, 10-K, S-1, NEWS, and other filing types
    link = alert.get("link")
    link_line = format_link_line(link, time_str) if should_have_link(source, filing_type) else ""
    item_types = extra.get("item_types", [])
    items_str = ""
    if item_types:
        first_item = item_types[0].split(":")[0].strip()
        items_str = f" · {first_item}"

    return (
        f"{emoji} *{impact} — ${ticker}*"
        f"{price_line}\n"
        f"🔍 *Xray Intel:* {summary}\n\n"
        f"📋 {source_name}{items_str} · {time_str}{link_line}\n\n"
        f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
        f"_Disclaimer: gquants.com/disclaimer_\n\n"
        f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
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
    except Exception:
        pass


# ── Run log: one row per alert actually sent to Telegram ─────────────────────
def log_alert_run(alert, telegram_success, telegram_error=None):
    """
    Writes to alert_run_log -- the review/audit trail for every alert this
    system sends, regardless of whether it went through the AI summarizer
    (news/filings/transcripts, where summarization_attempts and the token
    counts are real numbers pulled out of the alert's `extra`) or was a
    templated alert with no LLM involved at all (technical/IPO/ETF flow/
    result snapshot/heatmap, where those fields are simply absent from
    `extra` and land here as None/null -- that's expected, not a bug).
    Logged for every alert, success or failure, so a failed Telegram send
    is visible here too rather than just vanishing.
    """
    try:
        extra = alert.get("extra") or {}
        supabase.table("alert_run_log").insert({
            "alert_id": alert.get("id"),
            "ticker": alert.get("ticker", "UNKNOWN"),
            "source": alert.get("source"),
            "filing_type": alert.get("filing_type"),
            "feature_id": extra.get("feature_id"),
            "feature_name": extra.get("feature_name"),
            "impact": alert.get("impact"),
            "summarization_attempts": extra.get("summarization_attempts"),
            "input_tokens": extra.get("input_tokens"),
            "output_tokens": extra.get("output_tokens"),
            "total_tokens": extra.get("total_tokens"),
            "llm_calls": extra.get("llm_calls"),
            "telegram_success": telegram_success,
            "telegram_error": (str(telegram_error)[:500] if telegram_error else None)
        }).execute()
    except Exception as e:
        # A logging failure must never take down real alert delivery.
        print(f"[ERROR] Failed to write alert_run_log for {alert.get('ticker', 'UNKNOWN')}: {e}")


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

        for alert in alerts:
            ticker = alert.get("ticker", "UNKNOWN")
            impact = alert.get("impact", "LOW")

            # Send ALL alerts to Telegram channel — no watchlist/user filtering
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
                except Exception as e:
                    print(f"[ERROR] Channel post failed for {ticker}: {e}")
                    log_alert_run(alert, telegram_success=False, telegram_error=e)
            else:
                # No channel configured -- nothing was actually sent, but
                # still log it so the run record isn't silently incomplete.
                log_alert_run(alert, telegram_success=False, telegram_error="TELEGRAM_CHANNEL_ID not configured")

            # Mark delivered
            supabase.table("alerts") \
                .update({"delivered": True}) \
                .eq("id", alert["id"]) \
                .execute()
            print(f"[DELIVERED] {impact} — ${ticker}")

    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")


# ── Market report helpers (FMP replaces TwelveData) ──────────────────────────
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


# ── Scheduler thread ──────────────────────────────────────────────────────────
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

    # FMP news + events pollers (Features 2, 4, 5)
    poll_eodhd_news()
    poll_eodhd_events()
    schedule.every(10).minutes.do(poll_eodhd_news)
    schedule.every(60).minutes.do(poll_eodhd_events)

    # Technical + IPO pollers (Features 6, 8)
    run_technical_poller()
    run_ipo_poller()
    schedule.every(60).minutes.do(run_technical_poller)
    schedule.every().day.at("08:00").do(run_ipo_poller)

    # ETF Xray + ETF Flow (Features 7, 10)
    schedule.every().day.at("09:00").do(run_etf_xray)
    run_etf_flow_poller()
    schedule.every(60).minutes.do(run_etf_flow_poller)

    # Market reports + Sector Heatmap (Feature 9)
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
╔══════════════════════════════════════════════════════╗
║            GQ FinXray US — Starting Up               ║
║  SEC EDGAR + FMP + Massive + News + AI + Telegram    ║
╚══════════════════════════════════════════════════════╝
    """)
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
    pipeline_thread.start()
    await delivery_loop()


if __name__ == "__main__":
    asyncio.run(main())
