"""
technical_poller.py
GQ FinXray US — Technical indicators via FMP + Massive. Feature 6.

ARCHITECTURE:
- Massive's full-market snapshot (ONE call, https://massive.com/docs ->
  /v2/snapshot/locale/us/markets/stocks/tickers) returns day + prevDay OHLCV
  for ~10,000+ tickers in a single response. Volume-spike detection is
  computed locally from that single payload.
- RSI and SMA200 come from Massive's indicator endpoints
  (/v1/indicators/rsi, /v1/indicators/sma).
- 52-week high/low is checked per watchlist ticker via FMP's quote endpoint
  (yearHigh/yearLow are native fields).

Every failure path writes to Supabase `poller_error_log` for visibility.

Runs every 60 minutes via scheduler in main.py.
"""

import os
import logging
import traceback
from datetime import date
from supabase import create_client
from dotenv import load_dotenv

import fmp_client
import massive_client
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

RSI_OVERBOUGHT    = 70
RSI_OVERSOLD      = 30
VOLUME_SPIKE_MULT = 2.0
MIN_PRICE         = 1.0
MIN_VOLUME        = 50000


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def log_poller_error(job_name, error, context=None):
    logger.error(f"[TECHNICAL] {job_name}: {error}")
    try:
        sb = get_supabase()
        sb.table("poller_error_log").insert({
            "poller_name": "technical_poller",
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {}
        }).execute()
    except Exception as log_err:
        logger.error(f"[TECHNICAL] Failed to write to poller_error_log: {log_err}")


def get_all_stocks():
    """Fetch all US stocks (NYSE + NASDAQ) from the stocks table."""
    try:
        sb = get_supabase()
        result = sb.table("stocks").select("ticker").execute()
        return [row["ticker"] for row in result.data if row.get("ticker")]
    except Exception as e:
        log_poller_error("get_all_stocks", e)
        return []


def already_sent_today(ticker, alert_type):
    try:
        sb = get_supabase()
        today = date.today().isoformat()
        result = sb.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", "TECHNICAL") \
            .eq("filing_type", alert_type) \
            .gte("created_at", f"{today}T00:00:00+00:00") \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def save_alert(ticker, alert_type, summary, impact, extra=None):
    try:
        sb = get_supabase()
        sb.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "TECHNICAL",
            "filing_type": alert_type,
            "delivered": False,
            "extra": tag_extra(extra, "TECHNICAL", alert_type),
            "filing_url": None
        }).execute()
        logger.info(f"[TECHNICAL] Alert saved: {ticker} | {alert_type} | {impact}")
    except Exception as e:
        log_poller_error("save_alert", e, {"ticker": ticker, "alert_type": alert_type})


