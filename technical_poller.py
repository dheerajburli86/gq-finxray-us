"""
technical_poller.py
GQ FinXray US — Feature 6. RSI, 52-week extremes, volume spikes, 200-SMA crossovers.

WHAT WAS WRONG WITH THE PREVIOUS VERSION (14 alerts produced, ever)
-------------------------------------------------------------------
1. It issued `get_rsi + get_snapshot`, `get_sma + get_snapshot` and
   `fmp_client.get_quote` per ticker — ~5 calls per ticker per run. At the real
   6,353-name universe that is ~31,500 calls every 30 minutes, so in practice
   the run died on rate limits long before it finished and the alerts that did
   land were whatever happened to complete first.
2. The volume-spike test divided *partial intraday* volume by a *full prior
   session* volume. At 10:00 ET a stock has ~13% of a day's volume on the tape,
   so the 2.0x threshold was unreachable until roughly 15:00 no matter how
   extreme the activity actually was.
3. The market-hours guard was 09:00–17:30, the India session copied across. US
   regular trading is 09:30–16:00 ET, so the poller spent 90 minutes each day
   firing "crossover" alerts off a frozen post-close snapshot.
4. `already_sent_today()` returned False when the dedup query raised, meaning a
   transient Supabase error re-sent every alert of the day.

THE REARCHITECTURE
------------------
* ONE `get_full_market_snapshot()` call per run supplies day + prevDay OHLCV for
  the whole market. Price, prior close, today's move and today's volume for
  every watched ticker are read out of that single payload. No per-ticker
  snapshot call is ever made for data the snapshot already contains.
* RSI and SMA200 have no bulk equivalent, so they stay per-ticker — but only for
  watched tickers, and capped at MAX_INDICATOR_TICKERS_PER_RUN. When the
  watchlist exceeds the cap the poller rotates deterministically through it
  across runs and logs exactly how many names were covered and how many were
  deferred. Nothing is silently dropped.
* 52-week high/low comes from `fmp_client.get_quotes()` (yearHigh/yearLow), and
  the levels are cached for the trading day — a 52-week extreme moves at most
  once per session, so re-fetching it every 30 minutes buys nothing. Comparing
  today's price against the *start-of-day* level is also the more correct
  breakout test: FMP folds today's move into yearHigh, so a same-call comparison
  degenerates to `price >= price`.
* Dedup is one batched query at the top of the run, and it fails CLOSED: if the
  ledger cannot be read the run emits nothing rather than risk re-sending.

Alerts are written to `alerts` with delivered=False. This module never talks to
Telegram — delivery.py fans each row out to the users watching that ticker.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
import massive_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ET = ZoneInfo("America/New_York")
SOURCE = "TECHNICAL"
POLLER = "technical_poller"

# ── Thresholds ────────────────────────────────────────────────────────────────
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

# 2.0x the volume *expected by this point in the session*. Below ~1.8x the
# signal is indistinguishable from an ordinary busy morning; above ~2.5x it
# fires only a handful of times a month and stops being useful as a heads-up.
VOLUME_SPIKE_MULT = 2.0

MIN_PRICE = 1.0                # sub-$1 names move 30% on no news; not signal
MIN_PREV_VOLUME = 50_000       # illiquid names make the ratio meaningless
MIN_DAY_VOLUME = 25_000        # a 5x ratio on 3,000 shares is noise

# The first half hour carries the opening auction plus every overnight order, so
# even a normal session looks like a spike against a smooth expectation curve.
# Wait until the profile has settled before trusting the ratio.
MIN_SESSION_ELAPSED = 0.08

# Per-run ceiling on tickers that get indicator calls (2 calls each: RSI + SMA).
# 150 tickers = 300 calls per run; at the 30-minute cadence in main.py that is
# ~4,800 indicator calls per session, which sits inside a standard Massive plan.
# A larger watchlist rotates across runs rather than being truncated.
MAX_INDICATOR_TICKERS_PER_RUN = 150

# 52-week levels are cached per trading day, so this cap only bites on the first
# run of the day (or when the watchlist grows mid-session).
MAX_YEAR_RANGE_REFRESH_PER_RUN = 250

ALERT_IMPACT = {
    "RSI_OVERBOUGHT": "MEDIUM",
    "RSI_OVERSOLD": "MEDIUM",
    "52W_HIGH": "HIGH",
    "52W_LOW": "HIGH",
    "VOLUME_SPIKE": "MEDIUM",
    "SMA200_CROSSOVER_UP": "HIGH",
    "SMA200_CROSSOVER_DOWN": "HIGH",
}

# Rotation cursor for the indicator and quote-refresh slices. Process-local by
# design: losing it on restart re-covers the front of the watchlist, which is
# harmless because the per-day dedup stops any repeat alert.
_run_counter = 0

# {"date": <ET date>, "levels": {ticker: (year_high, year_low)}}
_year_range_cache = {"date": None, "levels": {}}


# ── Session clock ─────────────────────────────────────────────────────────────
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)

# Cumulative share of a typical US equity session's volume by wall-clock time.
# Real intraday volume is U-shaped — heavy at the open, thin at lunch, heavy
# into the close — so a linear "minutes elapsed / 390" expectation understates
# the morning and overstates midday, which is what made the old ratio unusable.
# Piecewise-linear interpolation over this profile is close enough to make the
# 2.0x threshold mean roughly the same thing at 10:00 as it does at 15:00.
_VOLUME_PROFILE = [
    (9 * 60 + 30, 0.00), (10 * 60, 0.13), (10 * 60 + 30, 0.21),
    (11 * 60, 0.28), (11 * 60 + 30, 0.34), (12 * 60, 0.39),
    (12 * 60 + 30, 0.44), (13 * 60, 0.49), (13 * 60 + 30, 0.54),
    (14 * 60, 0.59), (14 * 60 + 30, 0.64), (15 * 60, 0.71),
    (15 * 60 + 30, 0.80), (16 * 60, 1.00),
]


def is_market_open(now_et=None):
    """True during US regular trading hours: 09:30–16:00 ET, Monday to Friday."""
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return (MARKET_OPEN[0] * 60 + MARKET_OPEN[1]) <= minutes <= (MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1])


def session_volume_fraction(now_et=None):
    """
    Fraction of a normal session's volume that should be on the tape by now.
    Returns 0.0 before the open and 1.0 at or after the close.
    """
    now_et = now_et or datetime.now(ET)
    minutes = now_et.hour * 60 + now_et.minute + now_et.second / 60.0

    if minutes <= _VOLUME_PROFILE[0][0]:
        return 0.0
    if minutes >= _VOLUME_PROFILE[-1][0]:
        return 1.0

    for (m0, f0), (m1, f1) in zip(_VOLUME_PROFILE, _VOLUME_PROFILE[1:]):
        if m0 <= minutes <= m1:
            span = m1 - m0
            return f0 + (f1 - f0) * ((minutes - m0) / span) if span else f0
    return 1.0


# ── Small helpers ─────────────────────────────────────────────────────────────
def _num(value):
    """Float or None. Tolerates None, '', and comma-formatted strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _pct(new, old):
    """Percentage change, or None when the base is unusable."""
    if old in (None, 0) or new is None:
        return None
    return (new - old) / old * 100.0


