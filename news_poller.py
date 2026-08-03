"""
news_poller.py
GQ FinXray US — Feature 2, RSS aggregation layer.

CHANGES 2026-08-03
------------------
1. REMOVED ALL 5 REUTERS FEEDS. `feeds.reuters.com` no longer resolves --
   Reuters retired their public RSS. Every poll cycle was throwing five DNS
   errors, forever. Removed: Reuters Business, Reuters Markets, Reuters
   Technology, Reuters Health, Reuters Economy. Source list 25 -> 20.

2. BATCHED THE DEDUP LOOKUP. Previously news_exists() ran ONE Supabase SELECT
   PER ARTICLE. With 20 feeds x ~30 items that was ~600 sequential round trips
   every 60 seconds -- visible in the Railway logs as an unbroken wall of HTTP
   requests, and the single largest source of database load in the system.
   Now one query per feed covers all its URLs.

WORTH KNOWING ABOUT THIS WHOLE LAYER
------------------------------------
Measured across 14 days of live data, RSS is a poor yield source:

    CNBC          362 articles -> 12 with a ticker   (3.3%)
    Fortune       226 articles ->  7 with a ticker   (3.1%)
    IBD           175 articles -> 11 with a ticker   (6.3%)
    MarketWatch   112 articles ->  1 with a ticker   (0.9%)
    FMP           121 articles -> 92 with a ticker   (76%)

FMP wins because it is fetched per-watchlist-ticker, so it is tickered by
construction, while RSS casts a wide net and matches against a small watchlist.
As the watchlist grows RSS yield will improve.

Also note: RSS gives title + description only, averaging ~35 words. That is
NOT enough source material to summarise -- see the passthrough logic in
ai_pipeline.py, which now ships short items verbatim rather than asking a
model to expand 37 words into 90.
"""

import requests
import xml.etree.ElementTree as ET
from supabase import create_client
from datetime import datetime
import os
import time
import schedule
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HEADERS = {
    "User-Agent": "GQFinXray/1.0 contact@gqfinxray.com",
    "Accept-Encoding": "gzip, deflate"
}

# ── News sources — organised by sector ───────────────────────────────────────
NEWS_SOURCES = [

    # ── GENERAL MARKET ────────────────────────────────────────────────────────
    {
        "name": "CNBC Markets",
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "MARKET"
    },
    {
        "name": "CNBC Earnings",
        "url": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "MARKET"
    },
    {
        "name": "CNBC Investing",
        "url": "https://www.cnbc.com/id/20409666/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "MARKET"
    },
    # REMOVED: "Reuters Business"  -> https://feeds.reuters.com/reuters/businessNews
    # REMOVED: "Reuters Markets"   -> https://feeds.reuters.com/reuters/USmarketsnews
    # feeds.reuters.com no longer resolves. Reuters retired public RSS.
    {
        "name": "MarketWatch Top Stories",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories",
        "source_key": "MARKETWATCH",
        "sector": "MARKET"
    },
    {
        "name": "MarketWatch Market Pulse",
        "url": "https://feeds.marketwatch.com/marketwatch/marketpulse",
        "source_key": "MARKETWATCH",
        "sector": "MARKET"
    },
    {
        "name": "Nasdaq Original Content",
        "url": "https://www.nasdaq.com/feed/nasdaq-originals/rss.xml",
        "source_key": "NASDAQ",
        "sector": "MARKET"
    },
    {
        "name": "Investor's Business Daily",
        "url": "https://www.investors.com/feed/",
        "source_key": "IBD",
        "sector": "MARKET"
    },
    {
        "name": "Fortune Business",
        "url": "https://fortune.com/feed",
        "source_key": "FORTUNE",
        "sector": "MARKET"
    },
    {
        "name": "CNN Business",
        "url": "https://rss.cnn.com/rss/money_latest.rss",
        "source_key": "CNN",
        "sector": "MARKET"
    },

    # ── TECHNOLOGY ────────────────────────────────────────────────────────────
    {
        "name": "CNBC Technology",
        "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "TECHNOLOGY"
    },
    # REMOVED: "Reuters Technology" -> https://feeds.reuters.com/reuters/technologyNews

    # ── FINANCE & BANKING ─────────────────────────────────────────────────────
    {
        "name": "CNBC Finance",
        "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "FINANCE"
    },
    {
        "name": "MarketWatch Banking",
        "url": "https://feeds.marketwatch.com/marketwatch/financialservices",
        "source_key": "MARKETWATCH",
        "sector": "FINANCE"
    },

    # ── HEALTHCARE & PHARMA ───────────────────────────────────────────────────
    {
        "name": "CNBC Healthcare",
        "url": "https://www.cnbc.com/id/10000108/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "HEALTHCARE"
    },
    # REMOVED: "Reuters Health" -> https://feeds.reuters.com/reuters/healthNews

    # ── ENERGY ────────────────────────────────────────────────────────────────
    {
        "name": "CNBC Energy",
        "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "ENERGY"
    },

    # ── CONSUMER & RETAIL ─────────────────────────────────────────────────────
    {
        "name": "CNBC Retail",
        "url": "https://www.cnbc.com/id/10000116/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "CONSUMER"
    },

    # ── MEDIA & COMMUNICATION SERVICES ───────────────────────────────────────
    {
        "name": "CNBC Media",
        "url": "https://www.cnbc.com/id/10000110/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "COMMUNICATION"
    },

    # ── INDUSTRIALS ───────────────────────────────────────────────────────────
    {
        "name": "CNBC Industrials",
        "url": "https://www.cnbc.com/id/10000113/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "INDUSTRIALS"
    },

    # ── AUTOS ─────────────────────────────────────────────────────────────────
    {
        "name": "CNBC Autos",
        "url": "https://www.cnbc.com/id/10000101/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "CONSUMER"
    },

    # ── REAL ESTATE ───────────────────────────────────────────────────────────
    {
        "name": "CNBC Real Estate",
        "url": "https://www.cnbc.com/id/10000115/device/rss/rss.html",
        "source_key": "CNBC",
        "sector": "REAL_ESTATE"
    },

    # ── ECONOMY & MACRO ───────────────────────────────────────────────────────
    # REMOVED: "Reuters Economy" -> https://feeds.reuters.com/reuters/economicNews
    {
        "name": "MarketWatch Economy",
        "url": "https://feeds.marketwatch.com/marketwatch/economy-politics",
        "source_key": "MARKETWATCH",
        "sector": "MACRO"
    },
]

