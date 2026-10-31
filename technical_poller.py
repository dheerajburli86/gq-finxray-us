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
2. The volume-spike test divided partial intraday volume by a *full prior
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
* 52-week high/low and the 20-day average volume come from one Massive daily-
  bars call per ticker per day, over COMPLETED sessions only, cached for the
  trading day. Today's session high/low is compared against those levels.
  (FMP's quote was used until 2026-10-23; its yearHigh already contains today's
  prints, pre-market included, so it could not say what the previous high was.)
* Dedup is one batched query at the top of the run, and it fails CLOSED: if the
  ledger cannot be read the run emits nothing rather than risk re-sending.

Alerts are written to `alerts` with delivered=False. This module never talks to
Telegram — delivery.py fans each row out to the users watching that ticker.
"""

import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
import massive_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error
# The exchange holiday calendar lives here. Importing it rather than
# reimplementing it keeps one definition of "is the market actually open".
from heatmap_generator import is_trading_day

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

# Sessions behind "average daily volume" for the spike baseline -- the same
# 20-day window Feature 7 uses, so both features mean the same "normal".
ADV_SESSIONS = 20
ADV_MIN_SESSIONS = 10

# A 52-week high/low alert that has already reversed this far by the time it is
# sent says so, rather than reading as a breakout still in progress (XLE on
# 2026-10-22: high of $68.78 at the open, alert at 10:00 with the fund down 1%).
REVERSAL_NOTE_PCT = 1.0

# ── Deadbands ─────────────────────────────────────────────────────────────────
# A "golden cross" is a trend signal, so the price has to actually CLEAR the
# average. Without a deadband the test fired on any price that happened to sit
# a hair past the line: live on 2026-10-15/16 that produced SCHA up +0.1% then
# down -0.1% the next morning, VB up +0.3% then down -0.4%, and VGK up +0.2% —
# six HIGH-impact "trend reversals" in two days from a price oscillating around
# its own 200-day average. The margin is what separates a crossover from chop.
SMA200_MIN_DISTANCE_PCT = float(os.getenv("GQ_SMA200_MIN_DISTANCE_PCT", "0.5"))

# How long one direction of a crossover stays spent for a ticker. The dedup is
# otherwise per-day, so EEM alerted SMA200_CROSSOVER_DOWN on both Oct 15 and
# Oct 16 — the same death cross, announced twice, because it closed back above
# the average in between. A trend reversal is not a daily event.
SMA200_REPEAT_DAYS = int(os.getenv("GQ_SMA200_REPEAT_DAYS", "10"))

# A 52-week breakout must exceed the prior high, not equal it. FMP folds the
# current session into `yearHigh`, so `session_high >= year_high` was
# tautologically true for any ticker sitting at its high and the alert printed
# its own premise back: XLE on 2026-10-16 read "hit a new 52-week high: $67.90
# (up 0.0% from previous high of $67.90)".
YEAR_BREAKOUT_MIN_PCT = float(os.getenv("GQ_YEAR_BREAKOUT_MIN_PCT", "0.05"))

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
_year_range_cache = {"date": None, "levels": {}, "avg_volume": {}, "level_dates": {}}


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
    """
    True during US regular trading hours: 09:30–16:00 ET on a trading day.

    BUGFIX 2026-08-19: this tested `weekday() >= 5` and nothing else. On every
    US market holiday — Thanksgiving, Good Friday, Juneteenth, Christmas, MLK
    Day, Memorial Day, Labor Day, July 4th, Presidents' Day — it reported the
    market open, so this poller and etf_flow_poller (which imports this exact
    function) spent the day computing "volume spikes" and "crossovers" against
    the previous session's stale closing prices and alerting on them. A
    holiday-aware calendar already existed in heatmap_generator; it is used now.

    The close boundary is also exclusive now: 16:00:00 is the close, not a
    moment of open trading, and the market lane's 30-minute cadence lands runs
    on the hour often enough for that to matter.
    """
    now_et = now_et or datetime.now(ET)
    if not is_trading_day(now_et.date()):
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return ((MARKET_OPEN[0] * 60 + MARKET_OPEN[1]) <= minutes
            < (MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]))


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
_ETF_VOLUME_SIGNALS = {"ETF_MOMENTUM_UP", "ETF_MOMENTUM_DOWN",
                       "ETF_BIG_MOVE_UP", "ETF_BIG_MOVE_DOWN"}


def alert_types(row):
    """filing_type plus any signal merged into the row (merge_alerts_by_ticker)."""
    ex = row.get("extra") or {}
    return [row.get("filing_type")] + list(ex.get("merged_filing_types") or [])


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
                .select("ticker, filing_type, source, extra")
                .in_("source", [SOURCE, "ETF_SIGNAL"])
                .gte("created_at", midnight_et.isoformat())
                .execute()).data or []
    except Exception as e:
        log_poller_error(POLLER, "load_sent_today", e)
        logger.error("[TECHNICAL] Dedup ledger unreadable (%s) — emitting nothing this run", e)
        return None
    sent = set()
    for r in rows:
        t = (r.get("ticker") or "").upper()
        for ft in alert_types(r):
            if r.get("source", SOURCE) == SOURCE:
                sent.add((t, ft))
            elif ft in _ETF_VOLUME_SIGNALS:
                # 2026-10-20: XLV "Heavy Buying" (Feature 7, 10:04) then "Trading
                # Volume Far Above Normal Pace" (Feature 6, 10:21) -- the same
                # surge twice. The ETF signal already carries the volume.
                sent.add((t, "VOLUME_SPIKE"))
    return sent


# Crossovers are the one alert type whose per-day dedup is not enough: a trend
# reversal is a multi-week event, so the SAME direction repeating days later is
# the same signal, not a new one. Fails CLOSED (returns everything as spent) so
# an unreadable ledger cannot produce a duplicate storm.
_CROSSOVER_TYPES = ("SMA200_CROSSOVER_UP", "SMA200_CROSSOVER_DOWN")


def load_recent_crossovers(days=SMA200_REPEAT_DAYS):
    """{(ticker, filing_type)} crossovers alerted in the last `days` days."""
    since = (datetime.now(ET) - timedelta(days=days)).isoformat()
    try:
        # No filing_type filter: a crossover merged under a 52-week-high lead
        # is stored with filing_type 52W_HIGH and must still count.
        rows = (supabase.table("alerts")
                .select("ticker, filing_type, extra")
                .eq("source", SOURCE)
                .gte("created_at", since)
                .execute()).data or []
    except Exception as e:
        log_poller_error(POLLER, "load_recent_crossovers", e)
        logger.error("[TECHNICAL] Crossover history unreadable (%s) — suppressing "
                     "crossovers this run rather than risking a repeat", e)
        return None
    return {((r.get("ticker") or "").upper(), ft) for r in rows
            for ft in alert_types(r) if ft in _CROSSOVER_TYPES}


def load_names(tickers):
    """{ticker: name} from stocks / etf_master / mf_master. Feature 6 alerts went
    out headed "$MGK" with no fund name while every other feature named it.
    Best effort: a failed read just leaves the header as it was."""
    tickers = sorted({t.upper() for t in tickers if t})
    out = {}
    if not tickers:
        return out
    for table, cols, key in (("stocks", "*", None), ("etf_master", "ticker, name", "name"),
                             ("mf_master", "ticker, series_name", "series_name")):
        todo = [t for t in tickers if t not in out]
        if not todo:
            break
        try:
            rows = supabase.table(table).select(cols).in_("ticker", todo).execute().data or []
        except Exception as e:
            log_poller_error(POLLER, f"load_names_{table}", e)
            continue
        for r in rows:
            n = r.get(key) if key else (r.get("name") or r.get("company_name") or r.get("companyName"))
            if r.get("ticker") and n:
                out[r["ticker"].upper()] = n
    return out


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


_IMPACT_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def merge_alerts_by_ticker(alerts):
    """
    One message per ticker per run. On 2026-10-30 XLV got "New 52-Week High"
    and "Trading Volume Far Above Normal Pace" from the same 10:27 run as two
    messages (and on 10-29 XLV and SCHD each got RSI + 52-week high). Lead =
    most severe signal; the others go in extra.merged_filing_types, which
    load_sent_today, load_recent_crossovers and the ETF engine's volume check
    all read, so a merged signal can't fire again on its own.
    """
    groups = {}
    for a in alerts:
        groups.setdefault(a["ticker"].upper(), []).append(a)
    out = []
    for rows in groups.values():
        if len(rows) == 1:
            out.append(rows[0])
            continue
        rows = sorted(rows, key=lambda r: -_IMPACT_RANK.get(r.get("impact"), 0))
        lead = dict(rows[0])
        extra = dict(lead["extra"])
        extra["headline"] = " · ".join((r["extra"].get("headline") or r["filing_type"]) for r in rows)
        extra["merged_filing_types"] = [r["filing_type"] for r in rows[1:]]
        extra["merged_signals"] = [dict({k: v for k, v in r["extra"].items()
                                         if k not in ("headline", "company_name")},
                                        filing_type=r["filing_type"]) for r in rows[1:]]
        lead["summary"] = " ".join(r["summary"] for r in rows)
        lead["extra"] = extra
        out.append(lead)
    return out


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

        # One prior session is a noisy baseline: on 2026-10-20 XLV "2.5x" was
        # measured against Monday's 5.9M shares while its 20-day average is
        # 8.4M -- against the average it was 1.75x, below the threshold. Use
        # FMP's average daily volume when the day cache has it.
        avg = (_year_range_cache.get("avg_volume") or {}).get(ticker)
        baseline = avg or prev_volume
        expected = baseline * elapsed_fraction
        if expected <= 0:
            continue
        ratio = volume / expected
        if ratio < VOLUME_SPIKE_MULT:
            continue

        change_pct = d["change_pct"]
        summary = (
            f"{ticker} trading surge: {int(volume):,} shares traded today — "
            f"{ratio:.1f}x the {int(expected):,} expected by this time of day "
            f"({'average daily volume' if avg else 'previous session'} {int(baseline):,}). "
            f"Price: ${price:,.2f}, {_move_phrase(change_pct)}."
        )
        alerts.append(_build_alert(
            ticker, "VOLUME_SPIKE", summary,
            "Trading Volume Far Above Normal Pace",
            {
                "price": price,
                "day_volume": int(volume),
                "prev_volume": int(prev_volume),
                "baseline_volume": int(baseline),
                "baseline": "avg_volume" if avg else "prev_day",
                "expected_volume_so_far": int(expected),
                "volume_ratio": round(ratio, 2),
                "session_elapsed_fraction": round(elapsed_fraction, 3),
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
            },
        ))
        sent.add((ticker, "VOLUME_SPIKE"))
    return alerts


# ── Check 2: 52-week high / low (day-cached FMP quotes) ───────────────────────
YEAR_SESSIONS = 252


def _daily_history(ticker, today):
    """Complete sessions strictly before `today` from Massive daily bars,
    oldest first: [(date, high, low, volume)]. None when the fetch failed."""
    frm = (today - timedelta(days=372)).isoformat()
    to = (today - timedelta(days=1)).isoformat()
    try:
        bars = massive_client.get_aggregates(ticker, 1, "day", frm, to)
    except Exception as e:
        log_poller_error(POLLER, "daily_history", e, {"ticker": ticker})
        return None
    by_day = {}
    for b in bars or []:
        t = b.get("t")
        if t is None:
            continue
        d = datetime.fromtimestamp(float(t) / 1000.0, tz=ET).date()
        if d < today:
            by_day[d] = (_num(b.get("h")), _num(b.get("l")), _num(b.get("v")))
    return [(d,) + by_day[d] for d in sorted(by_day)]


def _avg_daily_volume(ticker, today, history=None):
    """Mean volume of the last ADV_SESSIONS complete sessions. FMP's
    /stable/quote has no avgVolume field, so Feature 6 used to measure spikes
    against one prior session."""
    history = _daily_history(ticker, today) if history is None else history
    recent = [v for (_, _, _, v) in (history or []) if v][-ADV_SESSIONS:]
    if len(recent) < ADV_MIN_SESSIONS:
        return None
    return sum(recent) / len(recent)


def _year_levels(history):
    """((high, high_date), (low, low_date)) over the last YEAR_SESSIONS complete
    sessions, or None."""
    year = [r for r in (history or [])[-YEAR_SESSIONS:] if r[1] and r[2]]
    if len(year) < 20:
        return None
    hi = max(year, key=lambda r: (r[1], r[0]))
    lo = min(year, key=lambda r: (r[2], -r[0].toordinal()))
    return (hi[1], hi[0]), (lo[2], lo[0])


def refresh_year_ranges(watched):
    """
    Fill the day cache with each ticker's 52-week high/low and 20-day average
    volume, all from COMPLETED sessions (Massive daily bars). Returns
    (refreshed, still_missing).

    2026-10-23: the levels used to come from FMP's quote, whose yearHigh already
    contains today's prints -- including pre-market ones the regular-session
    day high does not. USO's pre-market $167.90 became its "52-week high"
    before the open, so the real breakout above Thursday's $163.66 was never
    reported at 10:07, and the 12:21 alert called today's own $167.90 the
    "previous high". Prior sessions only, so "previous" means previous.
    """
    today = datetime.now(ET).date()
    if _year_range_cache["date"] != today:
        _year_range_cache.update(date=today, levels={}, avg_volume={}, level_dates={})

    levels = _year_range_cache["levels"]
    missing = sorted(t for t in watched if t not in levels)
    if not missing:
        return 0, 0

    batch = missing[:MAX_YEAR_RANGE_REFRESH_PER_RUN]
    done = 0
    for ticker in batch:
        history = _daily_history(ticker, today)
        if not history:
            continue          # retried next run
        lv = _year_levels(history)
        if lv:
            (high, hd), (low, ld) = lv
            levels[ticker] = (high, low)
            _year_range_cache.setdefault("level_dates", {})[ticker] = (hd, ld)
        avg = _avg_daily_volume(ticker, today, history)
        if avg:
            _year_range_cache.setdefault("avg_volume", {})[ticker] = avg
        done += 1

    return done, max(0, len(missing) - done)


def _reversal(price, level, high_side):
    """(headline, sentence) when price has moved REVERSAL_NOTE_PCT or more back
    from the level it just set; (None, "") otherwise."""
    if not price or not level:
        return None, ""
    gap = (level - price) / level * 100 if high_side else (price - level) / level * 100
    if gap < REVERSAL_NOTE_PCT:
        return None, ""
    if high_side:
        return ("Hits New 52-Week High, Then Pulls Back",
                f" It has since pulled back {gap:.1f}% from that high.")
    return ("Hits New 52-Week Low, Then Rebounds",
            f" It has since rebounded {gap:.1f}% from that low.")


def _on(d):
    return f" on {d.strftime('%b')} {d.day}, {d.year}" if d else ""


def check_52_week(index, sent):
    levels = _year_range_cache["levels"]
    dates = _year_range_cache.get("level_dates") or {}
    alerts = []
    for ticker, d in index.items():
        price = d["price"]
        if not price or price < MIN_PRICE:
            continue
        year_high, year_low = levels.get(ticker, (None, None))
        high_date, low_date = dates.get(ticker, (None, None))

        # day_high/day_low catch a breakout that has already faded by the time
        # this run reads the snapshot; price alone would miss an intraday poke
        # through the level.
        session_high = d["day_high"] or price
        session_low = d["day_low"] or price

        # The levels are from completed sessions only, so a breakout must clear
        # the old high by a real margin (YEAR_BREAKOUT_MIN_PCT) -- a session
        # that merely touches it is not a new high.
        above = _pct(session_high, year_high) if year_high else None
        if (above is not None and above >= YEAR_BREAKOUT_MIN_PCT
                and (ticker, "52W_HIGH") not in sent):
            rev_head, rev_text = _reversal(price, session_high, True)
            summary = (
                f"{ticker} hit a new 52-week high: ${session_high:,.2f} (up {above:.1f}% from the "
                f"previous high of ${year_high:,.2f}{_on(high_date)}). Current price: ${price:,.2f}, "
                f"{_move_phrase(d['change_pct'])}.{rev_text}"
            )
            alerts.append(_build_alert(
                ticker, "52W_HIGH", summary, rev_head or "Breaks Out To New 52-Week High",
                {"price": price, "session_high": session_high, "year_high": year_high,
                 "year_high_date": high_date.isoformat() if high_date else None,
                 "pct_above_prior_high": round(above, 2),
                 "change_pct": d["change_pct"]},
            ))
            sent.add((ticker, "52W_HIGH"))

        # Independent `if`, not `elif`: a session that gaps down and then
        # reverses can print both a new 52-week low and a new 52-week high.
        below = _pct(year_low, session_low) if year_low else None
        if (below is not None and below >= YEAR_BREAKOUT_MIN_PCT
                and (ticker, "52W_LOW") not in sent):
            rev_head, rev_text = _reversal(price, session_low, False)
            summary = (
                f"{ticker} dropped to a new 52-week low: ${session_low:,.2f} (down {below:.1f}% from the "
                f"previous low of ${year_low:,.2f}{_on(low_date)}). Current price: ${price:,.2f}, "
                f"{_move_phrase(d['change_pct'])}.{rev_text}"
            )
            alerts.append(_build_alert(
                ticker, "52W_LOW", summary, rev_head or "Breaks Down To New 52-Week Low",
                {"price": price, "session_low": session_low, "year_low": year_low,
                 "year_low_date": low_date.isoformat() if low_date else None,
                 "pct_below_prior_low": round(below, 2),
                 "change_pct": d["change_pct"]},
            ))
            sent.add((ticker, "52W_LOW"))
    return alerts


# ── Check 3 & 4: RSI and SMA200 (the only per-ticker API calls) ───────────────
def _rnd(x, n=2):
    return None if x is None else round(x, n)


def _rsi_and_prior(values):
    """(latest RSI, RSI at the session before it) from Massive's newest-first
    values. The prior is the first value dated before the latest one, so this
    works whether or not Massive includes today's partial session."""
    pts = []
    for v in values or []:
        val = _num((v or {}).get("value"))
        if val is None:
            continue
        ts = _num(v.get("timestamp"))
        d = datetime.fromtimestamp(ts / 1000.0, tz=ET).date() if ts else None
        pts.append((d, val))
    if not pts:
        return None, None
    d0, rsi = pts[0]
    for d, val in pts[1:]:
        if d0 is None or d is None or d < d0:
            return rsi, val
    return rsi, None