def _move_phrase(change_pct):
    if change_pct is None:
        return "with today's move unavailable"
    direction = "up" if change_pct >= 0 else "down"
    return f"{direction} {abs(change_pct):.1f}% on the session"


def _rotating_slice(items, size):
    """
    A deterministic, wrap-around window over `items`, advancing one window per
    run. Returns (window, deferred_count) so the caller can report coverage.
    """
    ordered = sorted(items)
    if not ordered or size >= len(ordered):
        return ordered, 0
    start = (_run_counter * size) % len(ordered)
    window = (ordered + ordered)[start:start + size]
    return window, len(ordered) - size


# ── Dedup ledger ──────────────────────────────────────────────────────────────
def load_sent_today():
    """
    {(ticker, filing_type)} already alerted today, measured from ET midnight.

    Returns None on failure. Callers MUST treat None as "abort the run": the old
    behaviour of swallowing the error and returning "nothing sent yet" turned one
    Supabase blip into a full re-send of every alert already delivered that day.
    """
    midnight_et = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        rows = (supabase.table("alerts")
                .select("ticker, filing_type")
                .eq("source", SOURCE)
                .gte("created_at", midnight_et.isoformat())
                .execute()).data or []
    except Exception as e:
        log_poller_error(POLLER, "load_sent_today", e)
        logger.error("[TECHNICAL] Dedup ledger unreadable (%s) — emitting nothing this run", e)
        return None
    return {((r.get("ticker") or "").upper(), r.get("filing_type")) for r in rows}


