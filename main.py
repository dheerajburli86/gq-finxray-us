import asyncio
import os
import time
import threading
import traceback
import schedule
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
from datetime import datetime
import gquants_format_converter as gq_fmt

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Latency budget ────────────────────────────────────────────────────────────
# End-to-end delay is the sum of three queue drains: SEC poll -> AI pipeline ->
# delivery fan-out. It used to be 30s + 60s + 30s, so a filing SEC published
# instantly reached a phone up to ~2 minutes later, ~60s on average, before any
# AI time. Each drain is a single indexed Supabase query when its queue is
# empty, so tightening the idle gaps costs queries, not tokens or API quota.
PIPELINE_IDLE_SECONDS = int(os.getenv("GQ_PIPELINE_IDLE_SECONDS", "5"))
DELIVERY_IDLE_SECONDS = int(os.getenv("GQ_DELIVERY_IDLE_SECONDS", "5"))
# 8-K and Form 4 are the time-critical ones (material events, insider trades).
SEC_FAST_POLL_SECONDS = int(os.getenv("GQ_SEC_POLL_SECONDS", "15"))

import fmp_client
from feature_map import feature_footer
from delivery import deliver_pending_alerts as delivery_deliver

from edgar_poller_async import poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k, poll_sec_s1, load_cik_map
from news_poller import poll_all_news
from fmp_poller import poll_fmp_news, poll_fmp_events
from result_snapshot import process_pending_snapshots
from technical_poller import run_technical_poller
from ipo_poller import run_ipo_poller
from earnings_transcript_poller import run_earnings_transcript_poller
from news_roundup import run_etf_xray
from etf_flow_poller import run_etf_flow_poller
from heatmap_generator import (run_sector_heatmap_midday, run_sector_heatmap_afternoon,
                               run_sector_heatmap_weekly, run_sector_heatmap_monthly)
from ai_pipeline import run_pipeline as process_with_ai


# ── FMP price fetch ───────────────────────────────────────────────────────────

def format_alert(alert):
    """
    Render the alert, then splice in the GQuants deep link when there is one.

    Wraps _format_alert_body() instead of editing its ten return branches:
    every branch ends with the feature footer, so the link goes immediately
    before it. If GQUANTS_ALERT_BASE_URL is unset make_frontend_link() returns
    "" and the message is byte-identical to the pre-payload output.
    """
    body = _format_alert_body(alert)
    extra = alert.get("extra") or {}
    payload = extra.get("structured_payload")
    if not payload:
        return body

    link = gq_fmt.make_frontend_link(payload, alert.get("id", ""))
    if not link:
        return body

    link_line = f"\n🔗 [View full report on GQuants]({link})\n"
    marker = "\n\n🏷 Feature"
    if marker in body:
        head, _, tail = body.rpartition(marker)
        return f"{head}{link_line}{marker}{tail}"
    return f"{body}{link_line}"


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


# ── Alert formatter ───────────────────────────────────────────────────────────
def _format_alert_body(alert):
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
        "SEC_XBRL": "SEC XBRL",
        "FMP_RATINGS": "FMP Analyst Ratings",
        "MARKET_WIDE": "Market-Wide",
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
    footer = f"\n\n🏷 {feature_footer(source, filing_type)}"

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
        action = extra.get("transaction_type", "TRADE") if extra else "TRADE"
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
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    if source == "FMP_IPO":
        return (
            f"{summary}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

    item_types = extra.get("item_types", [])
    items_str = ""
    if item_types:
        first_item = item_types[0].split(":")[0].strip()
        items_str = f" · {first_item}"

    return (
        f"{emoji} *{impact} — ${ticker}*"
        f"{price_line}\n"
        f"🔍 *Xray Intel:* {summary}\n\n"
        f"📋 {source_name}{items_str} · {time_str}\n\n"
        f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
        f"_Disclaimer: gquants.com/disclaimer_\n\n"
        f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
        f"{footer}"
    )


# ── Error alerting ────────────────────────────────────────────────────────────
async def send_error_alert(message: str):
    try:
        bot = get_bot()
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
_BOTS = {}


def get_bot():
    """
    One Bot (and one HTTP connection pool) per event loop.

    The old code built a fresh Bot on every delivery tick, which opened a
    new pool every 30 seconds. Caching is keyed on the running loop
    because the market-report and error-alert paths call asyncio.run()
    from worker threads, and a Bot's transport is bound to the loop that
    created it — a single process-wide instance would raise
    "Event loop is closed" the moment it crossed loops.
    """
    try:
        key = id(asyncio.get_running_loop())
    except RuntimeError:
        key = 0
    bot = _BOTS.get(key)
    if bot is None:
        bot = Bot(token=TELEGRAM_TOKEN)
        _BOTS[key] = bot
    return bot


async def deliver_pending_alerts():
    """Dispatch to delivery.py which handles per-user routing and watchlist filtering."""
    try:
        await delivery_deliver()
    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")


# ── Market report helpers (FMP) ──────────────────────────────────────────────
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
    """
    Queue a market report as an alert row so delivery.py fans it out per user.

    It used to bot.send_message() straight to TELEGRAM_CHANNEL_ID, which put
    every scheduled report into the shared GQ FinXray US channel and never into
    a subscriber's own chat. That bypassed the whole routing layer: no
    min_impact floor, no muted_features, no daily cap, no alert_deliveries
    ledger, and no way for a user to stop receiving them.

    Filed as ticker='MARKET' / MARKET_REPORT, which delivery.py already treats
    as a whole-market product: it reaches exactly the users who have
    receive_market_wide on, in their own Telegram chat.
    """
    try:
        supabase.table("alerts").insert({
            "ticker": "MARKET",
            "summary": body,
            "impact": "LOW",
            "source": "MARKET_REPORT",
            "filing_type": "MARKET_REPORT",
            "extra": {"headline": title, "report_title": title},
            "delivered": False,
        }).execute()
        print(f"[REPORT] Queued for fan-out: {title}")
    except Exception as e:
        print(f"[ERROR] Failed to queue market report: {e}")


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
_JOB_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="job")
# SEC EDGAR gets its own lane. Sharing one pool meant the latency-critical 8-K
# and Form 4 polls queued behind whatever slow FMP/Massive/news/technical job
# happened to hold the workers — the heatmap, ETF Xray and transcript jobs are
# all minutes long, and six of them at once stalled SEC polling completely.
# A separate executor means an SEC tick never waits on a non-SEC job, which is
# what "prioritise SEC EDGAR over FMP/Massive" actually requires.
_SEC_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sec")
_JOB_RUNNING = {}
_JOB_LOCK = threading.Lock()


def job(fn, pool=None):
    """
    Hand a scheduled job to the pool instead of running it inline.

    schedule.run_pending() executes jobs on the calling thread, so before
    this a 90-second technical_poller would hold up the 30-second SEC
    poll behind it. Wrapping every job means one slow feature can no
    longer add latency to any other. The running-set guard drops a tick
    if the previous run of that same job hasn't finished, which stops
    fast schedules from stacking up work faster than it drains.
    """
    name = getattr(fn, "__name__", str(fn))

    def _submit():
        with _JOB_LOCK:
            if _JOB_RUNNING.get(name):
                print(f"[SCHEDULER] Skipping {name} — previous run still active")
                return
            _JOB_RUNNING[name] = True

        def _run():
            started = time.monotonic()
            try:
                fn()
            except Exception as e:
                print(f"[SCHEDULER ERROR] {name}: {e}")
                traceback.print_exc()
            finally:
                took = time.monotonic() - started
                if took > 30:
                    print(f"[SCHEDULER] {name} took {took:.1f}s")
                with _JOB_LOCK:
                    _JOB_RUNNING[name] = False

        (pool or _JOB_POOL).submit(_run)

    _submit.__name__ = f"job_{name}"
    return _submit


def sec_job(fn):
    """Schedule on the dedicated SEC lane so filings never queue behind FMP."""
    return job(fn, pool=_SEC_POOL)


