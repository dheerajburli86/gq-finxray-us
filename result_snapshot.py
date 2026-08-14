"""
result_snapshot.py
GQ FinXray US — Feature 3. Quarterly/annual result snapshots, on FMP.

Triggered when edgar_poller stores a 10-Q or 10-K. Pulls the structured income
statement from FMP, composes a factual text block (revenue, gross profit,
operating income, EBITDA, net income, EPS, margins, QoQ and YoY deltas) and
queues it in `raw_filings` with status='PENDING' so ai_pipeline.py summarises
and validates it exactly like a filing or a news article.

WHAT CHANGED, AND WHY
---------------------
* **It used to write straight into `alerts`.** The "summary" was a hand-built
  template string full of emoji and pipe characters, and it reached users without
  passing the gibberish check, the relevance check, V.1 validation, the word-count
  ladder or semantic dedup — the only alert type in the system with no quality
  gate at all. It now produces raw material and lets the pipeline write the alert.

* **It never advanced.** The old code set `extra["snapshot_done"] = True` but
  never filtered on it, so the same 10-Q rows were re-selected and re-fetched
  every 30 minutes, forever, at two FMP calls per ticker per pass. The trigger
  flag `needs_result_snapshot` (set by edgar_poller) is now the query filter and
  is cleared on every terminal outcome, so each filing is worked exactly once.

* **The dedup key collapsed.** `period` was built as
  f"{period} FY{fiscalYear}" and, when FMP omitted both fields, became the
  literal string "FY" — so the first ticker's snapshot suppressed every other
  ticker's snapshot from then on. A real period label is now required; without
  one the filing is skipped rather than deduped against garbage.

* **Truthiness bugs dropped legitimate zeros.** `if gross_margin` treats a 0.0%
  margin — a real and newsworthy number — as missing, and the same pattern hid
  a genuine 0 in the YoY comparisons. Every check is `is not None` now.

* **`quarters[4]` was assumed to be the year-ago quarter.** With a missing
  filing, a fiscal-calendar change or a restatement it is not, and the alert
  silently compared against the wrong period. The year-ago quarter is now found
  by date and validated to be 330-400 days back, or the YoY comparison is
  omitted.

* The watchlist gate runs before the two FMP calls, not after.
"""

import os
import re
import time
import hashlib
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

POLLER_NAME = "result_snapshot"
SOURCE = "FMP"
FILING_TYPE = "RESULT_SNAPSHOT"

BATCH_SIZE = 10
TICKER_GAP_SECONDS = 1.0

# FMP posts a statement days after the filing hits EDGAR. Retry a few cycles,
# then give up so a company that simply never gets covered cannot occupy the
# batch window indefinitely.
MAX_ATTEMPTS = 4

# A year-ago quarter must actually be about a year ago.
YOY_MIN_DAYS = 330
YOY_MAX_DAYS = 400

VALID_PERIODS = {"Q1", "Q2", "Q3", "Q4", "FY"}


# ── Formatting ────────────────────────────────────────────────────────────────
def fmt_value(val, prefix="$"):
    """1234567890 -> '$1.23B'. Returns None when the value is unusable."""
    if val is None:
        return None
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    sign = "-" if val < 0 else ""
    a = abs(val)
    if a >= 1_000_000_000:
        return f"{sign}{prefix}{a / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{sign}{prefix}{a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}{prefix}{a / 1_000:.2f}K"
    return f"{sign}{prefix}{a:.2f}"


def pct_change(current, previous):
    """
    Percentage change, or None when it cannot be stated honestly.

    A move from a negative base (a loss narrowing to a smaller loss) has no
    meaningful percentage, so it is reported as None rather than as a number that
    reads backwards.
    """
    try:
        curr = float(current)
        prev = float(previous)
    except (TypeError, ValueError):
        return None
    if prev == 0 or prev < 0:
        return None
    return (curr - prev) / prev * 100.0


def describe_change(label, current, previous):
    """'up 12.4% from $1.10B' — or a plain from/to when a percentage is meaningless."""
    if current is None or previous is None:
        return None
    pct = pct_change(current, previous)
    prev_str = fmt_value(previous)
    if prev_str is None:
        return None
    if pct is None:
        return f"{label} {prev_str}"
    direction = "up" if pct >= 0 else "down"
    return f"{label} {direction} {abs(pct):.1f}% from {prev_str}"


def g(row, *keys):
    """
    First present, non-None value among several key spellings.

    FMP has renamed statement fields across API versions (older v3 responses used
    lowercase keys, current /stable responses use camelCase), so every field is
    read through this rather than assuming one spelling.
    """
    if not isinstance(row, dict):
        return None
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return None


