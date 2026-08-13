"""
etf_flow_poller.py
GQ FinXray US — Feature 7. Unusual volume + price movement across a fixed ETF set.

WHAT THIS FEATURE ACTUALLY OBSERVES — AND WHAT IT DOES NOT
----------------------------------------------------------
The only inputs here are price and exchange volume. That is NOT fund flow data.
Actual ETF creations and redemptions are reported by the issuer after the close;
secondary-market volume is a different thing entirely, and a heavily traded
session can be a wash between buyers and sellers with no net creation at all.
The previous version's copy asserted "institutional buying likely" off nothing
but a volume ratio, which is a claim the data cannot support. Every string in
this module is worded as what was measured — unusual volume alongside a price
move — and the summaries say outright that creation/redemption data is not
observed. Do not reintroduce flow language here.

FIXES OVER THE PREVIOUS VERSION
-------------------------------
* Added a US market-hours guard. It previously ran hourly around the clock, so
  overnight and weekend runs compared a frozen post-close snapshot against the
  prior session and re-derived the same "signal" from stale numbers.
* Volume ratio is now measured against the volume expected by this point in the
  session, not against the prior session's full-day total. The old maths made an
  intraday 1.5x threshold unreachable before roughly 14:00.
* Failures write to `poller_error_log`; previously they went to the logger only
  and disappeared with the container.
* Dedup is one batched query and fails closed.

ETF flow alerts (feature 7) are now watchlist-scoped. Only users whose watchlist
contains an ETF will receive alerts about that ETF's flows. If the ETF ticker is
not on any user's watchlist, the alert will be skipped by the delivery layer.
"""

import os
import logging
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

import massive_client
from feature_map import tag_extra
from watchlist_util import log_poller_error

# The session clock lives in technical_poller as the single canonical
# implementation. Two copies of a market-hours window is exactly how this
# codebase ended up running the US market on India's 09:00-17:30 schedule.
from technical_poller import ET, is_market_open, session_volume_fraction

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

SOURCE = "ETF_FLOW"
POLLER = "etf_flow_poller"

# Both conditions must hold. Volume alone is often an index rebalance or an
# options expiry; a price move alone is just a normal directional day.
VOLUME_SPIKE_THRESHOLD = 1.5
PRICE_MOVE_THRESHOLD = 1.0

# Same rationale as technical_poller: the opening auction distorts any
# volume-pace comparison until roughly half an hour into the session.
MIN_SESSION_ELAPSED = 0.08

ETF_UNIVERSE = [
    {"ticker": "SPY", "name": "S&P 500 ETF",               "category": "Broad Market"},
    {"ticker": "QQQ", "name": "NASDAQ 100 ETF",            "category": "Technology"},
    {"ticker": "IWM", "name": "Russell 2000 ETF",          "category": "Small Cap"},
    {"ticker": "XLK", "name": "Technology Select ETF",     "category": "Technology"},
    {"ticker": "XLF", "name": "Financial Select ETF",      "category": "Finance"},
    {"ticker": "XLE", "name": "Energy Select ETF",         "category": "Energy"},
    {"ticker": "XLV", "name": "Health Care Select ETF",    "category": "Healthcare"},
    {"ticker": "XLI", "name": "Industrial Select ETF",     "category": "Industrials"},
    {"ticker": "XLY", "name": "Consumer Discretionary ETF", "category": "Consumer"},
    {"ticker": "GLD", "name": "Gold ETF",                  "category": "Commodities"},
    {"ticker": "TLT", "name": "20+ Year Treasury ETF",     "category": "Bonds"},
]

ETF_BY_TICKER = {e["ticker"]: e for e in ETF_UNIVERSE}


