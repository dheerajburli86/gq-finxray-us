import os
import time
import requests
import schedule
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
EODHD_API_KEY = os.getenv("EODHD_API_KEY")
BASE_URL = "https://eodhd.com/api"

# ── Sector tags supported by EODHD ───────────────────────────────────────────
EODHD_SECTORS = [
    "TECHNOLOGY",
    "FINANCIAL",
    "HEALTH_CARE",
    "ENERGY",
    "CONSUMER_DISCRETIONARY",
    "INDUSTRIALS",
    "REAL_ESTATE",
    "COMMUNICATION_SERVICES",
    "UTILITIES",
    "MATERIALS",
]

# ── Sector display names ──────────────────────────────────────────────────────
SECTOR_LABELS = {
    "TECHNOLOGY": "Technology",
    "FINANCIAL": "Finance & Banking",
    "HEALTH_CARE": "Healthcare",
    "ENERGY": "Energy",
    "CONSUMER_DISCRETIONARY": "Consumer",
    "INDUSTRIALS": "Industrials",
    "REAL_ESTATE": "Real Estate",
    "COMMUNICATION_SERVICES": "Communication",
    "UTILITIES": "Utilities",
    "MATERIALS": "Materials",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_watched_tickers():
    """Get all tickers users are subscribed to from Supabase watchlists."""
    try:
        result = supabase.table("watchlists").select("ticker").execute()
        tickers = list(set([r["ticker"] for r in result.data if r.get("ticker")]))
        return tickers if tickers else []
    except Exception as e:
        print(f"[EODHD] Failed to get watchlist tickers: {e}")
        return []

def eodhd_get(endpoint, params={}):
    """Make a GET request to EODHD API."""
    params["api_token"] = EODHD_API_KEY
    params["fmt"] = "json"
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[EODHD] {endpoint} returned {r.status_code}: {r.text[:100]}")
            return None
    except Exception as e:
        print(f"[EODHD] Request failed for {endpoint}: {e}")
        return None

def news_already_stored(article_id):
    """Check if this EODHD news article is already in Supabase by article ID."""
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("filing_url", str(article_id)) \
            .execute()
        return len(result.data) > 0
    except Exception as e:
        print(f"[EODHD] Dedup check failed: {e}")
        return False

def store_news(ticker, title, content, url, published_at, source, sector, sentiment):
    """Store EODHD news article into raw_filings as PENDING."""
    try:
        # Use URL as the filing_url for deduplication
        supabase.table("raw_filings").insert({
            "source": "EODHD",
            "filing_type": "NEWS",
            "ticker": ticker,
            "company_name": ticker,
            "raw_text": f"{title}\n\n{content[:3000]}",
            "filing_url": url,
            "filed_at": published_at,
            "status": "PENDING",
            "extra": {
                "title": title,
                "source_name": source,
                "sector": sector,
                "sentiment": sentiment,
                "eodhd_news": True
            }
        }).execute()
        print(f"[EODHD NEWS] {sector} | {ticker} | {sentiment} | {title[:60]}...")
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            pass  # already stored
        else:
            print(f"[EODHD] Failed to store news: {e}")

def store_alert(ticker, summary, impact, filing_type, extra):
    """Store a pre-built alert directly into alerts table."""
    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "EODHD",
            "filing_type": filing_type,
            "extra": extra,
            "delivered": False
        }).execute()
        print(f"[EODHD ALERT] {impact} — {ticker}: {summary[:80]}...")
    except Exception as e:
        print(f"[EODHD] Failed to store alert: {e}")

def earnings_alert_already_sent(ticker, report_date):
    """Check if we already sent an earnings heads-up for this ticker + date."""
    try:
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "EARNINGS_CALENDAR") \
            .eq("extra->>report_date", str(report_date)) \
            .execute()
        return len(result.data) > 0
    except:
        return False

def insider_already_stored(ticker, transaction_date, insider_name, shares):
    """Check if this insider transaction is already stored."""
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "INSIDER_EODHD") \
            .eq("extra->>transaction_date", str(transaction_date)) \
            .eq("extra->>insider_name", str(insider_name)) \
            .execute()
        return len(result.data) > 0
    except:
        return False