def _write_alerts(rows):
    """Bulk insert in chunks so one oversized request cannot lose the whole run."""
    written = 0
    for i in range(0, len(rows), 50):
        chunk = rows[i:i + 50]
        try:
            supabase.table("alerts").insert(chunk).execute()
            written += len(chunk)
        except Exception as e:
            log_poller_error(POLLER, "write_alerts", e,
                             {"tickers": [r["ticker"] for r in chunk]})
    return written


def _build_alert(ticker, alert_type, summary, headline, extra):
    payload = dict(extra or {})
    payload["headline"] = headline

    # Build TradingView link for technical alerts (clickable source)
    filing_url = f"https://www.tradingview.com/chart/?symbol={ticker.upper()}"

    return {
        "ticker": ticker,
        "summary": summary,
        "impact": ALERT_IMPACT.get(alert_type, "MEDIUM"),
        "source": SOURCE,
        "filing_type": alert_type,
        "delivered": False,
        "extra": tag_extra(payload, SOURCE, alert_type),
        "filing_url": filing_url,
    }


# ── Snapshot ──────────────────────────────────────────────────────────────────
def build_snapshot_index(watched):
    """
    {TICKER: {price, prev_close, day_volume, prev_volume, day_high, day_low,
              change_pct}} for watched tickers, from ONE market-wide call.
    Returns None when the snapshot itself failed.
    """
    try:
        rows = massive_client.get_full_market_snapshot()
    except Exception as e:
        log_poller_error(POLLER, "full_market_snapshot", e)
        return None

    if not rows:
        log_poller_error(POLLER, "full_market_snapshot",
                         "Massive returned an empty full-market snapshot")
        return None

    # A holiday or a half-day close returns a full ticker list with no volume on
    # it. Alerting off that is alerting off yesterday's data with today's date.
    live = sum(1 for r in rows if ((r.get("day") or {}).get("v") or 0) > 0)
    if live < 100:
        logger.info("[TECHNICAL] Snapshot has volume on only %d/%d tickers — "
                    "market appears closed (holiday?). Skipping run.", live, len(rows))
        return None

    index = {}
    for r in rows:
        symbol = (r.get("ticker") or "").upper()
        if symbol not in watched:
            continue
        day = r.get("day") or {}
        prev = r.get("prevDay") or {}
        price = _num(day.get("c")) or _num(prev.get("c"))
        prev_close = _num(prev.get("c"))
        index[symbol] = {
            "price": price,
            "prev_close": prev_close,
            "day_volume": _num(day.get("v")),
            "prev_volume": _num(prev.get("v")),
            "day_high": _num(day.get("h")),
            "day_low": _num(day.get("l")),
            "change_pct": _pct(price, prev_close),
        }
    return index


