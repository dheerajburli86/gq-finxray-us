"""
technical_poller.py
GQ FinXray US — Feature 6. v2, rewired onto market_data.

WHAT CHANGED IN v2 (2026-08-03)
-------------------------------
1. STOPPED RE-FETCHING DATA IT ALREADY HAD.
   v1 pulled the full-market snapshot (day + prevDay OHLCV for ~10,000
   tickers, one call) and then called massive_client.get_snapshot(ticker)
   TWICE MORE for every watchlist ticker -- once inside the RSI loop and once
   inside the SMA loop -- for price and previous close that were already
   sitting in the snapshot it had just downloaded. For N watchlisted tickers
   that was 1 + 5N calls where 1 + 2N does the same work.

2. MARKET HOURS NOW COME FROM THE EXCHANGE.
   v1 hardcoded "09:00-17:30 ET, Mon-Fri". That has no idea about
   Thanksgiving, Good Friday, half-days, or an unscheduled close -- so on a
   market holiday it would happily fire "crossover" alerts computed off a
   stale snapshot of the last real session. Now asks Massive for actual
   exchange status. Also note v1's window was wrong on both ends: the US cash
   session is 09:30-16:00 ET, not 09:00-17:30 (that was the Indian NSE window
   carried over from the India codebase).

3. 52-WEEK LEVELS ARE CACHED.
   These move at most once a day. v1 burned a fresh FMP quote per ticker per
   hourly run to re-read a number that hadn't changed. market_data caches
   them for six hours.

4. FAILOVER. Price lookups now go through market_data, which prefers the
   shared Massive snapshot, falls back to a Massive single-ticker call, then
   to FMP. Previously an FMP hiccup took 52-week alerts offline entirely.

Every failure path still writes to Supabase `poller_error_log`.
"""

import os
import logging
import traceback
from datetime import date, datetime, timezone

from supabase import create_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import massive_client
import market_data
from feature_map import tag_extra

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
        get_supabase().table("poller_error_log").insert({
            "poller_name": "technical_poller",
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {}
        }).execute()
    except Exception as log_err:
        logger.error(f"[TECHNICAL] Failed to write to poller_error_log: {log_err}")


def get_watchlist_tickers():
    try:
        result = get_supabase().table("watchlists").select("ticker").execute()
        return sorted({row["ticker"] for row in result.data if row.get("ticker")})
    except Exception as e:
        log_poller_error("get_watchlist_tickers", e)
        return []