# ── 1. Ticker News ────────────────────────────────────────────────────────────
def poll_ticker_news(tickers):
    """
    Fetch news for each watchlisted ticker from EODHD.
    Every article is already tagged to a specific stock — no keyword matching needed.
    """
    if not tickers:
        return 0

    total = 0
    for ticker in tickers:
        eodhd_symbol = f"{ticker}.US"
        data = eodhd_get("news", {"s": eodhd_symbol, "limit": 10, "offset": 0})
        if not data or not isinstance(data, list):
            continue

        for article in data:
            url = article.get("link", "") or article.get("url", "")
            if not url:
                continue
            if news_already_stored(url):
                continue

            title = article.get("title", "")
            content = article.get("content", "") or article.get("summary", "")
            published_at = article.get("date", datetime.now(timezone.utc).isoformat())
            source = article.get("source", "EODHD")

            # Sentiment from EODHD — polarity score -1 to 1
            sentiment_data = article.get("sentiment", {}) or {}
            polarity = sentiment_data.get("polarity", 0) if isinstance(sentiment_data, dict) else 0
            if polarity > 0.1:
                sentiment = "POSITIVE"
            elif polarity < -0.1:
                sentiment = "NEGATIVE"
            else:
                sentiment = "NEUTRAL"

            # Tags contain sector info
            tags = article.get("tags", []) or []
            sector = "MARKET"
            for tag in tags:
                tag_upper = str(tag).upper().replace(" ", "_")
                if tag_upper in EODHD_SECTORS:
                    sector = tag_upper
                    break

            store_news(
                ticker=ticker,
                title=title,
                content=content,
                url=url,
                published_at=published_at,
                source=source,
                sector=sector,
                sentiment=sentiment
            )
            total += 1
            time.sleep(0.05)

        time.sleep(0.2)  # rate limit between tickers

    return total

# ── 2. Sector News ────────────────────────────────────────────────────────────
def poll_sector_news():
    """
    Fetch news by sector from EODHD.
    Covers market-wide sector stories even when no specific watched ticker is named.
    """
    total = 0
    for sector in EODHD_SECTORS:
        data = eodhd_get("news", {"t": sector, "limit": 10})
        if not data or not isinstance(data, list):
            continue

        for article in data:
            url = article.get("link", "") or article.get("url", "")
            if not url:
                continue
            if news_already_stored(url):
                continue

            title = article.get("title", "")
            content = article.get("content", "") or article.get("summary", "")
            published_at = article.get("date", datetime.now(timezone.utc).isoformat())
            source = article.get("source", "EODHD")

            # Extract tickers mentioned in article tags
            tags = article.get("tags", []) or []
            tickers_in_article = [
                t.replace(".US", "")
                for t in tags
                if isinstance(t, str) and t.endswith(".US")
            ]

            sentiment_data = article.get("sentiment", {}) or {}
            polarity = sentiment_data.get("polarity", 0) if isinstance(sentiment_data, dict) else 0
            if polarity > 0.1:
                sentiment = "POSITIVE"
            elif polarity < -0.1:
                sentiment = "NEGATIVE"
            else:
                sentiment = "NEUTRAL"

            if tickers_in_article:
                # Store one record per tagged ticker
                for ticker in tickers_in_article[:5]:  # cap at 5 tickers per article
                    store_news(
                        ticker=ticker,
                        title=title,
                        content=content,
                        url=url,
                        published_at=published_at,
                        source=source,
                        sector=sector,
                        sentiment=sentiment
                    )
            else:
                # Store as sector-level MARKET article
                store_news(
                    ticker="MARKET",
                    title=title,
                    content=content,
                    url=url,
                    published_at=published_at,
                    source=source,
                    sector=sector,
                    sentiment=sentiment
                )
            total += 1
            time.sleep(0.05)

        time.sleep(0.3)

    return total

