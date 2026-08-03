"""
main.py
GQ FinXray US — scheduler, alert formatting, Telegram delivery. v2.

WHAT CHANGED IN v2 (2026-08-03)
-------------------------------
1. TIMEZONE. Every daily job was scheduled in SERVER LOCAL TIME, and Railway
   runs UTC. So `at("09:30")` -- meant to be the opening bell -- fired at
   09:30 UTC, which is 05:30 ET: four hours before the market opens. Every
   time-of-day job in the system was wrong. Confirmed in the database, where
   HEATMAP_DAILY_09 rows are stamped 09:30 UTC. Jobs are now pinned to
   America/New_York explicitly, with a startup check that says so out loud.

2. MARKET REPORTS RAN ON THE WRONG DATA SOURCE. fetch_top_movers() looped FMP
   quotes over ten hardcoded megacaps and called the best and worst of THOSE
   the day's "top gainers and losers" -- which they usually were not. Now uses
   Massive's whole-market movers with a dollar-volume liquidity filter.

3. EVERY PRICE LOOKUP WAS A SEPARATE FMP CALL. Index levels, movers and the
   per-alert price line each hit FMP independently. All now read from the one
   shared market snapshot via market_data, with automatic FMP fallback.

4. LARGE TRADES POLLER WIRED IN (Feature 5). Scans the Massive tick tape for
   block-size prints -- the US-legal replacement for India's bulk/block deal
   feed.

REQUIRED ENV: SUPABASE_URL, SUPABASE_KEY (service_role), TELEGRAM_TOKEN,
TELEGRAM_CHANNEL_ID, FMP_API_KEY, MASSIVE_API_KEY, DEEPINFRA_API_KEY
"""

import asyncio
import os
import time
import threading
from datetime import datetime, timezone

import schedule
from supabase import create_client
from telegram import Bot

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

import market_data
from feature_map import feature_footer

from edgar_poller import (poll_sec_8k, poll_sec_form4, poll_sec_10q,
                          poll_sec_10k, poll_sec_s1, load_cik_map)
from news_poller import poll_all_news
from fmp_poller import poll_eodhd_news, poll_eodhd_events
from result_snapshot import process_pending_snapshots
from technical_poller import run_technical_poller
from ipo_poller import run_ipo_poller
from earnings_transcript_poller import run_earnings_transcript_poller
from large_trades_poller import run_large_trades_poller
from news_roundup import run_etf_xray
from etf_flow_poller import run_etf_flow_poller
from heatmap_generator import (run_sector_heatmap_daily, run_sector_heatmap_weekly,
                               run_sector_heatmap_monthly)

MARKET_TZ = "America/New_York"


def et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(MARKET_TZ))
    except Exception:
        return datetime.now(timezone.utc)


def _verify_timezone():
    """Say clearly, at boot, what time the scheduler thinks it is.

    The single most expensive silent bug in v1 was that nobody could see the
    scheduler was running four hours early.
    """
    local = datetime.now().astimezone()
    et = et_now()
    print(f"[TZ] server local: {local:%Y-%m-%d %H:%M %Z}  |  market (ET): {et:%Y-%m-%d %H:%M %Z}")
    offset_hours = (local.utcoffset().total_seconds() - et.utcoffset().total_seconds()) / 3600
    if abs(offset_hours) > 0.01:
        print(f"[TZ] Server is {offset_hours:+.0f}h from market time. "
              f"Jobs are pinned to {MARKET_TZ} explicitly, so this is handled — "
              f"but setting TZ={MARKET_TZ} on Railway makes logs easier to read.")


def daily_at(time_str):
    """Schedule a daily job in MARKET time, whatever the server's clock says.

    schedule >= 1.2 accepts a timezone on .at(). On older versions we fall
    back to server-local and warn loudly, because silently drifting four
    hours is exactly the failure this function exists to prevent.
    """
    try:
        return schedule.every().day.at(time_str, MARKET_TZ)
    except TypeError:
        print(f"[TZ] WARNING: installed `schedule` does not support timezones. "
              f"'{time_str}' will run in SERVER LOCAL TIME. "
              f"Set TZ={MARKET_TZ} on Railway, or upgrade: pip install -U schedule")
        return schedule.every().day.at(time_str)