def check_indicators(tickers, index, sent, recent_crossovers=None):
    """
    RSI overbought/oversold and 200-SMA crossovers for one rotation window.

    `recent_crossovers` is the multi-day crossover ledger. None means it could
    not be read, in which case crossovers are suppressed for this run — the RSI
    half still works, and a missed crossover is recoverable while a duplicate
    one is not.
    """
    alerts = []
    crossovers_ok = recent_crossovers is not None
    spent = set(recent_crossovers or ())
    for ticker in tickers:
        d = index.get(ticker)
        if not d or not d["price"]:
            continue
        price = d["price"]
        prev_close = d["prev_close"]

        # ── RSI ──
        try:
            values = massive_client.get_rsi(ticker, window=14, limit=5)
        except Exception as e:
            log_poller_error(POLLER, "get_rsi", e, {"ticker": ticker})
            values = None
        rsi, prev_rsi = _rsi_and_prior(values)

        # "Moves Into" overbought/oversold means a crossing. The dedup is per
        # day, so a fund that STAYED above 70 re-alerted every session it was
        # in the window (XLP: Oct 20 at 70.3, Oct 22 at 70.8). Only alert when
        # the previous session's RSI was on the other side of the line.
        crossed_up = prev_rsi is None or prev_rsi < RSI_OVERBOUGHT
        crossed_dn = prev_rsi is None or prev_rsi > RSI_OVERSOLD
        prior_txt = (f", from {prev_rsi:.1f} at the previous close" if prev_rsi is not None else "")

        if rsi is not None:
            if rsi >= RSI_OVERBOUGHT and crossed_up and (ticker, "RSI_OVERBOUGHT") not in sent:
                summary = (
                    f"{ticker} overbought alert: RSI(14) = {rsi:.1f}{prior_txt} (threshold: {RSI_OVERBOUGHT:.0f}). "
                    f"Price: ${price:,.2f}, {_move_phrase(d['change_pct'])}. "
                    f"An RSI above 70 means gains have outweighed losses over the last 14 sessions; it "
                    f"describes recent momentum and does not on its own predict a reversal."
                )
                alerts.append(_build_alert(
                    ticker, "RSI_OVERBOUGHT", summary, "RSI Moves Into Overbought Territory",
                    {"rsi": round(rsi, 2), "prev_rsi": _rnd(prev_rsi), "threshold": RSI_OVERBOUGHT, "price": price,
                     "change_pct": round(d["change_pct"], 2) if d["change_pct"] is not None else None},
                ))
                sent.add((ticker, "RSI_OVERBOUGHT"))
            elif rsi <= RSI_OVERSOLD and crossed_dn and (ticker, "RSI_OVERSOLD") not in sent:
                summary = (
                    f"{ticker} oversold alert: RSI(14) = {rsi:.1f}{prior_txt} (threshold: {RSI_OVERSOLD:.0f}). "
                    f"Price: ${price:,.2f}, {_move_phrase(d['change_pct'])}. "
                    f"An RSI below 30 means losses have outweighed gains over the last 14 sessions; it "
                    f"describes recent momentum and does not on its own predict a rebound."
                )
                alerts.append(_build_alert(
                    ticker, "RSI_OVERSOLD", summary, "RSI Moves Into Oversold Territory",
                    {"rsi": round(rsi, 2), "prev_rsi": _rnd(prev_rsi), "threshold": RSI_OVERSOLD, "price": price,
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

        # The price must CLEAR the average, not graze it. Both branches below
        # already require the previous close on the other side; the deadband is
        # what stops a price hovering on the line from printing a fresh "trend
        # reversal" every session.
        if distance is None or abs(distance) < SMA200_MIN_DISTANCE_PCT:
            continue
        if not crossovers_ok:
            continue

        if (prev_close < sma200 <= price
                and (ticker, "SMA200_CROSSOVER_UP") not in sent
                and (ticker, "SMA200_CROSSOVER_UP") not in spent):
            summary = (
                f"{ticker} bullish crossover: price crossed above 200-day MA (${sma200:,.2f}). "
                f"Current: ${price:,.2f}, +{abs(distance):.1f}% above MA. "
                f"The 200-day average is a widely watched long-term trend line; a single cross does not "
                f"confirm a change in trend."
            )
            alerts.append(_build_alert(
                ticker, "SMA200_CROSSOVER_UP", summary, "Price Crosses Above 200-Day Average",
                {"price": price, "prev_close": prev_close, "sma200": sma200,
                 "change_pct": d["change_pct"],
                 "pct_from_sma": round(distance, 2) if distance is not None else None},
            ))
            sent.add((ticker, "SMA200_CROSSOVER_UP"))
            spent.add((ticker, "SMA200_CROSSOVER_UP"))

        elif (prev_close > sma200 >= price
              and (ticker, "SMA200_CROSSOVER_DOWN") not in sent
              and (ticker, "SMA200_CROSSOVER_DOWN") not in spent):
            summary = (
                f"{ticker} bearish crossover: price crossed below 200-day MA (${sma200:,.2f}). "
                f"Current: ${price:,.2f}, -{abs(distance):.1f}% below MA. "
                f"The 200-day average is a widely watched long-term trend line; a single cross does not "
                f"confirm a change in trend."
            )
            alerts.append(_build_alert(
                ticker, "SMA200_CROSSOVER_DOWN", summary, "Price Crosses Below 200-Day Average",
                {"price": price, "prev_close": prev_close, "sma200": sma200,
                 "change_pct": d["change_pct"],
                 "pct_from_sma": round(distance, 2) if distance is not None else None},
            ))
            sent.add((ticker, "SMA200_CROSSOVER_DOWN"))
            spent.add((ticker, "SMA200_CROSSOVER_DOWN"))

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
        # Day cache first: it now also carries the average volume the spike
        # check is measured against.
        refreshed, still_missing = refresh_year_ranges(set(index.keys()))
        if refreshed:
            logger.info("[TECHNICAL] Refreshed 52-week levels for %d tickers (%d still pending, "
                        "will be picked up on the next run).", refreshed, still_missing)
        alerts += check_volume_spikes(index, sent, elapsed)
        alerts += check_52_week(index, sent)

        window, deferred = _rotating_slice(index.keys(), MAX_INDICATOR_TICKERS_PER_RUN)
        logger.info("[TECHNICAL] Indicator pass: %d tickers covered this run, %d deferred to "
                    "later runs (cap=%d).", len(window), deferred, MAX_INDICATOR_TICKERS_PER_RUN)
        alerts += check_indicators(window, index, sent,
                                   recent_crossovers=load_recent_crossovers())

        # Re-read the ledger right before writing. Feature 7's first pass of the
        # day fetches bars for every fund and writes ~a minute after it starts,
        # so on 2026-10-22 XLP "Heavy Buying" (9:59) and this run's XLP volume
        # spike (10:00) each missed the other in a ledger read at the start.
        if any(a["filing_type"] == "VOLUME_SPIKE" for a in alerts):
            fresh = load_sent_today()
            if fresh:
                alerts = [a for a in alerts if not (
                    a["filing_type"] == "VOLUME_SPIKE"
                    and (a["ticker"].upper(), "VOLUME_SPIKE") in fresh)]

        if alerts:
            names = load_names({a["ticker"] for a in alerts})
            for a in alerts:
                if names.get(a["ticker"]):
                    a["extra"]["company_name"] = names[a["ticker"]]
        alerts = merge_alerts_by_ticker(alerts)
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