# ── 3. Earnings Calendar ──────────────────────────────────────────────────────
def poll_earnings_calendar(tickers):
    """
    Check upcoming earnings for watchlisted stocks.
    Sends a 24-hour heads-up alert before a company reports earnings.
    """
    if not tickers:
        return 0

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build comma-separated symbols list for EODHD
    symbols = ",".join([f"{t}.US" for t in tickers[:50]])  # EODHD cap
    data = eodhd_get("calendar/earnings", {
        "from": today,
        "to": day_after,
        "symbols": symbols
    })

    if not data or not isinstance(data, dict):
        return 0

    earnings_list = data.get("earnings", [])
    count = 0

    for item in earnings_list:
        code = item.get("code", "")
        ticker = code.replace(".US", "")
        report_date = item.get("report_date", "")
        timing = item.get("before_after_market", "") or "Market Hours"
        eps_estimate = item.get("estimate")
        currency = item.get("currency", "USD")

        # Only alert for tomorrow's earnings
        if report_date != tomorrow:
            continue

        if ticker not in tickers:
            continue

        if earnings_alert_already_sent(ticker, report_date):
            continue

        # Format timing
        if timing == "BeforeMarket":
            timing_str = "Before Market Open"
        elif timing == "AfterMarket":
            timing_str = "After Market Close"
        else:
            timing_str = "During Market Hours"

        # Build summary
        eps_line = f"Analyst EPS Estimate: {currency} {eps_estimate}" if eps_estimate else "No EPS estimate available"
        summary = (
            f"{ticker} is reporting earnings tomorrow ({report_date}), {timing_str}. "
            f"{eps_line}. Watch for potential volatility around the announcement."
        )

        store_alert(
            ticker=ticker,
            summary=summary,
            impact="HIGH",
            filing_type="EARNINGS_CALENDAR",
            extra={
                "report_date": report_date,
                "timing": timing_str,
                "eps_estimate": eps_estimate,
                "currency": currency,
                "source": "EODHD Calendar"
            }
        )
        count += 1

    return count

# ── 4. Insider Transactions ───────────────────────────────────────────────────
def poll_insider_transactions(tickers):
    """
    Fetch insider buy/sell transactions for watchlisted stocks.
    Complements the existing SEC Form 4 EDGAR poller with structured EODHD data.
    Only processes transactions from the last 7 days.
    """
    if not tickers:
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    total = 0

    for ticker in tickers:
        data = eodhd_get("insider-transactions", {
            "code": f"{ticker}.US",
            "limit": 10
        })
        if not data or not isinstance(data, list):
            continue

        for txn in data:
            txn_date = txn.get("date", "") or txn.get("transactionDate", "")

            # Skip old transactions
            if txn_date and txn_date < cutoff:
                continue

            insider_name = txn.get("ownerName", "") or txn.get("insider_name", "Unknown")
            transaction_type = (txn.get("transactionCode", "") or "").upper()
            shares = txn.get("numberOfShares", 0) or txn.get("shares", 0)
            price = txn.get("price", 0)
            value = txn.get("value", 0) or (float(shares or 0) * float(price or 0))
            role = txn.get("ownerRelationship", "") or txn.get("role", "")

            # Map transaction codes to readable labels
            if transaction_type in ("P", "BUY"):
                action = "BUY"
                action_emoji = "🟢"
                impact = "HIGH"
            elif transaction_type in ("S", "SELL"):
                action = "SELL"
                action_emoji = "🔴"
                impact = "MEDIUM"
            else:
                continue  # skip grants, options, etc.

            if insider_already_stored(ticker, txn_date, insider_name, shares):
                continue

            # Format value
            value_str = f"${value:,.0f}" if value else "N/A"
            shares_str = f"{int(shares):,}" if shares else "N/A"
            price_str = f"${float(price):.2f}" if price else "N/A"

            summary = (
                f"{insider_name} ({role}) {action}S {shares_str} shares of {ticker} "
                f"at {price_str} per share. Total value: {value_str}."
            )

            # Store as raw filing for AI pipeline to process
            try:
                supabase.table("raw_filings").insert({
                    "source": "EODHD",
                    "filing_type": "INSIDER_EODHD",
                    "ticker": ticker,
                    "company_name": ticker,
                    "raw_text": summary,
                    "filing_url": f"eodhd_insider_{ticker}_{txn_date}_{insider_name.replace(' ','_')}",
                    "filed_at": txn_date,
                    "status": "PENDING",
                    "extra": {
                        "insider_name": insider_name,
                        "transaction_type": action,
                        "transaction_emoji": action_emoji,
                        "shares": shares_str,
                        "price": price_str,
                        "value": value_str,
                        "role": role,
                        "transaction_date": txn_date,
                        "source": "EODHD"
                    }
                }).execute()
                print(f"[EODHD INSIDER] {action_emoji} {ticker} — {insider_name} {action}S {shares_str} shares @ {price_str}")
                total += 1
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    print(f"[EODHD] Failed to store insider txn: {e}")

        time.sleep(0.2)

    return total


