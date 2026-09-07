"""
large_trades_poller.py
GQ FinXray US — Feature 5, rebuilt for the US market.

THE PROBLEM THIS SOLVES
-----------------------
The India system had a genuine "Large Transactions" feature fed by NSE/BSE
bulk-deal and block-deal files. SEBI mandates that disclosure: exchanges must
publish every large trade next day, WITH the client name.

The US has no equivalent regime. There is no public feed naming who traded.
The previous US port papered over this by relabelling FMP insider-trading rows
as `BULK_DEAL` -- but a Form 4 insider transaction (a CFO selling 4,000 shares
of her own company) is a completely different event from an institution
crossing 500,000 shares. Shipping one under the other's name produces alerts
that read as wrong to anyone who knows the market.

WHAT THIS DOES INSTEAD
----------------------
Massive's Advanced tier includes tick-level trades. Every large print is on
the public tape in seconds, with size, price, exchange and condition codes --
everything except the counterparty name, which simply does not exist in US
market data at any price.

So the feature is reframed honestly:
    India:  "Fund X bought 2.1M shares of RELIANCE"   (next day, with name)
    US:     "480,000 shares crossed at $184.20, $88.4M"  (seconds, no name)

Arguably more actionable, since it arrives during the session rather than
after the close. For the "who", the legal instruments are 13F (quarterly,
~45-day lag) and SC 13D/G above 5% -- both already reachable and neither
real-time.

HOW IT WORKS
------------
Polls each watchlisted ticker's tape for the window since the last run,
filters locally for prints above a notional threshold, merges consecutive
prints that are obviously one order being filled, and writes one alert per
distinct block.

Templated output -- no LLM, no summarisation, so this feature is completely
independent of the DeepInfra pipeline and its failure modes.
"""

import os
import logging
import traceback
from datetime import datetime, timezone, timedelta

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

# A print must clear BOTH bars to alert. Notional catches megacap blocks that
# aren't many shares; share count catches heavy size in cheaper names.
MIN_NOTIONAL_USD = 2_000_000
MIN_SHARES = 10_000

# How far back to look each run. Keep this comfortably above the scheduler
# interval so a slow run or a restart doesn't leave a gap in coverage.
LOOKBACK_MINUTES = 20

# Per-ticker page cap. A megacap prints hundreds of thousands of trades an
# hour; without a cap one liquid name could dominate an entire run. Anything
# dropped is logged rather than silently discarded.
MAX_PAGES_PER_TICKER = 3
PAGE_LIMIT = 50_000

# Consecutive prints at the same price within this many milliseconds are
# treated as one order being filled across multiple venues, not as separate
# blocks -- otherwise a single institutional order sprays a dozen alerts.
MERGE_WINDOW_MS = 2_000


def get_supabase():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def log_poller_error(job_name, error, context=None):
    logger.error(f"[LARGE_TRADES] {job_name}: {error}")
    try:
        get_supabase().table("poller_error_log").insert({
            "poller_name": "large_trades_poller",
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {}
        }).execute()
    except Exception as log_err:
        logger.error(f"[LARGE_TRADES] Failed to write poller_error_log: {log_err}")


def get_watchlist_tickers():
    try:
        result = get_supabase().table("watchlists").select("ticker").execute()
        return sorted({row["ticker"] for row in result.data if row.get("ticker")})
    except Exception as e:
        log_poller_error("get_watchlist_tickers", e)
        return []


def already_sent(ticker, block_key):
    """Dedupe on the block's identity, not just ticker+day.

    Two genuinely distinct institutional blocks in the same name on the same
    day are two newsworthy events, not a duplicate -- so the key includes
    timestamp, size and price.
    """
    try:
        result = get_supabase().table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", "LARGE_TRADE") \
            .eq("extra->>block_key", str(block_key)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def _fmt_notional(value):
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def fetch_recent_trades(ticker, since_ns):
    """Pull the tape since `since_ns`, paging backwards from now."""
    collected = []
    cursor_lte = None
    for page in range(MAX_PAGES_PER_TICKER):
        trades = massive_client.get_trades(
            ticker,
            timestamp_gte=since_ns,
            timestamp_lte=cursor_lte,
            limit=PAGE_LIMIT,
            order="desc",
        )
        if not trades:
            break
        collected.extend(trades)
        if len(trades) < PAGE_LIMIT:
            break
        oldest = trades[-1].get("sip_timestamp") or trades[-1].get("participant_timestamp")
        if not oldest:
            break
        cursor_lte = oldest - 1
        if page == MAX_PAGES_PER_TICKER - 1:
            logger.warning(
                f"[LARGE_TRADES] {ticker}: hit the {MAX_PAGES_PER_TICKER}-page cap "
                f"({MAX_PAGES_PER_TICKER * PAGE_LIMIT:,} trades). Older prints in this "
                f"window were NOT scanned -- shorten LOOKBACK_MINUTES or raise the cap."
            )
    return collected


