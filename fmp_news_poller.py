"""
fmp_news_poller.py
GQ FinXray US — FMP ticker-specific news with deduplication.

Polls /stable/news/stock for watchlisted tickers every 30-60 seconds.
Deduplicates by URL before storing in raw_filings.

Key optimizations from FMP support:
- Batch tickers: /stable/news/stock?symbols=AAPL,MSFT,GOOGL (up to 50 per request)
- Poll interval: 30-60 seconds (uses ~150 API calls/min for 50 tickers, well under 750 limit)
- Dedup: cache URLs, skip duplicates within 5-minute window
- Rate limit: 750 calls/min Pro plan — we use ~20% headroom
"""

import os
import time
import hashlib
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error
from news_deduplicator import should_send, cache_size

load_dotenv()

logger = logging.getLogger(__name__)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "fmp_news_poller"
POLL_INTERVAL = 30  # seconds (FMP support: safe for 750 calls/min Pro plan)
BATCH_SIZE = 50  # tickers per request


def poll_fmp_news():
    """
    Poll /stable/news/stock for all watched tickers.

    Batches tickers (up to 50 per request) to minimize API calls.
    Deduplicates by URL before storing.
    """
    try:
        watched = get_watched_tickers()
        if not watched:
            logger.info("[FMP NEWS] No watched tickers — nothing to poll")
            return

        logger.info(f"[FMP NEWS] Polling {len(watched)} watched tickers for news")

        # Batch tickers into groups of BATCH_SIZE
        ticker_list = sorted(watched)
        new_articles = 0
        duplicates = 0
        errors = 0

        for i in range(0, len(ticker_list), BATCH_SIZE):
            batch = ticker_list[i:i + BATCH_SIZE]
            symbols = ",".join(batch)

            try:
                articles = fmp_client.get_stock_news(symbols, limit=250)
                if not articles:
                    logger.debug(f"[FMP NEWS] No articles for batch {i//BATCH_SIZE + 1}")
                    continue

                logger.debug(f"[FMP NEWS] Batch {i//BATCH_SIZE + 1}: {len(articles)} articles returned")

                for article in articles:
                    # Dedup check
                    if not should_send(article):
                        duplicates += 1
                        continue

                    # Extract fields
                    ticker = article.get('symbol', 'MARKET').upper()
                    url = article.get('url')
                    title = article.get('title', '')
                    text = article.get('text', '')
                    pub_date = article.get('publishedDate', datetime.now(timezone.utc).isoformat())
                    source = "FMP_NEWS"
                    filing_type = "NEWS"

                    # Content hash for dedup at DB level
                    content_hash = hashlib.sha256(f"{url}".encode()).hexdigest()

                    # Store in raw_filings
                    try:
                        supabase.table("raw_filings").insert({
                            "ticker": ticker,
                            "company_name": "",  # FMP doesn't provide company name
                            "source": source,
                            "filing_type": filing_type,
                            "filing_url": url,
                            "filing_date": pub_date,
                            "content_hash": content_hash,
                            "raw_text": text or title,
                            "status": "PENDING",
                            "extra": tag_extra(
                                {"title": title, "source": source},
                                source,
                                filing_type
                            ),
                        }).execute()
                        new_articles += 1
                    except Exception as e:
                        logger.warning(f"[FMP NEWS] Failed to insert article for {ticker}: {e}")
                        errors += 1

            except Exception as e:
                logger.error(f"[FMP NEWS] Batch {i//BATCH_SIZE + 1} failed: {e}")
                log_poller_error(POLLER_NAME, f"Batch error: {e}")
                errors += 1

        cache_info = cache_size()
        logger.info(f"[FMP NEWS] Done — {new_articles} new, {duplicates} dup, {errors} errors. Cache: {cache_info} URLs")

    except Exception as e:
        logger.error(f"[FMP NEWS] Poll failed: {e}")
        log_poller_error(POLLER_NAME, str(e))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poll_fmp_news()
