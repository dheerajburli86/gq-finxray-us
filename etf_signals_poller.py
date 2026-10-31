"""
etf_signals_poller.py
GQ FinXray US — ETF signal engine: Feature 7 momentum (v2) + Feature 17 price signals.

WHY THIS EXISTS
---------------
etf_flow_poller.py (Feature 7 v1) sent three alerts in three days across a
36-ETF watchlist, and two of those were the same Friday signal re-sent on
Saturday and Sunday. Four things in it suppress or corrupt signals:

  1. Partial-day volume was divided by the FULL prior session. At 10:15 ET an
     ETF has ~17% of a day's volume on the tape, so "1.5x" really demanded ~9x
     the normal pace; in practice it could only fire in the last hour or after
     the close. technical_poller fixed this exact bug in August -- the ETF
     poller never got the fix.
  2. One flat 1.0% move threshold for every fund. A bond fund's ordinary day
     is +/-0.3%, so it can never fire; a 3x leveraged fund passes 1% before
     lunch on a quiet day. One number cannot mean "unusual" for both, which is
     why a 36-fund watchlist behaved like a 5-fund one.
  3. The volume baseline was ONE prior session: a heavy Thursday hid a heavy
     Friday.
  4. No session gate. Dedup reset at midnight while the snapshot still held
     Friday's bar, so PSP re-fired Sat 00:15 and Sun 00:16 ET.

WHAT THIS DOES INSTEAD
----------------------
Every threshold is relative to the fund's OWN recent history (daily bars from
Massive, fetched once per session), so a 0.8% day can be news for AGG and a 6%
day noise for TQQQ -- every kind of ETF on a watchlist can produce signals.

  Intraday, every 15 min 09:30-16:05 ET, one snapshot call per run:
    ETF_MOMENTUM_UP / _DOWN      volume >= 1.5x its 20-day average PACE for
                                 this time of day AND a move >= 1.5x its
                                 typical daily move                   [Feature 7]
    ETF_BIG_MOVE_UP / _DOWN      a move >= 2.5x its typical daily move,
                                 volume or not                        [Feature 17]
    ETF_OUTPERFORM / _UNDERPERFORM
                                 beta-adjusted gap vs SPY >= 2x its usual
                                 gap (equity funds that are not S&P
                                 clones)                              [Feature 17]
  Once per session, the next morning from final daily bars:
    ETF_STREAK_UP / _DOWN        5 / 7 / 10 straight higher or lower closes
    ETF_DRAWDOWN_5 / _10 / _20   close crosses 5% / 10% / 20% below its 52-week
                                 closing high (14-day cooldown)       [Feature 17]

"Typical daily move" is the standard deviation of the last 60 daily returns.
Everything is measured on TOTAL return: each fund's own distribution history
(Massive /v3/reference/dividends, once per fund per session) is added back, so
a bond or covered-call fund going ex -- or drifting down by its payouts -- does
not read as a drop, a losing streak or a drawdown. Massive's adjusted=true bars
and the snapshot's prevDay adjust for splits only.

ROLLOUT -- per user, opt-in
---------------------------
Only users with user_preferences.etf_signals_beta = true receive these, and
only for ETFs on their own watchlist (delivery.py enforces both). Those users
stop receiving v1 ETF_FLOW rows, which this supersedes. Everyone else keeps v1
exactly as before. With nobody opted in this module makes no API calls at all.

CLI (dry runs -- nothing is written)
------------------------------------
  python etf_signals_poller.py backtest [SESSIONS] [TICKER,TICKER]
      Replay the last N complete sessions (default 20) from daily bars and
      print what would have fired. Universe: opted-in watchlists, else every
      watched ETF, else the tickers given.
  python etf_signals_poller.py now [TICKERS] [--force]
      One live intraday pass. --force evaluates the latest snapshot as of its
      own close, so it can be tried on a weekend.
  python etf_signals_poller.py review [TICKERS]
      One session review (streaks, drawdowns) for the last complete session.
"""

import logging
import os
import statistics
import sys
import threading
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import massive_client
from feature_map import tag_extra
from heatmap_generator import is_trading_day, previous_trading_day
from technical_poller import session_volume_fraction
from watchlist_util import log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SOURCE = "ETF_SIGNAL"
POLLER = "etf_signals_poller"
ENGINE = "etf_signals_v2"
BENCHMARK = "SPY"
BETA_PREF = "etf_signals_beta"


def _f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def _ints(name, default):
    raw = os.getenv(name, default)
    return sorted({int(x) for x in raw.split(",") if x.strip().isdigit()})


# ── Thresholds (all overridable from Railway variables) ───────────────────────
MOMO_VOL_RATIO = _f("ETF_SIG_MOMO_VOL_RATIO", 1.5)     # x 20-day average pace
MOMO_Z = _f("ETF_SIG_MOMO_Z", 1.5)                     # x typical daily move
MOMO_MIN_MOVE_PCT = _f("ETF_SIG_MOMO_MIN_MOVE_PCT", 0.4)
BIG_Z = _f("ETF_SIG_BIG_Z", 2.5)
BIG_MIN_MOVE_PCT = _f("ETF_SIG_BIG_MIN_MOVE_PCT", 0.75)
REL_Z = _f("ETF_SIG_REL_Z", 2.0)
REL_MIN_GAP_PCT = _f("ETF_SIG_REL_MIN_GAP_PCT", 1.0)
# Relative signals only make sense for funds that actually move with stocks
# (R^2 floor) and are not near-copies of the S&P 500 (R^2 ceiling), whose
# "gap" vs SPY is tracking noise.
REL_MIN_R2 = _f("ETF_SIG_REL_MIN_R2", 0.30)
REL_MAX_R2 = _f("ETF_SIG_REL_MAX_R2", 0.95)
# Leveraged and inverse funds are a multiple of another index, so their "gap"
# vs SPY is that index's gap times the leverage: on 2026-10-23 QQQ, TQQQ (beta
# 4.78) and SQQQ (beta -4.80) each sent an out/under-performance alert for the
# one fact "Nasdaq beat the S&P". Relative signals need a positive beta no
# larger than this; the leveraged fund's own big moves still alert.
REL_MAX_BETA = _f("ETF_SIG_REL_MAX_BETA", 2.5)
STREAKS = _ints("ETF_SIG_STREAKS", "5,7,10")
# Each depth is its own filing type, so only depths feature_map knows about are
# allowed -- anything else would land as an unmapped, unmutable feature 0.
SUPPORTED_DRAWDOWNS = (5, 10, 15, 20, 25, 30)
DRAWDOWNS = [t for t in _ints("ETF_SIG_DRAWDOWNS", "5,10,20") if t in SUPPORTED_DRAWDOWNS]
DRAWDOWN_COOLDOWN_DAYS = int(_f("ETF_SIG_DRAWDOWN_COOLDOWN_DAYS", 14))
MIN_DAY_VOLUME = int(_f("ETF_SIG_MIN_DAY_VOLUME", 10000))
# Price-only signals (big move, relative) still need a live tape: at least this
# share of the volume normally traded by now, so a thin fund's stale last print
# cannot read as a move.
MIN_TAPE_SHARE = _f("ETF_SIG_MIN_TAPE_SHARE", 0.20)
# Share of a normal session's volume that must be on the tape before volume is
# compared at all (~09:53 ET); the opening auction distorts anything earlier.
MIN_ELAPSED = _f("ETF_SIG_MIN_ELAPSED", 0.10)
REL_MIN_ELAPSED = _f("ETF_SIG_REL_MIN_ELAPSED", 0.15)
REVIEW_AFTER_ET = os.getenv("ETF_SIG_REVIEW_AFTER", "08:00")
CLOSE_GRACE_MIN = 5          # evaluate until 16:05 so the closing print is seen

