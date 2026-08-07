"""
analyst_ratings_poller.py
GQ FinXray US — Analyst ratings and price target changes via FMP API. Feature 11.

Covers:
- Rating consensus (BUY, HOLD, SELL)
- Price target changes (current, previous, change %)
- Analyst count per rating
- Rating changes (upgrades/downgrades)

Stores in alerts table with source=FMP_ANALYST, impact based on magnitude of change.
"""

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

RATING_IMPACT = {
    "BUY": "HIGH",      # Analyst upgrades to BUY
    "HOLD": "MEDIUM",   # Neutral/Hold ratings
    "SELL": "HIGH",     # Downgrades to SELL
}


def get_all_stocks():
    """Fetch all US stocks (NYSE + NASDAQ) from the stocks table."""
    try:
        result = supabase.table("stocks").select("ticker").execute()
        return [row["ticker"] for row in result.data if row.get("ticker")]
    except Exception as e:
        logger.error(f"[ANALYST] Failed to fetch stocks list: {e}")
        return []


def rating_already_stored(ticker, rating_consensus, price_target):
    """Check if this exact rating + price target combo already exists."""
    try:
        result = supabase.table("raw_filings").select("id").eq(
            "filing_url", f"analyst_{ticker}_{rating_consensus}_{price_target}"
        ).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"[ANALYST] Dedup check failed: {e}")
        return False


def detect_rating_change(ticker, current_rating):
    """Check if this is an upgrade/downgrade from last stored rating."""
    try:
        result = supabase.table("alerts").select("*").eq(
            "ticker", ticker
        ).eq("source", "FMP_ANALYST").order("created_at", desc=True).limit(1).execute()

        if not result.data:
            return None, None  # First time seeing this rating

        last_alert = result.data[0]
        last_rating = last_alert.get("extra", {}).get("rating_consensus", "")

        # Define upgrade/downgrade paths
        rating_order = {"BUY": 3, "HOLD": 2, "SELL": 1}
        current_rank = rating_order.get(current_rating, 0)
        last_rank = rating_order.get(last_rating, 0)

        if current_rank > last_rank:
            return "UPGRADE", last_rating
        elif current_rank < last_rank:
            return "DOWNGRADE", last_rating

        return None, None
    except Exception as e:
        logger.error(f"[ANALYST] Rating change detection failed: {e}")
        return None, None


def store_analyst_alert(ticker, rating_consensus, price_target, analyst_count, change_pct, rating_change=None, previous_rating=None):
    """Store analyst rating alert."""
    try:
        change_direction = rating_change or ("↑" if change_pct >= 0 else "↓")
        impact = "HIGH" if rating_change or abs(change_pct) > 10 else "MEDIUM"

        summary = f"Analyst consensus: {rating_consensus} with ${price_target:.2f} PT. "
        if rating_change:
            summary += f"{rating_change.upper()} from {previous_rating}. "
        if change_pct != 0:
            summary += f"Price target {change_direction} {abs(change_pct):.1f}%. {analyst_count} analysts covering."

        alert_data = {
            "ticker": ticker,
            "source": "FMP_ANALYST",
            "filing_type": "ANALYST_RATING",
            "summary": summary,
            "impact": impact,
            "delivered": False,
            "extra": tag_extra({
                "rating_consensus": rating_consensus,
                "price_target": price_target,
                "analyst_count": analyst_count,
                "price_target_change_pct": change_pct,
                "rating_change": rating_change,
                "previous_rating": previous_rating,
            })
        }

        supabase.table("alerts").insert(alert_data).execute()
        logger.info(f"[ANALYST] {ticker}: {rating_consensus} PT ${price_target:.2f} ({change_direction} {abs(change_pct):.1f}%) — {analyst_count} analysts")
    except Exception as e:
        logger.error(f"[ANALYST] Failed to store alert for {ticker}: {e}")


def poll_analyst_ratings():
    """Poll all stocks for analyst rating consensus and price targets."""
    tickers = get_all_stocks()
    if not tickers:
        logger.warning("[ANALYST] No stocks found")
        return

    logger.info(f"[ANALYST] Checking ratings for {len(tickers)} stocks...")
    processed = 0

    for ticker in tickers:
        try:
            # Fetch analyst consensus from FMP
            data = fmp_client.get_analyst_estimates(ticker)
            if not data:
                continue

            rating_consensus = data.get("rating", "")
            price_target = data.get("price_target", 0)
            analyst_count = data.get("analyst_count", 0)
            previous_price_target = data.get("previous_price_target", price_target)

            if not rating_consensus or price_target == 0:
                continue

            # Calculate change %
            change_pct = 0
            if previous_price_target > 0:
                change_pct = ((price_target - previous_price_target) / previous_price_target) * 100

            # Check for dedup
            dedup_key = f"analyst_{ticker}_{rating_consensus}_{price_target}"
            if rating_already_stored(ticker, rating_consensus, price_target):
                continue

            # Detect upgrade/downgrade
            rating_change, previous_rating = detect_rating_change(ticker, rating_consensus)

            # Store alert if new rating, significant PT change, or rating change
            if rating_change or abs(change_pct) > 5:  # Only alert on >5% PT move or rating change
                store_analyst_alert(
                    ticker,
                    rating_consensus,
                    price_target,
                    analyst_count,
                    change_pct,
                    rating_change,
                    previous_rating
                )
                processed += 1

        except Exception as e:
            logger.error(f"[ANALYST] Error processing {ticker}: {e}")
            continue

    logger.info(f"[ANALYST] Complete. {processed} new alerts generated.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    poll_analyst_ratings()
