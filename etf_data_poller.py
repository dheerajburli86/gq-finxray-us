"""
etf_data_poller.py
GQ FinXray US — Feature 14: ETF Portfolio & Expense Changes (FMP).

WHAT IT DOES
------------
Walks every ETF in etf_master and refreshes, from FMP:

  holdings      -> etf_holdings          (current snapshot, one row per security)
  adds / exits  -> etf_holdings_changes  (WhatsIn / WhatsOut between snapshots)
  expense ratio -> etf_expenses          (one row per ETF per day)
  performance   -> etf_performance       (1M .. max returns, full runs only)
  company data  -> etf_company_data      (ISIN, sector/country mix, weekly)
  master fields -> etf_master            (AUM, NAV, fund house, inception ...)

Data is collected for the WHOLE universe so that an ETF added to a watchlist
tomorrow already has a baseline to diff against. ALERTS are watchlist-gated:
an alert row is written only for an ETF that at least one user watches, and
delivery.py then routes it to exactly those users.

ALERTS (zero LLM calls — every alert here is rule-based)
------
  ETF_HOLDINGS_ADDED / ETF_HOLDINGS_REMOVED / ETF_HOLDINGS_CHANGE
      One alert per fund per new FMP snapshot, listing every addition and exit
      at or above ETF_ALERT_MIN_WEIGHT (default 0.5% of the fund).
  ETF_EXPENSE_INCREASE / ETF_EXPENSE_DECREASE
      The expense ratio moved by at least ETF_EXPENSE_MIN_DELTA percentage
      points AND the new value was seen on ETF_EXPENSE_CONFIRM_RUNS consecutive
      runs (default 2), so a vendor rounding flap cannot alert.

WHY A HOLDINGS ALERT CANNOT BE NOISE
------------------------------------
  * Version gate     — nothing is diffed unless FMP published a new snapshot
                       (its updatedAt or the security set changed).
  * Baseline rule    — the first snapshot of any ETF is stored silently.
  * Empty / partial  — an empty response, or one under half the size of the
                       stored portfolio, is treated as a bad read: nothing is
                       replaced, nothing alerts.
  * Churn guard      — if more than ETF_MAX_CHURN of a 20+ name portfolio
                       turns over at once, that is an identifier change on the
                       vendor side, not a trade: re-baseline, do not alert.
  * Weight floor     — only positions >= 0.5% are alerted; cash, futures,
                       swaps and currency lines never are.
  * Bond funds       — routine maturity roll is not news, so asset classes in
                       ETF_HOLDINGS_ALERT_SKIP_CLASSES (fixed income by default)
                       store their changes but do not alert on them.

SCHEDULE (main.py, ET)
--------
  07:00  run_etf_data_poller_full   holdings, expenses, performance, company data
  18:30  run_etf_data_poller_light  holdings + expenses only
Mon–Fri only (ETF_POLL_ANY_DAY=true overrides).
"""

import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

import etf_fmp_client as fmp
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

load_dotenv()

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
POLLER = "etf_data_poller"

SOURCE_PORTFOLIO = "ETF_PORTFOLIO"
SOURCE_EXPENSE = "ETF_EXPENSE"


def _env_bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


WORKERS = int(os.getenv("ETF_POLL_WORKERS", "6"))
MAX_ROWS = int(os.getenv("ETF_HOLDINGS_MAX_ROWS", "500"))          # 0 = keep every line
ALERT_MIN_WEIGHT = float(os.getenv("ETF_ALERT_MIN_WEIGHT", "0.5"))  # percent of fund
ALERT_HIGH_WEIGHT = float(os.getenv("ETF_ALERT_HIGH_WEIGHT", "5.0"))
CHANGE_STORE_MIN_WEIGHT = float(os.getenv("ETF_CHANGE_STORE_MIN_WEIGHT", "0.01"))
MAX_CHURN = float(os.getenv("ETF_MAX_CHURN", "0.6"))
MIN_KEEP_FRACTION = float(os.getenv("ETF_MIN_KEEP_FRACTION", "0.5"))
SKIP_CLASSES = {c.strip().lower() for c in os.getenv(
    "ETF_HOLDINGS_ALERT_SKIP_CLASSES", "fixed income,bond,bonds,money market").split(",") if c.strip()}