SIGMA_WINDOW = 60            # sessions of daily returns behind "typical move"
ADV_WINDOW = 20              # sessions behind "average volume"
YEAR_SESSIONS = 252
MIN_HISTORY = 30             # sessions needed before any relative signal
SIGMA_FLOOR = 0.0010         # 0.10%: stops T-bill funds reading every tick as huge
SPLIT_GUARD = 0.02           # snapshot prevDay vs bar close mismatch -> skip
BARS_LOOKBACK_DAYS = 400


# ── Supabase (lazy, so the CLI and tests can run without a live client) ───────
_sb = None


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _shares(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{int(n):,}"


def _d(d):
    """'Fri Oct 9' -- built by hand because %-d does not exist on Windows."""
    return f"{d:%a %b} {d.day}"


def _dy(d):
    return f"{d:%b} {d.day}, {d.year}"


# ── Daily bars -> per-session history ─────────────────────────────────────────
def bars_to_sessions(bars, before):
    """
    Massive daily bars -> ascending [{date, o, h, l, c, v}] for COMPLETE
    sessions only, i.e. strictly before `before` (an ET date). Today's partial
    bar is dropped so it can never leak into its own baseline.
    """
    out = {}
    for b in bars or []:
        t = b.get("t")
        c = _num(b.get("c"))
        if t is None or not c or c <= 0:
            continue
        d = datetime.fromtimestamp(float(t) / 1000.0, tz=timezone.utc).astimezone(ET).date()
        if d >= before:
            continue
        out[d] = {"date": d, "o": _num(b.get("o")), "h": _num(b.get("h")),
                  "l": _num(b.get("l")), "c": c, "v": _num(b.get("v")) or 0.0}
    return [out[d] for d in sorted(out)]


def apply_distributions(sessions, dividends):
    """
    Give every session `ac` (total-return close) and `div` (cash going ex that
    session), in place. Standard back-adjustment: an ex-date multiplies every
    EARLIER close by (1 - cash / previous close), so the return across the
    ex-date includes the payout and every other return is untouched. The last
    session's `ac` always equals its real close, so it still lines up with the
    snapshot's prevDay. Ex-dates after the last session (today, or declared
    future ones) are ignored here -- today's is added back to today's price.
    `dividends` None means unknown: ac = c, i.e. the old, unadjusted view.
    """
    for s in sessions:
        s["ac"], s["div"] = s["c"], 0.0
    if not dividends or not sessions:
        return sessions
    dates = [s["date"] for s in sessions]
    factor = [1.0] * len(sessions)
    for d in dividends:
        try:
            ex = date.fromisoformat(d["ex_date"])
        except (KeyError, TypeError, ValueError):
            continue
        k = bisect_left(dates, ex)                 # first session on/after the ex-date
        if k <= 0 or k >= len(sessions):
            continue
        prev = sessions[k - 1]["c"]
        if not 0 < d["cash"] < prev:
            continue
        sessions[k]["div"] += d["cash"]
        factor[k] *= 1.0 - d["cash"] / prev
    mult = 1.0
    for i in range(len(sessions) - 1, -1, -1):
        sessions[i]["ac"] = sessions[i]["c"] * mult
        mult *= factor[i]
    return sessions


def _ac(s):
    return s.get("ac", s["c"])


def _returns(sessions):
    return [_ac(sessions[i]) / _ac(sessions[i - 1]) - 1.0 for i in range(1, len(sessions))]


def _beta_stats(sessions, bench_sessions):
    """(beta, residual sigma, r^2) of daily total returns vs the benchmark, or None."""
    bc = {s["date"]: _ac(s) for s in bench_sessions or []}
    pairs = []
    for i in range(1, len(sessions)):
        d0, d1 = sessions[i - 1]["date"], sessions[i]["date"]
        if d0 in bc and d1 in bc and bc[d0] > 0:
            pairs.append((_ac(sessions[i]) / _ac(sessions[i - 1]) - 1.0, bc[d1] / bc[d0] - 1.0))
    pairs = pairs[-SIGMA_WINDOW:]
    if len(pairs) < MIN_HISTORY:
        return None
    ys = [p[0] for p in pairs]
    xs = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    n = len(pairs)
    var_x = sum((x - mx) ** 2 for x in xs) / (n - 1)
    var_y = sum((y - my) ** 2 for y in ys) / (n - 1)
    if var_x <= 0 or var_y <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)
    beta = cov / var_x
    resid = [y - beta * x for x, y in zip(xs, ys)]
    return beta, max(statistics.stdev(resid), SIGMA_FLOOR), (cov * cov) / (var_x * var_y)