def _num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def load_sent_today():
    """
    {(ticker, filing_type)} already alerted today from ET midnight, or None if
    the ledger could not be read — in which case the run must emit nothing
    rather than risk duplicating alerts users already received.
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
        logger.error("[ETF FLOW] Dedup ledger unreadable (%s) — emitting nothing this run", e)
        return None
    return {((r.get("ticker") or "").upper(), r.get("filing_type")) for r in rows}


def fetch_etf_snapshots():
    """
    day + prevDay OHLCV for the whole ETF universe in ONE call. Falls back to
    per-ticker snapshots only if the filtered market snapshot comes back empty.
    """
    tickers = [e["ticker"] for e in ETF_UNIVERSE]
    try:
        rows = massive_client.get_full_market_snapshot(tickers=tickers)
    except Exception as e:
        log_poller_error(POLLER, "get_full_market_snapshot", e)
        rows = []

    if not rows:
        rows = []
        for t in tickers:
            try:
                snap = massive_client.get_snapshot(t)
            except Exception as e:
                log_poller_error(POLLER, "get_snapshot", e, {"ticker": t})
                continue
            if snap:
                rows.append(snap)

    return {(r.get("ticker") or "").upper(): r for r in rows}


def evaluate_etf(ticker, snap, elapsed_fraction):
    """
    Returns an `alerts` row dict when the volume-and-price conditions both hold,
    otherwise None. Pure — no I/O, so the thresholds are trivially testable.
    """
    info = ETF_BY_TICKER[ticker]
    day = snap.get("day") or {}
    prev = snap.get("prevDay") or {}

    price = _num(day.get("c")) or _num(prev.get("c"))
    prev_close = _num(prev.get("c"))
    volume = _num(day.get("v"))
    prev_volume = _num(prev.get("v"))

    if not price or not prev_close or not volume or not prev_volume:
        return None

    change_pct = (price - prev_close) / prev_close * 100.0
    expected = prev_volume * elapsed_fraction
    if expected <= 0:
        return None
    volume_ratio = volume / expected

    if volume_ratio < VOLUME_SPIKE_THRESHOLD or abs(change_pct) < PRICE_MOVE_THRESHOLD:
        return None

    if change_pct > 0:
        filing_type = "INFLOW"
        headline = "Heavy Volume Alongside A Price Gain"
        reading = "consistent with net buying interest"
    else:
        filing_type = "OUTFLOW"
        headline = "Heavy Volume Alongside A Price Decline"
        reading = "consistent with net selling pressure"

    impact = "HIGH" if volume_ratio >= 2.0 else "MEDIUM"
    sign = "+" if change_pct > 0 else ""

    summary = (
        f"{info['name']} ({ticker}, {info['category']}) has traded {int(volume):,} shares "
        f"so far today, roughly {volume_ratio:.1f}x the {int(expected):,} shares a normal "
        f"session would have on the tape by this point. It is at ${price:,.2f}, "
        f"{sign}{change_pct:.2f}% against the prior close of ${prev_close:,.2f}. "
        f"Unusual volume with a move of this size is {reading}, though this is "
        f"secondary-market trading activity — actual creation and redemption flows are "
        f"reported by the issuer after the close and are not observed here."
    )

    extra = tag_extra({
        "headline": headline,
        "name": info["name"],
        "category": info["category"],
        "price": price,
        "prev_close": prev_close,
        "change_pct": round(change_pct, 2),
        "volume": int(volume),
        "prev_volume": int(prev_volume),
        "expected_volume_so_far": int(expected),
        "volume_ratio": round(volume_ratio, 2),
        "session_elapsed_fraction": round(elapsed_fraction, 3),
        "signal_basis": "price_and_volume_only",
    }, SOURCE, filing_type)

    # Build FMP link for ETF flow alerts (clickable source)
    filing_url = f"https://site.financialmodelingprep.com/etf/{ticker.upper()}"

    return {
        "ticker": ticker,
        "summary": summary,
        "impact": impact,
        "source": SOURCE,
        "filing_type": filing_type,
        "delivered": False,
        "extra": extra,
        "filing_url": filing_url,
    }


def _write_alerts(rows):
    if not rows:
        return 0
    try:
        supabase.table("alerts").insert(rows).execute()
        return len(rows)
    except Exception as e:
        log_poller_error(POLLER, "write_alerts", e, {"tickers": [r["ticker"] for r in rows]})
        return 0


def run_etf_flow_poller():
    """One full pass over the ETF universe. Never raises."""
    try:
        now_et = datetime.now(ET)
        if not is_market_open(now_et):
            logger.info("[ETF FLOW] Outside US regular hours (09:30-16:00 ET, Mon-Fri) — skipping.")
            return

        elapsed = session_volume_fraction(now_et)
        if elapsed < MIN_SESSION_ELAPSED:
            logger.info("[ETF FLOW] Only %.0f%% of the session's expected volume has elapsed — "
                        "too early for a reliable volume comparison. Skipping.", elapsed * 100)
            return

        # Delivery is watchlist-only, so an alert for an ETF nobody watches is
        # written and then dropped with no audience. This poller used to report
        # "N alerts written" either way, making total non-delivery invisible.
        try:
            from watchlist_util import get_watched_tickers
            watched = get_watched_tickers() or set()
            covered = sorted({e.upper() for e in ETF_UNIVERSE} & {w.upper() for w in watched})
            if not covered:
                logger.info("[ETF FLOW] None of the %d tracked ETFs (%s) is on any "
                            "watchlist — nothing written would reach a user. Skipping.",
                            len(ETF_UNIVERSE), ", ".join(sorted(ETF_UNIVERSE)))
                return
            logger.info("[ETF FLOW] %d/%d tracked ETFs are watched: %s",
                        len(covered), len(ETF_UNIVERSE), ", ".join(covered))
        except Exception as e:
            # Visibility only — never let this check block the run.
            logger.warning("[ETF FLOW] Watchlist coverage check failed (%s); continuing.", e)

        sent = load_sent_today()
        if sent is None:
            return

        snapshots = fetch_etf_snapshots()
        if not snapshots:
            log_poller_error(POLLER, "fetch_etf_snapshots",
                             "No snapshot data returned for any ETF in the universe")
            return

        rows = []
        for etf in ETF_UNIVERSE:
            ticker = etf["ticker"]
            snap = snapshots.get(ticker)
            if not snap:
                continue
            try:
                row = evaluate_etf(ticker, snap, elapsed)
            except Exception as e:
                log_poller_error(POLLER, "evaluate_etf", e, {"ticker": ticker})
                continue
            if not row:
                continue
            if (ticker, row["filing_type"]) in sent:
                continue
            sent.add((ticker, row["filing_type"]))
            rows.append(row)

        written = _write_alerts(rows)
        logger.info("[ETF FLOW] Done. %d/%d ETFs priced, %d alerts written.",
                    len(snapshots), len(ETF_UNIVERSE), written)

    except Exception as e:
        log_poller_error(POLLER, "run_etf_flow_poller", e)
        logger.exception("[ETF FLOW] Run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_flow_poller()
