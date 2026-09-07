"""
news_deduplicator.py
GQ FinXray US — News article deduplication cache.

FIXED (Aug 13 2026):
- Increased TTL_SECONDS from 300 (5 min) to 3600 (1 hour)
- Previous TTL was shorter than 10-minute poll interval
- Cache was always cold, causing 100% re-ingestion every cycle
- Every article was rejected by database unique index as duplicate
- Hundreds of wasted Supabase round-trips per cycle
"""

import time

# Store (url, timestamp) to detect duplicates
_news_cache = {}

# FIXED: TTL must be larger than the longest news polling interval.
# poll_fmp_news runs every 10 minutes (main.py), so a 300s TTL meant 100% of each
# batch was re-inserted every run, rejected by the unique index, and the exception
# swallowed — hundreds of wasted Supabase round-trips per run, on the same single
# thread that has to run every other poller.
# TTL 3600s (1 hour) is comfortably above the 10-minute poll interval.
TTL_SECONDS = 3600


def should_send(article):
    """
    Returns True if this article is new and should be sent.
    Returns False if we've already seen it within the TTL window.

    Args:
        article: dict with at least 'url' and 'publishedDate' fields

    Returns:
        bool: True if new, False if duplicate
    """
    url = article.get('url')
    if not url:
        return False  # No URL = can't dedupe, skip it

    now = time.time()

    # Clean expired entries
    _cleanup_expired(now)

    # Check if we've seen this URL before
    if url in _news_cache:
        return False  # Duplicate

    # New article — cache it
    pub_date = article.get('publishedDate', now)
    _news_cache[url] = now

    return True


def dedupe_articles(articles):
    """
    Filter a list of articles, keeping only new ones.

    Args:
        articles: list of article dicts

    Returns:
        list: only new articles
    """
    return [a for a in articles if should_send(a)]


def _cleanup_expired(now):
    """Remove articles older than TTL_SECONDS."""
    global _news_cache
    _news_cache = {
        url: timestamp
        for url, timestamp in _news_cache.items()
        if (now - timestamp) < TTL_SECONDS
    }


def clear_cache():
    """Clear all cached articles (for testing/reset)."""
    global _news_cache
    _news_cache = {}


def cache_size():
    """Return current cache size (number of unique URLs)."""
    return len(_news_cache)
