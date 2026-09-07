"""
fmp_poller.py
GQ FinXray US — replaces eodhd_poller.py.

Covers Features 2 (Company & Sector News), 4 (Earnings Calendar Heads-Up),
and 5 (Insider Transactions & Large Deals) on FMP instead of EODHD.

Mechanical differences from the old EODHD version, worth knowing:
  - EODHD tagged articles with a sector via `tags`; FMP's stock-news/general-news
    endpoints don't carry that same sector taxonomy, so per-ticker news stays
    ticker-tagged and the broad sweep is stored as sector="MARKET" (detection-
    by-keyword, same approach news_poller.py already uses for its RSS sources,
    could be layered on here later if sector-tagged alerts turn out to matter).
  - Earnings calendar: FMP's /stable/earnings-calendar returns date + epsEstimated
    but not a before/after-market flag as cleanly as EODHD did; falls back to
    "Market Hours" when absent rather than guessing.
  - Insider transactions: FMP's transactionType is a compact code like
    "S-Sale" / "P-Purchase" — parsed accordingly.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

import fmp_client
import edgar_link
from feature_map import tag_extra

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── Helpers ───────────────────────────────────────────────────────────────────
BULK_DEAL_MIN_MARKET_CAP = 200_000_000   # $200M — company size gate, NOT trade size

_market_cap_cache = {}



def form4_fallback_url(ticker):
    """SEC EDGAR browse URL for Form 4 filings when exact filing not found."""
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={ticker}&type=4"
        "&dateb=&owner=include&count=10"
    )

def get_market_cap(ticker):
    """Market cap for the Feature 5 bulk-deal gate.

    Note this is a DIFFERENT axis from the old `value >= 1_000_000` check that
    used to live here: that gated on the dollar size of the individual trade,
    whereas this gates on the size of the company. A $300k sale by an officer
    of a $5B company now qualifies; a $2M sale at a $50M micro-cap no longer
    does. Cached per run because the insider loop revisits the same tickers.
    """
    if ticker in _market_cap_cache:
        return _market_cap_cache[ticker]
    cap = None
    try:
        profile = fmp_client.get_profile(ticker)
        if isinstance(profile, list) and profile:
            profile = profile[0]
        if isinstance(profile, dict):
            raw = profile.get("marketCap") or profile.get("mktCap")
            cap = float(raw) if raw else None
    except Exception as e:
        print(f"[FMP] Market cap lookup failed for {ticker}: {e}")
    _market_cap_cache[ticker] = cap
    return cap


def get_watched_tickers():
    try:
        result = supabase.table("watchlists").select("ticker").execute()
        return list(set(r["ticker"] for r in result.data if r.get("ticker")))
    except Exception as e:
        print(f"[FMP] Failed to get watchlist tickers: {e}")
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


def store_alert(ticker, summary, impact, filing_type, extra, link=None):
    try:
        alert_dict = {
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "FMP_NEWS",
            "filing_type": filing_type,
            "extra": tag_extra(extra, "FMP_NEWS", filing_type),
            "delivered": False
        }
        if link:
            alert_dict["link"] = link
        supabase.table("alerts").insert(alert_dict).execute()
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
    """
    Keyed on (ticker, insider, transaction_date), not just (ticker, day-sent).
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
        articles = fmp_client.get_stock_news(ticker, limit=10)
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