def now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def is_market_hours():
    """
    Alerts are only meaningful 09:00-17:30 US Eastern, Monday-Friday -- outside
    that window Massive's snapshot is just yesterday's closing data repeated,
    and firing "crossover"/"overbought" alerts off a stale, closed-market
    snapshot (e.g. right after a Saturday process restart) would be actively
    misleading. Mirrors the market-window guard the India system's alert
    checker already used (09:00-17:30 local).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_t = now_et.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now_et.replace(hour=17, minute=30, second=0, microsecond=0)
    return open_t <= now_et <= close_t


# ── Volume spike — whole market, ONE Massive call ─────────────────────────────
def run_volume_spike_snapshot(watchlist_set):
    logger.info("[TECHNICAL] Running whole-market volume spike check via Massive snapshot...")
    alerts = 0
    try:
        tickers = massive_client.get_full_market_snapshot()
    except Exception as e:
        log_poller_error("run_volume_spike_snapshot:fetch", e)
        return 0

    if not tickers:
        log_poller_error("run_volume_spike_snapshot:empty", "Massive full snapshot returned no tickers")
        return 0

    for t in tickers:
        try:
            symbol = t.get("ticker")
            # The whole point of pulling the FULL market snapshot in one call
            # is efficiency (vs. one call per watchlisted ticker) -- but that
            # doesn't mean every qualifying ticker should alert. Without this
            # filter, a volume spike in a stock nobody follows still gets
            # broadcast to every subscriber on the shared Telegram channel.
            if not symbol or symbol not in watchlist_set:
                continue
            day = t.get("day", {}) or {}
            prev = t.get("prevDay", {}) or {}
            price = day.get("c") or prev.get("c")
            volume = day.get("v")
            prev_volume = prev.get("v")
            if not price or not volume or not prev_volume:
                continue
            if price < MIN_PRICE or volume < MIN_VOLUME:
                continue
            ratio = volume / prev_volume if prev_volume else 0
            if ratio < VOLUME_SPIKE_MULT:
                continue
            if already_sent_today(symbol, "VOLUME_SPIKE"):
                continue

            summary = (
                f"📊 *Technical Alert — Volume Spike*\n\n"
                f"*Ticker:* ${symbol}\n"
                f"*Price:* ${float(price):.2f}\n"
                f"*Volume:* {int(volume):,} ({ratio:.1f}x prior session)\n"
                f"*Signal:* Unusual trading volume — potential significant move ahead.\n"
                f"_Source: Massive Full-Market Snapshot | {now_utc()}_"
            )
            save_alert(symbol, "VOLUME_SPIKE", summary, "MEDIUM", {
                "price": price, "volume": volume, "prev_volume": prev_volume, "ratio": round(ratio, 2)
            })
            alerts += 1
        except Exception as e:
            log_poller_error("run_volume_spike_snapshot:row", e, {"row": t})

    logger.info(f"[TECHNICAL] Volume spike: {alerts} alerts from {len(tickers)} tickers scanned")
    return alerts


# ── RSI — per watchlist ticker via Massive indicator endpoint ────────────────
def run_rsi_watchlist(tickers):
    logger.info(f"[TECHNICAL] Running RSI for {len(tickers)} watchlisted tickers...")
    alerts = 0
    for ticker in tickers:
        try:
            values = massive_client.get_rsi(ticker, window=14)
            snapshot = massive_client.get_snapshot(ticker)
            if not values or not snapshot:
                continue
            rsi = values[0].get("value")
            price = (snapshot.get("day") or {}).get("c") or (snapshot.get("prevDay") or {}).get("c")
            if rsi is None or price is None:
                continue

            if rsi >= RSI_OVERBOUGHT and not already_sent_today(ticker, "RSI_OVERBOUGHT"):
                summary = (
                    f"📈 *Technical Alert — RSI Overbought*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*RSI(14):* {rsi:.1f} (above {RSI_OVERBOUGHT})\n"
                    f"*Signal:* Stock may be overbought — potential pullback zone.\n"
                    f"_Source: Massive Technical Indicators | {now_utc()}_"
                )
                save_alert(ticker, "RSI_OVERBOUGHT", summary, "MEDIUM", {"rsi": rsi, "price": price})
                alerts += 1
            elif rsi <= RSI_OVERSOLD and not already_sent_today(ticker, "RSI_OVERSOLD"):
                summary = (
                    f"📉 *Technical Alert — RSI Oversold*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*RSI(14):* {rsi:.1f} (below {RSI_OVERSOLD})\n"
                    f"*Signal:* Stock may be oversold — potential bounce zone.\n"
                    f"_Source: Massive Technical Indicators | {now_utc()}_"
                )
                save_alert(ticker, "RSI_OVERSOLD", summary, "MEDIUM", {"rsi": rsi, "price": price})
                alerts += 1
        except Exception as e:
            log_poller_error("run_rsi_watchlist", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] RSI: {alerts} alerts")
    return alerts


# ── 52-week high/low — per watchlist ticker via FMP quote ────────────────────
def run_52week_watchlist(tickers):
    logger.info(f"[TECHNICAL] Running 52-week high/low for {len(tickers)} watchlisted tickers...")
    alerts = 0
    for ticker in tickers:
        try:
            quote = fmp_client.get_quote(ticker)
            if not quote:
                continue
            price = quote.get("price")
            high_52 = quote.get("yearHigh")
            low_52 = quote.get("yearLow")
            if price is None:
                continue

            if high_52 and price >= high_52 and not already_sent_today(ticker, "52W_HIGH"):
                summary = (
                    f"🚀 *Technical Alert — 52-Week High*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*52-Week High:* ${high_52:.2f}\n"
                    f"*Signal:* Stock is at/above its 52-week high — strong bullish momentum.\n"
                    f"_Source: FMP Quote | {now_utc()}_"
                )
                save_alert(ticker, "52W_HIGH", summary, "HIGH", {"price": price, "high_52w": high_52})
                alerts += 1
            elif low_52 and price <= low_52 and not already_sent_today(ticker, "52W_LOW"):
                summary = (
                    f"⚠️ *Technical Alert — 52-Week Low*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*52-Week Low:* ${low_52:.2f}\n"
                    f"*Signal:* Stock is at/below its 52-week low — watch for further downside.\n"
                    f"_Source: FMP Quote | {now_utc()}_"
                )
                save_alert(ticker, "52W_LOW", summary, "HIGH", {"price": price, "low_52w": low_52})
                alerts += 1
        except Exception as e:
            log_poller_error("run_52week_watchlist", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] 52-week: {alerts} alerts")
    return alerts


# ── SMA200 crossover — per watchlist ticker via Massive ───────────────────────
def run_sma200_crossover(tickers):
    logger.info(f"[TECHNICAL] Running SMA200 crossover for {len(tickers)} watchlisted tickers...")
    alerts = 0
    for ticker in tickers:
        try:
            sma_values = massive_client.get_sma(ticker, window=200)
            snapshot = massive_client.get_snapshot(ticker)
            if not sma_values or not snapshot:
                continue

            sma200 = sma_values[0].get("value")
            price = (snapshot.get("day") or {}).get("c")
            prev_close = (snapshot.get("prevDay") or {}).get("c")
            if not all([sma200, price, prev_close]):
                continue

            if prev_close < sma200 and price > sma200 and not already_sent_today(ticker, "SMA200_CROSSOVER_UP"):
                summary = (
                    f"🟢 *Technical Alert — 200-SMA Breakout*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f} (crossed above 200-SMA)\n"
                    f"*200-SMA:* ${sma200:.2f}\n"
                    f"*Signal:* Bullish crossover — price moved above long-term trend.\n"
                    f"_Source: Massive Technical Indicators | {now_utc()}_"
                )
                save_alert(ticker, "SMA200_CROSSOVER_UP", summary, "HIGH",
                           {"price": price, "sma200": sma200, "prev_close": prev_close})
                alerts += 1
            elif prev_close > sma200 and price < sma200 and not already_sent_today(ticker, "SMA200_CROSSOVER_DOWN"):
                summary = (
                    f"🔴 *Technical Alert — 200-SMA Breakdown*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f} (crossed below 200-SMA)\n"
                    f"*200-SMA:* ${sma200:.2f}\n"
                    f"*Signal:* Bearish crossover — price dropped below long-term trend.\n"
                    f"_Source: Massive Technical Indicators | {now_utc()}_"
                )
                save_alert(ticker, "SMA200_CROSSOVER_DOWN", summary, "HIGH",
                           {"price": price, "sma200": sma200, "prev_close": prev_close})
                alerts += 1
        except Exception as e:
            log_poller_error("run_sma200_crossover", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] SMA200 crossover: {alerts} alerts")
    return alerts


# ── Main entry point ──────────────────────────────────────────────────────────
def run_technical_poller():
    if not is_market_hours():
        logger.info("[TECHNICAL] Outside market hours (09:00-17:30 ET, Mon-Fri) — skipping this run.")
        return

    logger.info("[TECHNICAL] ===== Starting Technical Alerts Poller (FMP + Massive) =====")
    total = 0
    watchlist = get_all_stocks()
    total += run_volume_spike_snapshot(set(watchlist))

    if watchlist:
        total += run_rsi_watchlist(watchlist)
        total += run_52week_watchlist(watchlist)
        total += run_sma200_crossover(watchlist)

    logger.info(f"[TECHNICAL] ===== Done. Total alerts generated: {total} =====")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_technical_poller()
