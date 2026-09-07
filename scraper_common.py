"""
Shared utilities for all heatmap/universe scrapers.
TwelveData (td_time_series/td_quote) removed — replaced with FMP equivalents
via fmp_client.py. calc_returns/apply_glocom/upsert_* are vendor-agnostic
and unchanged.
"""
import os
import time
import requests
import pandas as pd
from datetime import datetime, date
from dotenv import load_dotenv
from supabase import create_client

import fmp_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]).strip()

_sb = None

def supabase():
    global _sb
    if _sb is None:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


def fmp_time_series(symbol, from_date=None, to_date=None, retries=4):
    """Fetch daily OHLCV newest->oldest via FMP historical-price-eod/full.
    Returns list of dicts shaped like {'date':..., 'close':...} (same keys
    calc_returns() already expects) or None."""
    if not from_date:
        from_date = "2014-01-01"
    if not to_date:
        to_date = date.today().isoformat()

    wait = 5
    for attempt in range(retries):
        try:
            data = fmp_client.get_historical_prices(symbol, from_date, to_date)
            if not data:
                print(f"  [no data] {symbol}")
                return None
            return sorted(data, key=lambda r: r.get("date", ""), reverse=True)
        except Exception as e:
            print(f"  [exception] {symbol} attempt {attempt+1}: {e}")
            time.sleep(wait)
            wait *= 2
    return None


def fmp_quote(symbol, retries=3):
    for _ in range(retries):
        try:
            q = fmp_client.get_quote(symbol)
            if q:
                return q
        except Exception:
            pass
        time.sleep(3)
    return None


def calc_returns(prices):
    """
    prices: list of dicts with 'close' and 'datetime' (or 'date'), newest first.
    Returns dict: ret_1d, ret_1w, ret_1m, ret_3m, ret_6m, ret_1y,
                  ret_5y, ret_10y, ret_all, ret_ytd.
    """
    if not prices or len(prices) < 2:
        return {}

    def to_date(p):
        s = p.get("datetime") or p.get("date") or ""
        if isinstance(s, date):
            return s
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()

    closes = [float(p["close"]) for p in prices]
    dates = [to_date(p) for p in prices]
    cur = closes[0]

    def ret(base):
        return (cur - base) / base if base and base != 0 else None

    def at(n):
        return closes[n] if len(closes) > n else closes[-1]

    def pct(base):
        return ret(base)

    result = {
        "ret_1d":  pct(at(1)),
        "ret_1w":  pct(at(5)),
        "ret_1m":  pct(at(21)),
        "ret_3m":  pct(at(63)),
        "ret_6m":  pct(at(126)),
        "ret_1y":  pct(at(252)),
        "ret_5y":  pct(at(1260)),
        "ret_10y": pct(at(2520)),
        "ret_all": pct(closes[-1]),
    }

    year_start = date(date.today().year, 1, 1)
    ytd_base = next((closes[i] for i, d in enumerate(dates) if d < year_start), None)
    result["ret_ytd"] = pct(ytd_base)

    return result


def apply_glocom(rows):
    """
    Add glocom_code (1=best...5=worst), glocom_label, sort_score.
    """
    if not rows:
        return rows
    df = pd.DataFrame(rows)

    def pct_rank(col):
        return pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0).rank(pct=True)

    w  = pct_rank("ret_1w")
    m6 = pct_rank("ret_6m")
    y1 = pct_rank("ret_1y")

    df["sort_score"] = (w * 0.50 + m6 * 0.30 + y1 * 0.20) * 100
    df["_w"]  = w
    df["_m6"] = m6
    df["_y1"] = y1

    def classify(r):
        wi, m6i, y1i = r["_w"], r["_m6"], r["_y1"]
        if m6i > 0.4 and y1i > 0.8:
            return (1, "US 80 80") if wi > 0.8 else (1, "LTS 80")
        if m6i > 0.4 and 0.6 < y1i <= 0.8:
            return (2, "US 60 60") if wi > 0.6 else (2, "LTS 60")
        if wi < 0.3 and m6i < 0.3:
            return (5, "UW 30 30")
        if wi <= 0.3 and m6i >= 0.3:
            return (4, "LTW 30")
        if wi < 0.4 and m6i < 0.4:
            return (4, "UW 40 40")
        if wi <= 0.4 and m6i < 0.4:
            return (4, "LTW 40")
        return (3, "Neutral 40 60")

    results = df.apply(classify, axis=1)
    df["glocom_code"]  = results.apply(lambda x: x[0])
    df["glocom_label"] = results.apply(lambda x: x[1])
    df = df.drop(columns=["_w", "_m6", "_y1"])
    df = df.where(pd.notna(df), other=None)
    return df.to_dict("records")


def upsert_returns(rows, batch=50):
    import math
    def clean(row):
        return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
    sb = supabase()
    cleaned = [clean(r) for r in rows]
    for i in range(0, len(cleaned), batch):
        sb.table("returns_latest").upsert(cleaned[i:i+batch], on_conflict="instrument_id").execute()
        time.sleep(0.1)
    print(f"  -> {len(cleaned)} rows -> returns_latest")


def upsert_prices(rows, batch=50):
    sb = supabase()
    for i in range(0, len(rows), batch):
        sb.table("prices").upsert(rows[i:i+batch], on_conflict="instrument_id").execute()
        time.sleep(0.05)


def upsert_history(rows, batch=200):
    seen, deduped = set(), []
    for r in rows:
        key = (r["instrument_id"], r["date"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        for attempt in range(4):
            try:
                supabase().table("price_history").upsert(chunk, on_conflict="instrument_id,date").execute()
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"  [upsert_history retry {attempt+1}] {e} -- sleeping {wait}s")
                time.sleep(wait)
        time.sleep(0.05)
    print(f"  -> {len(rows)} rows -> price_history")


def load_instruments(universe=None):
    sb = supabase()
    rows, offset = [], 0
    while True:
        q = sb.table("instruments").select("*").eq("is_active", True)
        if universe:
            if isinstance(universe, list):
                q = q.in_("universe", universe)
            else:
                q = q.eq("universe", universe)
        for attempt in range(5):
            try:
                batch = q.range(offset, offset + 999).execute().data or []
                break
            except Exception as e:
                if attempt == 4:
                    raise
                wait = 10 * (attempt + 1)
                print(f"  [load_instruments retry {attempt+1}] {e} -- sleeping {wait}s")
                time.sleep(wait)
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows
