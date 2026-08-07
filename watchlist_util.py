"""
watchlist_util.py
GQ FinXray US — the single source of truth for "which tickers matter".

WHY THIS EXISTS
---------------
Every ticker-scoped poller needs the same answer to the same question: which of
the 6,353 stocks should I spend an API call on? The answer is "the ones at least
one user is watching", and it must be the same answer everywhere, or the system
either wastes quota on companies nobody follows or misses ones people do.

This also enforces the product rule from the other direction. Content for an
unwatched ticker is never fetched, never summarised, and never becomes an alert,
so there is no path by which it could reach a user who did not ask for it.

Scale note: `get_watched_tickers()` typically returns tens of tickers, not
thousands. A poller that loops over this set is cheap. A poller that loops over
`get_all_stocks()` issues one API call per stock — at 6,353 tickers that is a
quota-ending mistake, which is why `get_all_stocks()` is deliberately NOT the
default and carries the warning it does.
"""

import os
import time
import logging

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)
_sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# Set true only for backfills or a smoke test on an empty watchlist.
PROCESS_ALL_TICKERS = os.getenv("PROCESS_ALL_TICKERS", "false").lower() == "true"

_CACHE_TTL = 120
_cache = {"watched": None, "watched_at": 0.0, "all": None, "all_at": 0.0}


def get_watched_tickers(force=False):
    """
    Set of uppercase tickers on at least one user's watchlist.

    On a database error this returns the last known good set (or an empty set on
    a cold start) rather than falling back to "everything" — failing open here
    would fan thousands of API calls at the whole market.
    """
    now = time.monotonic()
    if not force and _cache["watched"] is not None and (now - _cache["watched_at"]) < _CACHE_TTL:
        return _cache["watched"]

    try:
        rows = _sb.table("watchlists").select("ticker").execute().data or []
        watched = {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
        _cache.update({"watched": watched, "watched_at": now})
        return watched
    except Exception as e:
        logger.error("[WATCHLIST] Lookup failed (%s); reusing last known set", e)
        return _cache["watched"] or set()


def get_target_tickers():
    """
    What a ticker-scoped poller should iterate over.

    Normally the watchlist. With PROCESS_ALL_TICKERS=true it becomes the full
    stocks table — only do that knowingly.
    """
    if PROCESS_ALL_TICKERS:
        logger.warning("[WATCHLIST] PROCESS_ALL_TICKERS is on — iterating all %d stocks. "
                       "Expect very high API usage.", len(get_all_stocks()))
        return set(get_all_stocks())
    return get_watched_tickers()


def is_watched(ticker):
    return bool(ticker) and ticker.upper() in get_watched_tickers()


def get_all_stocks(force=False):
    """
    Every ticker in the `stocks` table (~6,353).

    USE FOR MEMBERSHIP TESTS AND BULK-SNAPSHOT FILTERING ONLY. Do not iterate
    this making one API call per ticker — that is ~6,300 calls per run and will
    exhaust an FMP or Massive plan within the hour. If you need per-ticker data
    for many symbols, use a bulk snapshot endpoint and filter locally.

    Paginated in 1,000-row pages because PostgREST caps an unbounded select at
    1,000 rows by default — a plain `.select()` here silently returns 1,000 of
    6,353 and everything downstream quietly covers a sixth of the market.
    """
    now = time.monotonic()
    if not force and _cache["all"] is not None and (now - _cache["all_at"]) < 3600:
        return _cache["all"]

    tickers, page, size = [], 0, 1000
    try:
        while True:
            rows = (_sb.table("stocks").select("ticker")
                    .range(page * size, page * size + size - 1).execute()).data or []
            tickers.extend((r.get("ticker") or "").upper() for r in rows if r.get("ticker"))
            if len(rows) < size:
                break
            page += 1
        _cache.update({"all": tickers, "all_at": now})
        return tickers
    except Exception as e:
        logger.error("[WATCHLIST] Failed to load stocks table: %s", e)
        return _cache["all"] or []


def stock_exists(ticker):
    try:
        rows = (_sb.table("stocks").select("ticker")
                .eq("ticker", (ticker or "").upper()).limit(1).execute()).data
        return bool(rows)
    except Exception:
        return False


def log_poller_error(poller_name, job_name, error, context=None):
    """Shared persistent error sink so failures are visible without live logs."""
    import traceback
    try:
        _sb.table("poller_error_log").insert({
            "poller_name": poller_name,
            "job_name": job_name,
            "error_message": str(error)[:1000],
            "error_traceback": traceback.format_exc()[:4000],
            "context": context or {},
        }).execute()
    except Exception:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    w = get_watched_tickers()
    a = get_all_stocks()
    print(f"watched  = {len(w)} tickers -> {sorted(w)[:20]}")
    print(f"universe = {len(a)} tickers -> {a[:10]}")
