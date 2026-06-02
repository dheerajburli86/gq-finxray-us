import requests
import xml.etree.ElementTree as ET
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import os
import time
import schedule
import re

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HEADERS = {
    "User-Agent": "GQFinXray/1.0 contact@gqfinxray.com",
    "Accept-Encoding": "gzip, deflate"
}

# ── News sources ──────────────────────────────────────────────────────────────
NEWS_SOURCES = [
    {
        "name": "CNBC",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
        "source_key": "CNBC"
    },
    {
        "name": "Bloomberg",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "source_key": "BLOOMBERG"
    },
    {
        "name": "MarketWatch",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories",
        "source_key": "MARKETWATCH"
    },
    
]

# US stock tickers to watch for in news articles
# This list will grow as users add tickers via the bot
WATCH_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "INTC", "CRM", "ORCL", "IBM", "UBER", "LYFT",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "PYPL",
    "JNJ", "PFE", "MRNA", "ABBV", "UNH", "CVS",
    "XOM", "CVX", "COP", "SLB",
    "WMT", "TGT", "COST", "AMZN", "HD", "LOW",
    "BA", "LMT", "RTX", "NOC", "GE",
    "NFLX", "DIS", "CMCSA", "T", "VZ",
    "SPY", "QQQ", "IWM", "GLD", "SLV"
]

def get_watched_tickers_from_db():
    """Get all tickers users are subscribed to from the database."""
    try:
        result = supabase.table("watchlists") \
            .select("ticker") \
            .execute()
        tickers = list(set([r["ticker"] for r in result.data if r.get("ticker")]))
        return tickers if tickers else WATCH_TICKERS
    except Exception as e:
        print(f"[ERROR] Failed to get tickers from DB: {e}")
        return WATCH_TICKERS

def extract_tickers_from_text(text, watched_tickers):
    """Find any watched tickers mentioned in the article text."""
    found = []
    text_upper = text.upper()
    for ticker in watched_tickers:
        # Match ticker as whole word — avoids matching IT inside TWITTER etc
        pattern = r'\b' + re.escape(ticker) + r'\b'
        if re.search(pattern, text_upper):
            found.append(ticker)
    return found

def news_exists(url):
    """Check if we already stored this news article."""
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("filing_url", url) \
            .execute()
        return len(result.data) > 0
    except Exception as e:
        print(f"[ERROR] DB check failed: {e}")
        return False

def store_news(source_key, ticker, title, summary, url, published_at):
    """Store news article in raw_filings table."""
    try:
        supabase.table("raw_filings").insert({
            "source": source_key,
            "filing_type": "NEWS",
            "ticker": ticker,
            "company_name": ticker,
            "raw_text": f"{title}\n\n{summary}",
            "filing_url": url,
            "filed_at": published_at,
            "status": "PENDING",
            "extra": {
                "title": title,
                "source_name": source_key
            }
        }).execute()
        print(f"[STORED] NEWS — {source_key} | {ticker} | {title[:60]}... → PENDING")
    except Exception as e:
        print(f"[ERROR] Failed to store news: {e}")

def parse_rss_date(date_str):
    """Parse RSS date formats into ISO format."""
    if not date_str:
        return datetime.now().isoformat()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).isoformat()
        except:
            continue
    return datetime.now().isoformat()

def poll_news_source(source):
    """Poll a single news RSS source."""
    name = source["name"]
    url = source["url"]
    source_key = source["source_key"]

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] {name} returned {r.status_code}")
            return 0

        root = ET.fromstring(r.content)

        # Handle both RSS and Atom formats
        items = root.findall(".//item")
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall("atom:entry", ns)

        if not items:
            print(f"[{name}] No items found in feed")
            return 0

        watched_tickers = get_watched_tickers_from_db()
        new_count = 0

        for item in items:
            # Extract title
            title_elem = item.find("title")
            title = title_elem.text if title_elem is not None else ""
            if not title:
                continue

            # Extract URL
            link_elem = item.find("link")
            article_url = ""
            if link_elem is not None:
                article_url = link_elem.text or link_elem.attrib.get("href", "")
            if not article_url:
                continue

            # Skip if already stored
            if news_exists(article_url):
                continue

            # Extract description/summary
            desc_elem = item.find("description")
            if desc_elem is None:
                desc_elem = item.find("summary")
            summary = desc_elem.text if desc_elem is not None else ""

            # Clean HTML tags from summary
            if summary:
                summary = re.sub(r'<[^>]+>', '', summary).strip()

            # Extract publish date
            date_elem = item.find("pubDate")
            if date_elem is None:
                date_elem = item.find("updated")
            published_at = parse_rss_date(date_elem.text if date_elem is not None else "")

            # Find tickers mentioned in title + summary
            full_text = f"{title} {summary}"
            found_tickers = extract_tickers_from_text(full_text, watched_tickers)

            if not found_tickers:
                # No watched tickers mentioned — store as general market news
                # with MARKET as ticker so it still gets processed
                store_news(
                    source_key=source_key,
                    ticker="MARKET",
                    title=title,
                    summary=summary,
                    url=article_url,
                    published_at=published_at
                )
            else:
                # Store one record per ticker mentioned
                for ticker in found_tickers:
                    store_news(
                        source_key=source_key,
                        ticker=ticker,
                        title=title,
                        summary=summary,
                        url=article_url,
                        published_at=published_at
                    )

            new_count += 1
            time.sleep(0.1)

        return new_count

    except Exception as e:
        print(f"[ERROR] Failed to poll {name}: {e}")
        return 0

def poll_all_news():
    """Poll all news sources."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling news sources...")
    total = 0
    for source in NEWS_SOURCES:
        count = poll_news_source(source)
        if count > 0:
            print(f"[{source['name']}] {count} new articles stored")
        total += count
    if total == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No new news articles.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Total: {total} new articles stored.")

def run_news_poller():
    poll_all_news()
    schedule.every(60).seconds.do(poll_all_news)
    print("\n[RUNNING] News poller started — checking every 60 seconds.\n")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    run_news_poller()