def _num(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:len("2026-01-01T00:00:00")], fmt)
        except (ValueError, TypeError):
            continue
    return None


# ── Period label ──────────────────────────────────────────────────────────────
def build_period_label(row):
    """
    'Q3 FY2026', or None when FMP did not tell us which period this is.

    Returning None is the whole point: the previous version fell through to the
    literal "FY", which every ticker shared, so one snapshot deduped away all the
    others. No period means no reliable dedup key, which means do not send.
    """
    period = str(g(row, "period", "periodType") or "").strip().upper()
    if period not in VALID_PERIODS:
        return None

    year = g(row, "fiscalYear", "calendarYear")
    if year is None:
        # The statement date is factual and always present; deriving the fiscal
        # year from it is safe. Deriving the *quarter* from it would not be.
        dt = _parse_date(g(row, "date"))
        year = dt.year if dt else None
    if year is None:
        return None

    try:
        year = int(str(year)[:4])
    except (TypeError, ValueError):
        return None
    # FMP labels an annual statement period="FY", which the plain template
    # rendered as "FY FY2025" -- and that string reached users in the opening
    # sentence of every annual snapshot. Quarters are unaffected ("Q2 FY2026").
    if period == "FY":
        return f"FY{year}"
    return f"{period} FY{year}"


def find_year_ago(quarters, latest_date):
    """
    The quarter roughly one year before `latest_date`, or None.

    Indexing quarters[4] assumes a perfectly regular eight-quarter history. A
    skipped filing, a fiscal-year change or a restatement breaks that, and the
    old code then compared this quarter against something 6 or 18 months old and
    called it "YoY".
    """
    if latest_date is None:
        return None
    best, best_gap = None, None
    for row in quarters[1:]:
        dt = _parse_date(g(row, "date"))
        if dt is None:
            continue
        gap = (latest_date - dt).days
        if YOY_MIN_DAYS <= gap <= YOY_MAX_DAYS and (best_gap is None or abs(gap - 365) < abs(best_gap - 365)):
            best, best_gap = row, gap
    return best


# ── Dedup ─────────────────────────────────────────────────────────────────────
def snapshot_already_queued(ticker, period):
    """Fails CLOSED — on a database error, treat as already done and skip."""
    try:
        rows = (supabase.table("raw_filings")
                .select("id")
                .eq("ticker", ticker)
                .eq("filing_type", FILING_TYPE)
                .eq("extra->>period", period)
                .limit(1).execute()).data
        return bool(rows)
    except Exception as e:
        logger.error("[SNAPSHOT] Dedup check failed for %s %s: %s", ticker, period, e)
        return True