# ── Price line ────────────────────────────────────────────────────────────────
def get_stock_price(ticker: str):
    """Live price + % change, served from the shared snapshot where possible."""
    try:
        q = market_data.quote(ticker)
        if not q:
            return None
        arrow = "🟢" if q["change_pct"] >= 0 else "🔴"
        sign = "+" if q["change_pct"] >= 0 else ""
        return {"price": f"${q['price']:,.2f}",
                "change": f"{sign}{q['change_pct']:.2f}%",
                "arrow": arrow}
    except Exception as e:
        print(f"[PRICE] Lookup failed for {ticker}: {e}")
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
        "SEC_EDGAR": "SEC EDGAR", "CNBC": "CNBC", "REUTERS": "Reuters",
        "MARKETWATCH": "MarketWatch", "FMP_NEWS": "FMP",
        "TECHNICAL": "Technical (Massive/FMP)", "FMP_IPO": "FMP IPO Calendar",
        "FMP_TRANSCRIPT": "FMP Earnings Call Transcript",
        "ETF_FLOW": "ETF Flow (Massive)", "SECTOR_HEATMAP": "Sector Heatmap",
        "ETF_XRAY": "ETF Xray", "LARGE_TRADE": "Massive Tick Tape",
    }

    emoji = impact_emoji.get(impact, "🟢")
    source_name = source_labels.get(source, source)
    time_str = et_now().strftime("%I:%M %p ET")
    footer = f"\n\n{feature_footer(source, filing_type)}"

    disclaimer = (
        f"_You are receiving this notification based on your request to monitor "
        f"this stock's news, updates and transactions._\n"
        f"_Disclaimer: gquants.com/disclaimer_\n\n"
        f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
    )

    price_line = ""
    if ticker and ticker != "MARKET":
        pd = get_stock_price(ticker)
        if pd:
            price_line = f"\n📈 *Stock:* {ticker} {pd['arrow']} {pd['price']} ({pd['change']})\n"

    # Pre-templated alerts arrive fully formatted from their poller.
    if source in ("TECHNICAL", "FMP_IPO", "LARGE_TRADE", "ETF_FLOW"):
        return f"{summary}\n\n{price_line}{disclaimer}{footer}"

    if filing_type == "EARNINGS_CALENDAR":
        eps = extra.get("eps_estimate")
        return (f"📅 *Earnings Tomorrow — ${ticker}*{price_line}\n"
                f"🕐 *When:* {extra.get('timing', '')} on {extra.get('report_date', '')}\n"
                f"📊 {f'Analyst EPS Estimate: {eps}' if eps else 'No EPS estimate available'}\n\n"
                f"Watch for potential volatility.\n\n{disclaimer}{footer}")

    if filing_type == "EARNINGS_TRANSCRIPT":
        return (f"📞 *Earnings Call Transcript — ${ticker}*{price_line}\n"
                f"🗓 *Quarter:* Q{extra.get('quarter', '')} FY{extra.get('year', '')}\n\n"
                f"{summary}\n\n📋 FMP Transcript · {time_str}\n\n{disclaimer}{footer}")

    if filing_type == "RESULT_SNAPSHOT":
        form = extra.get("form_type", "")
        label = "Quarterly Results" if form == "10-Q" else "Annual Results"
        return (f"📊 *{label} — ${ticker}*{price_line}\n"
                f"📅 *Period:* {extra.get('period', '')}\n\n"
                f"{summary}\n\n📋 SEC {form} · {time_str}\n\n{disclaimer}{footer}")

    if filing_type == "4":
        tx = extra.get("transaction_type", "")
        te = "🟢" if tx == "BUY" else "🔴" if tx == "SELL" else "📋"
        return (f"{te} *INSIDER {tx or 'TRADE'} — ${ticker}*{price_line}\n"
                f"{summary}\n\n👤 {extra.get('insider_name', 'An insider')}\n"
                f"📋 SEC Form 4 · {time_str}\n\n{disclaimer}{footer}")

    if filing_type == "S-1":
        return (f"🚀 *IPO FILING — ${ticker}*{price_line}\n{summary}\n\n"
                f"📋 SEC S-1 · {time_str}\n\n{disclaimer}{footer}")

    if filing_type == "NEWS":
        return (f"{emoji} *{source_name} — ${ticker}*{price_line}\n"
                f"🔍 *Xray Intel:* {summary}\n\n📰 {source_name} · {time_str}\n\n"
                f"{disclaimer}{footer}")

    items = extra.get("item_types", [])
    items_str = f" · {items[0].split(':')[0].strip()}" if items else ""
    return (f"{emoji} *{impact} — ${ticker}*{price_line}\n"
            f"🔍 *Xray Intel:* {summary}\n\n"
            f"📋 {source_name}{items_str} · {time_str}\n\n{disclaimer}{footer}")