def already_sent_today(ticker, alert_type):
    try:
        today = date.today().isoformat()
        result = get_supabase().table("alerts") \
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
        get_supabase().table("alerts").insert({
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Volume spike — whole market, from the shared snapshot ────────────────────
def run_volume_spike(watchlist_set, snapshot):
    logger.info("[TECHNICAL] Volume spike check across shared market snapshot...")
    alerts = 0
    for symbol in watchlist_set:
        try:
            row = snapshot.get(symbol)
            if not row:
                continue
            day = row.get("day") or {}
            prev = row.get("prevDay") or {}
            price = day.get("c") or prev.get("c")
            volume = day.get("v")
            prev_volume = prev.get("v")
            if not price or not volume or not prev_volume:
                continue
            if price < MIN_PRICE or volume < MIN_VOLUME:
                continue
            ratio = volume / prev_volume
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
                f"_Source: Massive full-market snapshot | {now_utc()}_"
            )
            save_alert(symbol, "VOLUME_SPIKE", summary, "MEDIUM", {
                "price": price, "volume": volume,
                "prev_volume": prev_volume, "ratio": round(ratio, 2)
            })
            alerts += 1
        except Exception as e:
            log_poller_error("run_volume_spike", e, {"ticker": symbol})

    logger.info(f"[TECHNICAL] Volume spike: {alerts} alert(s)")
    return alerts


# ── RSI — indicator call per ticker, price from the shared snapshot ──────────
def run_rsi(tickers, snapshot):
    logger.info(f"[TECHNICAL] RSI for {len(tickers)} ticker(s)...")
    alerts = 0
    for ticker in tickers:
        try:
            values = massive_client.get_rsi(ticker, window=14)
            if not values:
                continue
            rsi = values[0].get("value")
            if rsi is None:
                continue

            row = snapshot.get(ticker) or {}
            price = (row.get("day") or {}).get("c") or (row.get("prevDay") or {}).get("c")
            if price is None:
                q = market_data.quote(ticker)
                price = q["price"] if q else None
            if price is None:
                continue

            if rsi >= RSI_OVERBOUGHT and not already_sent_today(ticker, "RSI_OVERBOUGHT"):
                summary = (
                    f"📈 *Technical Alert — RSI Overbought*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*RSI(14):* {rsi:.1f} (above {RSI_OVERBOUGHT})\n"
                    f"*Signal:* Stock may be overbought — potential pullback zone.\n"
                    f"_Source: Massive technical indicators | {now_utc()}_"
                )
                save_alert(ticker, "RSI_OVERBOUGHT", summary, "MEDIUM", {"rsi": rsi, "price": price})
                alerts += 1
            elif rsi <= RSI_OVERSOLD and not already_sent_today(ticker, "RSI_OVERSOLD"):
                summary = (
                    f"📉 *Technical Alert — RSI Oversold*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*RSI(14):* {rsi:.1f} (below {RSI_OVERSOLD})\n"
                    f"*Signal:* Stock may be oversold — potential bounce zone.\n"
                    f"_Source: Massive technical indicators | {now_utc()}_"
                )
                save_alert(ticker, "RSI_OVERSOLD", summary, "MEDIUM", {"rsi": rsi, "price": price})
                alerts += 1
        except Exception as e:
            log_poller_error("run_rsi", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] RSI: {alerts} alert(s)")
    return alerts


# ── 52-week high/low — cached FMP levels, live price from the snapshot ───────
def run_52week(tickers, snapshot):
    logger.info(f"[TECHNICAL] 52-week levels for {len(tickers)} ticker(s)...")
    alerts = 0
    for ticker in tickers:
        try:
            high_52, low_52 = market_data.year_high_low(ticker)
            if high_52 is None and low_52 is None:
                continue

            row = snapshot.get(ticker) or {}
            price = (row.get("day") or {}).get("c")
            if price is None:
                q = market_data.quote(ticker)
                price = q["price"] if q else None
            if price is None:
                continue

            if high_52 and price >= high_52 and not already_sent_today(ticker, "52W_HIGH"):
                summary = (
                    f"🚀 *Technical Alert — 52-Week High*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*52-Week High:* ${high_52:.2f}\n"
                    f"*Signal:* Trading at or above its 52-week high — strong bullish momentum.\n"
                    f"_Source: FMP levels + Massive price | {now_utc()}_"
                )
                save_alert(ticker, "52W_HIGH", summary, "HIGH", {"price": price, "high_52w": high_52})
                alerts += 1
            elif low_52 and price <= low_52 and not already_sent_today(ticker, "52W_LOW"):
                summary = (
                    f"⚠️ *Technical Alert — 52-Week Low*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f}\n"
                    f"*52-Week Low:* ${low_52:.2f}\n"
                    f"*Signal:* Trading at or below its 52-week low — watch for further downside.\n"
                    f"_Source: FMP levels + Massive price | {now_utc()}_"
                )
                save_alert(ticker, "52W_LOW", summary, "HIGH", {"price": price, "low_52w": low_52})
                alerts += 1
        except Exception as e:
            log_poller_error("run_52week", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] 52-week: {alerts} alert(s)")
    return alerts


# ── SMA200 crossover — indicator per ticker, prices from the shared snapshot ─
def run_sma200(tickers, snapshot):
    logger.info(f"[TECHNICAL] SMA200 crossover for {len(tickers)} ticker(s)...")
    alerts = 0
    for ticker in tickers:
        try:
            sma_values = massive_client.get_sma(ticker, window=200)
            if not sma_values:
                continue
            sma200 = sma_values[0].get("value")
            if sma200 is None:
                continue

            row = snapshot.get(ticker) or {}
            price = (row.get("day") or {}).get("c")
            prev_close = (row.get("prevDay") or {}).get("c")
            if price is None or prev_close is None:
                q = market_data.quote(ticker)
                if not q:
                    continue
                price = price if price is not None else q["price"]
                prev_close = prev_close if prev_close is not None else q.get("prev_close")
            if price is None or prev_close is None:
                continue

            if prev_close < sma200 <= price and not already_sent_today(ticker, "SMA200_CROSSOVER_UP"):
                summary = (
                    f"🟢 *Technical Alert — 200-SMA Breakout*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f} (crossed above 200-SMA)\n"
                    f"*200-SMA:* ${sma200:.2f}\n"
                    f"*Signal:* Bullish crossover — price moved above long-term trend.\n"
                    f"_Source: Massive technical indicators | {now_utc()}_"
                )
                save_alert(ticker, "SMA200_CROSSOVER_UP", summary, "HIGH",
                           {"price": price, "sma200": sma200, "prev_close": prev_close})
                alerts += 1
            elif prev_close > sma200 >= price and not already_sent_today(ticker, "SMA200_CROSSOVER_DOWN"):
                summary = (
                    f"🔴 *Technical Alert — 200-SMA Breakdown*\n\n"
                    f"*Ticker:* ${ticker}\n*Price:* ${price:.2f} (crossed below 200-SMA)\n"
                    f"*200-SMA:* ${sma200:.2f}\n"
                    f"*Signal:* Bearish crossover — price dropped below long-term trend.\n"
                    f"_Source: Massive technical indicators | {now_utc()}_"
                )
                save_alert(ticker, "SMA200_CROSSOVER_DOWN", summary, "HIGH",
                           {"price": price, "sma200": sma200, "prev_close": prev_close})
                alerts += 1
        except Exception as e:
            log_poller_error("run_sma200", e, {"ticker": ticker})

    logger.info(f"[TECHNICAL] SMA200: {alerts} alert(s)")
    return alerts


# ── Main entry point ──────────────────────────────────────────────────────────
def run_technical_poller():
    if not market_data.market_open():
        logger.info("[TECHNICAL] Exchange reports market closed — skipping this run.")
        return

    logger.info("[TECHNICAL] ===== Technical Alerts Poller (Massive + FMP) =====")
    watchlist = get_watchlist_tickers()
    if not watchlist:
        logger.info("[TECHNICAL] Watchlist empty — nothing to scan.")
        return

    # ONE snapshot for the whole run. Every price and previous close below
    # comes out of this rather than from a per-ticker call.
    snapshot = market_data.snapshot_all(force=True)
    if not snapshot:
        log_poller_error("run_technical_poller", "Market snapshot unavailable — "
                         "per-ticker fallbacks will be used, expect a slower run")

    total = 0
    total += run_volume_spike(set(watchlist), snapshot)
    total += run_rsi(watchlist, snapshot)
    total += run_52week(watchlist, snapshot)
    total += run_sma200(watchlist, snapshot)

    logger.info(f"[TECHNICAL] ===== Done. {total} alert(s). "
                f"Provider stats: {market_data.get_stats()} =====")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_technical_poller()
