"""
fmp_poller.py
GQ FinXray US — FMP integration for news, earnings, and insider data.

Covers Features 2 (Company & Sector News), 4 (Earnings Calendar Heads-Up),
and 5 (Insider Transactions & Large Deals) via FMP API.

FMP API notes:
  - Stock-specific news and general news are fetched separately; both stored
    with sector="MARKET" (keyword-matched against per-ticker news).
  - Earnings calendar returns date + epsEstimated; timing defaults to
    "Market Hours" when absent.
  - Insider transactions: transactionType is a compact code like
    "S-Sale" / "P-Purchase" — parsed accordingly.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_all_stocks():
    """Fetch all US stocks (NYSE + NASDAQ) from the stocks table with pagination."""
    try:
        tickers = []
        page = 0
        page_size = 1000
        while True:
            result = supabase.table("stocks").select("ticker").range(
                page * page_size, page * page_size + page_size - 1
            ).execute()
            rows = result.data or []
            if not rows:
                break
            tickers.extend([row["ticker"] for row in rows if row.get("ticker")])
            if len(rows) < page_size:
                break
            page += 1
        return tickers
    except Exception as e:
        print(f"[FMP] Failed to fetch stocks list: {e}")
        return []


def news_already_stored(article_id):
    try:
        result = supabase.table("raw_filings").select("id").eq("filing_url", str(article_id)).execute()
        return len(result.data) > 0
    except Exception as e:
        print(f"[FMP] Dedup check failed: {e}")
        return False


def store_news(ticker, title, content, url, published_at, source, sector, sentiment="NEUTRAL"):
    try:
        supabase.table("raw_filings").insert({
            "source": "FMP_NEWS",
            "filing_type": "NEWS",
            "ticker": ticker,
            "company_name": ticker,
            "raw_text": f"{title}\n\n{content[:3000]}",
            "filing_url": url,
            "filed_at": published_at,
            "status": "PENDING",
            "extra": tag_extra({
                "title": title,
                "source_name": source,
                "sector": sector,
                "sentiment": sentiment,
                "fmp_news": True
            }, "FMP_NEWS", "NEWS")
        }).execute()
        print(f"[FMP NEWS] {sector} | {ticker} | {title[:60]}...")
    except Exception as e:
        if "duplicate" not in str(e).lower() and "unique" not in str(e).lower():
            print(f"[FMP] Failed to store news: {e}")


def store_alert(ticker, summary, impact, filing_type, extra):
    try:
        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "FMP_NEWS",
            "filing_type": filing_type,
            "extra": tag_extra(extra, "FMP_NEWS", filing_type),
            "delivered": False
        }).execute()
        print(f"[FMP ALERT] {impact} — {ticker}: {summary[:80]}...")
    except Exception as e:
        print(f"[FMP] Failed to store alert: {e}")


def earnings_alert_already_sent(ticker, report_date):
    try:
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "EARNINGS_CALENDAR") \
            .eq("extra->>report_date", str(report_date)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def insider_already_stored(ticker, transaction_date, insider_name, shares):
    try:
        result = supabase.table("raw_filings") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("filing_type", "INSIDER_FMP") \
            .eq("extra->>transaction_date", str(transaction_date)) \
            .eq("extra->>insider_name", str(insider_name)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def bulk_deal_already_sent(ticker, insider_name, transaction_date):
    """\n    Keyed on (ticker, insider, transaction_date), not just (ticker, day-sent).
    A ticker-only/day-only check would drop a second, genuinely distinct
    large transaction on the same stock the same day -- e.g. one insider
    selling $2M in the morning and a different insider selling $5M that
    afternoon is two separate newsworthy events, not a duplicate.
    """
    try:
        result = supabase.table("alerts") \
            .select("id") \
            .eq("ticker", ticker) \
            .eq("source", "FMP_NEWS") \
            .eq("filing_type", "BULK_DEAL") \
            .eq("extra->>insider_name", str(insider_name)) \
            .eq("extra->>transaction_date", str(transaction_date)) \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


# ── 1. Ticker news ────────────────────────────────────────────────────────────
def poll_ticker_news(tickers):
    if not tickers:
        return 0
    total = 0
    for ticker in tickers:
        articles = fmp_client.get_stock_news(ticker, limit=3)
        for article in articles:
            url = article.get("url", "")
            if not url or news_already_stored(url):
                continue
            title = article.get("title", "")
            content = article.get("text", "") or article.get("content", "")
            published_at = article.get("publishedDate", datetime.now(timezone.utc).isoformat())
            source = article.get("site", "FMP")
            store_news(ticker, title, content, url, published_at, source, sector="MARKET")
            total += 1
            time.sleep(0.05)
        time.sleep(0.2)
    return total


# ── 2. Broad market news sweep ────────────────────────────────────────────────
def poll_market_news():
    total = 0
    articles = fmp_client.get_general_news(limit=10)
    for article in articles:
        url = article.get("url", "")
        if not url or news_already_stored(url):
            continue
        title = article.get("title", "")
        content = article.get("text", "") or article.get("content", "")
        published_at = article.get("publishedDate", datetime.now(timezone.utc).isoformat())
        source = article.get("site", "FMP")
        store_news("MARKET", title, content, url, published_at, source, sector="MARKET")
        total += 1
        time.sleep(0.05)
    return total


# ── 3. Earnings calendar ───────────────────────────────────────────────────────
def poll_earnings_calendar(tickers):
    if not tickers:
        return 0
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_after = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

    calendar = fmp_client.get_earnings_calendar(today, day_after)
    count = 0
    watch_set = set(tickers)

    for item in calendar:
        ticker = item.get("symbol", "")
        report_date = item.get("date", "")
        if report_date != tomorrow or ticker not in watch_set:
            continue
        if earnings_alert_already_sent(ticker, report_date):
            continue

        timing = item.get("time", "") or ""
        if timing.lower().startswith("bmo"):
            timing_str = "Before Market Open"
        elif timing.lower().startswith("amc"):
            timing_str = "After Market Close"
        else:
            timing_str = "During Market Hours"

        eps_estimate = item.get("epsEstimated")
        eps_line = f"Analyst EPS Estimate: {eps_estimate}" if eps_estimate else "No EPS estimate available"
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
                "source": "FMP Earnings Calendar"
            }
        )
        count += 1

    return count


# ── 4. Insider transactions ───────────────────────────────────────────────────
def poll_insider_transactions(tickers):
    if not tickers:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    total = 0

    for ticker in tickers:
        txns = fmp_client.get_insider_trading(ticker, limit=10)
        for txn in txns:
            txn_date = txn.get("transactionDate", "") or txn.get("filingDate", "")
            if txn_date and txn_date < cutoff:
                continue

            insider_name = txn.get("reportingName", "Unknown")
            code = (txn.get("transactionType", "") or "").upper()
            shares = txn.get("securitiesTransacted", 0) or 0
            price = txn.get("price", 0) or 0
            value = float(shares or 0) * float(price or 0)
            role = txn.get("typeOfOwner", "")

            if "P-PURCHASE" in code or code.startswith("P"):
                action, action_emoji, impact = "BUY", "🟢", "HIGH"
            elif "S-SALE" in code or code.startswith("S"):
                action, action_emoji, impact = "SELL", "🔴", "MEDIUM"
            else:
                continue

            if insider_already_stored(ticker, txn_date, insider_name, shares):
                continue

            value_str = f"${value:,.0f}" if value else "N/A"
            shares_str = f"{int(shares):,}" if shares else "N/A"
            price_str = f"${float(price):.2f}" if price else "N/A"

            summary = (
                f"{insider_name} ({role}) {action}S {shares_str} shares of {ticker} "
                f"at {price_str} per share. Total value: {value_str}."
            )

            try:
                # Create a unique filing_url by including shares + price, not just date/name
                # Multiple fills on same date by same insider will have different (shares, price) pairs
                filing_url = f"fmp_insider_{ticker}_{txn_date}_{insider_name.replace(' ', '_')}_s{int(shares)}_p{price:.2f}".replace(".", "_")

                supabase.table("raw_filings").insert({
                    "source": "FMP_NEWS",
                    "filing_type": "INSIDER_FMP",
                    "ticker": ticker,
                    "company_name": ticker,
                    "raw_text": summary,
                    "filing_url": filing_url,
                    "filed_at": txn_date,
                    "status": "PENDING",
                    "extra": tag_extra({
                        "insider_name": insider_name,
                        "transaction_type": action,
                        "transaction_emoji": action_emoji,
                        "shares": shares_str,
                        "price": price_str,
                        "value": value_str,
                        "role": role,
                        "transaction_date": txn_date,
                        "source": "FMP"
                    }, "FMP_NEWS", "INSIDER_FMP")
                }).execute()
                print(f"[FMP INSIDER] {action_emoji} {ticker} — {insider_name} {action}S {shares_str} @ {price_str}")
                total += 1

                # Bulk/block deal flag — same feed, $1M+ threshold
                if value >= 1_000_000 and not bulk_deal_already_sent(ticker, insider_name, txn_date):
                    bulk_summary = (
                        f"{action_emoji} *Bulk Deal Alert — ${ticker}*\n\n"
                        f"*Insider:* {insider_name} ({role})\n"
                        f"*Action:* {action}\n"
                        f"*Shares:* {shares_str}\n"
                        f"*Price:* {price_str}\n"
                        f"*Total Value:* {value_str}\n"
                        f"*Date:* {txn_date}\n"
                        f"_Source: FMP Insider Trading | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
                    )
                    store_alert(ticker, bulk_summary, "HIGH", "BULK_DEAL", {
                        "insider_name": insider_name, "action": action, "shares": shares_str,
                        "price": price_str, "value": value_str, "role": role,
                        "transaction_date": txn_date, "source": "FMP"
                    })
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    print(f"[FMP] Failed to store insider txn: {e}")

        time.sleep(0.2)

    return total


# ── Master poll functions ────────────────────────────────────────────────────
def poll_fmp_news():
    """Poll stock-specific and market-wide news from FMP."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] FMP — Polling news...")
    tickers = get_all_stocks()
    ticker_count = poll_ticker_news(tickers)
    market_count = poll_market_news()
    print(f"[FMP NEWS] Ticker articles: {ticker_count} | Market sweep: {market_count}")


def poll_fmp_events():
    """Poll earnings calendar and insider transactions from FMP."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] FMP — Polling events...")
    tickers = get_all_stocks()
    earnings_count = poll_earnings_calendar(tickers)
    insider_count = poll_insider_transactions(tickers)
    print(f"[FMP EVENTS] Earnings alerts: {earnings_count} | Insider transactions: {insider_count}")


def run_fmp_poller():
    poll_fmp_news()
    poll_fmp_events()


if __name__ == "__main__":
    run_fmp_poller()