def compute_baseline(sessions, bench_sessions=None):
    """
    What "normal" looks like for one fund, from complete sessions only.
    None when there is not enough history to say.
    """
    if len(sessions) < 21:
        return None
    rets = _returns(sessions)
    window = rets[-SIGMA_WINDOW:]
    sigma = max(statistics.stdev(window), SIGMA_FLOOR)
    vols = [s["v"] for s in sessions[-ADV_WINDOW:] if s["v"]]
    base = {
        "sessions": sessions,
        "rets": rets,
        "last_date": sessions[-1]["date"],
        "last_close": sessions[-1]["c"],
        "sigma": sigma,
        "adv": (sum(vols) / len(vols)) if vols else 0.0,
        "beta": None, "resid_sigma": None, "r2": None,
    }
    stats = _beta_stats(sessions, bench_sessions) if bench_sessions else None
    if stats:
        base["beta"], base["resid_sigma"], base["r2"] = stats
    return base


def largest_since(base, r):
    """'its largest one-day gain since Mar 4, 2026' for today's return r."""
    word = "gain" if r > 0 else "drop"
    rets, sessions = base["rets"], base["sessions"]
    for i in range(len(rets) - 1, -1, -1):
        past = rets[i]
        if (past > 0) == (r > 0) and abs(past) >= abs(r):
            return f"its largest one-day {word} since {_dy(sessions[i + 1]['date'])}"
    if len(rets) >= YEAR_SESSIONS - 2:
        return f"its largest one-day {word} in over a year"
    return f"its largest one-day {word} in the {len(rets)} sessions on record"


def streak_at(closes, idx):
    """(direction +1/-1/0, length) of the run of same-direction closes ending at idx."""
    def sign(a, b):
        return 1 if b > a else (-1 if b < a else 0)
    if idx < 1:
        return 0, 0
    d = sign(closes[idx - 1], closes[idx])
    if d == 0:
        return 0, 0
    n, i = 0, idx
    while i >= 1 and sign(closes[i - 1], closes[i]) == d:
        n += 1
        i -= 1
    return d, n


def drawdown_at(sessions, idx, window=YEAR_SESSIONS):
    """(total-return drawdown <= 0, index of the peak, sessions in window) at idx."""
    lo = max(0, idx - window + 1)
    best = max(range(lo, idx + 1), key=lambda k: (_ac(sessions[k]), k))
    return _ac(sessions[idx]) / _ac(sessions[best]) - 1.0, best, idx - lo + 1


# ── Alert rows ────────────────────────────────────────────────────────────────
def dedup_key(ticker, ftype, session_iso=None):
    return f"{SOURCE}:{ticker}:{ftype}" + (f":{session_iso}" if session_iso else "")


def _alert(ticker, name, ftype, impact, headline, summary, key, stats):
    extra = dict(stats)
    extra.update({"dedup_key": key, "headline": headline, "company_name": name or ticker,
                  "etf_ticker": ticker, "engine": ENGINE, "filed_at": _now_iso()})
    return {
        "ticker": ticker, "summary": summary, "impact": impact, "source": SOURCE,
        "filing_type": ftype, "delivered": False,
        "extra": tag_extra(extra, SOURCE, ftype),
        "filing_url": f"https://www.tradingview.com/chart/?symbol={ticker}",
    }


def _r(x, n=4):
    return None if x is None else round(float(x), n)


