"""
fmp_poller.py
GQ FinXray US — FMP integration. Features 2 (company news), 4 (earnings
calendar) and 5 (insider transactions & large trades).

WHAT CHANGED, AND WHY
---------------------
* **Iterates the watchlist, not the whole market.** The previous version called
  `get_all_stocks()` and then hit `fmp_client.get_stock_news` once per ticker —
  6,353 HTTP calls every 10-minute cycle, roughly 900,000 calls a day. That
  exhausts any FMP plan within the hour. It now iterates `get_watched_tickers()`,
  which is the only set that can produce a deliverable alert anyway: content for
  a ticker nobody watches has no recipient.

* **News goes to `raw_filings` PENDING** so ai_pipeline summarises and validates
  it. Earnings-calendar and insider rows are structured facts rather than free
  text, so those write straight to `alerts` — but they now carry their own
  `extra["headline"]`, because nothing downstream will generate one for them.

* **The bulk-deal summary is no longer pre-formatted Markdown.** It used to embed
  `*bold*` markers and newlines directly in `summary`; the delivery layer now
  renders HTML and escapes the body, so those markers would have shown up as
  literal asterisks. The summary is plain prose and the formatter does the
  styling.
"""

import os
import time
import hashlib
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from fmp_client import FMPError
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error
from news_deduplicator import should_send, cache_size

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# A single insider transaction at or above this notional is treated as a
# separately newsworthy "large trade" on top of the routine Form 4 record.
LARGE_TRADE_THRESHOLD_USD = 200_000_000

# How far back an insider filing can be and still be worth alerting on.
INSIDER_LOOKBACK_DAYS = 7


# ── Dedup helpers (all fail CLOSED: on error, assume already stored) ──────────
def news_already_stored(url):
    try:
        res = supabase.table("raw_filings").select("id").eq("filing_url", str(url)).limit(1).execute()
        return bool(res.data)
    except Exception as e:
        print(f"[FMP] Dedup check failed for {url}: {e}")
        return True


def earnings_alert_already_sent(ticker, report_date):
    try:
        res = (supabase.table("alerts").select("id")
               .eq("ticker", ticker).eq("filing_type", "EARNINGS_CALENDAR")
               .eq("extra->>report_date", str(report_date)).limit(1).execute())
        return bool(res.data)
    except Exception:
        return True


def insider_already_stored(ticker, transaction_date, insider_name):
    try:
        res = (supabase.table("raw_filings").select("id")
               .eq("ticker", ticker).eq("filing_type", "INSIDER_FMP")
               .eq("extra->>transaction_date", str(transaction_date))
               .eq("extra->>insider_name", str(insider_name)).limit(1).execute())
        return bool(res.data)
    except Exception:
        return True


def large_trade_already_sent(ticker, insider_name, transaction_date):
    """
    Keyed on (ticker, insider, date) rather than (ticker, day). Two different
    insiders selling the same stock on the same day are two distinct events, not
    a duplicate — a ticker-only key would silently drop the second one.
    """
    try:
        res = (supabase.table("alerts").select("id")
               .eq("ticker", ticker).eq("filing_type", "BULK_DEAL")
               .eq("extra->>insider_name", str(insider_name))
               .eq("extra->>transaction_date", str(transaction_date)).limit(1).execute())
        return bool(res.data)
    except Exception:
        return True


# ── Writers ───────────────────────────────────────────────────────────────────
def store_news(ticker, title, content, url, published_at, source_site):
    """News is unstructured, so it goes through the AI pipeline like everything else."""
    try:
        supabase.table("raw_filings").insert({
            "source": "FMP_NEWS",
            "filing_type": "NEWS",
            "ticker": ticker,
            "company_name": ticker,
            "raw_text": f"{title}\n\n{(content or '')[:3000]}",
            "filing_url": url,
            "filed_at": published_at,
            "status": "PENDING",
            "extra": tag_extra({
                "title": title,
                "source_name": source_site,
                "url": url,
            }, "FMP_NEWS", "NEWS"),
        }).execute()
        print(f"[FMP NEWS] {ticker} | {title[:60]}")
        return True
    except Exception as e:
        if "duplicate" not in str(e).lower() and "unique" not in str(e).lower():
            print(f"[FMP] Failed to store news: {e}")
        return False


def store_alert(ticker, summary, impact, filing_type, extra, headline=None, url=None):
    try:
        payload = dict(extra or {})
        if headline:
            payload["headline"] = headline

        # Build filing_url based on alert type
        filing_url = url
        if not filing_url:
            if filing_type == "EARNINGS_CALENDAR":
                filing_url = "https://site.financialmodelingprep.com/calendar/earnings"
            elif filing_type in ("INSIDER_FMP", "BULK_DEAL"):
                filing_url = f"https://site.financialmodelingprep.com/insider-trading/{ticker}"
            elif filing_type == "RESULT_SNAPSHOT":
                filing_url = f"https://site.financialmodelingprep.com/financials/{ticker}"

        supabase.table("alerts").insert({
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "source": "FMP_NEWS",
            "filing_type": filing_type,
            "filing_url": filing_url,
            "extra": tag_extra(payload, "FMP_NEWS", filing_type),
            "delivered": False,
        }).execute()
        print(f"[FMP ALERT] {impact} — {ticker}: {summary[:70]}")
        return True
    except Exception as e:
        print(f"[FMP] Failed to store alert: {e}")
        return False