# ── Snapshot build ────────────────────────────────────────────────────────────
def build_result_snapshot(ticker, form_type=None, trigger="SEC"):
    """
    Compose the factual text block for one ticker, or None when there is nothing
    reportable. Costs one FMP call when the period is already queued (the dedup
    gate runs before the profile lookup), two when it is genuinely new.

    `form_type=None` is the FMP-direct path: no EDGAR filing has been seen, so the
    form is derived from the period label FMP itself returned rather than asserted.
    `trigger` only changes the wording of the opening sentence -- an FMP-sourced
    snapshot must not claim to have been "disclosed in its 10-Q" when this process
    never saw a 10-Q.
    """
    quarters = fmp_client.get_income_statement(ticker, period="quarter", limit=8) or []
    if len(quarters) < 2:
        logger.info("[SNAPSHOT] %s: fewer than two quarters of data from FMP.", ticker)
        return None

    quarters = sorted(quarters, key=lambda r: str(g(r, "date") or ""), reverse=True)
    latest, prev = quarters[0], quarters[1]

    period = build_period_label(latest)
    if not period:
        logger.info("[SNAPSHOT] %s: FMP returned no usable period label; skipping.", ticker)
        return None

    if snapshot_already_queued(ticker, period):
        logger.info("[SNAPSHOT] %s %s already queued.", ticker, period)
        return None

    latest_date = _parse_date(g(latest, "date"))
    yoy = find_year_ago(quarters, latest_date)

    profile = fmp_client.get_profile(ticker) or {}
    company_name = profile.get("companyName") or ticker

    revenue      = _num(g(latest, "revenue", "totalRevenue"))
    revenue_prev = _num(g(prev, "revenue", "totalRevenue"))
    revenue_yoy  = _num(g(yoy, "revenue", "totalRevenue"))

    gross        = _num(g(latest, "grossProfit", "grossprofit"))
    gross_prev   = _num(g(prev, "grossProfit", "grossprofit"))

    op_income      = _num(g(latest, "operatingIncome", "operatingincome"))
    op_income_prev = _num(g(prev, "operatingIncome", "operatingincome"))

    ebitda      = _num(g(latest, "ebitda", "EBITDA"))
    ebitda_prev = _num(g(prev, "ebitda", "EBITDA"))

    net        = _num(g(latest, "netIncome", "netincome"))
    net_prev   = _num(g(prev, "netIncome", "netincome"))
    net_yoy    = _num(g(yoy, "netIncome", "netincome"))

    eps        = _num(g(latest, "epsDiluted", "epsdiluted", "eps"))
    eps_prev   = _num(g(prev, "epsDiluted", "epsdiluted", "eps"))
    eps_yoy    = _num(g(yoy, "epsDiluted", "epsdiluted", "eps"))

    if revenue is None and net is None and eps is None:
        logger.info("[SNAPSHOT] %s: statement carried no revenue, net income or EPS.", ticker)
        return None

    def margin(numerator):
        # `is not None` throughout — a 0.0% margin is a real number and used to be
        # silently dropped by an `if margin:` test.
        if numerator is None or revenue is None or revenue == 0:
            return None
        return numerator / revenue * 100.0

    gross_margin = margin(gross)
    op_margin = margin(op_income)
    net_margin = margin(net)

    period_end = g(latest, "date")

    # An FY period is an annual statement; anything else is a quarter. When EDGAR
    # supplied the trigger we already know the form for certain and use it.
    if form_type is None:
        form_type = "10-K" if period.startswith("FY") else "10-Q"
    form_label = "annual report (10-K)" if form_type == "10-K" else "quarterly report (10-Q)"

    if trigger == "SEC":
        provenance = f", disclosed in its {form_label}"
    else:
        provenance = (", as reported in its audited annual financial statements"
                      if form_type == "10-K"
                      else ", as reported in its quarterly financial statements")

    lines = [
        f"{company_name} ({ticker}) reported financial results for {period}"
        + (f", period ended {period_end}" if period_end else "")
        + provenance + ".",
        "",
    ]

    def add(label, value, prev_value, yoy_value=None, margin_value=None, is_eps=False):
        display = f"${value:,.2f}" if (is_eps and value is not None) else fmt_value(value)
        if display is None:
            return
        parts = [f"{label}: {display}"]
        if margin_value is not None:
            parts.append(f"margin {margin_value:.1f}% of revenue")
        qoq = describe_change("versus the prior quarter", value, prev_value)
        if qoq:
            parts.append(qoq)
        if yoy_value is not None:
            yoy_txt = describe_change("versus the year-ago quarter", value, yoy_value)
            if yoy_txt:
                parts.append(yoy_txt)
        lines.append("; ".join(parts) + ".")

    add("Revenue", revenue, revenue_prev, revenue_yoy)
    add("Gross profit", gross, gross_prev, margin_value=gross_margin)
    add("Operating income", op_income, op_income_prev, margin_value=op_margin)
    add("EBITDA", ebitda, ebitda_prev)
    add("Net income", net, net_prev, net_yoy, margin_value=net_margin)
    add("Diluted earnings per share", eps, eps_prev, eps_yoy, is_eps=True)

    raw_text = "\n".join(lines).strip()
    if raw_text.count("\n") < 2:
        logger.info("[SNAPSHOT] %s: not enough populated line items to be worth an alert.", ticker)
        return None

    metrics = {
        "revenue": fmt_value(revenue),
        "gross_profit": fmt_value(gross),
        "operating_income": fmt_value(op_income),
        "ebitda": fmt_value(ebitda),
        "net_income": fmt_value(net),
        "eps": f"${eps:,.2f}" if eps is not None else None,
        "gross_margin_pct": round(gross_margin, 2) if gross_margin is not None else None,
        "operating_margin_pct": round(op_margin, 2) if op_margin is not None else None,
        "net_margin_pct": round(net_margin, 2) if net_margin is not None else None,
        "revenue_qoq_pct": round(pct_change(revenue, revenue_prev), 2) if pct_change(revenue, revenue_prev) is not None else None,
        "revenue_yoy_pct": round(pct_change(revenue, revenue_yoy), 2) if pct_change(revenue, revenue_yoy) is not None else None,
        "net_income_yoy_pct": round(pct_change(net, net_yoy), 2) if pct_change(net, net_yoy) is not None else None,
        "eps_yoy_pct": round(pct_change(eps, eps_yoy), 2) if pct_change(eps, eps_yoy) is not None else None,
        "period": period,
        "period_end": period_end,
        "yoy_comparison_available": yoy is not None,
    }

    return {
        "ticker": ticker,
        "company_name": company_name,
        "period": period,
        "period_end": period_end,
        "form_type": form_type,
        "raw_text": raw_text,
        "metrics": metrics,
    }