EXPENSE_MIN_DELTA = float(os.getenv("ETF_EXPENSE_MIN_DELTA", "0.005"))   # percentage points
EXPENSE_HIGH_DELTA = float(os.getenv("ETF_EXPENSE_HIGH_DELTA", "0.10"))
EXPENSE_CONFIRM_RUNS = int(os.getenv("ETF_EXPENSE_CONFIRM_RUNS", "2"))
COMPANY_REFRESH_DAYS = int(os.getenv("ETF_COMPANY_REFRESH_DAYS", "7"))
ANY_DAY = _env_bool("ETF_POLL_ANY_DAY")
FLUSH_EVERY = 500
LIST_LIMIT = 8

# Holding lines that are not a security position. Stored, never alerted.
_NON_SECURITY = re.compile(
    r"\b(CASH|CURRENCY|U\.?\s?S\.? DOLLARS?|USD|FUTURES?|FUT|SWAPS?|FORWARDS?|REPO|"
    r"MONEY MARKET|OTHER ASSETS|NET OTHER|MARGIN|COLLATERAL)\b", re.I)


# ── Supabase (one client per worker thread) ───────────────────────────────────
_local = threading.local()


def sb():
    c = getattr(_local, "client", None)
    if c is None:
        c = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        _local.client = c
    return c


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _select_all(table, cols, order, filters=None):
    """Paginated select. PostgREST caps an unbounded read at 1,000 rows — the
    2026-10-02 first run read etf_master without paging and covered 1,000 of
    4,153 ETFs with no error anywhere."""
    out, page, size = [], 0, 1000
    while True:
        q = sb().table(table).select(cols)
        for k, v in (filters or {}).items():
            q = q.eq(k, v)
        chunk = q.order(order).range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


def _upsert_chunks(table, rows, on_conflict, size=500):
    written = 0
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        try:
            sb().table(table).upsert(chunk, on_conflict=on_conflict).execute()
            written += len(chunk)
        except Exception as e:
            log_poller_error(POLLER, f"upsert_{table}", e, {"rows": len(chunk)})
            logger.error("[ETF] upsert into %s failed for %d rows: %s", table, len(chunk), e)
    return written


# ── Loads ─────────────────────────────────────────────────────────────────────
def load_master():
    """{ticker: name} for every ETF in etf_master."""
    return {r["ticker"].upper(): r.get("name") for r in
            _select_all("etf_master", "ticker, name", "ticker") if r.get("ticker")}


def load_states():
    return {r["ticker"].upper(): r for r in
            _select_all("etf_snapshot_state", "*", "ticker") if r.get("ticker")}


def load_stored_holdings(ticker):
    rows = _select_all("etf_holdings", "security_key, security_symbol, security_name, portfolio_pct",
                       "security_key", {"ticker": ticker})
    return {r["security_key"]: r for r in rows}


# ── Pure logic (unit-tested offline) ──────────────────────────────────────────
def _w(row):
    return fmp.num((row or {}).get("portfolio_pct")) or 0.0


def diff_holdings(old, new_full, max_rows=MAX_ROWS):
    """
    old       {key: row} as stored (at most max_rows, the heaviest ones)
    new_full  {key: row} every line FMP returned this time
    -> (added_keys, removed_keys, new_kept)

    Storage keeps only the top max_rows positions, so a position sliding below
    the cut-off must not read as an exit, and one climbing above it must not
    read as a new buy:
      removed = stored before, absent from FMP's FULL new list
      added   = in the new kept set, not stored before, and (when the stored set
                was itself truncated) heavier than the lightest stored position
    """
    ranked = sorted(new_full.items(), key=lambda kv: -_w(kv[1]))
    new_kept = dict(ranked[:max_rows] if max_rows > 0 else ranked)
    old_truncated = max_rows > 0 and len(old) >= max_rows
    old_cut = min((_w(v) for v in old.values()), default=0.0) if old_truncated else None
    removed = [k for k in old if k not in new_full]
    added = [k for k in new_kept
             if k not in old and (not old_truncated or _w(new_full[k]) > old_cut)]
    return added, removed, new_kept


