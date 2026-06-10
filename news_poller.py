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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate"
}

NEWS_SOURCES = [
    {"name": "CNBC Markets", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147", "source_key": "CNBC"},
    {"name": "Bloomberg", "url": "https://feeds.bloomberg.com/markets/news.rss", "source_key": "BLOOMBERG"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories", "source_key": "MARKETWATCH"},
    {"name": "Reuters", "url": "https://news.google.com/rss/search?q=reuters+business+markets&ceid=US:en&hl=en-US&gl=US", "source_key": "REUTERS"},
]

WATCH_TICKERS = [
    "AAPL", "NVDA", "TSLA", "MSFT", "META",
    "AMZN", "GOOGL", "JPM", "NFLX", "AMD"
]

MARKET_KEYWORDS = [
    "wall street", "s&p 500", "nasdaq", "dow jones", "stock market",
    "federal reserve", "fed rate", "interest rate", "inflation",
    "earnings season", "bull market", "bear market", "market rally",
    "market selloff", "market close", "market open", "equity markets",
    "us markets", "us stocks", "us equities",
    "rate hike", "rate cut", "jerome powell", "fomc", "treasury yields",
    "bond yields", "risk appetite", "safe haven", "market volatility",
    "ipo", "initial public offering", "record high", "record low",
    "dow hits", "s&p hits", "nasdaq hits", "wall st", "nyse", "trading day",
    "morning bid", "week ahead", "market check", "market wrap"
]

NON_US_KEYWORDS = [
    # India
    "state bank of india", "sun pharmaceutical", "sun pharma",
    "reliance industries", "infosys", "wipro", "hdfc bank",
    "icici bank", "bajaj finance", "mahindra", "adani group",
    "sensex", "nifty 50", "reserve bank of india", "rbi rule",
    "rbi policy", "sebi", "india stock", "india fund",
    "india market", "india boost", "india lng", "india buying",
    "india inflow", "india dollar", "india equity",
    # China
    "alibaba group", "tencent holdings", "baidu inc", "xiaomi corp",
    "huawei technologies", "sinopec", "petrochina",
    "shanghai composite", "hang seng index", "hkex",
    "peoples bank of china", "pboc rate",
    # Japan
    "bank of japan rate", "nikkei 225",
    # Europe only
    "ftse 100 index", "dax index", "cac 40 index",
    # Middle East only
    "suez canal",
]

US_ANCHOR_KEYWORDS = [
    "nasdaq", "nyse", "s&p 500", "dow jones", "wall street",
    "federal reserve", "sec filing", "us stock", "american stock",
    "new york stock exchange", "us market", "us listed",
    "us shares", "us investors", "us economy", "us gdp",
    "us dollar", "treasury", "fed funds"
]

def is_us_relevant(title, summary):
    text = f"{title} {summary}".lower()
    for kw in US_ANCHOR_KEYWORDS:
        if kw in text:
            return True
    non_us_hits = sum(1 for kw in NON_US_KEYWORDS if kw in text)
    if non_us_hits >= 2:
        return False
    return True

def get_watched_tickers_from_db():
    try:
        result = supabase.table("watchlists").select("ticker").execute()
        tickers = list(set([r["ticker"] for r in result.data if r.get("ticker")]))
        combined = list(set(tickers + WATCH_TICKERS))
        return combined if combined else WATCH_TICKERS
    except Exception as e:
        print(f"[ERROR] Failed to get tickers from DB: {e}")
        return WATCH_TICKERS

def extract_tickers_from_text(text, watched_tickers):
    found = []
    text_upper = text.upper()
    for ticker in watched_tickers:
        pattern = r'\b' + re.escape(ticker) + r'\b'
        if re.search(pattern, text_upper):
            found.append(ticker)
    return found

def tag_market_article(title, summary):
    text = f"{title} {summary}".lower()
    for keyword in MARKET_KEYWORDS:
        if keyword in text:
            return True
    return False

def extract_image_url(item, description_text):
    raw = ET.tostring(item, encoding='unicode')
    content_match = re.search(r'<(?:ns\d+|media):content[^>]+url=["\']([^"\']+)["\']', raw)
    if content_match:
        url = content_match.group(1)
        if url.startswith("http"):
            return url
    thumb_match = re.search(r'<(?:ns\d+|media):thumbnail[^>]+url=["\']([^"\']+)["\']', raw)
    if thumb_match:
        url = thumb_match.group(1)
        if url.startswith("http"):
            return url
    namespaces = {"media": "http://search.yahoo.com/mrss/", "content": "http://purl.org/rss/1.0/modules/content/"}
    for ns_key, ns_url in namespaces.items():
        media_content = item.find(f"{{{ns_url}}}content")
        if media_content is not None:
            url = media_content.attrib.get("url", "")
            if url and url.startswith("http"):
                return url
        media_thumb = item.find(f"{{{ns_url}}}thumbnail")
        if media_thumb is not None:
            url = media_thumb.attrib.get("url", "")
            if url and url.startswith("http"):
                return url
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url", "")
        mime = enclosure.attrib.get("type", "")
        if url and "image" in mime:
            return url
    if description_text:
        img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description_text, re.IGNORECASE)
        if img_match:
            url = img_match.group(1)
            if url.startswith("http"):
                return url
    if description_text:
        url_match = re.search(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp|gif)', description_text, re.IGNORECASE)
        if url_match:
            return url_match.group(0)
    return ""

def news_exists(url):
    try:
        result = supabase.table("raw_filings").select("id").eq("filing_url", url).execute()
        return len(result.data) > 0
    except Exception as e:
        print(f"[ERROR] DB check failed: {e}")
        return False

def store_news(source_key, ticker, title, summary, url, published_at, image_url=""):
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
            "extra": {"title": title, "source_name": source_key, "image_url": image_url}
        }).execute()
        print(f"[STORED] NEWS — {source_key} | {ticker} | {title[:60]}... → PENDING")
    except Exception as e:
        if "duplicate key" not in str(e).lower():
            print(f"[ERROR] Failed to store news: {e}")

