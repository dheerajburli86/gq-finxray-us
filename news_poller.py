import requests
import logging
import logging.handlers

# Rotating logger — keeps last 5MB of logs, 3 backup files
logger = logging.getLogger("news_poller")
if not logger.handlers:
    handler = logging.handlers.RotatingFileHandler(
        "logs/news_poller.log", maxBytes=5*1024*1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
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

# ── News sources — organised by sector ───────────────────────────────────────
# Each source has a sector tag so alerts can be routed to sector-relevant users
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
    {
        "name": "Reuters Business",
        "url": "https://feeds.reuters.com/reuters/businessNews",
        "source_key": "REUTERS",
        "sector": "MARKET"
    },
    {
        "name": "Reuters Markets",
        "url": "https://feeds.reuters.com/reuters/USmarketsnews",
        "source_key": "REUTERS",
        "sector": "MARKET"
    },
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
    {
        "name": "Reuters Technology",
        "url": "https://feeds.reuters.com/reuters/technologyNews",
        "source_key": "REUTERS",
        "sector": "TECHNOLOGY"
    },

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
    {
        "name": "Reuters Health",
        "url": "https://feeds.reuters.com/reuters/healthNews",
        "source_key": "REUTERS",
        "sector": "HEALTHCARE"
    },

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
    {
        "name": "Reuters Economy",
        "url": "https://feeds.reuters.com/reuters/economicNews",
        "source_key": "REUTERS",
        "sector": "MACRO"
    },
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

# US stock tickers to watch for in news articles
WATCH_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "NFLX", "AMD", "INTC", "CRM", "ORCL", "IBM", "UBER", "LYFT",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "PYPL",
    "JNJ", "PFE", "MRNA", "ABBV", "UNH", "CVS",
    "XOM", "CVX", "COP", "SLB",
    "WMT", "TGT", "COST", "HD", "LOW",
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
        pattern = r'\b' + re.escape(ticker) + r'\b'
        if re.search(pattern, text_upper):
            found.append(ticker)
    return found

def detect_sector_from_text(text):
    """Detect sector from article text using keyword matching."""
    text_lower = text.lower()
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[sector] = score
    if scores:
        return max(scores, key=scores.get)
    return "MARKET"

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

def store_news(source_key, ticker, title, summary, url, published_at, sector="MARKET"):
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
                "source_name": source_key,
                "sector": sector
            }
        }).execute()
        print(f"[STORED] {source_key} | {sector} | {ticker} | {title[:60]}...")
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
    sector = source.get("sector", "MARKET")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] {name} returned {r.status_code}")
            return 0

        root = ET.fromstring(r.content)

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

            if news_exists(article_url):
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

            full_text = f"{title} {summary}"
            found_tickers = extract_tickers_from_text(full_text, watched_tickers)

            # If sector is MARKET, try to detect more specific sector
            article_sector = sector
            if sector == "MARKET":
                article_sector = detect_sector_from_text(full_text)

            if not found_tickers:
                # No ticker found — skip entirely to avoid unnecessary AI pipeline costs
                logger.debug(f"[NEWS] No ticker found in: {title[:60]} — skipping")
                continue
            else:
                for ticker in found_tickers:
                    store_news(
                        source_key=source_key,
                        ticker=ticker,
                        title=title,
                        summary=summary,
                        url=article_url,
                        published_at=published_at,
                        sector=article_sector
                    )

            new_count += 1
            time.sleep(0.1)

        return new_count

    except Exception as e:
        print(f"[ERROR] Failed to poll {name}: {e}")
        return 0

def poll_all_news():
    """Poll all news sources."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling {len(NEWS_SOURCES)} news sources...")
    total = 0
    sector_counts = {}
    for source in NEWS_SOURCES:
        count = poll_news_source(source)
        if count > 0:
            sector = source.get("sector", "MARKET")
            sector_counts[sector] = sector_counts.get(sector, 0) + count
            print(f"  [{source['name']}] {count} new articles")
        total += count

    if total == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No new articles.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Total: {total} new articles.")
        for sector, count in sector_counts.items():
            print(f"  {sector}: {count}")

def run_news_poller():
    poll_all_news()
    schedule.every(60).seconds.do(poll_all_news)
    print(f"\n[RUNNING] News poller started — {len(NEWS_SOURCES)} sources across all sectors, checking every 60 seconds.\n")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    run_news_poller()