def is_alertable_line(change):
    name = f"{change.get('security_name') or ''} {change.get('security_key') or ''}"
    return not _NON_SECURITY.search(name)


def class_alerts_holdings(asset_class):
    return (asset_class or "").strip().lower() not in SKIP_CLASSES


def _pct_str(x):
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{s}%"


def _label(c):
    sym = (c.get("security_symbol") or "").strip()
    name = (c.get("security_name") or "").strip()
    if name and sym and sym.upper() != name.upper():
        return f"{name} ({sym})"
    return name or sym or c.get("security_key")


def _fmt_as_of(stamp):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).strftime("%b %d, %Y")
    except Exception:
        return str(stamp)[:10]


def holdings_alert(fund, ticker, added, removed, n_small, n_positions, as_of):
    """-> (summary, impact, filing_type). added/removed are change dicts."""
    parts = [f"{fund} ({ticker}) changed its portfolio holdings."]
    if added:
        shown = "; ".join(f"{_label(c)} at {c['weight_new']:.2f}% of the fund" for c in added[:LIST_LIMIT])
        more = f"; and {len(added) - LIST_LIMIT} more" if len(added) > LIST_LIMIT else ""
        parts.append(f"Added: {shown}{more}.")
    if removed:
        shown = "; ".join(f"{_label(c)}, previously {c['weight_prev']:.2f}%" for c in removed[:LIST_LIMIT])
        more = f"; and {len(removed) - LIST_LIMIT} more" if len(removed) > LIST_LIMIT else ""
        parts.append(f"Removed: {shown}{more}.")
    if n_small:
        parts.append(f"{n_small} smaller change{'s' if n_small != 1 else ''} under "
                     f"{ALERT_MIN_WEIGHT:.2f}% of the fund {'are' if n_small != 1 else 'is'} not listed.")
    when = _fmt_as_of(as_of)
    parts.append(f"The fund holds {n_positions} positions" + (f" as of {when}." if when else "."))
    top = max([c["weight_new"] or 0 for c in added] + [c["weight_prev"] or 0 for c in removed] + [0])
    impact = "HIGH" if top >= ALERT_HIGH_WEIGHT else "MEDIUM"
    if added and removed:
        ftype = "ETF_HOLDINGS_CHANGE"
    elif added:
        ftype = "ETF_HOLDINGS_ADDED"
    else:
        ftype = "ETF_HOLDINGS_REMOVED"
    return " ".join(parts), impact, ftype


def expense_alert(fund, ticker, old, new):
    """-> (summary, impact, filing_type). Ratios are in percent."""
    delta = new - old
    up = delta > 0
    bp = abs(delta) * 100
    summary = (f"{fund} ({ticker}) {'raised' if up else 'lowered'} its expense ratio from "
               f"{_pct_str(old)} to {_pct_str(new)}, a change of {'+' if up else '-'}{bp:.1f} "
               f"basis points. On a $10,000 position the annual fund expense moves from "
               f"${old * 100:,.2f} to ${new * 100:,.2f}. The new ratio was confirmed on "
               f"{EXPENSE_CONFIRM_RUNS} separate trading days.")
    impact = "HIGH" if (up and delta >= EXPENSE_HIGH_DELTA) else "MEDIUM"
    return summary, impact, ("ETF_EXPENSE_INCREASE" if up else "ETF_EXPENSE_DECREASE")