def parse_rss_date(date_str):
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

def fetch_source_items(source):
    name = source["name"]
    url = source["url"]
    source_key = source["source_key"]
    items_to_store = []
    seen_urls = set()

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return items_to_store

        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall("atom:entry", ns)

        watched_tickers = get_watched_tickers_from_db()

        for item in items:
            title_elem = item.find("title")
            title = title_elem.text if title_elem is not None else ""
            if not title:
                continue

            if " - Reuters" in title:
                title = title.replace(" - Reuters", "").strip()
            elif " - " in title and source_key == "REUTERS":
                title = title.rsplit(" - ", 1)[0].strip()

            link_elem = item.find("link")
            article_url = ""
            if link_elem is not None:
                article_url = link_elem.text or link_elem.attrib.get("href", "")
            if not article_url:
                continue

            if article_url in seen_urls or news_exists(article_url):
                continue
            seen_urls.add(article_url)

            desc_elem = item.find("description")
            if desc_elem is None:
                desc_elem = item.find("summary")
            raw_description = desc_elem.text if desc_elem is not None else ""
            summary = re.sub(r'<[^>]+>', '', raw_description).strip() if raw_description else ""

            # Filter non-US articles early
            if not is_us_relevant(title, summary):
                print(f"[FILTERED] Non-US: {title[:60]}")
                continue

            image_url = extract_image_url(item, raw_description)

            date_elem = item.find("pubDate")
            if date_elem is None:
                date_elem = item.find("updated")
            published_at = parse_rss_date(date_elem.text if date_elem is not None else "")

            full_text = f"{title} {summary}"
            found_tickers = extract_tickers_from_text(full_text, watched_tickers)

            if found_tickers:
                items_to_store.append({
                    "source_key": source_key,
                    "ticker": found_tickers[0],
                    "title": title,
                    "summary": summary,
                    "url": article_url,
                    "published_at": published_at,
                    "image_url": image_url
                })
            elif tag_market_article(title, summary):
                items_to_store.append({
                    "source_key": source_key,
                    "ticker": "SPY",
                    "title": title,
                    "summary": summary,
                    "url": article_url,
                    "published_at": published_at,
                    "image_url": image_url
                })
            else:
                items_to_store.append({
                    "source_key": source_key,
                    "ticker": "MARKET",
                    "title": title,
                    "summary": summary,
                    "url": article_url,
                    "published_at": published_at,
                    "image_url": image_url
                })

    except Exception as e:
        print(f"[ERROR] Failed to fetch {name}: {e}")

    return items_to_store

def poll_all_news():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Polling news sources...")
    all_source_items = []
    source_counts = {}

    for source in NEWS_SOURCES:
        items = fetch_source_items(source)
        if items:
            all_source_items.append(items)
            source_counts[source["source_key"]] = source_counts.get(source["source_key"], 0) + len(items)

    if not all_source_items:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No new news articles.")
        return

    total = 0
    max_len = max(len(s) for s in all_source_items)
    for i in range(max_len):
        for source_items in all_source_items:
            if i < len(source_items):
                item = source_items[i]
                store_news(
                    source_key=item["source_key"],
                    ticker=item["ticker"],
                    title=item["title"],
                    summary=item["summary"],
                    url=item["url"],
                    published_at=item["published_at"],
                    image_url=item["image_url"]
                )
                total += 1
                time.sleep(0.05)

    for source_key, count in source_counts.items():
        print(f"[{source_key}] {count} new articles")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Total: {total} new articles stored — interleaved.")

def run_news_poller():
    poll_all_news()
    schedule.every(60).seconds.do(poll_all_news)
    print("\n[RUNNING] News poller started — checking every 60 seconds.\n")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    run_news_poller()