# ── Errors & run log ──────────────────────────────────────────────────────────
async def send_error_alert(message: str):
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=f"⚠️ *GQ FinXray US — System Alert*\n\n{message}\n\n"
                 f"🕐 {et_now():%I:%M %p ET}",
            parse_mode="Markdown")
    except Exception:
        pass


def log_alert_run(alert, telegram_success, telegram_error=None):
    """Audit row per alert sent. Templated alerts legitimately have null
    token/attempt fields -- they never touch the LLM."""
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
            "telegram_error": (str(telegram_error)[:500] if telegram_error else None),
        }).execute()
    except Exception as e:
        print(f"[ERROR] alert_run_log write failed for "
              f"{alert.get('ticker', 'UNKNOWN')}: {e} "
              f"(if this is a permissions error, SUPABASE_KEY is still the anon key)")


# ── Delivery ──────────────────────────────────────────────────────────────────
async def deliver_pending_alerts():
    try:
        result = supabase.table("alerts").select("*") \
            .eq("delivered", False).order("created_at").limit(20).execute()
        alerts = result.data
        if not alerts:
            return

        bot = Bot(token=TELEGRAM_TOKEN)
        for alert in alerts:
            ticker = alert.get("ticker", "UNKNOWN")
            impact = alert.get("impact", "LOW")
            if TELEGRAM_CHANNEL_ID:
                try:
                    await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID,
                                           text=format_alert(alert),
                                           parse_mode="Markdown")
                    print(f"[CHANNEL] {impact} — ${ticker}")
                    log_alert_run(alert, True)
                except Exception as e:
                    print(f"[ERROR] Channel post failed for {ticker}: {e}")
                    log_alert_run(alert, False, e)
            else:
                log_alert_run(alert, False, "TELEGRAM_CHANNEL_ID not configured")

            supabase.table("alerts").update({"delivered": True}) \
                .eq("id", alert["id"]).execute()
    except Exception as e:
        print(f"[ERROR] Delivery failed: {e}")


# ── Market reports ────────────────────────────────────────────────────────────
def fetch_index_data():
    lines = []
    for symbol, name in {"SPY": "S&P 500", "QQQ": "NASDAQ", "DIA": "Dow Jones"}.items():
        q = market_data.quote(symbol)
        if q:
            arrow = "🟢" if q["change_pct"] >= 0 else "🔴"
            sign = "+" if q["change_pct"] >= 0 else ""
            lines.append(f"{arrow} *{name}:* ${q['price']:,.2f} ({sign}{q['change_pct']:.2f}%)")
    return "\n".join(lines) if lines else "Index data unavailable"