# ── Intraday evaluation (pure: no I/O) ────────────────────────────────────────
def evaluate_intraday(ticker, name, base, m, elapsed, session_iso, sent,
                      bench=None, div=0.0):
    """
    m       {"price", "prev_close", "volume"} from the live snapshot
    elapsed share of a normal session's volume expected by now (1.0 at close)
    sent    set of dedup keys already written (updated in place)
    bench   {"r": SPY's return today} or None
    div     cash distribution going ex today, added back to the move
    -> list of alert rows
    """
    out = []
    price, prev_close, volume = m["price"], m["prev_close"], m["volume"]
    r = (price + (div or 0.0)) / prev_close - 1.0
    up = r > 0
    z = r / base["sigma"]
    typical = base["sigma"] * 100
    expected = base["adv"] * min(max(elapsed, 0.0), 1.0)
    ratio = (volume / expected) if expected > 0 else None
    pace = ("its 20-day average volume" if elapsed >= 1.0
            else "its normal volume for this time of day")
    live_tape = volume >= 1000 and (expected <= 0 or volume >= MIN_TAPE_SHARE * expected)
    stats = {
        "session_date": session_iso, "price": _r(price, 4), "prev_close": _r(prev_close, 4),
        "change_pct": _r(r * 100, 6), "typical_move_pct": _r(typical, 3), "move_multiple": _r(abs(z), 2),
        "day_volume": int(volume), "expected_volume_so_far": int(expected),
        "avg_volume_20d": int(base["adv"]), "volume_ratio": _r(ratio, 2),
        "session_elapsed_fraction": _r(elapsed, 3),
        "dividend_adjustment": _r(div, 4) if div else None,
    }
    bench_line = ""
    if bench is not None and ticker != BENCHMARK:
        bench_line = f" The S&P 500 (SPY) is {bench['r'] * 100:+.2f}%."
        stats["spy_change_pct"] = _r(bench["r"] * 100, 3)
    stats["live_tape"] = live_tape
    div_line = (f" (Adjusted for its ${div:,.4f} distribution going ex today.)" if div else "")

    big_ft = "ETF_BIG_MOVE_UP" if up else "ETF_BIG_MOVE_DOWN"
    big_key = dedup_key(ticker, big_ft, session_iso)
    big_now = live_tape and abs(z) >= BIG_Z and abs(r) * 100 >= BIG_MIN_MOVE_PCT
    if big_now and big_key not in sent:
        vol_line = (f" Volume is running {ratio:.1f}x {pace}." if ratio else "")
        summary = (f"{ticker} is {'up' if up else 'down'} {abs(r) * 100:.2f}% today at ${price:,.2f}, "
                   f"about {abs(z):.1f} times its typical daily move of +/-{typical:.2f}% and "
                   f"{largest_since(base, r)}.{vol_line}{bench_line}{div_line}")
        out.append(_alert(ticker, name, big_ft, "HIGH" if abs(z) >= 3.0 else "MEDIUM",
                          "Unusually Large Gain" if up else "Unusually Large Drop",
                          summary, big_key, stats))
        sent.add(big_key)

    # One move, one message: when a move is already big enough for the
    # big-move signal (sent now or earlier today), it is not ALSO reported as
    # momentum -- the big-move text carries the volume context. Momentum first
    # and big move later is allowed, since that is an escalation. Trade-off,
    # deliberate: a user who mutes Feature 17 but keeps 7 hears nothing on
    # those days.
    momo_ft = "ETF_MOMENTUM_UP" if up else "ETF_MOMENTUM_DOWN"
    momo_key = dedup_key(ticker, momo_ft, session_iso)
    if (not big_now and big_key not in sent and momo_key not in sent
            and ratio is not None and ratio >= MOMO_VOL_RATIO
            and abs(z) >= MOMO_Z and abs(r) * 100 >= MOMO_MIN_MOVE_PCT
            and volume >= MIN_DAY_VOLUME):
        if elapsed >= 1.0:
            vol_part = (f"{ratio:.1f}x its 20-day average volume ({_shares(volume)} shares vs "
                        f"{_shares(base['adv'])} a day)")
        else:
            vol_part = (f"{ratio:.1f}x its normal volume for this time of day ({_shares(volume)} shares "
                        f"vs {_shares(expected)} expected; 20-day average {_shares(base['adv'])} a day)")
        summary = (f"{ticker} is {'up' if up else 'down'} {abs(r) * 100:.2f}% at ${price:,.2f} on {vol_part}. "
                   f"That move is about {abs(z):.1f} times its typical daily move of +/-{typical:.2f}%."
                   f"{bench_line} Heavy volume behind a directional move often reflects institutional "
                   f"{'buying' if up else 'selling'}; this signal is built from price and volume, not "
                   f"reported fund flows.{div_line}")
        impact = "HIGH" if (ratio >= 2.5 and abs(z) >= 2.0) else "MEDIUM"
        out.append(_alert(ticker, name, momo_ft, impact,
                          "Heavy Buying: Volume Surge, Price Up" if up
                          else "Heavy Selling: Volume Surge, Price Down",
                          summary, momo_key, stats))
        sent.add(momo_key)

    any_big = (big_now or dedup_key(ticker, "ETF_BIG_MOVE_UP", session_iso) in sent
               or dedup_key(ticker, "ETF_BIG_MOVE_DOWN", session_iso) in sent)
    if (bench is not None and ticker != BENCHMARK and not any_big and live_tape
            and elapsed >= REL_MIN_ELAPSED and base.get("beta") is not None
            and 0 < base["beta"] <= REL_MAX_BETA
            and REL_MIN_R2 <= base["r2"] < REL_MAX_R2):
        excess = r - base["beta"] * bench["r"]
        z_e = excess / base["resid_sigma"]
        rel_ft = "ETF_OUTPERFORM" if excess > 0 else "ETF_UNDERPERFORM"
        rel_key = dedup_key(ticker, rel_ft, session_iso)
        # Heavy buying/selling already told the holder about this move today.
        # On 2026-10-19 UVXY got "Heavy Selling" at 10:07 and "Underperforming
        # the Market" at 10:22 -- one drop, two messages. Only a gap in the
        # SAME direction as the price move is a repeat; an ETF rising on heavy
        # buying yet lagging its beta is a different story and still goes out.
        same_dir_momo = (excess > 0) == up and dedup_key(
            ticker, "ETF_MOMENTUM_UP" if up else "ETF_MOMENTUM_DOWN", session_iso) in sent
        if (abs(z_e) >= REL_Z and abs(excess) * 100 >= REL_MIN_GAP_PCT and rel_key not in sent
                and not same_dir_momo):
            rstats = dict(stats, beta=_r(base["beta"], 3), r_squared=_r(base["r2"], 3),
                          excess_pct=_r(excess * 100, 3), typical_gap_pct=_r(base["resid_sigma"] * 100, 3),
                          gap_multiple=_r(abs(z_e), 2))
            summary = (f"{ticker} is {r * 100:+.2f}% today while the S&P 500 (SPY) is {bench['r'] * 100:+.2f}%. "
                       f"Allowing for how much {ticker} normally moves with the market (beta {base['beta']:.2f}), "
                       f"it is running {abs(excess) * 100:.2f} points {'ahead of' if excess > 0 else 'behind'} it -- "
                       f"about {abs(z_e):.1f} times its usual daily gap of {base['resid_sigma'] * 100:.2f} points. "
                       f"A gap like this usually comes from sector, region or asset-class news rather than "
                       f"the broad market.{div_line}")
            out.append(_alert(ticker, name, rel_ft, "HIGH" if abs(z_e) >= 3.0 else "MEDIUM",
                              "Outperforming the Market" if excess > 0 else "Underperforming the Market",
                              summary, rel_key, rstats))
            sent.add(rel_key)
    return out


# ── Session review: streaks and drawdowns (pure: no I/O) ──────────────────────
_DD_LABEL = {5: ("Pulls Back 5% From Its High", "pullback"),
             10: ("Enters Correction Territory", "correction"),
             20: ("Enters Bear Market Territory", "bear-market")}
# Other supported depths fall back to a generic "Falls N% From Its High".