# Sector keyword map — used to tag MARKET articles with a sector if possible
SECTOR_KEYWORDS = {
    "TECHNOLOGY": ["apple", "microsoft", "nvidia", "google", "alphabet", "meta", "amazon", "semiconductor",
                   "software", "cloud", "ai", "artificial intelligence", "chip", "data center", "cybersecurity"],
    "FINANCE": ["bank", "federal reserve", "fed", "interest rate", "jpmorgan", "goldman", "morgan stanley",
                "wells fargo", "citigroup", "fintech", "insurance", "credit", "loan", "debt"],
    "HEALTHCARE": ["fda", "drug", "pharma", "biotech", "clinical trial", "pfizer", "moderna", "johnson",
                   "merck", "abbvie", "medicare", "medicaid", "hospital", "medical"],
    "ENERGY": ["oil", "gas", "crude", "exxon", "chevron", "opec", "pipeline", "refinery",
               "solar", "wind", "renewable", "coal", "lng", "energy"],
    "CONSUMER": ["retail", "consumer", "walmart", "target", "amazon", "nike", "apple",
                 "restaurant", "food", "beverage", "automobile", "auto", "tesla", "ford", "gm"],
    "INDUSTRIALS": ["boeing", "caterpillar", "defense", "manufacturing", "aerospace", "logistics",
                    "supply chain", "industrial", "infrastructure"],
    "REAL_ESTATE": ["real estate", "reit", "housing", "mortgage", "property", "construction"],
    "COMMUNICATION": ["media", "streaming", "netflix", "disney", "comcast", "verizon", "at&t",
                      "advertising", "social media", "telecom"],
    "MACRO": ["gdp", "inflation", "cpi", "unemployment", "federal reserve", "interest rate",
              "treasury", "recession", "economic", "fiscal policy"],
}

# Fallback universe when the watchlist table is empty or unreachable.
WATCH_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "INTC", "CRM", "ORCL", "IBM", "UBER", "LYFT",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "PYPL",
    "JNJ", "PFE", "MRNA", "ABBV", "UNH", "CVS",
    "XOM", "CVX", "COP", "SLB",
    "WMT", "TGT", "COST", "HD", "LOW",
    "BA", "LMT", "RTX", "NOC", "GE",
    "DIS", "CMCSA", "T", "VZ",
    "SPY", "QQQ", "IWM", "GLD", "SLV"
]

# The watchlist changes rarely but was being re-fetched once PER FEED. Cached
# for the duration of a poll cycle.
_watchlist_cache = {"at": 0.0, "tickers": []}
_WATCHLIST_TTL = 300


def get_watched_tickers_from_db():
    """Tickers users are subscribed to. Cached for 5 minutes."""
    now = time.time()
    if _watchlist_cache["tickers"] and (now - _watchlist_cache["at"]) < _WATCHLIST_TTL:
        return _watchlist_cache["tickers"]
    try:
        result = supabase.table("watchlists").select("ticker").execute()
        tickers = sorted({r["ticker"] for r in result.data if r.get("ticker")})
        tickers = tickers or WATCH_TICKERS
    except Exception as e:
        print(f"[ERROR] Failed to get tickers from DB: {e}")
        tickers = WATCH_TICKERS
    _watchlist_cache.update({"at": now, "tickers": tickers})
    return tickers


