"""
news_deduplicator.py
GQ FinXray US — News article deduplication cache.

Prevents sending the same article twice within a polling window.
Uses URL as the unique key (most reliable across FMP polls).

Cache is in-memory with TTL. Articles older than TTL_SECONDS are purged.
"""

import time

# Store (url, published_date) to detect duplicates
_news_cache = {}
TTL_SECONDS = 300  # Keep articles in cache for 5 minutes (handles 30-60sec polling)


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