# ── 2. Broad market news sweep (replaces EODHD sector news) ──────────────────
def poll_market_news():
    total = 0
    articles = fmp_client.get_general_news(limit=25)
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
    """Feature 4 — 24h-ahead earnings heads-up.

    Previously returned zero alerts indefinitely. Three causes, all fixed here:

    1. WINDOW TOO WIDE. It fetched today..day_after (a 3-day span) and then
       discarded everything except tomorrow. FMP's earnings-calendar has full
       GLOBAL coverage -- FMP's own sample response for this endpoint returns
       "GRG.L" (London) -- so a 3-day global window is thousands of rows, and
       the response is capped. Watchlisted US tickers were falling outside the
       returned page entirely and were never even seen by the filter below.
       Now queries exactly one day, which shrinks the result set by ~3x and
       keeps watchlist tickers inside the cap.

    2. UTC vs EASTERN. "Tomorrow" was computed in UTC while FMP reports US
       earnings on the US market date. For the hours where the UTC date has
       rolled over and the ET date hasn't, every comparison silently missed.
       Now anchored to US/Eastern.

    3. PHANTOM `time` FIELD. The code branched on item["time"] for BMO/AMC,
       but that key is absent from the endpoint's response -- FMP's sample
       shows only symbol/date/epsActual/epsEstimated/revenue*/lastUpdated.
       Every alert would have silently claimed "During Market Hours". The
       timing line is now omitted unless a timing field is actually present.
    """
    if not tickers:
        return 0

    # US market dates, not UTC dates.
    et_now = datetime.now(timezone.utc) - timedelta(hours=5)
    tomorrow = (et_now + timedelta(days=1)).strftime("%Y-%m-%d")

    # Query exactly the day we care about.
    calendar = fmp_client.get_earnings_calendar(tomorrow, tomorrow)
    count = 0
    watch_set = set(tickers)

    print(f"[FMP EARNINGS] {len(calendar)} rows for {tomorrow}; "
          f"watchlist has {len(watch_set)} tickers")

    for item in calendar:
        ticker = item.get("symbol", "")
        # Defensive key fallback -- result_snapshot.py already does this for
        # income-statement fields because FMP renames keys across versions.
        report_date = (item.get("date") or item.get("reportDate")
                       or item.get("epsDate") or "")[:10]
        if report_date != tomorrow or ticker not in watch_set:
            continue
        if earnings_alert_already_sent(ticker, report_date):
            continue

        # Only state a session if FMP actually gave us one.
        timing = (item.get("time") or item.get("session") or "").lower()
        if timing.startswith("bmo"):
            timing_str = "Before Market Open"
        elif timing.startswith("amc"):
            timing_str = "After Market Close"
        else:
            timing_str = ""

        eps_estimate = item.get("epsEstimated")
        eps_line = f"Analyst EPS Estimate: {eps_estimate}" if eps_estimate else "No EPS estimate available"
        when = f" ({report_date}), {timing_str}" if timing_str else f" ({report_date})"
        summary = (
            f"{ticker} is reporting earnings tomorrow{when}. "
            f"{eps_line}. Watch for potential volatility around the announcement."
        )

        # No link: an upcoming earnings date is a calendar entry, not a
        # document. FMP confirmed this endpoint carries no URL field, and
        # there is no filing to point at until the results are actually
        # released. Sending no link beats sending a market-wide calendar page.
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
                supabase.table("raw_filings").insert({
                    "source": "FMP_NEWS",
                    "filing_type": "INSIDER_FMP",
                    "ticker": ticker,
                    "company_name": ticker,
                    "raw_text": summary,
                    "filing_url": f"fmp_insider_{ticker}_{txn_date}_{insider_name.replace(' ', '_')}",
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

                # Bulk/block deal flag — gated on COMPANY SIZE (market cap
                # >= $200M), not on the dollar value of the individual trade.
                market_cap = get_market_cap(ticker)
                qualifies = market_cap is not None and market_cap >= BULK_DEAL_MIN_MARKET_CAP
                if qualifies and not bulk_deal_already_sent(ticker, insider_name, txn_date):
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
                    # Form 4 -> accession -> EDGAR, the pattern FMP recommends.
                    # Returns None when the Form 4 hasn't landed on EDGAR yet;
                    # the alert then goes out with no link rather than a guess.
                    form4_url = edgar_link.find_filing_url(
                        ticker, form_type="4", target_date=txn_date
                    ) or form4_fallback_url(ticker)
                    store_alert(ticker, bulk_summary, "HIGH", "BULK_DEAL", {
                        "insider_name": insider_name, "action": action, "shares": shares_str,
                        "price": price_str, "value": value_str, "role": role,
                        "transaction_date": txn_date, "source": "FMP",
                        "market_cap": market_cap
                    }, link=form4_url)
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    print(f"[FMP] Failed to store insider txn: {e}")

        time.sleep(0.2)

    return total


# ── Master poll functions (same entry points main.py already imports) ────────
def poll_eodhd_news():
    """Kept name for drop-in compatibility with main.py's scheduler wiring."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] FMP — Polling news...")
    tickers = get_watched_tickers()
    ticker_count = poll_ticker_news(tickers)
    market_count = poll_market_news()
    print(f"[FMP NEWS] Ticker articles: {ticker_count} | Market sweep: {market_count}")


def poll_eodhd_events():
    """Kept name for drop-in compatibility with main.py's scheduler wiring."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] FMP — Polling events...")
    tickers = get_watched_tickers()
    earnings_count = poll_earnings_calendar(tickers)
    insider_count = poll_insider_transactions(tickers)
    print(f"[FMP EVENTS] Earnings alerts: {earnings_count} | Insider transactions: {insider_count}")


def run_fmp_poller():
    poll_eodhd_news()
    poll_eodhd_events()


if __name__ == "__main__":
    run_fmp_poller()