def fetch_macro_data():
    import fmp_client
    lines = []
    for symbol, name in {"GCUSD": "Gold", "CLUSD": "Crude Oil",
                         "NGUSD": "Natural Gas"}.items():
        try:
            q = fmp_client.get_commodity_quote(symbol)
            if q and q.get("price"):
                chg = float(q.get("changePercentage", 0) or 0)
                arrow = "🟢" if chg >= 0 else "🔴"
                sign = "+" if chg >= 0 else ""
                lines.append(f"{arrow} *{name}:* ${float(q['price']):,.2f} ({sign}{chg:.2f}%)")
        except Exception:
            pass
    return "\n".join(lines) if lines else "Macro data unavailable"


def fetch_top_movers():
    """Whole-market movers, liquidity filtered.

    v1 looped FMP quotes over ten hardcoded megacaps and reported the best and
    worst of that fixed list as the day's top movers.
    """
    gainers, losers = market_data.top_movers(3)
    if not gainers and not losers:
        return "Movers data unavailable", "Movers data unavailable"
    g = "\n".join(f"🟢 *{q['ticker']}:* +{q['change_pct']:.2f}%" for q in gainers)
    l = "\n".join(f"🔴 *{q['ticker']}:* {q['change_pct']:.2f}%" for q in losers)
    return g or "—", l or "—"


async def send_market_report(title: str, body: str):
    if not TELEGRAM_CHANNEL_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        msg = (f"📊 *{title}*\n_{et_now():%I:%M %p ET}_\n\n{body}\n\n"
               f"_GQ FinXray US · gquants.com_")
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=msg, parse_mode="Markdown")
        print(f"[REPORT] Sent: {title}")
    except Exception as e:
        print(f"[ERROR] Market report failed: {e}")


def send_premarket_report():
    body = (f"*US Futures & Pre-Market Snapshot*\n\n{fetch_index_data()}\n\n"
            f"*Macro*\n{fetch_macro_data()}")
    asyncio.run(send_market_report("🌅 Pre-Market Report", body))


def send_market_open_report():
    g, l = fetch_top_movers()
    body = (f"*Markets are now open.*\n\n*Indices at Open*\n{fetch_index_data()}\n\n"
            f"*Early Gainers*\n{g}\n\n*Early Losers*\n{l}")
    asyncio.run(send_market_report("🔔 Market Open", body))


def send_midday_report():
    g, l = fetch_top_movers()
    body = (f"*Midday Market Check*\n\n*Indices*\n{fetch_index_data()}\n\n"
            f"*Top Gainers*\n{g}\n\n*Top Losers*\n{l}")
    asyncio.run(send_market_report("⏱ Midday Pulse", body))


def send_market_close_report():
    g, l = fetch_top_movers()
    body = (f"*Markets have closed.*\n\n*Final Index Levels*\n{fetch_index_data()}\n\n"
            f"*Top Gainers*\n{g}\n\n*Top Losers*\n{l}\n\n*Macro*\n{fetch_macro_data()}")
    asyncio.run(send_market_report("📉 Market Close Report", body))


def send_afterhours_report():
    g, l = fetch_top_movers()
    body = f"*After-Hours Notable Movers*\n\n*Gainers*\n{g}\n\n*Losers*\n{l}"
    asyncio.run(send_market_report("🌙 After-Hours Movers", body))


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _safe(fn, name):
    """Wrap a scheduled job so one poller's exception can't kill the thread."""
    def wrapped():
        try:
            fn()
        except Exception as e:
            print(f"[SCHEDULER] {name} raised: {e}")
    wrapped.__name__ = name
    return wrapped