def find_blocks(ticker, trades):
    """Filter the tape down to prints that clear both thresholds, then merge
    same-price fills that land within MERGE_WINDOW_MS of each other."""
    big = []
    for t in trades:
        size = t.get("size") or 0
        price = t.get("price") or 0
        if not size or not price:
            continue
        notional = size * price
        if size < MIN_SHARES or notional < MIN_NOTIONAL_USD:
            continue
        big.append({
            "ts": t.get("sip_timestamp") or t.get("participant_timestamp") or 0,
            "size": size,
            "price": price,
            "notional": notional,
            "exchange": t.get("exchange"),
            "conditions": t.get("conditions") or [],
        })

    if not big:
        return []

    big.sort(key=lambda x: x["ts"])
    merged = []
    for tr in big:
        if merged:
            last = merged[-1]
            same_price = abs(last["price"] - tr["price"]) < 0.005
            close_in_time = abs(tr["ts"] - last["ts"]) <= MERGE_WINDOW_MS * 1_000_000
            if same_price and close_in_time:
                last["size"] += tr["size"]
                last["notional"] += tr["notional"]
                last["fills"] = last.get("fills", 1) + 1
                continue
        tr["fills"] = 1
        merged.append(tr)
    return merged


def save_alert(ticker, block):
    ts_ns = block["ts"]
    dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc) if ts_ns else datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = dt.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
    except Exception:
        et = dt.strftime("%H:%M:%S UTC")

    block_key = f"{ts_ns}_{int(block['size'])}_{block['price']:.4f}"
    if already_sent(ticker, block_key):
        return False

    fills_note = f" · {block['fills']} fills" if block.get("fills", 1) > 1 else ""
    summary = (
        f"🐋 *Large Trade — ${ticker}*\n\n"
        f"*Size:* {int(block['size']):,} shares\n"
        f"*Price:* ${block['price']:.2f}\n"
        f"*Notional:* {_fmt_notional(block['notional'])}\n"
        f"*Time:* {et}{fills_note}\n\n"
        f"_A block of this size crossing the tape often signals institutional "
        f"repositioning. US markets do not disclose counterparties — for the "
        f"'who', watch for a 13F or SC 13D/G filing._\n"
        f"_Source: Massive tick tape_"
    )

    impact = "HIGH" if block["notional"] >= 10_000_000 else "MEDIUM"
    try:
        get_supabase().table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "LARGE_TRADE",
            "filing_type": "LARGE_TRADE",
            "delivered": False,
            "extra": tag_extra({
                "block_key": block_key,
                "size": int(block["size"]),
                "price": round(block["price"], 4),
                "notional": round(block["notional"], 2),
                "exchange": block.get("exchange"),
                "conditions": block.get("conditions"),
                "fills": block.get("fills", 1),
                "traded_at": dt.isoformat(),
            }, "LARGE_TRADE", "LARGE_TRADE"),
            "filing_url": None,
        }).execute()
        logger.info(f"[LARGE_TRADES] {ticker}: {int(block['size']):,} sh @ "
                    f"${block['price']:.2f} = {_fmt_notional(block['notional'])}")
        return True
    except Exception as e:
        log_poller_error("save_alert", e, {"ticker": ticker, "block_key": block_key})
        return False


def run_large_trades_poller():
    if not market_data.market_open(allow_extended=True):
        logger.info("[LARGE_TRADES] Market closed — skipping.")
        return

    tickers = get_watchlist_tickers()
    if not tickers:
        logger.info("[LARGE_TRADES] Watchlist empty — nothing to scan.")
        return

    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    since_ns = int(since.timestamp() * 1_000_000_000)

    logger.info(f"[LARGE_TRADES] Scanning tape for {len(tickers)} tickers "
                f"over the last {LOOKBACK_MINUTES}m "
                f"(min {MIN_SHARES:,} sh AND {_fmt_notional(MIN_NOTIONAL_USD)})...")

    total_alerts = 0
    total_scanned = 0
    for ticker in tickers:
        try:
            trades = fetch_recent_trades(ticker, since_ns)
            total_scanned += len(trades)
            for block in find_blocks(ticker, trades):
                if save_alert(ticker, block):
                    total_alerts += 1
        except Exception as e:
            log_poller_error("run_large_trades_poller", e, {"ticker": ticker})

    logger.info(f"[LARGE_TRADES] Done. {total_scanned:,} trades scanned, "
                f"{total_alerts} block alert(s).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_large_trades_poller()
