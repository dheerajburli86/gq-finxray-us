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


def _et_to_server_local(time_str):
    """Convert an ET wall-clock time to the equivalent server-local time.

    Uses zoneinfo from the stdlib, so it has no third-party dependency. The
    offset is resolved against TODAY's date, which means DST is correct now
    and drifts by an hour after a US DST transition until the process
    restarts -- acceptable for a service that redeploys regularly, and vastly
    better than being four hours out permanently.
    """
    from zoneinfo import ZoneInfo
    from datetime import time as dtime

    h, m = (int(x) for x in time_str.split(":"))
    today = datetime.now(ZoneInfo(MARKET_TZ)).date()
    et_dt = datetime.combine(today, dtime(h, m), tzinfo=ZoneInfo(MARKET_TZ))
    local_dt = et_dt.astimezone()
    return local_dt.strftime("%H:%M")


def daily_at(time_str):
    """Schedule a daily job in MARKET time, whatever the server's clock says.

    Three tiers, in order of preference:
      1. schedule >= 1.2 with pytz installed -- native timezone support, and
         DST is handled on every run.
      2. pytz missing (schedule imports it lazily inside .at(), so this only
         blows up at scheduling time, which is exactly what happened on the
         first Railway boot of v2) -- convert ET to server-local ourselves
         using the stdlib.
      3. Everything else -- server-local as-is, with a loud warning.

    Tier 2 exists because a missing optional dependency should never silently
    put the whole schedule four hours out. That was the original bug.
    """
    try:
        return schedule.every().day.at(time_str, MARKET_TZ)
    except (ModuleNotFoundError, ImportError):
        try:
            converted = _et_to_server_local(time_str)
            print(f"[TZ] pytz not installed -- converted {time_str} ET "
                  f"-> {converted} server-local via zoneinfo")
            return schedule.every().day.at(converted)
        except Exception as e:
            print(f"[TZ] WARNING: could not convert {time_str} to server time ({e}). "
                  f"Job will run in SERVER LOCAL TIME. Set TZ={MARKET_TZ} on Railway.")
            return schedule.every().day.at(time_str)
    except TypeError:
        converted = _et_to_server_local(time_str)
        print(f"[TZ] `schedule` too old for tz support -- converted {time_str} ET "
              f"-> {converted} server-local")
        return schedule.every().day.at(converted)


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
    if not TELEGRAM_CHANNEL_ID:
        return
    try:
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=f"⚠️ *GQ FinXray US — System Alert*\n\n{message}\n\n"
                     f"🕐 {et_now():%I:%M %p ET}",
                parse_mode="Markdown")
    except Exception:
        pass


VALID_IMPACTS = ("HIGH", "MEDIUM", "LOW")