def step_expense(state, er, today):
    """
    Advance the expense-ratio state machine for one observation.
    Mutates `state`. Returns (old, new) when a change is CONFIRMED, else None.

    Confirmations count DISTINCT ET dates, not runs: the 07:00 and 18:30 runs
    usually re-read the same FMP snapshot, so two same-day reads confirm
    nothing a one-day vendor glitch could not produce.
    """
    base = fmp.num(state.get("expense_ratio"))
    if base is None:
        state.update(expense_ratio=er, expense_pending=None, expense_pending_count=0,
                     expense_pending_date=None)
        return None
    if abs(er - base) < EXPENSE_MIN_DELTA:
        state.update(expense_pending=None, expense_pending_count=0, expense_pending_date=None)
        return None
    pending = fmp.num(state.get("expense_pending"))
    if pending is not None and abs(er - pending) < 1e-9:
        if str(state.get("expense_pending_date") or "") == today:
            return None                       # same day: not new evidence
        count = int(state.get("expense_pending_count") or 0) + 1
    else:
        count = 1
    state.update(expense_pending=er, expense_pending_count=count, expense_pending_date=today)
    if count >= EXPENSE_CONFIRM_RUNS:
        state.update(expense_ratio=er, expense_pending=None, expense_pending_count=0,
                     expense_pending_date=None)
        return base, er
    return None


# ── Writes ────────────────────────────────────────────────────────────────────
def replace_holdings(ticker, kept, old_keys, as_of):
    """Upsert the new snapshot, then delete what left it. Upsert-first means a
    failure part-way leaves the old snapshot readable, never an empty one."""
    rows = [{"ticker": ticker, "security_key": k,
             "security_symbol": r.get("security_symbol"), "security_name": r.get("security_name"),
             "isin": r.get("isin"), "cusip": r.get("cusip"),
             "portfolio_pct": r.get("portfolio_pct"), "market_value": r.get("market_value"),
             "shares": r.get("shares"), "as_of": as_of, "updated_at": _now_iso()}
            for k, r in kept.items()]
    for i in range(0, len(rows), 500):
        sb().table("etf_holdings").upsert(rows[i:i + 500], on_conflict="ticker,security_key").execute()
    # 25 per request: keys can be 130 characters, and PostgREST puts the in()
    # list in the URL, which gateways reject past a few KB.
    stale = [k for k in old_keys if k not in kept]
    for i in range(0, len(stale), 25):
        sb().table("etf_holdings").delete().eq("ticker", ticker).in_("security_key", stale[i:i + 25]).execute()


def store_changes(changes):
    rows = [c for c in changes
            if max(c.get("weight_new") or 0, c.get("weight_prev") or 0) >= CHANGE_STORE_MIN_WEIGHT]
    for i in range(0, len(rows), 500):
        sb().table("etf_holdings_changes").upsert(
            rows[i:i + 500], on_conflict="ticker,security_key,action,version_to",
            ignore_duplicates=True).execute()
    return len(rows)


def mark_changes_alerted(ticker, version_to):
    try:
        sb().table("etf_holdings_changes").update({"alerted": True}) \
            .eq("ticker", ticker).eq("version_to", version_to) \
            .gte("weight_new", ALERT_MIN_WEIGHT).execute()
        sb().table("etf_holdings_changes").update({"alerted": True}) \
            .eq("ticker", ticker).eq("version_to", version_to) \
            .gte("weight_prev", ALERT_MIN_WEIGHT).execute()
    except Exception as e:
        log_poller_error(POLLER, "mark_changes_alerted", e, {"ticker": ticker})


class AlertLedgerError(RuntimeError):
    """The alerts table could not be read or written. Raised, not swallowed:
    the caller must abandon this ETF for this run so its snapshot version and
    expense state are NOT advanced, and the next run re-detects the change.
    Returning False here used to mark the change as handled and lose it."""