# ── Check 1: volume spike (zero incremental API cost) ─────────────────────────
def check_volume_spikes(index, sent, elapsed_fraction):
    alerts = []
    for ticker, d in index.items():
        if (ticker, "VOLUME_SPIKE") in sent:
            continue
        price, volume, prev_volume = d["price"], d["day_volume"], d["prev_volume"]
        if not price or not volume or not prev_volume:
            continue
        if price < MIN_PRICE or prev_volume < MIN_PREV_VOLUME or volume < MIN_DAY_VOLUME:
            continue

        expected = prev_volume * elapsed_fraction
        if expected <= 0:
            continue
        ratio = volume / expected
        if ratio < VOLUME_SPIKE_MULT:
            continue

        change_pct = d["change_pct"]
        summary = (
            f"{ticker} trading surge: {int(volume):,} shares traded today — "
            f"{ratio:.1f}x the {int(expected):,} expected by this time of day. "
            f"Price: ${price:,.2f}, {_move_phrase(change_pct)}. "
            f"Unusual volume at this level often accompanies major news or institutional positioning."
        )
        alerts.append(_build_alert(
            ticker, "VOLUME_SPIKE", summary,
            "Trading Volume Far Above Normal Pace",
            {
                "price": price,
                "day_volume": int(volume),
                "prev_volume": int(prev_volume),
                "expected_volume_so_far": int(expected),
                "volume_ratio": round(ratio, 2),
                "session_elapsed_fraction": round(elapsed_fraction, 3),
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
            },
        ))
        sent.add((ticker, "VOLUME_SPIKE"))
    return alerts


# ── Check 2: 52-week high / low (day-cached FMP quotes) ───────────────────────
def refresh_year_ranges(watched):
    """
    Ensure `_year_range_cache` holds today's yearHigh/yearLow for as much of the
    watchlist as the per-run cap allows. Returns (refreshed, still_missing).
    """
    today = datetime.now(ET).date()
    if _year_range_cache["date"] != today:
        _year_range_cache["date"] = today
        _year_range_cache["levels"] = {}

    levels = _year_range_cache["levels"]
    missing = sorted(t for t in watched if t not in levels)
    if not missing:
        return 0, 0

    batch = missing[:MAX_YEAR_RANGE_REFRESH_PER_RUN]
    try:
        quotes = fmp_client.get_quotes(batch)
    except Exception as e:
        log_poller_error(POLLER, "refresh_year_ranges", e, {"count": len(batch)})
        return 0, len(missing)

    for ticker in batch:
        q = (quotes or {}).get(ticker) or {}
        high = _num(q.get("yearHigh"))
        low = _num(q.get("yearLow"))
        if high or low:
            levels[ticker] = (high, low)

    return len(batch), max(0, len(missing) - len(batch))


def check_52_week(index, sent):
    levels = _year_range_cache["levels"]
    alerts = []
    for ticker, d in index.items():
        price = d["price"]
        if not price or price < MIN_PRICE:
            continue
        year_high, year_low = levels.get(ticker, (None, None))

        # day_high/day_low catch a breakout that has already faded by the time
        # this run reads the snapshot; price alone would miss an intraday poke
        # through the level.
        session_high = d["day_high"] or price
        session_low = d["day_low"] or price

        if year_high and session_high >= year_high and (ticker, "52W_HIGH") not in sent:
            above = _pct(session_high, year_high)
            summary = (
                f"{ticker} hit a new 52-week high: ${session_high:,.2f} (up {above:.1f}% from "
                f"previous high of ${year_high:,.2f}). Current price: ${price:,.2f}, "
                f"{_move_phrase(d['change_pct'])}. Breakout moves often trigger breakup momentum "
                f"as stop-losses get cleared and technical traders enter long positions."
            )
            alerts.append(_build_alert(
                ticker, "52W_HIGH", summary, "Breaks Out To New 52-Week High",
                {"price": price, "session_high": session_high, "year_high": year_high,
                 "pct_above_prior_high": round(above, 2) if above is not None else None,
                 "change_pct": round(d["change_pct"], 2) if d["change_pct"] is not None else None},
            ))
            sent.add((ticker, "52W_HIGH"))

        # Independent `if`, not `elif`: a session that gaps down and then reverses
        # can print both a new 52-week low and a new 52-week high, and the second
        # signal is exactly the one worth knowing about. The `elif` silently
        # dropped it.
        if year_low and session_low <= year_low and (ticker, "52W_LOW") not in sent:
            below = _pct(year_low, session_low)
            summary = (
                f"{ticker} dropped to a new 52-week low: ${session_low:,.2f} (down {below:.1f}% from "
                f"previous low of ${year_low:,.2f}). Current price: ${price:,.2f}, "
                f"{_move_phrase(d['change_pct'])}. Breakdown below key support levels can accelerate selling "
                f"as long-term holders cut losses and short sellers add to positions."
            )
            alerts.append(_build_alert(
                ticker, "52W_LOW", summary, "Breaks Down To New 52-Week Low",
                {"price": price, "session_low": session_low, "year_low": year_low,
                 "pct_below_prior_low": round(below, 2) if below is not None else None,
                 "change_pct": round(d["change_pct"], 2) if d["change_pct"] is not None else None},
            ))
            sent.add((ticker, "52W_LOW"))
    return alerts


