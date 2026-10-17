"""
mf_expense_poller.py
GQ FinXray US — Feature 20: MUTUAL FUND expense ratios (SEC prospectus fee tables).

What a US mutual fund charges is in its prospectus fee table, share class by
share class, and nowhere else with authority: there is no daily expense feed
for mutual funds the way vendors carry one for ETFs. So this poller reads the
table itself, out of the inline XBRL in Form 485BPOS and 497K, and keeps the
full breakdown rather than one blended number:

    management fee + 12b-1 fee + other expenses + acquired fund fees
      = total (gross) expense ratio
    total + fee waiver = net expense ratio      (what the holder actually pays)

The 12b-1 fee is why this matters more for mutual funds than for ETFs: it is
what separates an A class from a C class of the SAME fund, often by a full
percentage point a year, and it changes when a fund restructures its classes.

ALERT: a watched share class whose NEW prospectus fee table differs from the
previous one on file, by at least MF_FEE_MIN_DELTA percentage points. The first
table seen for a class is a baseline and never alerts — otherwise every holder
would be told their fees "changed" the week this poller went live. The alert
quotes both the old and new figure and the annual cost on $10,000 invested.

This is the mutual-fund poller. ETF expense changes are Feature 14 and come
from a different file; the two never share an alert, a source or a schedule.
Rule-based: no LLM, zero tokens.

Jobs (scheduled separately in main.py)
--------------------------------------
  run_mf_expense_poll()      daily   new prospectuses from registrants that run
                                     a watched fund -> fee table -> change alert
  run_mf_expense_backfill()  daily   funds with no fee table yet, a few per run,
                                     so a fund added to a watchlist already has
                                     a baseline to compare against

Tables: mf_expense_breakdown (one row per share class per filing),
mf_filings_processed (one document, once).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import mf_common as mc
import sec_fee_tables as ft

POLLER = "mf_expense_poller"
TAG = "MF_FEE"
SOURCE = "MF_FEE"

# 485BPOS only. The fee table is inline XBRL and only the annual prospectus
# carries it: 2026-10-17 diagnostics on watched funds showed 497/497K stickers
# with ZERO inline facts while the same fund's 485BPOS had 32-448. Reading
# stickers filled the document budget and baselined nothing.
PROSPECTUS_FORMS = ("485BPOS",)
BACKFILL_PER_CIK = int(os.getenv("MF_BACKFILL_PER_CIK", "2"))  # newest N annual prospectuses per registrant
DAYS_BACK = int(os.getenv("MF_FEE_DAYS_BACK", "3"))
POLL_DOC_BUDGET = int(os.getenv("MF_FEE_POLL_DOCS", "300"))
BACKFILL_DOCS = int(os.getenv("MF_FEE_BACKFILL_DOCS", "150"))
BACKFILL_LOOKBACK_DAYS = int(os.getenv("MF_FEE_BACKFILL_DAYS", "460"))

# A fee table is re-filed every year with the same numbers; only a real change
# should alert. 0.005 points is below any genuine fee move and above rounding.
MIN_DELTA = float(os.getenv("MF_FEE_MIN_DELTA", "0.005"))      # percentage points
HIGH_DELTA = float(os.getenv("MF_FEE_HIGH_DELTA", "0.10"))     # an increase this big is HIGH

# How often a long run says where it is. A backfill reads up to 150 large
# prospectuses at 3 SEC requests a second, so without this the only output is
# the summary line at the very end and the run is indistinguishable from hung.
PROGRESS_EVERY = int(os.getenv("MF_PROGRESS_EVERY", "10"))


def log(msg):
    mc.log(TAG, msg)


def _err(job, e, ctx=None):
    mc.log_poller_error(POLLER, job, e, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def latest_breakdown(ticker, before_accession=None):
    """The newest stored fee table for a share class, optionally the newest one
    filed BEFORE a given accession (so a re-run compares like for like)."""
    try:
        q = (mc.sb().table("mf_expense_breakdown").select("*")
             .eq("ticker", ticker).order("filing_date", desc=True).limit(5))
        rows = q.execute().data or []
    except Exception as e:
        _err("latest_breakdown", e, {"ticker": ticker})
        return None
    for r in rows:
        if before_accession and r.get("accession") == before_accession:
            continue
        return r
    return None


def store(tables, meta) -> int:
    rows = []
    for ticker, b in tables.items():
        rows.append({
            "ticker": ticker, "series_id": b.get("series_id"), "class_id": b.get("class_id"),
            "management_fee": b.get("management_fee"),
            "distribution_12b1": b.get("distribution_12b1"),
            "other_expenses": b.get("other_expenses"),
            "acquired_fund_fees": b.get("acquired_fund_fees"),
            "gross_expense_ratio": b.get("gross_expense_ratio"),
            "fee_waiver": b.get("fee_waiver"),
            "net_expense_ratio": b.get("net_expense_ratio"),
            "accession": meta["accession"], "form": meta.get("form"),
            "filing_date": meta.get("filing_date"), "source_url": meta.get("url"),
            "cik": meta.get("cik"), "updated_at": mc.now_iso(),
        })
    if not rows:
        return 0
    try:
        for i in range(0, len(rows), 200):
            (mc.sb().table("mf_expense_breakdown")
             .upsert(rows[i:i + 200], on_conflict="ticker,accession").execute())
        # The headline number also lands on mf_master, so the bot can answer
        # "what does this fund cost?" without a second table.
        for r in rows:
            net = r.get("net_expense_ratio")
            if net is None:
                continue
            (mc.sb().table("mf_master")
             .update({"expense_ratio": net, "updated_at": mc.now_iso()})
             .eq("ticker", r["ticker"]).execute())
    except Exception as e:
        _err("store", e, {"accession": meta.get("accession")})
        return 0
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────────────

def _compare_field(prev, new):
    """Which number to compare: net if both filings disclose it, else gross.
    Comparing a net against a gross would read a waiver expiring as a fee rise."""
    for f in ("net_expense_ratio", "gross_expense_ratio"):
        if prev.get(f) is not None and new.get(f) is not None:
            return f
    return None


def build_alert(fund_name, family, ticker, class_name, prev, new, field, meta):
    a, b = float(prev[field]), float(new[field])
    up = b > a
    kind = "net" if field == "net_expense_ratio" else "total"
    cls = f" {class_name}" if class_name else ""
    parts = [
        # `{b - a:+.2f}` already carries the sign. A second literal "+" in front
        # of it rendered every increase as "(++0.07 points)" in the live alert.
        f"{fund_name}{cls} ({ticker}) {'raised' if up else 'lowered'} its {kind} expense ratio "
        f"from {a:.2f}% to {b:.2f}% ({b - a:+.2f} points).",
        f"On $10,000 invested the annual cost moves from ${a * 100:,.2f} to ${b * 100:,.2f}.",
    ]
    bd = ft.breakdown_text(new)
    if bd:
        parts.append(f"New fee table: {bd}.")
    waiver_gone = (prev.get("fee_waiver") or 0) < 0 and not (new.get("fee_waiver") or 0) < 0
    if waiver_gone:
        parts.append("The fee waiver that was in force at the last prospectus is not in this one.")
    if family:
        parts.append(f"Fund family: {family}.")
    parts.append(f"Source: SEC Form {meta.get('form')} filed {mc.fmt_date(meta.get('filing_date'))}; "
                 f"the previous fee table was filed {mc.fmt_date(prev.get('filing_date'))}.")
    ftype = "MF_EXPENSE_INCREASE" if up else "MF_EXPENSE_DECREASE"
    impact = "HIGH" if up and (b - a) >= HIGH_DELTA else "MEDIUM"
    return " ".join(parts), impact, ftype


def fee_alerts(tables, meta, watched) -> int:
    """Called BEFORE store(), so "previous" is still the previous filing."""
    u = mc.load_mf_universe()
    sent = 0
    for ticker in sorted(t for t in tables if t in watched and t in u["by_ticker"]):
        new = tables[ticker]
        prev = latest_breakdown(ticker, before_accession=meta["accession"])
        if not prev:
            continue                              # first table for this class: baseline
        field = _compare_field(prev, new)
        if not field:
            continue
        a, b = float(prev[field]), float(new[field])
        if abs(b - a) < MIN_DELTA:
            continue
        row = u["by_ticker"][ticker]
        name = row.get("series_name") or ticker
        summary, impact, ftype = build_alert(name, row.get("entity_name"), ticker,
                                             row.get("class_name"), prev, new, field, meta)
        key = f"{SOURCE}:{ticker}:{meta['accession']}"
        sent += mc.emit_alerts(
            SOURCE, [ticker], summary, impact, ftype,
            {"series_id": row.get("series_id"), "class_id": row.get("class_id"),
             "series_name": name, "company_name": name, "fund_family": row.get("entity_name"),
             "fund_type": "MUTUAL_FUND", "class_name": row.get("class_name"),
             "expense_field": field, "expense_prev": a, "expense_new": b,
             "expense_delta": round(b - a, 4), "breakdown": {k: new.get(k) for k in ft.FIELDS},
             "breakdown_prev": {k: prev.get(k) for k in ft.FIELDS},
             "accession": meta["accession"], "form": meta.get("form"),
             "filing_date": meta.get("filing_date"),
             "prev_filing_date": prev.get("filing_date"),
             "headline": f"{name}: expense ratio {'raised' if b > a else 'lowered'}"},
            url=meta.get("url"), dedup_key=key, poller=POLLER)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Document handling
# ─────────────────────────────────────────────────────────────────────────────

def _maps(universe):
    by_class = {r["class_id"]: t for t, r in universe["by_ticker"].items() if r.get("class_id")}
    by_series = {sid: list(v["tickers"]) for sid, v in universe["by_series"].items()}
    return by_class, by_series


def process_filing(meta, watched, alert=True) -> dict:
    """One prospectus: read its fee tables, alert on changes, store them."""
    u = mc.load_mf_universe()
    series = mc.fetch_series_header(meta["cik"], meta["accession"])
    filing_series = [s["series_id"] for s in series] if series else \
        (u["by_cik"].get(mc.cik_plain(meta["cik"])) or [])
    mf_series = set(u["by_series"])
    filing_series = [s for s in filing_series if s in mf_series]
    if not filing_series:
        mc.mark_processed(meta["key"], "FEE", meta, "NOT_MUTUAL_FUND")
        return {"classes": 0, "alerts": 0}

    html = mc.sec.text(meta["url"])
    if not html:
        mc.mark_processed(meta["key"], "FEE", meta, "NO_DOC")
        return {"classes": 0, "alerts": 0}

    by_class, by_series = _maps(u)
    tables = ft.extract_fee_tables(html, by_class=by_class, by_series=by_series,
                                   filing_series=filing_series)
    # Only share classes this registry knows as mutual funds. A trust that runs
    # ETFs and mutual funds side by side files one document for both.
    tables = {t: b for t, b in tables.items() if t in u["by_ticker"]}
    if not tables:
        mc.mark_processed(meta["key"], "FEE", meta, "NO_FEE_TABLE")
        return {"classes": 0, "alerts": 0}

    alerts = fee_alerts(tables, meta, watched) if alert else 0
    n = store(tables, meta)
    mc.mark_processed(meta["key"], "FEE", meta, "OK", classes=n, alerts=alerts)
    return {"classes": n, "alerts": alerts}


def _prospectus_filings(ciks, days_back, budget, priority=(), per_cik=None):
    """
    Prospectuses from these registrants: a registrant in `priority` first, then
    everything else, newest first inside each group.

    Without `priority` the final date sort silently undid the backfill's own
    watched-first ordering: it ranked the registrants behind watched classes to
    the front of `ciks`, then re-sorted every document by filing date, so a
    watched class whose prospectus was filed eleven months ago lost its slot to
    newer filings from trusts nobody follows. The classes that can actually
    alert were baselined last.
    """
    priority = {mc.cik_plain(c) for c in priority}
    out = []
    for cik in ciks:
        if len(out) >= budget:
            break
        try:
            fl = sorted(mc.recent_filings(cik, PROSPECTUS_FORMS, days_back),
                        key=lambda f: f.get("filing_date") or "", reverse=True)
            for f in (fl[:per_cik] if per_cik else fl):
                name = (f.get("primary_doc") or "").rsplit("/", 1)[-1]
                if not name:
                    continue
                out.append({"accession": f["accession"], "cik": mc.cik_plain(cik),
                            "form": f["form"], "filing_date": f["filing_date"],
                            "url": mc.archive(cik, f["accession"], name),
                            "key": f"{f['accession']}:{name}"})
        except Exception as e:
            _err("prospectus_filings", e, {"cik": cik})
    # Two stable passes: newest-first, then priority-first. Python's sort is
    # stable, so the date order survives inside each group.
    out.sort(key=lambda m: m.get("filing_date") or "", reverse=True)
    if priority:
        out.sort(key=lambda m: m["cik"] not in priority)
    return out[:budget]


def run_mf_expense_poll(days_back: int = DAYS_BACK):
    """Daily: new prospectuses -> fee tables -> change alerts for watched classes."""
    try:
        watched = mc.watched_tickers()
        u = mc.load_mf_universe()
        if not u["by_ticker"]:
            log("mf_master is empty — run mf_poller master first")
            return 0
        watched_ciks = sorted({v["cik"] for t, v in u["by_ticker"].items() if t in watched})
        if not watched_ciks:
            log("no watched mutual funds — nothing to poll")
            return 0
        metas = _prospectus_filings(watched_ciks, days_back, POLL_DOC_BUDGET,
                                    priority=watched_ciks)
        done = mc.processed_keys("FEE", {m["key"] for m in metas})
        todo = [m for m in metas if m["key"] not in done]
        classes = alerts = 0
        for i, m in enumerate(todo, 1):
            try:
                r = process_filing(m, watched, alert=True)
                classes += r["classes"]
                alerts += r["alerts"]
            except Exception as e:
                _err("process_filing", e, {"accession": m["accession"]})
                mc.mark_processed(m["key"], "FEE", m, "ERROR")
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                log(f"expense poll: {i}/{len(todo)} prospectuses read, "
                    f"{classes} share classes, {alerts} alerts")
        log(f"expense poll: {len(todo)} prospectuses, {classes} share classes, {alerts} alerts")
        return alerts
    except Exception as e:
        _err("run_mf_expense_poll", e)
        return 0


def run_mf_expense_backfill(max_docs: int | None = None):
    """
    Daily: give share classes with no fee table on file a baseline.

    alert=False on purpose — a first fee table is not a change.
    """
    try:
        u = mc.load_mf_universe()
        have = set()
        try:
            have = {r["ticker"] for r in mc.paged(
                lambda: mc.sb().table("mf_expense_breakdown").select("ticker").order("ticker"))}
        except Exception as e:
            _err("backfill_coverage", e)
        watched = mc.watched_tickers()
        missing = [t for t in u["by_ticker"] if t not in have]
        # Watched classes first: those are the ones that can alert next quarter.
        missing.sort(key=lambda t: (t not in watched, t))
        ciks, seen = [], set()
        for t in missing:
            c = u["by_ticker"][t]["cik"]
            if c not in seen:
                seen.add(c)
                ciks.append(c)
        budget = max_docs if max_docs is not None else BACKFILL_DOCS
        watched_ciks = {u["by_ticker"][t]["cik"] for t in watched if t in u["by_ticker"]}
        # Say so BEFORE the slow part. Finding the prospectuses costs one SEC
        # submissions call per registrant, so a 150-document run spends a
        # couple of minutes here before it opens anything — and with the only
        # log line at the end, the run looked hung.
        log(f"expense backfill: {len(missing)} share classes without a fee table; "
            f"searching {min(len(ciks), budget * 2)} registrants for prospectuses "
            f"(one SEC call each, ~{min(len(ciks), budget * 2) / 3:.0f}s)")
        metas = _prospectus_filings(ciks[:budget * 2], BACKFILL_LOOKBACK_DAYS,
                                    budget * 2, priority=watched_ciks,
                                    per_cik=BACKFILL_PER_CIK)
        done = mc.processed_keys("FEE", {m["key"] for m in metas})
        todo = [m for m in metas if m["key"] not in done][:budget]
        log(f"expense backfill: {len(todo)} prospectuses to read "
            f"({len(metas) - len(todo)} already done)")
        classes = 0
        for i, m in enumerate(todo, 1):
            try:
                classes += process_filing(m, set(), alert=False)["classes"]
            except Exception as e:
                _err("backfill_filing", e, {"accession": m["accession"]})
                mc.mark_processed(m["key"], "FEE", m, "ERROR")
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                log(f"expense backfill: {i}/{len(todo)} prospectuses read, "
                    f"{classes} share classes baselined")
        log(f"expense backfill: {len(todo)} prospectuses, {classes} share classes baselined, "
            f"{len(missing) - classes} still without a fee table")
        return classes
    except Exception as e:
        _err("run_mf_expense_backfill", e)
        return 0


def coverage() -> dict:
    u = mc.load_mf_universe()
    try:
        have = {r["ticker"] for r in mc.paged(
            lambda: mc.sb().table("mf_expense_breakdown").select("ticker").order("ticker"))}
    except Exception:
        have = set()
    return {"share_classes": len(u["by_ticker"]),
            "with_fee_table": len(have & set(u["by_ticker"])),
            "without_fee_table": len(set(u["by_ticker"]) - have)}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if cmd == "poll":
        run_mf_expense_poll()
    elif cmd == "backfill":
        run_mf_expense_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "coverage":
        print(coverage())
    else:
        print("usage: python mf_expense_poller.py poll|backfill [n]|coverage")