def _as_int(value):
    """alert_run_log's numeric columns are integer. Anything that isn't
    cleanly an int becomes NULL rather than poisoning the whole insert."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_log_row(alert, telegram_success, telegram_error=None):
    """Build one audit row. Templated alerts legitimately have null
    token/attempt fields -- they never touch the LLM.

    Every value is coerced to the column's type here rather than trusting
    whatever landed in alerts.extra, because a single bad type rejects the
    entire insert and the failure is invisible from the delivery loop.
    """
    extra = alert.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    impact = alert.get("impact")
    return {
        "alert_id": alert.get("id"),
        "ticker": alert.get("ticker") or "UNKNOWN",
        "source": alert.get("source"),
        "filing_type": alert.get("filing_type"),
        "feature_id": _as_int(extra.get("feature_id")),
        "feature_name": extra.get("feature_name"),
        # CHECK constraint allows only HIGH/MEDIUM/LOW (or NULL). An
        # unexpected value would reject the row, so it is nulled instead.
        "impact": impact if impact in VALID_IMPACTS else None,
        "summarization_attempts": _as_int(extra.get("summarization_attempts")),
        "input_tokens": _as_int(extra.get("input_tokens")),
        "output_tokens": _as_int(extra.get("output_tokens")),
        "total_tokens": _as_int(extra.get("total_tokens")),
        "llm_calls": _as_int(extra.get("llm_calls")),
        "telegram_success": bool(telegram_success),
        "telegram_error": (str(telegram_error)[:500] if telegram_error else None),
    }


def log_alert_run(alert, telegram_success, telegram_error=None):
    try:
        supabase.table("alert_run_log").insert(
            _run_log_row(alert, telegram_success, telegram_error)).execute()
    except Exception as e:
        # Loud, and names the exception class -- an APIError here means
        # permissions or schema, a plain Exception means the payload.
        print(f"[ERROR] alert_run_log write failed for "
              f"{alert.get('ticker', 'UNKNOWN')}: {type(e).__name__}: {e}")


def verify_run_log():
    """Boot self-test for the audit table.

    alert_run_log stayed empty through a full trading session while alerts
    delivered normally, and nothing in the logs said why. A write that fails
    where nobody is looking is worse than one that fails loudly, so the very
    first thing this process does now is try the write once and report the
    verdict on its own line. The probe row is a legitimate deploy marker and
    is left in place -- the backend key has no DELETE policy on this table.
    """
    try:
        supabase.table("alert_run_log").insert({
            "ticker": "__BOOT_PROBE__",
            "source": "STARTUP",
            "filing_type": "PROBE",
            "telegram_success": False,
            "telegram_error": f"boot probe {et_now():%Y-%m-%d %H:%M:%S ET}",
        }).execute()
        print("[RUNLOG] Self-test PASSED — audit writes are landing.")
        return True
    except Exception as e:
        print(f"[RUNLOG] Self-test FAILED — {type(e).__name__}: {e}")
        print("[RUNLOG] Every alert this process sends will go unlogged. "
              "If this is a permissions error, SUPABASE_KEY on Railway is the "
              "anon key, not service_role.")
        return False


def reconcile_run_log():
    """Backfill audit rows for delivered alerts that never got one.

    Belt and braces for the per-alert write: even if delivery-time logging
    breaks again, the audit table converges to complete within half an hour.

    The read guard matters. alert_run_log has RLS on with an INSERT-only
    policy, so under the anon key a SELECT returns zero rows rather than an
    error -- and a naive "alerts with no log row" query would then look like
    EVERY alert is missing and insert duplicates on every pass, forever.
    So: if the table reads as empty while rows are known to exist, the read
    is untrustworthy and this bails out instead of guessing.
    """
    try:
        logged = supabase.table("alert_run_log").select("alert_id") \
            .order("sent_at", desc=True).limit(1000).execute().data or []

        recent = supabase.table("alerts").select("*") \
            .eq("delivered", True).order("created_at", desc=True) \
            .limit(200).execute().data or []
        if not recent:
            return

        if not logged:
            print("[RUNLOG] Reconcile skipped — audit table reads as empty. "
                  "Either it genuinely is, or SELECT is blocked by RLS and "
                  "backfilling now would duplicate. Check the boot self-test.")
            return

        known = {r["alert_id"] for r in logged if r.get("alert_id")}
        missing = [a for a in recent if a.get("id") not in known]
        if not missing:
            return

        rows = [_run_log_row(a, True, None) for a in missing]
        supabase.table("alert_run_log").insert(rows).execute()
        print(f"[RUNLOG] Reconciled {len(rows)} missing audit row(s).")
    except Exception as e:
        print(f"[RUNLOG] Reconcile failed: {type(e).__name__}: {e}")


# ── Delivery ──────────────────────────────────────────────────────────────────
# How many times an alert may fail to send before we stop retrying it. Without
# this a single un-sendable message would sit at the head of the queue forever
# and block every alert behind it.
MAX_SEND_ATTEMPTS = 3
_send_failures = {}


async def send_telegram(bot, text, chat_id=None):
    """Send one message, and actually get it there.

    Legacy Markdown is unforgiving: one unmatched _ * [ or ` anywhere in the
    text -- and summaries of SEC filings are full of them -- makes Telegram
    reject the WHOLE message with 'Can't parse entities'. Losing an alert over
    a stray underscore is not acceptable, so a parse rejection falls back to
    plain text. An unformatted alert beats no alert.
    """
    chat_id = chat_id or TELEGRAM_CHANNEL_ID
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return True, None
    except Exception as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            return False, e
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            print("[CHANNEL] Markdown rejected — sent as plain text instead")
            return True, None
        except Exception as e2:
            return False, e2


async def deliver_pending_alerts():
    try:
        result = supabase.table("alerts").select("*") \
            .eq("delivered", False).order("created_at").limit(20).execute()
        alerts = result.data
        if not alerts:
            return

        if not TELEGRAM_CHANNEL_ID:
            print(f"[ERROR] {len(alerts)} alert(s) waiting but TELEGRAM_CHANNEL_ID "
                  f"is not set. Leaving them undelivered — they will send as "
                  f"soon as the variable is configured.")
            return

        # python-telegram-bot v20+ will not talk to the API on a Bot that was
        # never initialized -- the underlying HTTPX pool is not started and the
        # call raises before a single byte goes out. `async with` is the
        # documented way to do this for a bare Bot; the old code constructed
        # Bot(token=...) and called send_message on it directly.
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            for alert in alerts:
                alert_id = alert.get("id")
                ticker = alert.get("ticker", "UNKNOWN")
                impact = alert.get("impact", "LOW")

                ok, err = await send_telegram(bot, format_alert(alert))

                if ok:
                    print(f"[CHANNEL] {impact} — ${ticker}")
                    log_alert_run(alert, True)
                    _send_failures.pop(alert_id, None)
                else:
                    fails = _send_failures.get(alert_id, 0) + 1
                    _send_failures[alert_id] = fails
                    print(f"[ERROR] Channel post failed for {ticker} "
                          f"(attempt {fails}/{MAX_SEND_ATTEMPTS}): "
                          f"{type(err).__name__}: {err}")
                    if fails < MAX_SEND_ATTEMPTS:
                        # Leave delivered=False so the next loop retries it.
                        continue
                    print(f"[ERROR] Giving up on {ticker} after "
                          f"{MAX_SEND_ATTEMPTS} attempts — marking delivered "
                          f"so it stops blocking the queue.")
                    log_alert_run(alert, False, err)
                    _send_failures.pop(alert_id, None)

                # Only reached on success, or on final give-up. THIS IS THE FIX:
                # the old code ran this unconditionally, outside the try, so an
                # alert that Telegram rejected was still stamped delivered and
                # never retried. That is how a full session's alerts showed
                # delivered=true in the database while nothing arrived in the
                # channel -- they were not lost in transit, they were marked
                # sent without ever having been sent.
                supabase.table("alerts").update({"delivered": True}) \
                    .eq("id", alert_id).execute()
    except Exception as e:
        print(f"[ERROR] Delivery failed: {type(e).__name__}: {e}")


async def verify_telegram():
    """Prove at boot that we can actually post to the channel.

    Everything upstream of this was working -- filings fetched, summaries
    written, alerts stored -- and the only broken link was the last hop. That
    hop is now tested explicitly instead of being assumed.
    """
    if not TELEGRAM_TOKEN:
        print("[TELEGRAM] Self-test FAILED — TELEGRAM_TOKEN is not set.")
        return False
    if not TELEGRAM_CHANNEL_ID:
        print("[TELEGRAM] Self-test FAILED — TELEGRAM_CHANNEL_ID is not set.")
        return False
    try:
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            me = await bot.get_me()
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=f"✅ GQ FinXray US online — {et_now():%d %b %Y, %I:%M %p ET}")
        print(f"[TELEGRAM] Self-test PASSED — posting as @{me.username} "
              f"to {TELEGRAM_CHANNEL_ID}")
        return True
    except Exception as e:
        print(f"[TELEGRAM] Self-test FAILED — {type(e).__name__}: {e}")
        print("[TELEGRAM] Nothing will reach the channel until this is fixed. "
              "Check: token correct, bot is an ADMIN of the channel, and "
              "TELEGRAM_CHANNEL_ID is the numeric -100... id or @channelname.")
        return False


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
        msg = (f"📊 *{title}*\n_{et_now():%I:%M %p ET}_\n\n{body}\n\n"
               f"_GQ FinXray US · gquants.com_")
        async with Bot(token=TELEGRAM_TOKEN) as bot:
            ok, err = await send_telegram(bot, msg)
        if ok:
            print(f"[REPORT] Sent: {title}")
        else:
            print(f"[ERROR] Market report '{title}' failed: "
                  f"{type(err).__name__}: {err}")
    except Exception as e:
        print(f"[ERROR] Market report failed: {type(e).__name__}: {e}")


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
    verify_run_log()
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

    # Audit self-heal: catches any alert whose delivery-time log write failed.
    schedule.every(30).minutes.do(_safe(reconcile_run_log, "reconcile_run_log"))

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
    await verify_telegram()
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