# ── 1. Ticker news (batch-optimized) ──────────────────────────────────────────
def poll_ticker_news(tickers):
    """
    Batch ticker news polling with deduplication.

    Instead of N requests (one per ticker), batches up to 50 tickers per request.
    For 50 tickers, this is ~150 API calls/min, well under the 750 calls/min limit.
    Deduplicates by URL with 5-minute TTL cache to prevent duplicate alerts.
    """
    total = 0
    duplicates = 0
    BATCH_SIZE = 50
    ticker_list = sorted(tickers)

    # Batch tickers into groups of BATCH_SIZE
    for i in range(0, len(ticker_list), BATCH_SIZE):
        batch = ticker_list[i:i + BATCH_SIZE]
        symbols = ",".join(batch)

        try:
            articles = fmp_client.get_stock_news(symbols, limit=250) or []
        except FMPError as e:
            # Quota exhausted or key rejected: every remaining batch will fail
            # the same way after burning its full retry/backoff budget. The
            # scheduler is single-threaded, so grinding through them delays the
            # 30-second SEC pollers behind this job. Abandon the run instead.
            print(f"[FMP] News aborted at batch {i//BATCH_SIZE + 1} — {e}")
            break
        except Exception as e:
            print(f"[FMP] News fetch failed for batch {i//BATCH_SIZE + 1}: {e}")
            continue

        for article in articles:
            # Dedup check using URL-based cache
            if not should_send(article):
                duplicates += 1
                continue

            url = article.get("url") or ""
            if not url:
                continue

            ticker = article.get("symbol", "MARKET").upper()

            if store_news(
                ticker=ticker,
                title=article.get("title", ""),
                content=article.get("text") or article.get("content") or "",
                url=url,
                published_at=article.get("publishedDate") or datetime.now(timezone.utc).isoformat(),
                source_site=article.get("site", "FMP"),
            ):
                total += 1

    cache_info = cache_size()
    print(f"[FMP NEWS] Dedup cache: {cache_info} URLs cached")
    return total


# ── 2. Earnings calendar (Feature 4) ──────────────────────────────────────────
def poll_earnings_calendar(tickers):
    """
    Heads-up the day before a watched company reports.

    One calendar call covers every ticker, so this is cheap regardless of
    watchlist size — the cost is the single range query, not per-ticker lookups.
    """
    if not tickers:
        return 0

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        calendar = fmp_client.get_earnings_calendar(today, day_after) or []
    except Exception as e:
        print(f"[FMP] Earnings calendar fetch failed: {e}")
        return 0

    watch = {t.upper() for t in tickers}
    count = 0

    for item in calendar:
        ticker = (item.get("symbol") or "").upper()
        report_date = item.get("date") or ""
        if ticker not in watch or report_date != tomorrow:
            continue
        if earnings_alert_already_sent(ticker, report_date):
            continue

        timing = (item.get("time") or "").lower()
        if timing.startswith("bmo"):
            when = "before the market opens"
        elif timing.startswith("amc"):
            when = "after the market closes"
        else:
            when = "during market hours"

        eps = item.get("epsEstimated")
        eps_line = (f"Analysts expect earnings per share of {eps}."
                    if eps not in (None, "") else "No consensus EPS estimate is published.")

        summary = (f"{ticker} reports quarterly earnings tomorrow, {report_date}, {when}. "
                   f"{eps_line} Expect wider than usual price swings around the release "
                   f"and in the session that follows.")

        if store_alert(ticker, summary, "HIGH", "EARNINGS_CALENDAR",
                       {"report_date": report_date, "timing": when, "eps_estimate": eps,
                        "source_name": "FMP Earnings Calendar"},
                       headline="Earnings Due Tomorrow",
                       url=f"https://site.financialmodelingprep.com/calendar/earnings?symbol={ticker}"):
            count += 1

    return count


# ── 3. Insider transactions & large trades (Feature 5) ───────────────────────
def _classify(code):
    """FMP returns compact codes like 'P-Purchase' / 'S-Sale'."""
    code = (code or "").upper()
    if code.startswith("P") or "PURCHASE" in code:
        return "bought", "HIGH"
    if code.startswith("S") or "SALE" in code:
        return "sold", "MEDIUM"
    return None, None