def evaluate_session(ticker, name, sessions, sent, idx=None):
    """
    Signals completed by the close of sessions[idx] (default: the last one).
    Streak keys carry the session date; drawdown keys do not, so the caller's
    cooldown window decides when the same line may alert again.
    """
    out = []
    idx = len(sessions) - 1 if idx is None else idx
    if idx < 21:
        return out
    closes = [_ac(s) for s in sessions]           # total return drives the logic
    raw = [s["c"] for s in sessions]              # real prices go in the text
    day = sessions[idx]["date"]
    day_iso = day.isoformat()

    d, n = streak_at(closes, idx)
    if d and n in STREAKS:
        ft = "ETF_STREAK_UP" if d > 0 else "ETF_STREAK_DOWN"
        key = dedup_key(ticker, ft, day_iso)
        if key not in sent:
            start = raw[idx - n]
            cum = closes[idx] / closes[idx - n] - 1.0
            paid = sum(s["div"] for s in sessions[idx - n + 1: idx + 1] if s.get("div"))
            since = None
            for j in range(idx - n, 0, -1):
                dj, nj = streak_at(closes, j)
                if dj == d and nj >= n:
                    since = sessions[j]["date"]
                    break
            word = "winning" if d > 0 else "losing"
            if since:
                tail = f"Its longest {word} streak since {_dy(since)}."
            elif idx >= YEAR_SESSIONS:
                tail = f"Its longest {word} streak in over a year."
            else:
                tail = f"Its longest {word} streak in the {idx} sessions on record."
            incl = f", including ${paid:,.4f} of distributions" if paid else ""
            summary = (f"{ticker} closed {'higher' if d > 0 else 'lower'} for {n} straight sessions through "
                       f"{_d(day)}, {'gaining' if d > 0 else 'losing'} {abs(cum) * 100:.2f}% over the run "
                       f"(${start:,.2f} to ${raw[idx]:,.2f}{incl}). {tail}")
            out.append(_alert(ticker, name, ft, "HIGH" if n >= 10 else "MEDIUM",
                              f"{n}-Day {'Winning' if d > 0 else 'Losing'} Streak", summary, key,
                              {"session_date": day_iso, "streak_length": n, "direction": "up" if d > 0 else "down",
                               "start_close": _r(start), "end_close": _r(raw[idx]),
                               "distributions_in_run": _r(paid) if paid else None,
                               "streak_return_pct": _r(cum * 100, 3),
                               "previous_equal_streak_ended": since.isoformat() if since else None}))
            sent.add(key)

    if idx >= 60 and DRAWDOWNS:
        dd, best, span = drawdown_at(sessions, idx)
        dd_prev, _, _ = drawdown_at(sessions, idx - 1)
        peak, peak_date = raw[best], sessions[best]["date"]
        adjusted = abs(closes[best] / raw[best] - 1.0) > 0.0005
        for t in sorted(DRAWDOWNS, reverse=True):
            line = -t / 100.0
            if dd <= line < dd_prev:
                ft = f"ETF_DRAWDOWN_{t}"
                key = dedup_key(ticker, ft)
                if key not in sent:
                    head, label = _DD_LABEL.get(t, (f"Falls {t}% From Its High", f"{t}%"))
                    high_name = ("52-week closing high" if span >= YEAR_SESSIONS - 2
                                 else f"{span}-session closing high")
                    basis = (" counting the distributions paid since" if adjusted else "")
                    summary = (f"{ticker} closed at ${raw[idx]:,.2f} on {_d(day)}, {abs(dd) * 100:.1f}% below "
                               f"its {high_name} of ${peak:,.2f} ({_dy(peak_date)}){basis}. That crosses the "
                               f"{t}% {label} line; a session earlier it was {abs(dd_prev) * 100:.1f}% below "
                               f"the high.")
                    out.append(_alert(ticker, name, ft, "MEDIUM" if t < 10 else "HIGH", head, summary, key,
                                      {"session_date": day_iso, "close": _r(raw[idx]),
                                       "total_return_basis": adjusted,
                                       "peak_close": _r(peak), "peak_date": peak_date.isoformat(),
                                       "drawdown_pct": _r(dd * 100, 3), "prev_drawdown_pct": _r(dd_prev * 100, 3),
                                       "threshold_pct": t}))
                    sent.add(key)
                break
    return merge_session_alerts(out)


_IMPACT_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def merge_session_alerts(rows):
    """
    One message per ETF per session close. On 2026-10-19 TQQQ received "Enters
    Bear Market Territory" and "5-Day Losing Streak" as two separate messages
    in the same minute, about the same Friday close; QQQ did the same with a
    pullback and a streak. They describe one move, so they go out as one alert
    led by the most severe signal. Every merged dedup key is kept in
    extra.merged_dedup_keys, and load_sent() reads those back, so no merged
    signal can fire again on its own later.
    """
    if len(rows) < 2:
        return rows
    rows = sorted(rows, key=lambda r: -_IMPACT_RANK.get(r["impact"], 0))
    lead = dict(rows[0])
    extra = dict(lead["extra"])
    extra["headline"] = " · ".join(r["extra"]["headline"] for r in rows)
    extra["merged_dedup_keys"] = [r["extra"]["dedup_key"] for r in rows[1:]]
    extra["merged_filing_types"] = [r["filing_type"] for r in rows[1:]]
    extra["merged_signals"] = [{k: v for k, v in r["extra"].items()
                                if k not in ("headline", "company_name", "etf_ticker", "engine")}
                               for r in rows[1:]]
    lead["summary"] = " ".join(r["summary"] for r in rows)
    lead["extra"] = extra
    return [lead]


def merge_alerts_by_ticker(alerts):
    """
    One message per ETF per intraday run. On 2026-10-29 XLV got heavy buying,
    an RSI overbought and a 52-week high in the same 10:21 run, and SCHD the
    same three; each went out as its own message. Signals on the same ETF in
    one run are merged with merge_session_alerts (lead = most severe, every
    key kept so none re-fires alone). Later runs are not merged into earlier ones.
    """
    groups = {}
    for a in alerts:
        groups.setdefault(a["ticker"], []).append(a)
    return [m for t in sorted(groups) for m in merge_session_alerts(groups[t])]


# ── I/O: who is opted in, what they watch, what was already sent ──────────────
def load_beta_users():
    """User ids with user_preferences.etf_signals_beta = true. Empty when the
    column does not exist yet -- the engine then simply does nothing."""
    try:
        rows = sb().table("user_preferences").select("*").execute().data or []
    except Exception as e:
        log_poller_error(POLLER, "load_beta_users", e)
        logger.error("[ETF-SIG] Could not read user_preferences: %s", e)
        return set()
    return {r["user_id"] for r in rows if r.get(BETA_PREF) is True and r.get("user_id")}


def _paged(query_fn, size=1000):
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


def load_etf_names(tickers):
    """{TICKER: name} for the tickers that are ETFs (present in etf_master)."""
    names, ordered = {}, sorted(tickers)
    for i in range(0, len(ordered), 200):
        rows = (sb().table("etf_master").select("ticker, name")
                .in_("ticker", ordered[i:i + 200]).execute().data or [])
        for r in rows:
            t = (r.get("ticker") or "").upper()
            if t:
                names[t] = r.get("name") or t
    return names