# ── Check 3 & 4: RSI and SMA200 (the only per-ticker API calls) ───────────────
def check_indicators(tickers, index, sent):
    """RSI overbought/oversold and 200-SMA crossovers for one rotation window."""
    alerts = []
    for ticker in tickers:
        d = index.get(ticker)
        if not d or not d["price"]:
            continue
        price = d["price"]
        prev_close = d["prev_close"]

        # ── RSI ──
        try:
            values = massive_client.get_rsi(ticker, window=14)
            rsi = _num((values or [{}])[0].get("value")) if values else None
        except Exception as e:
            log_poller_error(POLLER, "get_rsi", e, {"ticker": ticker})
            rsi = None

        if rsi is not None:
            if rsi >= RSI_OVERBOUGHT and (ticker, "RSI_OVERBOUGHT") not in sent:
                summary = (
                    f"{ticker} overbought alert: RSI(14) = {rsi:.1f} (threshold: {RSI_OVERBOUGHT:.0f}). "
                    f"Price: ${price:,.2f}, {_move_phrase(d['change_pct'])}. "
                    f"Overbought conditions (RSI > 70) typically precede mean reversion within 3-5 days as "
                    f"buyers exhaust and profit-taking sets in."
                )
                alerts.append(_build_alert(
                    ticker, "RSI_OVERBOUGHT", summary, "RSI Moves Into Overbought Territory",
                    {"rsi": round(rsi, 2), "threshold": RSI_OVERBOUGHT, "price": price,
                     "change_pct": round(d["change_pct"], 2) if d["change_pct"] is not None else None},
                ))
                sent.add((ticker, "RSI_OVERBOUGHT"))
            elif rsi <= RSI_OVERSOLD and (ticker, "RSI_OVERSOLD") not in sent:
                summary = (
                    f"{ticker} oversold alert: RSI(14) = {rsi:.1f} (threshold: {RSI_OVERSOLD:.0f}). "
                    f"Price: ${price:,.2f}, {_move_phrase(d['change_pct'])}. "
                    f"Oversold conditions (RSI < 30) often precede reversals as forced selling exhausts and "
                    f"bargain hunters step in."
                )
                alerts.append(_build_alert(
                    ticker, "RSI_OVERSOLD", summary, "RSI Moves Into Oversold Territory",
                    {"rsi": round(rsi, 2), "threshold": RSI_OVERSOLD, "price": price,
                     "change_pct": round(d["change_pct"], 2) if d["change_pct"] is not None else None},
                ))
                sent.add((ticker, "RSI_OVERSOLD"))

        # ── SMA200 crossover ──
        if prev_close is None:
            continue
        try:
            sma_values = massive_client.get_sma(ticker, window=200)
            sma200 = _num((sma_values or [{}])[0].get("value")) if sma_values else None
        except Exception as e:
            log_poller_error(POLLER, "get_sma", e, {"ticker": ticker})
            continue

        if not sma200:
            continue
        distance = _pct(price, sma200)

        if prev_close < sma200 <= price and (ticker, "SMA200_CROSSOVER_UP") not in sent:
            summary = (
                f"{ticker} bullish crossover: price crossed above 200-day MA (${sma200:,.2f}). "
                f"Current: ${price:,.2f}, +{abs(distance):.1f}% above MA. "
                f"This golden cross is a classic trend-reversal signal — stocks crossing above their 200-day MA "
                f"often see sustained uptrend as long-term momentum turns positive."
            )
            alerts.append(_build_alert(
                ticker, "SMA200_CROSSOVER_UP", summary, "Price Crosses Above 200-Day Average",
                {"price": price, "prev_close": prev_close, "sma200": sma200,
                 "pct_from_sma": round(distance, 2) if distance is not None else None},
            ))
            sent.add((ticker, "SMA200_CROSSOVER_UP"))

        elif prev_close > sma200 >= price and (ticker, "SMA200_CROSSOVER_DOWN") not in sent:
            summary = (
                f"{ticker} bearish crossover: price crossed below 200-day MA (${sma200:,.2f}). "
                f"Current: ${price:,.2f}, -{abs(distance):.1f}% below MA. "
                f"This death cross is a classic trend-reversal signal — stocks crossing below their 200-day MA "
                f"often trigger downtrend as long-term momentum turns negative."
            )
            alerts.append(_build_alert(
                ticker, "SMA200_CROSSOVER_DOWN", summary, "Price Crosses Below 200-Day Average",
                {"price": price, "prev_close": prev_close, "sma200": sma200,
                 "pct_from_sma": round(distance, 2) if distance is not None else None},
            ))
            sent.add((ticker, "SMA200_CROSSOVER_DOWN"))

    return alerts


