"""
FMP Main Scraper — fetches EOD history, returns, and market cap for any
universe. Replaces eodhd_scraper.py.

Usage:
    python fmp_scraper.py --universe us_stocks
    python fmp_scraper.py --universe us_etfs
    python fmp_scraper.py --universe global_stocks
    python fmp_scraper.py --universe us_stocks --workers 15
    python fmp_scraper.py --universe us_stocks --resume
    python fmp_scraper.py --universe us_stocks --no-history

Per instrument fetches:
    1. Historical EOD (10 years OHLCV, /stable/historical-price-eod/full) -> returns_latest + price_history
    2. Profile (/stable/profile) -> market_cap_usd, sector, industry (instruments table update)

FMP has no per-request "402 quota exceeded" convention like EODHD did —
rate limits come back as HTTP 429. QuotaExceededError is kept as the
signal name for compatibility with the rest of this script, but it's now
raised on a 429 that persists past the retry budget rather than a 402.
"""
import os, re, sys, time, argparse, threading
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

import fmp_client
from scraper_common import supabase, calc_returns, apply_glocom, upsert_returns, upsert_prices, upsert_history

load_dotenv()

HISTORY_FROM = "2014-01-01"


class QuotaExceededError(Exception):
    """Raised when FMP rate limiting (429) persists past the retry budget."""
    pass


def fmp_eod(symbol, retries=3):
    """Fetch EOD history newest->oldest. Returns list or None.

    Raises QuotaExceededError if FMP keeps returning 429 across the full
    retry budget -- that's a persistent rate limit, not a one-off blip, and
    the caller (run()/run_fundamentals()) needs to know so it can stop
    hammering every remaining instrument instead of burning through the
    rest of the universe one doomed retry-loop at a time.
    """
    wait = 5
    consecutive_429 = 0
    for attempt in range(retries):
        try:
            data = fmp_client.get_historical_prices(symbol, HISTORY_FROM, date.today().isoformat())
            if not data:
                return None
            return sorted(data, key=lambda r: r.get("date", ""), reverse=True)
        except Exception as e:
            if "429" in str(e):
                consecutive_429 += 1
                if consecutive_429 >= retries:
                    raise QuotaExceededError(
                        f"FMP rate limit (429) persisted for {symbol} across {retries} attempts"
                    )
                time.sleep(60)
                continue
            if attempt < retries - 1:
                time.sleep(wait)
                wait *= 2
    return None


def fmp_profile(symbol, retries=2):
    """Returns dict with marketCap, sector, industry, or None."""
    for attempt in range(retries):
        try:
            return fmp_client.get_profile(symbol)
        except Exception:
            time.sleep(3 * (attempt + 1))
    return None


ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9,10}$")


def to_fmp_symbol(instrument):
    """FMP takes bare US tickers (no .US suffix like EODHD needed)."""
    return instrument["symbol"]