def run_scheduler():
    print("[SCHEDULER] Starting...")
    load_cik_map()
    # Warm start in parallel — the old serial block delayed the first
    # scheduled tick by however long the slowest poller took.
    for warm in (poll_sec_8k, poll_sec_form4, poll_sec_10q, poll_sec_10k,
                 poll_sec_s1):
        sec_job(warm)()
    for warm in (poll_all_news, poll_fmp_news, poll_fmp_events,
                 run_technical_poller, run_ipo_poller, run_etf_flow_poller):
        job(warm)()
    # SEC lane — 8-K and Form 4 at 15s, the tightest interval SEC's fair-access
    # policy allows at 10 req/s with the required User-Agent.
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_8k))
    schedule.every(SEC_FAST_POLL_SECONDS).seconds.do(sec_job(poll_sec_form4))
    schedule.every(5).minutes.do(sec_job(poll_sec_10q))
    schedule.every(5).minutes.do(sec_job(poll_sec_10k))
    schedule.every(10).minutes.do(sec_job(poll_sec_s1))
    # 5 min, not 30: the 10-Q/10-K pollers run every 5 min, so a 30-min drain
    # here added up to 30 min of latency on top of a filing SEC published in
    # seconds -- the single largest delay in the financial-alert path. The job
    # is a no-op when no rows are PENDING, so the extra ticks cost one indexed
    # Supabase query each.
    schedule.every(5).minutes.do(sec_job(process_pending_snapshots))
    schedule.every(30).minutes.do(job(run_earnings_transcript_poller))
    schedule.every(60).seconds.do(job(poll_all_news))

    # FMP news + events pollers (Features 2, 4, 5)
    schedule.every(10).minutes.do(job(poll_fmp_news))
    schedule.every(60).minutes.do(job(poll_fmp_events))

    # Technical + IPO pollers (Features 6, 8)
    schedule.every(60).minutes.do(job(run_technical_poller))
    # NOTE: Times below are in UTC (EST: UTC-5, EDT: UTC-4)
    # If container timezone is not UTC, set TZ=America/New_York in environment
    # 08:00 ET = 13:00 UTC (EST)
    schedule.every().day.at("13:00").do(job(run_ipo_poller))

    # ETF Xray + ETF Flow (Features 7, 10)
    # 09:00 ET = 14:00 UTC (EST)
    schedule.every().day.at("14:00").do(job(run_etf_xray))
    schedule.every(60).minutes.do(job(run_etf_flow_poller))

    # Market reports + Sector Heatmap (Feature 9)
    # Market hours: 09:30-16:00 ET
    # 09:25 ET = 14:25 UTC, 09:30 ET = 14:30 UTC, 13:00 ET = 18:00 UTC, 16:00 ET = 21:00 UTC, 16:30 ET = 21:30 UTC (EST)
    schedule.every().day.at("14:25").do(job(send_premarket_report))
    schedule.every().day.at("14:30").do(job(send_market_open_report))
    schedule.every().day.at("14:30").do(job(run_sector_heatmap_midday))
    schedule.every().day.at("18:00").do(job(run_sector_heatmap_afternoon))
    schedule.every().day.at("21:00").do(job(run_sector_heatmap_weekly))
    schedule.every().day.at("21:30").do(job(run_sector_heatmap_monthly))
    schedule.every().day.at("18:00").do(job(send_midday_report))
    schedule.every().day.at("21:00").do(job(send_market_close_report))
    schedule.every().day.at("21:30").do(job(send_afterhours_report))

    print("[SCHEDULER] All pollers and market reports scheduled.")
    while True:
        schedule.run_pending()
        time.sleep(1)


# ── AI pipeline thread ────────────────────────────────────────────────────────
def run_pipeline():
    """
    Drain PENDING raw_filings continuously.

    The idle gap was 60s. A filing that the SEC poller captured seconds after
    publication then sat untouched for up to a further minute before the AI
    even looked at it — on its own the largest delay between "SEC published"
    and "user's phone buzzes". process_with_ai() is a single indexed Supabase
    query when the queue is empty, so a short gap costs one cheap query per
    tick and nothing else; when the queue is NOT empty there is no sleep at
    all, so a burst drains back-to-back instead of one batch per minute.
    """
    print("[PIPELINE] Starting...")
    while True:
        worked = False
        try:
            worked = bool(process_with_ai())
        except Exception as e:
            print(f"[PIPELINE ERROR] {e}")
            asyncio.run(send_error_alert(f"Pipeline error: {str(e)}"))
        if not worked:
            time.sleep(PIPELINE_IDLE_SECONDS)


# ── Delivery loop ─────────────────────────────────────────────────────────────
async def delivery_loop():
    """
    Fan out ready alerts. 30s -> 5s: this is the last hop before the user's
    phone, and a fixed 30s gap added up to half a minute to every alert
    regardless of how fast the poller and the AI had been. An empty cycle is
    one indexed query that returns nothing.
    """
    print("[DELIVERY] Starting...")
    while True:
        try:
            await deliver_pending_alerts()
        except Exception as e:
            print(f"[DELIVERY ERROR] {e}")
        await asyncio.sleep(DELIVERY_IDLE_SECONDS)


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