def poll_insider_transactions(tickers):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=INSIDER_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    total = 0

    for ticker in sorted(tickers):
        try:
            txns = fmp_client.get_insider_trading(ticker, limit=10) or []
        except FMPError as e:
            # One ticker per request: with hundreds watched, continuing past a
            # quota failure costs minutes of retries and starves other jobs.
            print(f"[FMP] Insider run aborted at {ticker} — {e}")
            break
        except Exception as e:
            print(f"[FMP] Insider fetch failed for {ticker}: {e}")
            continue

        for txn in txns:
            txn_date = txn.get("transactionDate") or txn.get("filingDate") or ""
            if txn_date and txn_date < cutoff:
                continue

            action, impact = _classify(txn.get("transactionType"))
            if not action:
                continue

            insider = txn.get("reportingName") or "An insider"
            role = txn.get("typeOfOwner") or ""
            try:
                shares = float(txn.get("securitiesTransacted") or 0)
                price = float(txn.get("price") or 0)
            except (TypeError, ValueError):
                continue
            value = shares * price

            if insider_already_stored(ticker, txn_date, insider):
                continue

            shares_str = f"{int(shares):,}" if shares else "an undisclosed number of"
            price_str = f"${price:,.2f}" if price else "an undisclosed price"
            value_str = f"${value:,.0f}" if value else "not disclosed"

            summary = (f"Insider transaction in {ticker}: {insider}{f' ({role})' if role else ''} {action} "
                       f"{shares_str} shares at {price_str} on {txn_date}. Value: {value_str}. "
                       f"When insiders {action.replace('SOLD', 'sell').replace('BOUGHT', 'buy').lower()}, it signals their "
                       f"view on the company's near-term direction — {'profit-taking when stock is strong' if action.lower() == 'sold' else 'confidence at current levels'}.")

            extra = tag_extra({
                "insider_name": insider, "transaction_type": action.upper(),
                "shares": shares_str, "price": price_str, "value": value_str,
                "role": role, "transaction_date": txn_date, "source_name": "FMP Insider Trading",
            }, "FMP_NEWS", "INSIDER_FMP")

            try:
                supabase.table("raw_filings").insert({
                    "source": "FMP_NEWS", "filing_type": "INSIDER_FMP",
                    "ticker": ticker, "company_name": ticker,
                    "raw_text": summary,
                    "filing_url": f"https://site.financialmodelingprep.com/insider-trading/{ticker}",
                    "filed_at": txn_date or None, "status": "PENDING", "extra": extra,
                }).execute()
                total += 1
                print(f"[FMP INSIDER] {ticker} — {insider} {action} {shares_str} @ {price_str}")
            except Exception as e:
                if "duplicate" not in str(e).lower() and "unique" not in str(e).lower():
                    print(f"[FMP] Failed to store insider txn: {e}")
                continue

            # A large notional is a separate, higher-signal event on top of the
            # routine Form 4 record.
            if value >= LARGE_TRADE_THRESHOLD_USD and not large_trade_already_sent(ticker, insider, txn_date):
                big_summary = (f"Major insider trade in {ticker}: {insider}{f' ({role})' if role else ''} "
                               f"{action} {value_str} ({shares_str} shares @ {price_str}) on {txn_date}. "
                               f"Insider transactions exceeding ${LARGE_TRADE_THRESHOLD_USD / 1_000_000:,.0f}M "
                               f"signal material conviction — {'significant profit-taking' if action.lower() == 'sold' else 'substantial confidence'} from company leadership.")
                store_alert(ticker, big_summary, "HIGH", "BULK_DEAL",
                            {"insider_name": insider, "transaction_type": action.upper(),
                             "shares": shares_str, "price": price_str, "value": value_str,
                             "role": role, "transaction_date": txn_date,
                             "source_name": "FMP Insider Trading"},
                            headline=f"Insider {action.title()} Over ${value/1_000_000:.1f}M In Stock",
                            url=f"https://site.financialmodelingprep.com/insider-trading/{ticker}")

        time.sleep(0.15)

    return total


# ── Entry points (imported by main.py) ───────────────────────────────────────
def poll_fmp_news():
    try:
        tickers = get_watched_tickers()
        if not tickers:
            print("[FMP NEWS] No watched tickers — nothing to poll.")
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] FMP news — {len(tickers)} watched tickers")
        stored = poll_ticker_news(tickers)
        print(f"[FMP NEWS] {stored} new article(s) queued")
    except Exception as e:
        print(f"[FMP NEWS] Run failed: {e}")
        log_poller_error("fmp_poller", "poll_fmp_news", e)


def poll_fmp_events():
    try:
        tickers = get_watched_tickers()
        if not tickers:
            print("[FMP EVENTS] No watched tickers — nothing to poll.")
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] FMP events — {len(tickers)} watched tickers")
        earnings = poll_earnings_calendar(tickers)
        insider = poll_insider_transactions(tickers)
        print(f"[FMP EVENTS] {earnings} earnings heads-up, {insider} insider transaction(s)")
    except Exception as e:
        print(f"[FMP EVENTS] Run failed: {e}")
        log_poller_error("fmp_poller", "poll_fmp_events", e)


def run_fmp_poller():
    poll_fmp_news()
    poll_fmp_events()


if __name__ == "__main__":
    run_fmp_poller()