def run_scheduler():
    print("[SCHEDULER] Starting...")
    _verify_timezone()
    load_cik_map()

    # ── High-frequency pollers ───────────────────────────────────────────────
    schedule.every(30).seconds.do(_safe(poll_sec_8k, "poll_sec_8k"))
    schedule.every(30).seconds.do(_safe(poll_sec_form4, "poll_sec_form4"))
    schedule.every(5).minutes.do(_safe(poll_sec_10q, "poll_sec_10q"))
    schedule.every(5).minutes.do(_safe(poll_sec_10k, "poll_sec_10k"))
    schedule.every(10).minutes.do(_safe(poll_sec_s1, "poll_sec_s1"))
    schedule.every(60).seconds.do(_safe(poll_all_news, "poll_all_news"))
    schedule.every(30).minutes.do(_safe(process_pending_snapshots, "result_snapshot"))
    schedule.every(30).minutes.do(_safe(run_earnings_transcript_poller, "transcripts"))

    # ── FMP news + events (Features 2, 4, 5) ─────────────────────────────────
    schedule.every(10).minutes.do(_safe(poll_eodhd_news, "fmp_news"))
    schedule.every(60).minutes.do(_safe(poll_eodhd_events, "fmp_events"))

    # ── Large trades — Massive tick tape (Feature 5) ─────────────────────────
    # Every 15m against a 20m lookback, so a slow run can't leave a gap.
    schedule.every(15).minutes.do(_safe(run_large_trades_poller, "large_trades"))

    # ── Technical + ETF flow + IPO (Features 6, 7, 8) ────────────────────────
    schedule.every(60).minutes.do(_safe(run_technical_poller, "technical"))
    schedule.every(60).minutes.do(_safe(run_etf_flow_poller, "etf_flow"))
    daily_at("08:00").do(_safe(run_ipo_poller, "ipo"))

    # ── ETF Xray (Feature 10) ────────────────────────────────────────────────
    daily_at("09:00").do(_safe(run_etf_xray, "etf_xray"))

    # ── Market reports — ALL IN EASTERN TIME ─────────────────────────────────
    # v1 ran these in UTC on a UTC box, so "Market Open" fired at 05:30 ET.
    daily_at("09:25").do(_safe(send_premarket_report, "premarket"))
    daily_at("09:30").do(_safe(send_market_open_report, "market_open"))
    daily_at("12:30").do(_safe(send_midday_report, "midday"))
    daily_at("16:05").do(_safe(send_market_close_report, "market_close"))
    daily_at("16:35").do(_safe(send_afterhours_report, "afterhours"))

    # ── Sector heatmap (Feature 9) ───────────────────────────────────────────
    daily_at("10:00").do(_safe(run_sector_heatmap_daily, "heatmap_daily_am"))
    daily_at("15:30").do(_safe(run_sector_heatmap_daily, "heatmap_daily_pm"))
    daily_at("16:10").do(_safe(run_sector_heatmap_weekly, "heatmap_weekly"))
    daily_at("16:15").do(_safe(run_sector_heatmap_monthly, "heatmap_monthly"))

    print(f"[SCHEDULER] {len(schedule.jobs)} jobs registered. "
          f"Next: {schedule.next_run()}")

    # Kick off the fast pollers once at boot so we don't wait a full interval.
    # Deliberately NOT running every poller synchronously here -- v1 did, and
    # a single slow API call at startup would stall the whole scheduler thread
    # before its loop ever began.
    for fn, name in [(poll_sec_8k, "poll_sec_8k"), (poll_all_news, "poll_all_news")]:
        _safe(fn, name)()

    while True:
        schedule.run_pending()
        time.sleep(1)


# ── Pipeline thread ───────────────────────────────────────────────────────────
def run_pipeline_loop():
    print("[PIPELINE] Starting...")
    while True:
        try:
            from ai_pipeline import run_pipeline
            run_pipeline()
        except Exception as e:
            print(f"[PIPELINE ERROR] {e}")
            try:
                asyncio.run(send_error_alert(f"Pipeline error: {e}"))
            except Exception:
                pass
        time.sleep(60)


async def delivery_loop():
    print("[DELIVERY] Starting...")
    while True:
        await deliver_pending_alerts()
        await asyncio.sleep(30)


async def main():
    print("""
╔══════════════════════════════════════════════════════╗
║            GQ FinXray US — Starting Up               ║
║   SEC EDGAR + FMP + Massive + AI + Telegram          ║
╚══════════════════════════════════════════════════════╝
    """)
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_pipeline_loop, daemon=True).start()
    await delivery_loop()


if __name__ == "__main__":
    asyncio.run(main())