def process_instrument(instrument, fetch_fundamentals=True):
    iid = instrument["id"]
    fmp_sym = to_fmp_symbol(instrument)
    universe = instrument.get("universe", "")
    print(f"  [{universe}] -> {fmp_sym} started", flush=True)

    if ISIN_RE.match(instrument.get("symbol") or ""):
        return None

    prices = fmp_eod(fmp_sym)
    if not prices or len(prices) < 2:
        return None

    first_valid = next((i for i, p in enumerate(prices) if float(p.get("close") or 0) > 0), None)
    if first_valid is None:
        return None
    prices = prices[first_valid:]
    if len(prices) < 2:
        return None

    rets = calc_returns(prices)
    if not rets:
        return None

    latest = prices[0]
    price = float(latest.get("close") or 0)
    currency = instrument.get("currency", "USD") or "USD"
    price_usd = price if currency == "USD" else None

    returns_row = {
        "instrument_id": iid, "universe": universe, "price_native": price,
        "price_usd": price_usd, "currency": currency, "as_of": latest.get("date"), **rets,
    }
    prices_row = {
        "instrument_id": iid, "price_native": price, "price_usd": price_usd,
        "currency": currency, "change_pct_1d": rets.get("ret_1d"), "as_of": latest.get("date"),
    }

    history_rows = []
    for p in prices:
        try:
            history_rows.append({
                "instrument_id": iid, "date": p["date"],
                "open": float(p["open"]) if p.get("open") else None,
                "high": float(p["high"]) if p.get("high") else None,
                "low": float(p["low"]) if p.get("low") else None,
                "close": float(p["close"]),
                "volume": int(p["volume"]) if p.get("volume") else None,
            })
        except (ValueError, TypeError, KeyError):
            continue

    instrument_update = None
    if fetch_fundamentals:
        profile = fmp_profile(fmp_sym)
        if profile:
            mcap_raw = profile.get("marketCap") or profile.get("mktCap")
            mcap = int(float(mcap_raw)) if mcap_raw and float(mcap_raw) > 0 else None
            instrument_update = {
                "id": iid, "market_cap_usd": mcap,
                "sector": profile.get("sector") or None,
                "industry": profile.get("industry") or None,
            }

    return returns_row, prices_row, history_rows, instrument_update


def process_fundamentals_only(instrument):
    iid = instrument["id"]
    fmp_sym = to_fmp_symbol(instrument)
    universe = instrument.get("universe", "")
    print(f"  [{universe} fundamentals] -> {fmp_sym} started", flush=True)
    if ISIN_RE.match(instrument.get("symbol") or ""):
        return None
    profile = fmp_profile(fmp_sym)
    if not profile:
        return None
    mcap_raw = profile.get("marketCap") or profile.get("mktCap")
    mcap = int(float(mcap_raw)) if mcap_raw and float(mcap_raw) > 0 else None
    return {"id": iid, "market_cap_usd": mcap, "sector": profile.get("sector") or None, "industry": profile.get("industry") or None}


def load_instruments_missing_fundamentals(universe):
    sb = supabase()
    rows, offset = [], 0
    while True:
        batch = sb.table("instruments").select("id,symbol,universe,exchange,currency,market_cap_usd")\
            .eq("universe", universe).eq("is_active", True)\
            .order("id").range(offset, offset + 999).execute().data or []
        for row in batch:
            if row.get("market_cap_usd") is None:
                rows.append(row)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def run_fundamentals(universe, workers=10):
    print(f"\n{'='*60}\nFMP Fundamentals -- universe: {universe}\nWorkers: {workers}\n{'='*60}\n")
    instruments = load_instruments_missing_fundamentals(universe)
    print(f"Instruments needing fundamentals: {len(instruments)}", flush=True)
    if not instruments:
        print("Nothing to do.")
        return

    updates, failed, done = [], [], 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_fundamentals_only, inst): inst for inst in instruments}
        for future in as_completed(futures):
            inst = futures[future]
            done += 1
            try:
                result = future.result()
                if result is None:
                    failed.append(inst["symbol"])
                else:
                    updates.append(result)
                if done % 100 == 0:
                    print(f"  [{universe} fundamentals] {done}/{len(instruments)} done | failed={len(failed)}", flush=True)
                if len(updates) >= 200:
                    upsert_instrument_enrichments(updates)
                    updates.clear()
            except Exception as e:
                failed.append(f"{inst['symbol']}:{e}")

    if updates:
        upsert_instrument_enrichments(updates)
    print(f"\nDone in {(time.time()-start)/60:.1f}min | processed={done} | failed={len(failed)}")


def upsert_instrument_enrichments(rows):
    sb = supabase()
    for r in [r for r in rows if r]:
        iid = r.pop("id")
        sb.table("instruments").update(r).eq("id", iid).execute()