def load_universe(user_ids=None):
    """
    (universe {TICKER: name}, skipped [tickers watched but not ETFs]).
    user_ids None -> every watchlist (CLI only); empty set -> nothing.
    """
    if user_ids is not None and not user_ids:
        return {}, []
    # Ordered, so range() pages are stable and no row is skipped or repeated.
    if user_ids is None:
        rows = _paged(lambda: sb().table("watchlists").select("ticker, user_id")
                      .order("ticker").order("user_id"))
    else:
        ids = sorted(user_ids)
        rows = _paged(lambda: sb().table("watchlists").select("ticker, user_id").in_("user_id", ids)
                      .order("ticker").order("user_id"))
    watched = {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
    names = load_etf_names(watched) if watched else {}
    return names, sorted(watched - set(names))


def load_sent(days=None):
    """Dedup keys written in the last `days` (default: the drawdown cooldown).
    None on failure -- callers must then emit nothing (fail closed)."""
    days = max(days or DRAWDOWN_COOLDOWN_DAYS, 2)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = _paged(lambda: sb().table("alerts").select("id, extra")
                      .eq("source", SOURCE).gte("created_at", since)
                      .order("created_at").order("id"))
    except Exception as e:
        log_poller_error(POLLER, "load_sent", e)
        logger.error("[ETF-SIG] Dedup ledger unreadable (%s) -- emitting nothing this run", e)
        return None
    keys = set()
    for r in rows:
        ex = r.get("extra") or {}
        keys.add(ex.get("dedup_key"))
        keys.update(ex.get("merged_dedup_keys") or ())
    return keys - {None}


_MOMO_TYPES = ("ETF_MOMENTUM_UP", "ETF_MOMENTUM_DOWN")


def technical_volume_tickers(session, tickers):
    """Tickers that already got a Feature 6 volume spike this session. Empty on
    any failure: the cost of a failed read is one possible repeat, not silence."""
    if not tickers:
        return set()
    midnight = datetime(session.year, session.month, session.day, tzinfo=ET)
    try:
        rows = (sb().table("alerts").select("ticker")
                .eq("source", "TECHNICAL").eq("filing_type", "VOLUME_SPIKE")
                .gte("created_at", midnight.isoformat())
                .in_("ticker", sorted(tickers)).execute()).data or []
        # A spike can also ride under another lead (technical_poller
        # merge_alerts_by_ticker), e.g. XLV's 52-week high on 2026-10-30.
        merged = (sb().table("alerts").select("ticker, extra")
                  .eq("source", "TECHNICAL")
                  .gte("created_at", midnight.isoformat())
                  .in_("ticker", sorted(tickers)).execute()).data or []
    except Exception as e:
        log_poller_error(POLLER, "technical_volume_tickers", e)
        return set()
    out = {(r.get("ticker") or "").upper() for r in rows}
    out |= {(r.get("ticker") or "").upper() for r in merged
            if "VOLUME_SPIKE" in ((r.get("extra") or {}).get("merged_filing_types") or [])}
    return out


def drop_momentum_covered_by_technical(alerts, session):
    """
    Heavy buying/selling is a volume surge with a direction. When Feature 6 has
    already told the holder about the surge today, the momentum alert repeats
    it: on 2026-10-22 GDX, TQQQ, SQQQ, XLI, XLE, ARKK and GLD each got a Feature
    6 volume spike and then a Feature 7 momentum alert 15 minutes to 2.5 hours
    later. Checked just before writing (not at the start of the pass) so a spike
    written while this pass was fetching bars is still seen. Big-move and
    relative signals are untouched -- they say something the spike did not.
    """
    momo = {a["ticker"] for a in alerts if a.get("filing_type") in _MOMO_TYPES}
    if not momo:
        return alerts
    covered = technical_volume_tickers(session, momo)
    if not covered:
        return alerts
    kept = [a for a in alerts
            if not (a.get("filing_type") in _MOMO_TYPES and a["ticker"] in covered)]
    logger.info("[ETF-SIG] %d momentum alert(s) skipped -- Feature 6 already reported the "
                "volume surge today: %s", len(alerts) - len(kept), ", ".join(sorted(covered & momo)))
    return kept


def write_alerts(rows):
    """-> (rows written, tickers whose rows failed). A failed row is simply
    re-detected next run, because dedup is read back from the table."""
    written, failed = 0, set()
    for i in range(0, len(rows), 50):
        chunk = rows[i:i + 50]
        try:
            sb().table("alerts").insert(chunk).execute()
            written += len(chunk)
        except Exception as e:
            failed |= {r["ticker"] for r in chunk}
            log_poller_error(POLLER, "write_alerts", e, {"tickers": [r["ticker"] for r in chunk]})
            logger.error("[ETF-SIG] alert insert failed for %d rows: %s", len(chunk), e)
    return written, failed


# ── Market data caches (per session) ──────────────────────────────────────────
# ticker -> {"sessions", "div_today", "div_ok", "div_tries"} for one session.
_bars = {"session": None, "data": {}}
_cache_lock = threading.Lock()
DIV_MAX_TRIES = 3


def _fetch_dividends(ticker, frm):
    try:
        return massive_client.get_dividend_history(ticker, frm)
    except Exception as e:
        log_poller_error(POLLER, "get_dividend_history", e, {"ticker": ticker})
        return None


def _entry(ticker, before):
    """Cached bars + distributions for one fund, fetched at most once per
    session. A failed bars fetch is never cached; a failed dividend fetch keeps
    the bars and retries the dividends (up to DIV_MAX_TRIES) on later runs."""
    with _cache_lock:
        if _bars["session"] != before:
            _bars["session"], _bars["data"] = before, {}
        hit = _bars["data"].get(ticker)
    frm = (before - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat()
    if hit is not None:
        if hit["div_ok"] or hit["div_tries"] >= DIV_MAX_TRIES:
            return hit
        divs = _fetch_dividends(ticker, frm)
        with _cache_lock:
            hit["div_tries"] += 1
            if divs is not None:
                apply_distributions(hit["sessions"], divs)
                hit["div_ok"] = True
                hit["div_today"] = sum(d["cash"] for d in divs if d["ex_date"] == before.isoformat())
        return hit
    try:
        bars = massive_client.get_aggregates(ticker, 1, "day", frm, before.isoformat())
    except Exception as e:
        log_poller_error(POLLER, "get_aggregates", e, {"ticker": ticker})
        bars = []
    sessions = bars_to_sessions(bars, before)
    if not sessions:
        return {"sessions": [], "div_today": 0.0, "div_ok": False, "div_tries": 0}
    divs = _fetch_dividends(ticker, frm)
    if divs is None:
        logger.warning("[ETF-SIG] %s: distribution history unavailable -- prices not "
                       "total-return adjusted this run", ticker)
    apply_distributions(sessions, divs)
    entry = {"sessions": sessions, "div_ok": divs is not None, "div_tries": 1,
             "div_today": sum(d["cash"] for d in (divs or []) if d["ex_date"] == before.isoformat())}
    with _cache_lock:
        if _bars["session"] == before:
            _bars["data"][ticker] = entry
    return entry


def get_sessions(ticker, before):
    """Complete daily sessions strictly before `before`, total-return adjusted."""
    return _entry(ticker, before)["sessions"]


def get_div_today(ticker, before):
    """Cash distribution going ex on `before` itself (0.0 when none or unknown)."""
    return _entry(ticker, before)["div_today"]


def _evict(ticker, before):
    with _cache_lock:
        if _bars["session"] == before:
            _bars["data"].pop(ticker, None)


def snapshot_metrics(row, session_date, force=False):
    """{price, prev_close, volume} from one snapshot row, or None if the row is
    not today's session (stale, empty, or pre-open)."""
    day = row.get("day") or {}
    prev = row.get("prevDay") or {}
    last = row.get("lastTrade") or {}
    price = _num(day.get("c")) or _num(last.get("p"))
    prev_close = _num(prev.get("c"))
    volume = _num(day.get("v")) or 0.0
    if not price or not prev_close or volume <= 0:
        return None
    if not force:
        stamp = _num(row.get("updated")) or _num(last.get("t"))
        if stamp:
            seconds = stamp / 1e9 if stamp > 1e14 else (stamp / 1e3 if stamp > 1e11 else stamp)
            if datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(ET).date() != session_date:
                return None
    return {"price": price, "prev_close": prev_close, "volume": volume}


def _snapshot_session_date(rows):
    """ET date of the freshest row -- what --force evaluates a closed market as."""
    stamps = []
    for r in rows:
        s = _num(r.get("updated")) or _num((r.get("lastTrade") or {}).get("t"))
        if s:
            stamps.append(s / 1e9 if s > 1e14 else (s / 1e3 if s > 1e11 else s))
    if not stamps:
        return None
    return datetime.fromtimestamp(max(stamps), tz=timezone.utc).astimezone(ET).date()


# ── Runs ──────────────────────────────────────────────────────────────────────
def _hm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def in_intraday_window(now):
    if not is_trading_day(now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60 + CLOSE_GRACE_MIN


def intraday_pass(universe, now, sent, force=False, dry_run=False):
    """Evaluate one snapshot. Returns the alert rows (written unless dry_run)."""
    tickers = sorted(set(universe) | {BENCHMARK})
    try:
        rows = massive_client.get_full_market_snapshot(tickers)
    except Exception as e:
        log_poller_error(POLLER, "snapshot", e)
        rows = []
    if not rows:
        logger.warning("[ETF-SIG] Empty snapshot -- skipping run")
        return []

    session = now.date()
    elapsed = session_volume_fraction(now)
    if force:
        session = _snapshot_session_date(rows) or session
        elapsed = 1.0
    session_iso = session.isoformat()
    by_ticker = {(r.get("ticker") or "").upper(): r for r in rows}

    bench_sessions = get_sessions(BENCHMARK, session)
    bench = None
    bm = snapshot_metrics(by_ticker.get(BENCHMARK) or {}, session, force)
    if bm:
        bench = {"r": (bm["price"] + get_div_today(BENCHMARK, session)) / bm["prev_close"] - 1.0}

    alerts, priced, skipped = [], 0, []
    for t in sorted(universe):
        m = snapshot_metrics(by_ticker.get(t) or {}, session, force)
        if not m:
            skipped.append(t)
            continue
        base = compute_baseline(get_sessions(t, session), bench_sessions)
        if not base:
            skipped.append(t)
            continue
        # The snapshot's prevDay and the last daily bar are the same session.
        # When they disagree the fund split, or one side is stale -- either way
        # the move cannot be trusted this run.
        if abs(m["prev_close"] / base["last_close"] - 1.0) > SPLIT_GUARD:
            skipped.append(t)
            continue
        priced += 1
        alerts += evaluate_intraday(t, universe[t], base, m, elapsed, session_iso, sent,
                                    bench=bench, div=get_div_today(t, session))

    alerts = drop_momentum_covered_by_technical(alerts, session)
    alerts = merge_alerts_by_ticker(alerts)
    written = 0 if dry_run else (write_alerts(alerts)[0] if alerts else 0)
    logger.info("[ETF-SIG] Intraday %s (%.0f%% of session volume): %d ETFs priced, %d skipped, "
                "%d alert(s)%s%s", session_iso, elapsed * 100, priced, len(skipped), len(alerts),
                "" if dry_run else f", {written} written",
                (" -- " + ", ".join(f"{a['ticker']} {a['filing_type']}" for a in alerts)) if alerts else "")
    return alerts


def review_pass(universe, today, sent, dry_run=False):
    """
    Streaks and drawdowns completed by the last session before `today`.
    Returns (alerts, reviewed tickers). A fund whose bars do not yet include
    that session is left out of `reviewed` and evicted from the cache, so the
    next run fetches it again instead of skipping it for the whole day.
    """
    last = previous_trading_day(today)
    alerts, reviewed = [], set()
    for t in sorted(universe):
        sessions = get_sessions(t, today)
        if not sessions or sessions[-1]["date"] != last:
            _evict(t, today)
            continue
        reviewed.add(t)
        alerts += evaluate_session(t, universe[t], sessions, sent)
    written, failed = (0, set()) if dry_run or not alerts else write_alerts(alerts)
    # A fund whose alert did not reach the table is NOT reviewed: a streak or a
    # line crossing is only true on the day it happens, so it must be retried
    # today rather than lost.
    reviewed -= failed
    logger.info("[ETF-SIG] Review of %s: %d/%d ETFs ready, %d alert(s)%s%s", last, len(reviewed),
                len(universe), len(alerts), "" if dry_run else f", {written} written",
                (" -- " + ", ".join(f"{a['ticker']} {a['filing_type']}" for a in alerts)) if alerts else "")
    return alerts, reviewed


# Funds already reviewed today. Per fund rather than one flag so a fund added
# to a watchlist mid-morning, or one whose bars were late, still gets reviewed.
_review = {"date": None, "done": set()}
_review_lock = threading.Lock()


def run_etf_signals():
    """
    Scheduled entry point (every 15 min + a 16:03 close run). Self-gating:
    nothing happens on weekends or holidays, the session review runs once per
    trading day from REVIEW_AFTER_ET, and intraday signals only inside
    09:30-16:05 ET once enough of the session has traded. Never raises.
    """
    try:
        now = datetime.now(ET)
        today = now.date()
        if not is_trading_day(today):
            return
        users = load_beta_users()
        if not users:
            return
        universe, not_etf = load_universe(users)
        if not universe:
            return

        minutes = now.hour * 60 + now.minute
        if minutes >= _hm(REVIEW_AFTER_ET):
            with _review_lock:
                if _review["date"] != today:
                    _review["date"], _review["done"] = today, set()
                todo = {t: n for t, n in universe.items() if t not in _review["done"]}
                if todo:
                    sent = load_sent()
                    if sent is not None:
                        _, reviewed = review_pass(todo, today, sent)
                        _review["done"] |= reviewed

        if not in_intraday_window(now) or session_volume_fraction(now) < MIN_ELAPSED:
            return
        sent = load_sent()
        if sent is None:
            return
        intraday_pass(universe, now, sent)
    except Exception as e:
        log_poller_error(POLLER, "run_etf_signals", e)
        logger.exception("[ETF-SIG] Run failed: %s", e)


# ── Backtest (CLI) ────────────────────────────────────────────────────────────
def backtest(n_sessions=20, universe=None):
    today = datetime.now(ET).date()
    bench_all = get_sessions(BENCHMARK, today)
    counts, by_ticker, lines = {}, {}, []
    dd_last = {}                                   # (ticker, ftype) -> session index fired
    data = {t: get_sessions(t, today) for t in sorted(universe)}
    print(f"\nTypical daily move (60-session sigma) and 20-day average volume, as of {today}:")
    for t, ses in data.items():
        base = compute_baseline(ses, bench_all)
        if base:
            r2 = f"{base['r2']:.2f}" if base["r2"] is not None else "  - "
            print(f"  {t:6} +/-{base['sigma'] * 100:5.2f}%   avg vol {_shares(base['adv']):>7}   R2 vs SPY {r2}")
        else:
            print(f"  {t:6} not enough history ({len(ses)} sessions)")

    days = sorted({s["date"] for ses in data.values() for s in ses})[-n_sessions:]
    for day in days:
        fired = []
        for t, ses in data.items():
            idx = next((i for i, s in enumerate(ses) if s["date"] == day), None)
            if idx is None or idx < 22:
                continue
            hist = ses[:idx]
            bidx = next((i for i, s in enumerate(bench_all) if s["date"] == day), None)
            bench = None
            if bidx:
                bench = {"r": _ac(bench_all[bidx]) / _ac(bench_all[bidx - 1]) - 1.0}
            base = compute_baseline(hist, [s for s in bench_all if s["date"] < day])
            if not base:
                continue
            sent = set()
            m = {"price": ses[idx]["c"], "prev_close": ses[idx - 1]["c"], "volume": ses[idx]["v"]}
            got = evaluate_intraday(t, universe[t], base, m, 1.0, day.isoformat(), sent, bench=bench,
                                    div=ses[idx].get("div", 0.0))
            for a in evaluate_session(t, universe[t], ses, sent, idx=idx):
                ft = a["filing_type"]
                if ft.startswith("ETF_DRAWDOWN"):
                    prev_i = dd_last.get((t, ft))
                    if prev_i is not None and idx - prev_i < DRAWDOWN_COOLDOWN_DAYS * 5 // 7:
                        continue
                    dd_last[(t, ft)] = idx
                got.append(a)
            fired += got
        for a in fired:
            counts[a["filing_type"]] = counts.get(a["filing_type"], 0) + 1
            by_ticker[a["ticker"]] = by_ticker.get(a["ticker"], 0) + 1
        lines.append((day, fired))

    print(f"\nLast {len(days)} sessions -- what would have fired (daily-bar replay; momentum judged on "
          f"full-day volume):")
    for day, fired in lines:
        tags = ", ".join(f"{a['ticker']} {a['filing_type'].replace('ETF_', '')}" for a in fired)
        print(f"  {_d(day):11} {len(fired):3}  {tags}")
    total = sum(counts.values())
    print(f"\nTotal {total} alerts over {len(days)} sessions "
          f"(~{total / max(len(days), 1):.1f}/session) from {len(by_ticker)} of {len(universe)} ETFs")
    for ft, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {ft:24} {c}")
    quiet = sorted(set(universe) - set(by_ticker))
    if quiet:
        print(f"Silent over the window: {', '.join(quiet)}")
    return counts


def _cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    cmd = args[0] if args else "backtest"
    explicit = next((a for a in args[1:] if not a.isdigit()), None)

    if explicit:
        wanted = {t.strip().upper() for t in explicit.split(",") if t.strip()}
        names = load_etf_names(wanted)
        universe = {t: names.get(t, t) for t in wanted}
        not_etf = sorted(wanted - set(names))
        label = "tickers given"
    else:
        users = load_beta_users()
        universe, not_etf = load_universe(users if users else None)
        label = ("opted-in watchlists" if users
                 else "every watched ETF (nobody has etf_signals_beta = true yet)")
    print(f"Universe: {len(universe)} ETFs from {label}")
    if not_etf:
        print(("Not in etf_master (evaluated anyway): " if explicit
               else "Watched but not in etf_master (skipped): ") + ", ".join(not_etf))
    if not universe:
        return

    if cmd == "backtest":
        n = next((int(a) for a in args[1:] if a.isdigit()), 20)
        backtest(n, universe)
    elif cmd == "now":
        now = datetime.now(ET)
        if not force and not in_intraday_window(now):
            print("Market is not in session -- rerun with --force to evaluate the latest snapshot "
                  "as of its close.")
            return
        if not force and session_volume_fraction(now) < MIN_ELAPSED:
            print("Too early in the session for a fair volume comparison -- try after ~09:55 ET.")
            return
        for a in intraday_pass(universe, now, set(), force=force, dry_run=True):
            print(f"\n[{a['impact']}] {a['ticker']} {a['filing_type']}\n  {a['extra']['headline']}\n  {a['summary']}")
    elif cmd == "review":
        today = datetime.now(ET).date()
        for a in review_pass(universe, today, set(), dry_run=True)[0]:
            print(f"\n[{a['impact']}] {a['ticker']} {a['filing_type']}\n  {a['extra']['headline']}\n  {a['summary']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