def queue_snapshot(snapshot):
    """Write the snapshot to raw_filings as PENDING. Returns True on insert."""
    ticker = snapshot["ticker"]
    period_slug = re.sub(r"[^A-Za-z0-9]+", "_", snapshot["period"]).strip("_")
    # Link to FMP financials page so users can verify and explore more.
    #
    # raw_filings.filing_url carries a UNIQUE constraint on the column ALONE
    # (not the composite UNIQUE(filing_url, ticker) this code assumed). A URL
    # that was constant per ticker therefore let exactly ONE snapshot per ticker
    # ever insert — every later quarter collided and was swallowed by the
    # "duplicate" branch below as if it had already been queued. period_slug was
    # computed for this purpose and then never used. The fragment keeps the link
    # clickable (browsers ignore #...) while making the row unique per period.
    filing_url = (f"https://site.financialmodelingprep.com/financials/"
                  f"{ticker.upper()}#{period_slug}")
    text = snapshot["raw_text"]

    try:
        supabase.table("raw_filings").insert({
            "source": SOURCE,
            "filing_type": FILING_TYPE,
            "ticker": ticker,
            "company_name": snapshot["company_name"],
            "raw_text": text,
            "filing_url": filing_url,
            "filed_at": datetime.now(timezone.utc).isoformat(),
            "status": "PENDING",
            "content_hash": hashlib.sha256(
                re.sub(r"\s+", " ", text.lower()).strip().encode("utf-8", "ignore")
            ).hexdigest(),
            "extra": tag_extra({
                "period": snapshot["period"],
                "period_end": snapshot["period_end"],
                "form_type": snapshot["form_type"],
                "company_name": snapshot["company_name"],
                "title": f"{snapshot['company_name']} {snapshot['period']} Results",
                "metrics": snapshot["metrics"],
                "source_name": "FMP Income Statement",
            }, SOURCE, FILING_TYPE),
        }).execute()
        logger.info("[SNAPSHOT] Queued %s %s for summarisation", ticker, snapshot["period"])
        return True
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            logger.debug("[SNAPSHOT] %s %s already queued.", ticker, snapshot["period"])
            return False
        logger.error("[SNAPSHOT] Insert failed for %s: %s", ticker, e)
        log_poller_error(POLLER_NAME, "queue_snapshot", e,
                         {"ticker": ticker, "period": snapshot["period"]})
        return False


# ── Trigger bookkeeping ───────────────────────────────────────────────────────
def _mark(filing, state, done=True):
    """
    Advance the trigger row so it is not selected again.

    `needs_result_snapshot` is both the queue flag and the query filter, so
    clearing it is what makes this poller terminate. `snapshot_state` records why.
    A non-terminal state (done=False) leaves the flag set for another attempt and
    bumps the attempt counter.
    """
    extra = dict(filing.get("extra") or {})
    attempts = int(extra.get("snapshot_attempts") or 0) + 1
    extra["snapshot_attempts"] = attempts
    extra["snapshot_state"] = state
    extra["snapshot_checked_at"] = datetime.now(timezone.utc).isoformat()

    if done or attempts >= MAX_ATTEMPTS:
        extra["needs_result_snapshot"] = False
        if not done:
            extra["snapshot_state"] = f"{state}_GAVE_UP"

    try:
        supabase.table("raw_filings").update({"extra": extra}).eq("id", filing["id"]).execute()
    except Exception as e:
        logger.error("[SNAPSHOT] Could not update trigger row %s: %s", filing.get("id"), e)
        log_poller_error(POLLER_NAME, "mark_trigger", e, {"filing_id": filing.get("id")})