# ── Entry point ───────────────────────────────────────────────────────────────
def run_technical_poller():
    """One full pass. Logs and swallows every failure — never raises."""
    global _run_counter
    try:
        now_et = datetime.now(ET)
        if not is_market_open(now_et):
            logger.info("[TECHNICAL] Outside US regular hours (09:30-16:00 ET, Mon-Fri) — skipping.")
            return

        elapsed = session_volume_fraction(now_et)
        if elapsed < MIN_SESSION_ELAPSED:
            logger.info("[TECHNICAL] Only %.0f%% of the session's expected volume has elapsed — "
                        "too early for a reliable volume comparison. Skipping.", elapsed * 100)
            return

        watched = {t for t in get_watched_tickers() if t}
        if not watched:
            logger.info("[TECHNICAL] No tickers on any watchlist — nothing to check.")
            return

        sent = load_sent_today()
        if sent is None:
            return  # fail closed: cannot prove an alert is new, so send nothing

        index = build_snapshot_index(watched)
        if index is None:
            return
        missing = len(watched) - len(index)
        if missing:
            logger.info("[TECHNICAL] %d of %d watched tickers absent from the market snapshot "
                        "(delisted, OTC or non-equity).", missing, len(watched))

        _run_counter += 1

        alerts = []
        alerts += check_volume_spikes(index, sent, elapsed)

        refreshed, still_missing = refresh_year_ranges(set(index.keys()))
        if refreshed:
            logger.info("[TECHNICAL] Refreshed 52-week levels for %d tickers (%d still pending, "
                        "will be picked up on the next run).", refreshed, still_missing)
        alerts += check_52_week(index, sent)

        window, deferred = _rotating_slice(index.keys(), MAX_INDICATOR_TICKERS_PER_RUN)
        logger.info("[TECHNICAL] Indicator pass: %d tickers covered this run, %d deferred to "
                    "later runs (cap=%d).", len(window), deferred, MAX_INDICATOR_TICKERS_PER_RUN)
        alerts += check_indicators(window, index, sent)

        written = _write_alerts(alerts) if alerts else 0
        api_calls = 1 + (2 * len(window)) + refreshed
        logger.info("[TECHNICAL] Done. %d watched / %d priced, %d alerts written, "
                    "~%d API calls this run.", len(watched), len(index), written, api_calls)

    except Exception as e:
        log_poller_error(POLLER, "run_technical_poller", e)
        logger.exception("[TECHNICAL] Run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_technical_poller()