def load_instruments_for_universe(universe, resume=False):
    sb = supabase()
    rows, offset = [], 0
    done_ids = set()
    if resume:
        try:
            r_offset = 0
            while True:
                batch = sb.table("returns_latest").select("instrument_id").eq("universe", universe)\
                    .not_.is_("ret_10y", "null").order("instrument_id").range(r_offset, r_offset+999).execute().data or []
                for r in batch:
                    done_ids.add(r["instrument_id"])
                if len(batch) < 1000:
                    break
                r_offset += 1000
        except Exception as e:
            print(f"  [resume] Could not load done IDs ({e}) -- scraping all instruments")
            done_ids = set()

    while True:
        batch = sb.table("instruments").select("*").eq("universe", universe)\
            .eq("is_active", True).order("id").range(offset, offset+999).execute().data or []
        for row in batch:
            if resume and row["id"] in done_ids:
                continue
            rows.append(row)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def count_active_instruments(universe):
    sb = supabase()
    return sb.table("instruments").select("id", count="exact").eq("universe", universe)\
        .eq("is_active", True).execute().count or 0


def run(universe, workers=10, resume=False, history=True, fundamentals=True):
    print(f"\n{'='*60}\nFMP Scraper -- universe: {universe}\nWorkers: {workers} | Resume: {resume} | History: {history} | Fundamentals: {fundamentals}\n{'='*60}\n")
    instruments = load_instruments_for_universe(universe, resume=resume)
    print(f"Instruments loaded from Supabase: {len(instruments)}", flush=True)
    if not instruments:
        print("Nothing to do.")
        return

    all_returns, all_prices, all_history, all_instrument_updates, failed = [], [], [], [], []
    start, done = time.time(), 0

    quota_hit = False
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_instrument, inst, fundamentals): inst for inst in instruments}
        for future in as_completed(futures):
            inst = futures[future]
            done += 1
            try:
                result = future.result()
                if result is None:
                    failed.append(inst["symbol"])
                else:
                    rets_row, prices_row, hist_rows, inst_update = result
                    all_returns.append(rets_row)
                    all_prices.append(prices_row)
                    if history:
                        all_history.extend(hist_rows)
                    if inst_update:
                        all_instrument_updates.append(inst_update)

                if done % 100 == 0:
                    print(f"  [{universe}] {done}/{len(instruments)} done | failed={len(failed)}", flush=True)

                if len(all_returns) >= 50:
                    _flush(all_returns, all_prices, all_history, all_instrument_updates, universe)
                    all_returns.clear(); all_prices.clear(); all_history.clear(); all_instrument_updates.clear()
            except QuotaExceededError as e:
                print(f"\n[QUOTA EXCEEDED] {e}\nStopping this run early -- remaining instruments will "
                      f"just hit the same rate limit. Re-run with --resume once the key resets.")
                quota_hit = True
                for f in futures:
                    f.cancel()
                break
            except Exception as e:
                failed.append(f"{inst['symbol']}:{e}")

    if all_returns:
        _flush(all_returns, all_prices, all_history, all_instrument_updates, universe)

    print(f"\nDone in {(time.time()-start)/60:.1f}min | processed={done} | failed={len(failed)}"
          f"{' | STOPPED EARLY: quota exceeded' if quota_hit else ''}")
    if failed[:20]:
        print(f"Failed samples: {failed[:20]}")


def _flush(returns_rows, prices_rows, history_rows, instrument_updates, universe):
    print(f"  Flushing {len(returns_rows)} returns + {len(history_rows)} history + {len(instrument_updates)} instrument updates...")
    scored = apply_glocom(returns_rows)
    upsert_returns(scored)
    upsert_prices(prices_rows)
    if history_rows:
        upsert_history(history_rows)
    if instrument_updates:
        upsert_instrument_enrichments(instrument_updates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FMP scraper")
    parser.add_argument("--universe", required=True, choices=["us_stocks", "us_etfs", "global_stocks"])
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--no-fundamentals", action="store_true")
    args = parser.parse_args()

    run(universe=args.universe, workers=args.workers, resume=args.resume,
        history=not args.no_history, fundamentals=not args.no_fundamentals)
