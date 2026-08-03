"""
market_data.py
GQ FinXray US — unified market data layer over Massive + FMP.

WHY THIS EXISTS
---------------
Before this, every poller called FMP or Massive directly and independently.
That produced three problems:

1. MASSIVE REDUNDANCY. technical_poller fetched the full-market snapshot
   (which contains day + prevDay OHLCV for every US ticker), then turned
   around and called get_snapshot(ticker) twice more per watchlist ticker for
   data it already had in hand. For N tickers that's 1 + 5N calls where
   1 + 3N would do. heatmap_generator, news_roundup, main.py's market reports
   and fetch_top_movers each independently looped FMP quotes for tickers that
   were already sitting in that same snapshot.

2. NO FAILOVER. When FMP rate-limited or an endpoint 404'd, the feature that
   depended on it simply went dark. Massive was right there with the same
   price data and unlimited call quota, unused.

3. WRONG PROVIDER FOR THE JOB. FMP caps at 3,000 calls/min and has no
   streaming; Massive Advanced is unlimited calls with real-time data. Every
   high-frequency price lookup belongs on Massive. FMP's strengths are
   fundamentals, calendars, transcripts, 13F and news -- things Massive
   either lacks or does worse.

ROUTING POLICY
--------------
  price / volume / prev close / movers / market status  -> Massive first, FMP fallback
  52-week high & low                                    -> FMP (native field, cached daily)
  fundamentals / calendars / transcripts / 13F / news   -> FMP only
  tick tape / whole-market scans                        -> Massive only

Callers get a normalised quote dict and never need to know who answered.
"""

import threading
import time
from datetime import datetime, timezone, date

import fmp_client
import massive_client

# ── Shared full-market snapshot cache ────────────────────────────────────────
# One Massive call serves every consumer inside the freshness window. The
# scheduler thread (pollers, heatmap) and the asyncio delivery loop both read
# this, hence the lock.
_SNAPSHOT_TTL_SECONDS = 90
_snapshot_lock = threading.Lock()
_snapshot_cache = {"at": 0.0, "by_ticker": {}, "count": 0}

# 52-week levels change once a day at most; no reason to re-ask hourly.
_YEAR_LEVELS_TTL_SECONDS = 6 * 3600
_year_levels_lock = threading.Lock()
_year_levels_cache = {}  # ticker -> {"at": ts, "high": float, "low": float}

# Simple provider accounting so we can see who is actually serving traffic.
_stats_lock = threading.Lock()
_stats = {"massive_snapshot_fetches": 0, "massive_hits": 0, "fmp_fallbacks": 0, "misses": 0}


def _bump(key, n=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def get_stats():
    with _stats_lock:
        return dict(_stats)


def reset_stats():
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0


# ── Normalised quote shape ────────────────────────────────────────────────────
def _normalise_massive(row):
    """Massive snapshot row -> common shape."""
    if not row:
        return None
    day = row.get("day") or {}
    prev = row.get("prevDay") or {}
    last = row.get("lastTrade") or {}

    price = day.get("c") or last.get("p") or prev.get("c")
    prev_close = prev.get("c")
    if not price:
        return None

    change_pct = row.get("todaysChangePerc")
    if change_pct is None and prev_close:
        change_pct = ((price - prev_close) / prev_close) * 100

    return {
        "ticker": row.get("ticker"),
        "price": float(price),
        "change_pct": float(change_pct or 0.0),
        "volume": day.get("v") or 0,
        "prev_volume": prev.get("v") or 0,
        "prev_close": float(prev_close) if prev_close else None,
        "day_high": day.get("h"),
        "day_low": day.get("l"),
        "open": day.get("o"),
        "source": "MASSIVE",
    }


def _normalise_fmp(q):
    """FMP quote -> common shape."""
    if not q or q.get("price") is None:
        return None
    price = float(q.get("price") or 0)
    if not price:
        return None
    return {
        "ticker": q.get("symbol"),
        "price": price,
        "change_pct": float(q.get("changePercentage") or 0.0),
        "volume": q.get("volume") or 0,
        "prev_volume": q.get("avgVolume") or 0,
        "prev_close": float(q["previousClose"]) if q.get("previousClose") else None,
        "day_high": q.get("dayHigh"),
        "day_low": q.get("dayLow"),
        "open": q.get("open"),
        "year_high": q.get("yearHigh"),
        "year_low": q.get("yearLow"),
        "source": "FMP",
    }


# ── The shared snapshot ───────────────────────────────────────────────────────
def snapshot_all(max_age_seconds=_SNAPSHOT_TTL_SECONDS, force=False):
    """Full US market snapshot, indexed by ticker, cached across callers.

    Returns {ticker: raw_massive_row}. Empty dict if Massive is unreachable --
    callers should fall back to per-ticker lookups via quote().
    """
    now = time.monotonic()
    with _snapshot_lock:
        age = now - _snapshot_cache["at"]
        if not force and _snapshot_cache["by_ticker"] and age < max_age_seconds:
            return _snapshot_cache["by_ticker"]

    # Fetch outside the lock -- this call can take a couple of seconds and
    # there's no point blocking every other reader on it.
    try:
        rows = massive_client.get_full_market_snapshot()
    except Exception as e:
        print(f"[MARKET_DATA] Full snapshot fetch failed: {e}")
        rows = []

    if not rows:
        # Keep whatever we had rather than blanking the cache -- a stale
        # snapshot beats no snapshot for a fallback path.
        with _snapshot_lock:
            return _snapshot_cache["by_ticker"]

    by_ticker = {r.get("ticker"): r for r in rows if r.get("ticker")}
    with _snapshot_lock:
        _snapshot_cache["by_ticker"] = by_ticker
        _snapshot_cache["at"] = time.monotonic()
        _snapshot_cache["count"] = len(by_ticker)
    _bump("massive_snapshot_fetches")
    print(f"[MARKET_DATA] Snapshot refreshed: {len(by_ticker):,} tickers in one call")
    return by_ticker


def snapshot_count():
    with _snapshot_lock:
        return _snapshot_cache["count"]


# ── Quotes ────────────────────────────────────────────────────────────────────
def quote(ticker, allow_fmp_fallback=True):
    """Normalised quote for one ticker.

    Order of preference:
      1. shared Massive full-market snapshot (free -- already fetched)
      2. Massive single-ticker snapshot
      3. FMP quote
    """
    if not ticker:
        return None

    row = snapshot_all().get(ticker)
    q = _normalise_massive(row)
    if q:
        _bump("massive_hits")
        return q

    try:
        q = _normalise_massive(massive_client.get_snapshot(ticker))
        if q:
            _bump("massive_hits")
            return q
    except Exception:
        pass

    if allow_fmp_fallback:
        try:
            q = _normalise_fmp(fmp_client.get_quote(ticker))
            if q:
                _bump("fmp_fallbacks")
                return q
        except Exception:
            pass

    _bump("misses")
    return None


def quotes(tickers):
    """Batch quotes. Serves everything it can from the one shared snapshot,
    and only falls back per-ticker for whatever is genuinely missing."""
    snap = snapshot_all()
    out, missing = {}, []
    for t in tickers:
        q = _normalise_massive(snap.get(t))
        if q:
            out[t] = q
            _bump("massive_hits")
        else:
            missing.append(t)

    for t in missing:
        q = quote(t)
        if q:
            out[t] = q
    return out


def prev_close(ticker):
    q = quote(ticker)
    return q.get("prev_close") if q else None


# ── 52-week levels (FMP native, cached) ──────────────────────────────────────
def year_high_low(ticker):
    """(year_high, year_low). FMP's quote carries these natively; Massive's
    snapshot does not. Cached for hours since they move at most once a day."""
    now = time.monotonic()
    with _year_levels_lock:
        hit = _year_levels_cache.get(ticker)
        if hit and (now - hit["at"]) < _YEAR_LEVELS_TTL_SECONDS:
            return hit["high"], hit["low"]

    try:
        q = fmp_client.get_quote(ticker)
    except Exception:
        q = None
    if not q:
        return None, None

    high, low = q.get("yearHigh"), q.get("yearLow")
    with _year_levels_lock:
        _year_levels_cache[ticker] = {"at": time.monotonic(), "high": high, "low": low}
    return high, low


# ── Market status ─────────────────────────────────────────────────────────────
def market_open(allow_extended=False):
    """True/False from the exchange itself, via Massive.

    Falls back to a clock check only if Massive is unreachable. The clock
    fallback cannot know about market holidays, which is precisely why the
    exchange-reported status is preferred.
    """
    try:
        status = massive_client.is_market_open(allow_extended=allow_extended)
        if status is not None:
            return status
    except Exception:
        pass
    return _clock_fallback_open()


def _clock_fallback_open():
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True  # can't tell -- don't block the pipeline
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


# ── Movers ────────────────────────────────────────────────────────────────────
# Quality floor for "top movers". Massive's gainers/losers endpoint scans the
# ENTIRE tape including OTC and sub-dollar microcaps, so the raw top of that
# list is almost always pump-and-dump noise -- a live preflight returned
# UPC +204%, RITR +90.8%, FCUV +45.9%, none of which any subscriber wants in
# a market report. These floors keep the list to names with real liquidity.
MOVERS_MIN_PRICE = 5.0
MOVERS_MIN_VOLUME = 1_000_000
MOVERS_MAX_ABS_CHANGE = 50.0  # beyond this it's a halt, reverse split or data artefact

# Dollar volume is the filter that actually works. A share-count floor treats
# 1M shares of a $5 microcap (=$5M traded) as equivalent to 1M shares of a
# $400 megacap (=$400M traded). A live run with only the share floor still
# surfaced FCUV, FFAI, DFNS and INHD as "top movers" -- technically past the
# bar, but not names any subscriber wants in a market report. Real liquidity
# starts around $25M/day of turnover.
MOVERS_MIN_DOLLAR_VOLUME = 25_000_000


def _effective_volume(q):
    """Larger of today's and the prior session's volume.

    Outside regular hours Massive's `day` block can carry zero or near-zero
    volume (the session has reset), which made a plain day-volume floor
    reject the entire market -- a live preflight after the close filtered out
    every single gainer AND every fallback candidate, returning empty movers.
    Falling back to the prior session keeps the liquidity floor meaningful
    around the clock.
    """
    return max(q.get("volume") or 0, q.get("prev_volume") or 0)


def _dollar_volume(q):
    return q["price"] * _effective_volume(q)


def _passes_movers_filter(q):
    return (
        q
        and q["price"] >= MOVERS_MIN_PRICE
        and _effective_volume(q) >= MOVERS_MIN_VOLUME
        and _dollar_volume(q) >= MOVERS_MIN_DOLLAR_VOLUME
        and abs(q["change_pct"]) <= MOVERS_MAX_ABS_CHANGE
    )


def top_movers(n=3, universe=None, min_price=None, min_volume=None):
    """(gainers, losers) as lists of normalised quotes.

    With no universe given, uses Massive's server-side gainers/losers -- one
    call each, whole market -- then applies a liquidity filter. Previously
    main.py looped FMP quotes over ten hardcoded megacaps and called the best
    and worst of those "top movers," which they very often were not.
    """
    global MOVERS_MIN_PRICE, MOVERS_MIN_VOLUME
    price_floor = MOVERS_MIN_PRICE if min_price is None else min_price
    vol_floor = MOVERS_MIN_VOLUME if min_volume is None else min_volume

    def _ok(q):
        return (q and q["price"] >= price_floor
                and _effective_volume(q) >= vol_floor
                and _dollar_volume(q) >= MOVERS_MIN_DOLLAR_VOLUME
                and abs(q["change_pct"]) <= MOVERS_MAX_ABS_CHANGE)

    if universe:
        qs = [q for q in quotes(universe).values() if q]
        qs.sort(key=lambda x: x["change_pct"], reverse=True)
        return qs[:n], list(reversed(qs[-n:]))

    try:
        gainers = [q for q in (_normalise_massive(r) for r in massive_client.get_gainers()) if _ok(q)]
        losers = [q for q in (_normalise_massive(r) for r in massive_client.get_losers()) if _ok(q)]
        gainers.sort(key=lambda x: x["change_pct"], reverse=True)
        losers.sort(key=lambda x: x["change_pct"])
        if gainers or losers:
            _bump("massive_hits", len(gainers[:n]) + len(losers[:n]))
            return gainers[:n], losers[:n]
        print("[MARKET_DATA] Massive movers all filtered out as illiquid — "
              "falling back to full snapshot scan")
    except Exception as e:
        print(f"[MARKET_DATA] Massive movers failed, falling back: {e}")

    # Fallback: scan the shared snapshot ourselves with the same floors.
    snap = snapshot_all()
    qs = [q for q in (_normalise_massive(r) for r in snap.values()) if _ok(q)]
    qs.sort(key=lambda x: x["change_pct"], reverse=True)
    return qs[:n], list(reversed(qs[-n:]))


# ── Sector / index helpers ────────────────────────────────────────────────────
def sector_performance(etf_list):
    """[{ticker, name, change_p, close}] for a list of {ticker, name} dicts.

    Serves every ETF from the one shared snapshot. Rows that genuinely have no
    data are returned with change_p=None so the caller can DROP them, rather
    than the old behaviour of silently substituting 0.0 -- which rendered as a
    white '+0.00%' tile and dragged the heatmap's colour normalisation.
    """
    tickers = [e["ticker"] for e in etf_list]
    qs = quotes(tickers)
    out = []
    for e in etf_list:
        q = qs.get(e["ticker"])
        out.append({
            "ticker": e["ticker"],
            "name": e.get("name", e["ticker"]),
            "change_p": round(q["change_pct"], 2) if q else None,
            "close": q["price"] if q else None,
            "source": q["source"] if q else None,
        })
    return out


def sector_performance_period(etf_list, days_back):
    """Period returns (weekly/monthly) via Massive aggregates, FMP fallback.

    Compares against the last trading day on or before the target CALENDAR
    date -- not an array index, which drifts because the series only contains
    trading days.
    """
    from datetime import timedelta
    today = date.today()
    target = (today - timedelta(days=days_back)).isoformat()
    frm = (today - timedelta(days=days_back + 15)).isoformat()
    out = []

    for e in etf_list:
        ticker = e["ticker"]
        rows = []
        try:
            bars = massive_client.get_aggregates(ticker, 1, "day", frm, today.isoformat(), sort="desc")
            rows = [{"date": datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc)
                                       .strftime("%Y-%m-%d"), "close": b["c"]}
                    for b in bars if b.get("t") and b.get("c")]
        except Exception:
            rows = []

        if len(rows) < 2:
            try:
                fmp_rows = fmp_client.get_historical_prices(ticker, frm, today.isoformat())
                rows = [{"date": r["date"], "close": r["close"]}
                        for r in fmp_rows if r.get("date") and r.get("close")]
                rows.sort(key=lambda r: r["date"], reverse=True)
            except Exception:
                rows = []

        if len(rows) < 2:
            out.append({"ticker": ticker, "name": e.get("name", ticker),
                        "change_p": None, "close": None})
            continue

        current = float(rows[0]["close"])
        cmp_row = next((r for r in rows if r["date"] <= target), rows[-1])
        cmp_close = float(cmp_row["close"])
        change = ((current - cmp_close) / cmp_close) * 100 if cmp_close else None
        out.append({
            "ticker": ticker,
            "name": e.get("name", ticker),
            "change_p": round(change, 2) if change is not None else None,
            "close": current,
        })
    return out


if __name__ == "__main__":
    print("Market open:", market_open())
    snap = snapshot_all()
    print(f"Snapshot tickers: {len(snap):,}")
    print("AAPL:", quote("AAPL"))
    g, l = top_movers(3)
    print("Gainers:", [(x['ticker'], round(x['change_pct'], 2)) for x in g])
    print("Losers:", [(x['ticker'], round(x['change_pct'], 2)) for x in l])
    print("Stats:", get_stats())