# ── 5. Bulk / Block Deals ─────────────────────────────────────────────────────
def bulk_deal_already_sent(ticker, transaction_date, insider_name, value):
    """Check if this bulk deal alert was already sent today."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", "EODHD") \
            .eq("filing_type", "BULK_DEAL") \
            .gte("created_at", f"{today}T00:00:00+00:00") \
            .execute()
        return len(result.data) > 0
    except:
        return False


def check_bulk_deals(tickers):
    """
    Check insider transactions for large block trades ($1M+).
    Flags HIGH impact deals and stores them as BULK_DEAL alerts.
    """
    if not tickers:
        return 0

    BULK_THRESHOLD = 1_000_000  # $1M minimum
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    total = 0

    for ticker in tickers:
        data = eodhd_get("insider-transactions", {
            "code": f"{ticker}.US",
            "limit": 10
        })
        if not data or not isinstance(data, list):
            continue

        for txn in data:
            txn_date = txn.get("date", "") or txn.get("transactionDate", "")
            if txn_date and txn_date < cutoff:
                continue

            insider_name = txn.get("ownerName", "") or "Unknown"
            transaction_type = (txn.get("transactionCode", "") or "").upper()
            shares = txn.get("numberOfShares", 0) or 0
            price = txn.get("price", 0) or 0
            value = txn.get("value", 0) or (float(shares) * float(price))
            role = txn.get("ownerRelationship", "") or ""

            # Only flag transactions above the threshold
            if not value or float(value) < BULK_THRESHOLD:
                continue

            if transaction_type in ("P", "BUY"):
                action = "BUY"
                emoji = "\U0001F7E2"  # green circle
            elif transaction_type in ("S", "SELL"):
                action = "SELL"
                emoji = "\U0001F534"  # red circle
            else:
                continue

            if bulk_deal_already_sent(ticker, txn_date, insider_name, value):
                continue

            value_str = f"${float(value):,.0f}"
            shares_str = f"{int(shares):,}" if shares else "N/A"
            price_str = f"${float(price):.2f}" if price else "N/A"

            summary = (
                f"{emoji} *Bulk Deal Alert — ${ticker}*\n\n"
                f"*Insider:* {insider_name} ({role})\n"
                f"*Action:* {action}\n"
                f"*Shares:* {shares_str}\n"
                f"*Price:* {price_str}\n"
                f"*Total Value:* {value_str}\n"
                f"*Date:* {txn_date}\n"
                f"_Source: EODHD Insider Transactions | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
            )

            store_alert(
                ticker=ticker,
                summary=summary,
                impact="HIGH",
                filing_type="BULK_DEAL",
                extra={
                    "insider_name": insider_name,
                    "action": action,
                    "shares": shares_str,
                    "price": price_str,
                    "value": value_str,
                    "role": role,
                    "transaction_date": txn_date,
                    "source": "EODHD"
                }
            )
            print(f"[BULK DEAL] {emoji} {ticker} — {insider_name} {action}S {shares_str} @ {price_str} = {value_str}")
            total += 1

        time.sleep(0.2)

    return total

# ── Master poll functions ─────────────────────────────────────────────────────
def poll_eodhd_news():
    """Poll ticker news and sector news."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] EODHD — Polling news...")
    # Ticker news for watchlisted stocks (specific stock alerts)
    tickers = get_watched_tickers()
    ticker_count = poll_ticker_news(tickers)
    # Sector news covers all stocks broadly via sector-level feeds
    sector_count = poll_sector_news()
    print(f"[EODHD NEWS] Ticker articles: {ticker_count} | Sector articles: {sector_count}")

def poll_eodhd_events():
    """Poll earnings calendar and insider transactions."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] EODHD — Polling events...")
    tickers = get_watched_tickers()
    earnings_count = poll_earnings_calendar(tickers)
    insider_count = poll_insider_transactions(tickers)
    bulk_count = check_bulk_deals(tickers)
    print(f"[EODHD EVENTS] Earnings alerts: {earnings_count} | Insider transactions: {insider_count} | Bulk deals: {bulk_count}")

def run_eodhd_poller():
    """Legacy entry point kept for compatibility. main.py handles scheduling."""
    poll_eodhd_news()
    poll_eodhd_events()

if __name__ == "__main__":
    run_eodhd_poller()