def _alert_exists(dedup_key, within_days=None):
    try:
        q = sb().table("alerts").select("id").eq("extra->>dedup_key", dedup_key)
        if within_days:
            q = q.gte("created_at", (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat())
        return bool(q.limit(1).execute().data)
    except Exception as e:
        log_poller_error(POLLER, "alert_dedup_lookup", e, {"dedup_key": dedup_key})
        raise AlertLedgerError(f"dedup lookup failed: {e}") from e


def emit_alert(ticker, source, filing_type, impact, summary, extra, link=None, within_days=None):
    """Write one alert row. True if written, False if already sent.
    Raises AlertLedgerError if the ledger cannot be read or written."""
    dedup_key = extra["dedup_key"]
    if _alert_exists(dedup_key, within_days):
        return False
    extra = dict(extra)
    extra["summary_hash"] = hashlib.sha1(f"{ticker}|{summary}".encode()).hexdigest()[:20]
    extra.setdefault("filed_at", _now_iso())
    row = {
        "ticker": ticker, "summary": summary, "impact": impact, "source": source,
        "filing_type": filing_type, "delivered": False,
        "extra": tag_extra(extra, source, filing_type),
    }
    if link and str(link).startswith("http"):
        row["filing_url"] = link
    try:
        sb().table("alerts").insert(row).execute()
    except Exception as e:
        log_poller_error(POLLER, "alert_insert", e, {"dedup_key": dedup_key})
        raise AlertLedgerError(f"alert insert failed: {e}") from e
    logger.info("[ETF] ALERT %s %s | %s", impact, ticker, filing_type)
    return True


# ── Per-ETF work ──────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, mode, today, watched, states):
        self.mode = mode
        self.today = today
        self.watched = watched
        self.states = states
        self.abort = threading.Event()


def _company_due(state):
    ts = state.get("company_refreshed_at")
    if not ts:
        return True
    try:
        then = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - then >= timedelta(days=COMPANY_REFRESH_DAYS)
    except Exception:
        return True


def handle_holdings(ctx, ticker, fund, asset_class, raw, state, website, res):
    rows, version = fmp.normalize_holdings(raw)
    if not rows:
        res["note"] = "no holdings returned"
        return
    prev_version = state.get("holdings_version")
    if version == prev_version:
        return
    old = load_stored_holdings(ticker)
    as_of = fmp.snapshot_as_of(version)

    baseline = prev_version is None or not old
    if not baseline:
        # Compare against the FULL position count of the last snapshot, not the
        # stored rows (capped at MAX_ROWS), so a short read of a 500+ position
        # fund is caught too instead of re-baselining onto partial data.
        prev_count = int(state.get("holdings_count") or 0) or len(old)
        if prev_count >= 10 and len(rows) < MIN_KEEP_FRACTION * prev_count:
            res["note"] = f"partial read ignored ({len(rows)} vs {prev_count} last snapshot)"
            return

    added, removed, kept = diff_holdings(old, rows, MAX_ROWS)

    if not baseline and len(old) >= 20:
        churn = (len(added) + len(removed)) / max(len(old), 1)
        if churn > MAX_CHURN:
            res["note"] = f"churn {churn:.0%} treated as vendor re-keying; re-baselined"
            baseline = True

    if not baseline and (added or removed):
        changes = (
            [{"ticker": ticker, "security_key": k, "security_symbol": rows[k].get("security_symbol"),
              "security_name": rows[k].get("security_name"), "action": "ADDED",
              "weight_prev": None, "weight_new": _w(rows[k]),
              "version_from": prev_version, "version_to": version} for k in added] +
            [{"ticker": ticker, "security_key": k, "security_symbol": old[k].get("security_symbol"),
              "security_name": old[k].get("security_name"), "action": "REMOVED",
              "weight_prev": _w(old[k]), "weight_new": None,
              "version_from": prev_version, "version_to": version} for k in removed])
        res["changes"] += store_changes(changes)

        if ticker in ctx.watched and class_alerts_holdings(asset_class):
            sig_add = sorted((c for c in changes if c["action"] == "ADDED"
                              and c["weight_new"] >= ALERT_MIN_WEIGHT and is_alertable_line(c)),
                             key=lambda c: -c["weight_new"])
            sig_rem = sorted((c for c in changes if c["action"] == "REMOVED"
                              and c["weight_prev"] >= ALERT_MIN_WEIGHT and is_alertable_line(c)),
                             key=lambda c: -c["weight_prev"])
            if sig_add or sig_rem:
                n_small = len(changes) - len(sig_add) - len(sig_rem)
                summary, impact, ftype = holdings_alert(fund, ticker, sig_add, sig_rem, n_small,
                                                        len(rows), as_of)
                extra = {
                    "dedup_key": f"{SOURCE_PORTFOLIO}:{ticker}:{version}",
                    "company_name": fund, "etf_ticker": ticker,
                    "version_from": prev_version, "version_to": version, "as_of": as_of,
                    "added": [{"symbol": c["security_symbol"], "name": c["security_name"],
                               "weight": c["weight_new"]} for c in sig_add],
                    "removed": [{"symbol": c["security_symbol"], "name": c["security_name"],
                                 "weight_prev": c["weight_prev"]} for c in sig_rem],
                    "positions": len(rows), "smaller_changes": n_small,
                }
                if emit_alert(ticker, SOURCE_PORTFOLIO, ftype, impact, summary, extra, website):
                    res["alerts"] += 1
                    mark_changes_alerted(ticker, version)

    replace_holdings(ticker, kept, list(old), as_of)
    state.update(holdings_version=version, holdings_count=len(rows),
                 holdings_checked_at=_now_iso())


EXPENSE_DEDUP_DAYS = 30


def handle_expense(ctx, ticker, fund, info, state, website, res):
    er = fmp.expense_ratio(info)
    if er is None:
        return
    res["expense"] = fmp.expense_row(ticker, er, ctx.today)
    # Advance a COPY. The real state only moves once any alert this
    # confirmation owes has been written — if the write fails, the
    # confirmation is still pending next run instead of silently consumed.
    trial = dict(state)
    confirmed = step_expense(trial, er, ctx.today)
    if confirmed and ticker in ctx.watched:
        old, new = confirmed
        summary, impact, ftype = expense_alert(fund, ticker, old, new)
        # No date in the key: a crash between alert and state flush must not
        # let the same change re-confirm and alert again tomorrow. The 30-day
        # window still lets a genuine repeat of the same move alert later.
        extra = {"dedup_key": f"{SOURCE_EXPENSE}:{ticker}:{old}->{new}",
                 "company_name": fund, "etf_ticker": ticker,
                 "expense_ratio_old": old, "expense_ratio_new": new,
                 "change_bp": round((new - old) * 100, 2)}
        if emit_alert(ticker, SOURCE_EXPENSE, ftype, impact, summary, extra, website,
                      within_days=EXPENSE_DEDUP_DAYS):
            res["alerts"] += 1
    state.update(trial)
    state["expense_checked_at"] = _now_iso()


def process_etf(ctx, ticker, name):
    res = {"ticker": ticker, "master": None, "expense": None, "perf": None,
           "company": None, "state": None, "alerts": 0, "changes": 0, "note": None}
    if ctx.abort.is_set():
        return res
    state = dict(ctx.states.get(ticker) or {"ticker": ticker})
    try:
        info = fmp.raw_info(ticker)
        raw = fmp.raw_holdings(ticker)
        fund = (info or {}).get("name") or name or ticker
        website = (info or {}).get("website")
        asset_class = (info or {}).get("assetClass")

        if info:
            res["master"] = fmp.master_row(ticker, name, info)
        handle_holdings(ctx, ticker, fund, asset_class, raw, state, website, res)
        handle_expense(ctx, ticker, fund, info, state, website, res)

        if ctx.mode == "full":
            res["perf"] = fmp.performance_row(ticker, fmp.raw_price_change(ticker), ctx.today)
            if _company_due(state):
                res["company"] = fmp.company_row(ticker, info, fmp.raw_profile(ticker),
                                                 fmp.raw_sector_weights(ticker),
                                                 fmp.raw_country_weights(ticker))
                state["company_refreshed_at"] = _now_iso()
    except fmp.FMPError as e:
        ctx.abort.set()
        log_poller_error(POLLER, "fmp_quota", e, {"ticker": ticker})
        logger.error("[ETF] FMP rate limit persisted at %s — stopping this run: %s", ticker, e)
        res["alerts"] = 0
        return res
    except Exception as e:
        # Includes AlertLedgerError. State is deliberately NOT written: it may
        # be half-advanced, and leaving the stored state as it was makes the
        # next run re-detect whatever this one could not finish. Anything
        # already written (change rows, a sent alert) is idempotent on retry.
        log_poller_error(POLLER, "process_etf", e, {"ticker": ticker})
        logger.warning("[ETF] %s failed, will retry next run: %s", ticker, e)
        res["note"] = f"error: {e}"[:300]
        return res

    state["last_note"] = res["note"]
    state["updated_at"] = _now_iso()
    res["state"] = state
    if res["alerts"]:
        # Write-through: an ETF that just alerted persists its state now, not
        # at the next 500-ETF flush, so a crash in between cannot re-alert it.
        _upsert_chunks("etf_snapshot_state", [{k: state.get(k) for k in _STATE_COLS}], "ticker")
    return res


# ── Runner ────────────────────────────────────────────────────────────────────
_STATE_COLS = ("ticker", "holdings_version", "holdings_count", "holdings_checked_at",
               "expense_ratio", "expense_pending", "expense_pending_count", "expense_pending_date",
               "expense_checked_at", "company_refreshed_at", "last_note", "updated_at")


def _flush(buf):
    _upsert_chunks("etf_master", buf.pop("master", []), "ticker")
    _upsert_chunks("etf_expenses", buf.pop("expense", []), "ticker,date")
    _upsert_chunks("etf_performance", buf.pop("perf", []), "ticker,date")
    company = buf.pop("company", [])
    for c in company:
        c["updated_at"] = _now_iso()
    _upsert_chunks("etf_company_data", company, "ticker")
    states = [{k: s.get(k) for k in _STATE_COLS} for s in buf.pop("state", [])]
    _upsert_chunks("etf_snapshot_state", states, "ticker")


def run_etf_data_poller(mode="full", now=None):
    """One pass over the ETF universe. Logs and swallows every failure.
    `now` (an ET datetime) is for tests; production always uses the clock."""
    started = time.monotonic()
    now_et = now or datetime.now(ET)
    if not ANY_DAY and now_et.weekday() >= 5:
        logger.info("[ETF] Weekend — skipping %s run", mode)
        return
    try:
        master = load_master()
        if not master:
            logger.error("[ETF] etf_master is empty — run etf_csv_importer.py first")
            return
        states = load_states()
        watched = {t.upper() for t in get_watched_tickers(force=True)} & set(master)
        ctx = Ctx(mode, now_et.date().isoformat(), watched, states)
        # Watched funds first: their alerts should not wait behind 4,000 others.
        tickers = sorted(watched) + sorted(t for t in master if t not in watched)
        logger.info("[ETF] %s run: %d ETFs (%d watched), %d with prior state",
                    mode, len(tickers), len(watched), len(states))

        buf = defaultdict(list)
        totals = defaultdict(int)
        with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="etf") as pool:
            for i, res in enumerate(pool.map(lambda t: process_etf(ctx, t, master.get(t)), tickers), 1):
                for k in ("master", "expense", "perf", "company", "state"):
                    if res.get(k):
                        buf[k].append(res[k])
                totals["alerts"] += res["alerts"]
                totals["changes"] += res["changes"]
                if res.get("note"):
                    totals["notes"] += 1
                if i % FLUSH_EVERY == 0:
                    _flush(buf)
                    logger.info("[ETF] %d/%d processed — %d changes, %d alerts so far",
                                i, len(tickers), totals["changes"], totals["alerts"])
        _flush(buf)
        logger.info("[ETF] %s run done in %.0fs%s — %d holdings changes stored, %d alerts, "
                    "%d ETFs with notes", mode, time.monotonic() - started,
                    " (STOPPED EARLY: FMP rate limit)" if ctx.abort.is_set() else "",
                    totals["changes"], totals["alerts"], totals["notes"])
    except Exception as e:
        log_poller_error(POLLER, "run_etf_data_poller", e, {"mode": mode})
        logger.exception("[ETF] run failed: %s", e)


def run_etf_data_poller_full():
    run_etf_data_poller("full")


def run_etf_data_poller_light():
    run_etf_data_poller("light")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ANY_DAY = True
    run_etf_data_poller(sys.argv[1] if len(sys.argv) > 1 else "full")
