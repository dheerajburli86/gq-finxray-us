"""
Diagnostic script to identify where news articles are being filtered out.
Traces one cycle through with detailed logging.
"""

import os
import re
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

# Import the functions we need to test
import sys
sys.path.insert(0, '/home/claude/gq')

from news_poller import (
    extract_watched_tickers, NEWS_SOURCES, FEED_TIMEOUT,
    parse_feed_date, _fetch_feed, _parse_entries, _child_text,
    strip_html, _entry_link, MAX_ARTICLE_AGE_DAYS
)
from watchlist_util import get_watched_tickers

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("STEP 1: Get watched tickers")
print("="*80)

watched = get_watched_tickers()
print(f"Raw watched from DB: {watched}")
print(f"Type: {type(watched)}")
print(f"Count: {len(watched)}")

watched_upper = {str(t).upper() for t in watched if t}
print(f"\nUppercase watched set: {sorted(watched_upper)}")
print(f"Count: {len(watched_upper)}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("STEP 2: Test ticker extraction on a sample article")
print("="*80)

# Simulate an article about AAPL
sample_text = """
Apple Reports Record Earnings
Apple Inc. announced record quarterly earnings today. (NASDAQ: AAPL) shares jumped 3% on the news.
The company's revenue growth exceeded expectations, with AAPL trading at new highs.
Analyst upgrade: Apple stock shows strength this quarter.
"""

print(f"\nSample text:\n{sample_text}\n")

tickers = extract_watched_tickers(sample_text, watched_upper)
print(f"extract_watched_tickers() returned: {tickers}")
print(f"Type: {type(tickers)}")

# Now do the redundant check
tickers_filtered = [t for t in tickers if t in watched_upper]
print(f"After 'if t in watched_upper': {tickers_filtered}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("STEP 3: Fetch and parse ONE feed")
print("="*80)

source = NEWS_SOURCES[0]  # CNBC Markets
print(f"\nTesting source: {source['name']}")
print(f"URL: {source['url']}")

r, reason = _fetch_feed(source)
if r is None:
    print(f"FAILED TO FETCH: {reason}")
else:
    print(f"✓ Fetched {len(r.content)} bytes")

    try:
        root = ET.fromstring(r.content)
        entries = _parse_entries(root)
        print(f"✓ Parsed {len(entries)} entries from feed")

        if entries:
            # Test first entry
            entry = entries[0]
            title = strip_html(_child_text(entry, "title"))
            url = _entry_link(entry)
            body = strip_html(_child_text(entry, "description", "summary", "content", "encoded"))
            published = parse_feed_date(_child_text(entry, "pubDate", "published", "updated", "date"))

            print(f"\n--- Entry 1 ---")
            print(f"Title: {title[:70]}")
            print(f"URL: {url}")
            print(f"Body (first 100 chars): {body[:100]}")
            print(f"Published: {published}")

            # Check article age
            cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
            if published and published < cutoff:
                print(f"[FILTERED] Article too old (published {published} < cutoff {cutoff})")
            else:
                print(f"✓ Article age OK")

            # Extract tickers
            combined_text = f"{title}. {body}"
            print(f"\nCombined text for ticker matching ({len(combined_text)} chars):")
            print(f"  {combined_text[:200]}")

            found_tickers = extract_watched_tickers(combined_text, watched_upper)
            print(f"\nextract_watched_tickers() found: {found_tickers}")

            if not found_tickers:
                print("[FILTERED] No tickers found in article")
            else:
                # Apply redundant filter
                filtered_tickers = [t for t in found_tickers if t in watched_upper]
                print(f"After redundant filter: {filtered_tickers}")

                if not filtered_tickers:
                    print("[FILTERED] Redundant check removed all tickers!")
                else:
                    print(f"✓ Would store for tickers: {filtered_tickers}")
    except Exception as e:
        print(f"ERROR parsing feed: {e}")
        import traceback
        traceback.print_exc()

print("\n" + "="*80)
print("DIAGNOSIS COMPLETE")
print("="*80)