def extract_tickers_from_text(text, watched_tickers):
    """Find any watched tickers mentioned in the article text."""
    found = []
    text_upper = text.upper()
    for ticker in watched_tickers:
        if re.search(r'\b' + re.escape(ticker) + r'\b', text_upper):
            found.append(ticker)
    return found


def detect_sector_from_text(text):
    text_lower = text.lower()
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[sector] = score
    return max(scores, key=scores.get) if scores else "MARKET"


def existing_urls(urls):
    """Which of these URLs are already stored? ONE query for the whole batch.

    Replaces a per-article SELECT that produced ~600 sequential Supabase round
    trips per 60-second cycle.
    """
    if not urls:
        return set()
    found = set()
    CHUNK = 100  # keep the URL filter well inside PostgREST's query-length limit
    url_list = list(urls)
    for i in range(0, len(url_list), CHUNK):
        chunk = url_list[i:i + CHUNK]
        try:
            result = supabase.table("raw_filings").select("filing_url") \
                .in_("filing_url", chunk).execute()
            found.update(r["filing_url"] for r in result.data if r.get("filing_url"))
        except Exception as e:
            print(f"[ERROR] Batch dedup lookup failed: {e}")
            # Fail closed: treat the chunk as already-seen rather than risk
            # re-inserting duplicates on a transient DB error.
            found.update(chunk)
    return found


def store_news(source_key, ticker, title, summary, url, published_at, sector="MARKET"):
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
                "source_name": source_key,
                "sector": sector
            }
        }).execute()
        print(f"[STORED] {source_key} | {sector} | {ticker} | {title[:60]}...")
    except Exception as e:
        if "duplicate" not in str(e).lower() and "unique" not in str(e).lower():
            print(f"[ERROR] Failed to store news: {e}")


def parse_rss_date(date_str):
    if not date_str:
        return datetime.now().isoformat()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(date_str.strip(), fmt).isoformat()
        except Exception:
            continue
    return datetime.now().isoformat()


def _parse_items(root):
    items = root.findall(".//item")
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall("atom:entry", ns)
    return items


def poll_news_source(source):
    """Poll a single RSS source."""
    name, url = source["name"], source["url"]
    source_key = source["source_key"]
    sector = source.get("sector", "MARKET")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] {name} returned {r.status_code}")
            return 0

        items = _parse_items(ET.fromstring(r.content))
        if not items:
            print(f"[{name}] No items found in feed")
            return 0

        # Extract everything first, then do ONE dedup query for the batch.
        parsed = []
        for item in items:
            title_elem = item.find("title")
            title = title_elem.text if title_elem is not None else ""
            if not title:
                continue

            link_elem = item.find("link")
            article_url = ""
            if link_elem is not None:
                article_url = link_elem.text or link_elem.attrib.get("href", "")
            if not article_url:
                continue

            desc_elem = item.find("description")
            if desc_elem is None:
                desc_elem = item.find("summary")
            summary = desc_elem.text if desc_elem is not None else ""
            if summary:
                summary = re.sub(r'<[^>]+>', '', summary).strip()

            date_elem = item.find("pubDate")
            if date_elem is None:
                date_elem = item.find("updated")
            published_at = parse_rss_date(date_elem.text if date_elem is not None else "")

            parsed.append((article_url, title, summary, published_at))

        if not parsed:
            return 0

        seen = existing_urls({p[0] for p in parsed})
        fresh = [p for p in parsed if p[0] not in seen]
        if not fresh:
            return 0

        watched_tickers = get_watched_tickers_from_db()
        new_count = 0

        for article_url, title, summary, published_at in fresh:
            full_text = f"{title} {summary}"
            found_tickers = extract_tickers_from_text(full_text, watched_tickers)
            article_sector = detect_sector_from_text(full_text) if sector == "MARKET" else sector

            if not found_tickers:
                store_news(source_key, "MARKET", title, summary,
                           article_url, published_at, article_sector)
            else:
                for ticker in found_tickers:
                    store_news(source_key, ticker, title, summary,
                               article_url, published_at, article_sector)
            new_count += 1

        return new_count

    except Exception as e:
        print(f"[ERROR] Failed to poll {name}: {e}")
        return 0


def poll_all_news():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling {len(NEWS_SOURCES)} news sources...")
    total = 0
    sector_counts = {}
    for source in NEWS_SOURCES:
        count = poll_news_source(source)
        if count > 0:
            sec = source.get("sector", "MARKET")
            sector_counts[sec] = sector_counts.get(sec, 0) + count
            print(f"  [{source['name']}] {count} new articles")
        total += count

    if total == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No new articles.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Total: {total} new articles.")
        for sec, count in sector_counts.items():
            print(f"  {sec}: {count}")


def run_news_poller():
    poll_all_news()
    schedule.every(60).seconds.do(poll_all_news)
    print(f"\n[RUNNING] News poller started — {len(NEWS_SOURCES)} sources.\n")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    run_news_poller()