# ── FMP-direct trigger ────────────────────────────────────────────────────────
def sweep_watchlist_snapshots():
    """
    Poll FMP directly for every watched ticker, independent of SEC EDGAR.

    WHY THIS EXISTS
    ---------------
    This feature used to fire only when edgar_poller stored a 10-Q or 10-K and
    flagged it `needs_result_snapshot`. `raw_filings` has never contained a single
    10-Q or 10-K row -- only 11 Form 4s, lifetime -- so the trigger query matched
    nothing on every run since the feature was written and Feature 3 has a
    lifetime alert count of zero. The SEC path above is kept because it is the
    fastest signal when EDGAR does deliver, but it can no longer be the only one.

    FMP publishes the income statement on its own schedule regardless of what this
    process saw on EDGAR, so the statement itself is the trigger. Cost is one FMP
    call per already-known ticker (the dedup gate in build_result_snapshot runs
    before the profile lookup) and two for a genuinely new period -- roughly 20-40
    calls per pass against a 750/min ceiling.

    Dedup is shared with the SEC path: both converge on the same
    (ticker, period) check and the same UNIQUE filing_url, so a ticker that
    arrives down both routes is queued exactly once.
    """
    watched = sorted(get_watched_tickers())
    if not watched:
        logger.info("[SNAPSHOT] FMP sweep: no watchlisted tickers.")
        return 0

    logger.info("[SNAPSHOT] FMP sweep across %d watched ticker(s)", len(watched))
    queued = 0
    for ticker in watched:
        try:
            snapshot = build_result_snapshot(ticker, form_type=None, trigger="FMP")
            if snapshot and queue_snapshot(snapshot):
                queued += 1
        except Exception as e:
            logger.error("[SNAPSHOT] FMP sweep failed for %s: %s", ticker, e)
            log_poller_error(POLLER_NAME, "sweep_watchlist_snapshots", e, {"ticker": ticker})
        time.sleep(TICKER_GAP_SECONDS)

    logger.info("[SNAPSHOT] FMP sweep done — %d snapshot(s) queued.", queued)
    return queued


# ── Entry point ───────────────────────────────────────────────────────────────
def process_pending_snapshots():
    """
    One pass. Never raises — a scheduler calls this every 30 minutes.

    Two independent triggers run every pass: the EDGAR-flagged filings below, and
    the FMP sweep. Either alone is sufficient to produce a snapshot; neither can
    silence the other, and a failure in one is caught before the other starts.
    """
    try:
        try:
            sweep_watchlist_snapshots()
        except Exception as e:
            # The SEC-triggered path below must still run even if FMP is down.
            logger.exception("[SNAPSHOT] FMP sweep aborted: %s", e)
            log_poller_error(POLLER_NAME, "sweep_watchlist_snapshots", e)

        try:
            filings = (supabase.table("raw_filings")
                       .select("id, ticker, filing_type, extra")
                       .in_("filing_type", ["10-Q", "10-K"])
                       .eq("extra->>needs_result_snapshot", "true")
                       .order("created_at")
                       .limit(BATCH_SIZE).execute()).data or []
        except Exception as e:
            logger.error("[SNAPSHOT] Trigger query failed: %s", e)
            log_poller_error(POLLER_NAME, "trigger_query", e)
            return

        if not filings:
            logger.info("[SNAPSHOT] No 10-Q/10-K filings awaiting a snapshot.")
            return

        watched = get_watched_tickers()
        logger.info("[SNAPSHOT] %d filing(s) awaiting a snapshot", len(filings))

        queued = 0
        for filing in filings:
            ticker = (filing.get("ticker") or "").upper()
            form_type = filing.get("filing_type") or "10-Q"

            if not ticker or ticker in ("UNKNOWN", "MARKET"):
                _mark(filing, "SKIPPED_NO_TICKER")
                continue

            # Gate before the two FMP calls, not after them.
            if ticker not in watched:
                _mark(filing, "SKIPPED_UNWATCHED")
                continue

            try:
                snapshot = build_result_snapshot(ticker, form_type)
            except Exception as e:
                logger.error("[SNAPSHOT] Build failed for %s: %s", ticker, e)
                log_poller_error(POLLER_NAME, "build_result_snapshot", e, {"ticker": ticker})
                _mark(filing, "ERROR", done=False)
                time.sleep(TICKER_GAP_SECONDS)
                continue

            if snapshot is None:
                # FMP may simply not have posted the statement yet; retry a few
                # cycles before giving up on this filing for good.
                _mark(filing, "NO_DATA", done=False)
            else:
                queue_snapshot(snapshot)
                queued += 1
                _mark(filing, "QUEUED")

            time.sleep(TICKER_GAP_SECONDS)

        logger.info("[SNAPSHOT] Done — %d snapshot(s) queued for summarisation.", queued)

    except Exception as e:
        logger.exception("[SNAPSHOT] Run failed: %s", e)
        log_poller_error(POLLER_NAME, "process_pending_snapshots", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = build_result_snapshot("AAPL", "10-Q")
    if snap:
        print("=" * 68)
        print(snap["raw_text"])
        print("=" * 68)
        print(snap["metrics"])
    else:
        print("No snapshot generated.")
