"""
etf_expense_extractor.py
GQ FinXray US — ETF fee-table breakdown from SEC prospectuses.

FMP gives one expense ratio per fund. The prospectus fee table has the parts:
management fee, 12b-1 fee, other expenses, acquired fund fees, total (gross)
expenses, fee waiver, and net expenses. Since 2011 those rows are tagged in
XBRL (the risk/return taxonomy, prefix `rr:` then `oef:`), and since 2023 the
485BPOS / 497K documents are inline XBRL, so the numbers can be read straight
out of the HTML the fund-manager poller already downloads.

Two entry points:
  extract_fee_tables(html, ...)   pure parser, unit-tested offline
  store_from_filing(html, meta)   called from fund_manager_poller for every
                                  prospectus it processes (new filings)
  run_expense_backfill()          daily: newest prospectus for ETF registrants
                                  that have no breakdown yet, a few per run

Data only: Feature 14 already alerts on expense-ratio changes, and Feature 16
on fee-change filings. The breakdown is added to the Feature 14 alert text.
"""

import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
POLLER = "etf_expense_extractor"

FEE_TAGS = {
    "ManagementFeesOverAssets": "management_fee",
    "DistributionAndService12b1FeesOverAssets": "distribution_12b1",
    "OtherExpensesOverAssets": "other_expenses",
    "AcquiredFundFeesAndExpensesOverAssets": "acquired_fund_fees",
    "ExpensesOverAssets": "gross_expense_ratio",
    "FeeWaiverOrReimbursementOverAssets": "fee_waiver",
    "NetExpensesOverAssets": "net_expense_ratio",
}
FIELDS = tuple(FEE_TAGS.values())
BACKFILL_DOCS = int(os.getenv("ETF_EXPENSE_BACKFILL_DOCS", "150"))
BACKFILL_LOOKBACK_DAYS = int(os.getenv("ETF_EXPENSE_BACKFILL_LOOKBACK_DAYS", "460"))
PROSPECTUS_FORMS = ("485BPOS", "497K")

_RE_CONTEXT = re.compile(r'<(?:[\w-]+:)?context\b[^>]*?\bid\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</(?:[\w-]+:)?context\s*>',
                         re.S | re.I)
_RE_MEMBER = re.compile(r'<(?:[\w-]+:)?explicitMember\b[^>]*?\bdimension\s*=\s*["\']([^"\']+)["\'][^>]*>\s*([^<]+?)\s*<',
                        re.S | re.I)
_RE_NONFRACTION = re.compile(r'<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction\s*>', re.S | re.I)
_RE_NONNUMERIC = re.compile(r'<ix:nonNumeric\b([^>]*)>(.*?)</ix:nonNumeric\s*>', re.S | re.I)
_RE_ATTR = re.compile(r'([\w:.-]+)\s*=\s*["\']([^"\']*)["\']')
_RE_TAG = re.compile(r'<[^>]+>')
_RE_SERIES = re.compile(r'S\d{9}')
_RE_CLASS = re.compile(r'C\d{9}')
_DASHES = {"", "-", "—", "–", "none", "n/a", "na", "nil"}


def _attrs(s):
    return {k.lower(): v for k, v in _RE_ATTR.findall(s)}


def _local(name):
    return (name or "").split(":")[-1]


def _text(inner):
    t = _RE_TAG.sub("", inner or "")
    return re.sub(r"&nbsp;|&#160;|\s+", " ", t).strip()


def parse_number(text, attrs):
    """Displayed text + ix attributes -> percent of assets (0.03 = 0.03%), or None."""
    fmt = (attrs.get("format") or "").lower()
    t = text.strip().lower()
    if "zero" in fmt or t in _DASHES:
        val = 0.0
    else:
        t = t.replace(",", "").replace("$", "").replace("%", "").strip()
        neg = t.startswith("(") and t.endswith(")")
        t = t.strip("()").strip()
        try:
            val = float(t)
        except ValueError:
            return None
        if neg:
            val = -val
    try:
        scale = int(attrs.get("scale") or 0)
    except ValueError:
        scale = 0
    val *= 10 ** scale
    if attrs.get("sign") == "-":
        val = -val
    pct = val * 100.0                        # XBRL "pure" fraction -> percent
    if not -10.0 <= pct <= 25.0:
        return None
    return round(pct, 4)


def parse_contexts(html):
    """{context id: {"series": S..., "class": C..., "members": (dims...)}}"""
    out = {}
    for cid, body in _RE_CONTEXT.findall(html):
        series = klass = None
        members = []
        for dim, member in _RE_MEMBER.findall(body):
            d = _local(dim).lower()
            members.append((d, member.strip()))
            if "legalentity" in d or "series" in d:
                m = _RE_SERIES.search(member)
                series = m.group(0) if m else series
            elif "class" in d:
                m = _RE_CLASS.search(member)
                klass = m.group(0) if m else klass
        out[cid] = {"series": series, "class": klass, "members": tuple(sorted(members))}
    return out


def extract_fee_tables(html, by_class=None, by_series=None, filing_series=None):
    """
    html           prospectus document (inline XBRL)
    by_class       {class_id: ticker}
    by_series      {series_id: [tickers]}
    filing_series  [series_id] named in this filing's SEC header
    -> {ticker: {field: percent, "series_id", "class_id"}}

    A context is tied to a ticker by, in order: a dei:TradingSymbol fact with
    the same dimensions, its ClassAxis id, a series with exactly one ticker, or
    -- for a filing about a single one-ticker series -- a context with no
    dimensions at all.
    """
    by_class, by_series = by_class or {}, by_series or {}
    ctx = parse_contexts(html)
    if not ctx:
        return {}

    sym_by_members = {}
    for attrs_s, inner in _RE_NONNUMERIC.findall(html):
        a = _attrs(attrs_s)
        if _local(a.get("name")) == "TradingSymbol" and a.get("contextref") in ctx:
            sym = _text(inner).upper()
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
                sym_by_members[ctx[a["contextref"]]["members"]] = sym

    def ticker_for(c):
        if c["members"] in sym_by_members:
            return sym_by_members[c["members"]]
        if c["class"] and c["class"] in by_class:
            return by_class[c["class"]]
        if c["series"] and len(by_series.get(c["series"]) or []) == 1:
            return by_series[c["series"]][0]
        if not c["members"] and filing_series and len(filing_series) == 1:
            ts = by_series.get(filing_series[0]) or []
            if len(ts) == 1:
                return ts[0]
        return None

    out = {}
    for attrs_s, inner in _RE_NONFRACTION.findall(html):
        a = _attrs(attrs_s)
        field = FEE_TAGS.get(_local(a.get("name")))
        c = ctx.get(a.get("contextref"))
        if not field or not c:
            continue
        t = ticker_for(c)
        if not t:
            continue
        v = parse_number(_text(inner), a)
        if v is None:
            continue
        rec = out.setdefault(t, {"series_id": c["series"], "class_id": c["class"]})
        rec.setdefault(field, v)                 # first value wins (fee table precedes examples)
    # Waivers are shown as negatives in the table; store them that way always.
    for rec in out.values():
        if rec.get("fee_waiver") is not None:
            rec["fee_waiver"] = -abs(rec["fee_waiver"])
        if rec.get("net_expense_ratio") is None and rec.get("gross_expense_ratio") is not None:
            rec["net_expense_ratio"] = round(rec["gross_expense_ratio"] + (rec.get("fee_waiver") or 0.0), 4)
    return {t: r for t, r in out.items() if any(r.get(f) is not None for f in FIELDS)}


def breakdown_text(b):
    """One sentence for an alert, e.g. 'management fee 0.20%, other expenses
    0.05%, total 0.25%, fee waiver -0.05%, net 0.20%'."""
    if not b:
        return None
    parts = []
    for field, label in (("management_fee", "management fee"), ("distribution_12b1", "12b-1 fee"),
                         ("other_expenses", "other expenses"), ("acquired_fund_fees", "acquired fund fees"),
                         ("gross_expense_ratio", "total"), ("fee_waiver", "fee waiver"),
                         ("net_expense_ratio", "net")):
        v = b.get(field)
        if v is None or (field in ("distribution_12b1", "acquired_fund_fees", "fee_waiver") and v == 0):
            continue
        parts.append(f"{label} {v:.2f}%")
    return ", ".join(parts) or None


# ── Storage (Supabase) ────────────────────────────────────────────────────────
def _now():
    return datetime.now(timezone.utc).isoformat()


def _etf_tickers(sb, tickers):
    tickers, out = sorted(tickers), set()
    for i in range(0, len(tickers), 200):
        rows = sb.table("etf_master").select("ticker").in_("ticker", tickers[i:i + 200]).execute().data or []
        out |= {r["ticker"] for r in rows}
    return out


def store(sb, tables, meta):
    """tables from extract_fee_tables; keeps ETFs only. -> rows written"""
    if not tables:
        return 0
    etfs = _etf_tickers(sb, tables.keys())
    rows = [dict({f: rec.get(f) for f in FIELDS}, ticker=t, series_id=rec.get("series_id"),
                 class_id=rec.get("class_id"), accession=meta["accession"], form=meta.get("form"),
                 filing_date=meta.get("filing_date"), source_url=meta.get("url"), extracted_at=_now())
            for t, rec in tables.items() if t in etfs]
    for i in range(0, len(rows), 500):
        sb.table("etf_expense_breakdown").upsert(rows[i:i + 500], on_conflict="ticker,accession").execute()
    return len(rows)


def latest(sb, ticker):
    """Most recent breakdown for one ETF, or None."""
    try:
        r = (sb.table("etf_expense_breakdown").select("*").eq("ticker", ticker)
             .order("filing_date", desc=True).limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        return None


def _maps(universe):
    by_class = {v["class_id"]: t for t, v in universe["by_ticker"].items() if v.get("class_id")}
    by_series = {sid: list(v.get("tickers") or []) for sid, v in universe["by_series"].items()}
    return by_class, by_series


def _fill_ids(tables, universe):
    """A ticker matched through dei:TradingSymbol may lack ids in its context."""
    for t, rec in tables.items():
        u = universe["by_ticker"].get(t) or {}
        rec["series_id"] = rec.get("series_id") or u.get("series_id")
        rec["class_id"] = rec.get("class_id") or u.get("class_id")
    return tables


def store_from_filing(html, meta, universe, filing_series, sb):
    """Hook for fund_manager_poller: the prospectus is already downloaded."""
    by_class, by_series = _maps(universe)
    tables = _fill_ids(extract_fee_tables(html, by_class, by_series, filing_series), universe)
    return store(sb, tables, meta)


def _paged_col(sb, table, col, eq=None):
    out, page = set(), 0
    while True:
        q = sb.table(table).select(col)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        chunk = q.range(page * 1000, page * 1000 + 999).execute().data or []
        out |= {r.get(col) for r in chunk}
        if len(chunk) < 1000:
            return out - {None}
        page += 1


def run_expense_backfill(max_docs=None):
    """
    Fill fee tables for ETF series that have none yet. Per registrant, walks
    its prospectuses newest first until every ETF series it runs is covered or
    the lookback runs out. A per-run document budget keeps the SEC lane quiet;
    new prospectuses after that arrive through the fund-manager hook. Each
    filing is read once (fund_filings_processed, purpose EXPENSE).
    """
    import fund_manager_poller as fm
    from datetime import date, timedelta
    budget = max_docs or BACKFILL_DOCS
    try:
        sb = fm.sb()
        u = fm.load_universe()
        if not u["by_ticker"]:
            return
        have = _paged_col(sb, "etf_expense_breakdown", "series_id")
        missing_by_cik = {}
        for sid, v in u["by_series"].items():
            if v.get("fund_type") == "ETF" and sid not in have:
                missing_by_cik.setdefault(v["cik"], set()).add(sid)
        cutoff = (date.today() - timedelta(days=BACKFILL_LOOKBACK_DAYS)).isoformat()
        by_class, by_series = _maps(u)
        written = docs = 0
        for cik in sorted(missing_by_cik):
            missing = missing_by_cik[cik]
            if budget <= 0:
                break
            try:
                sub = fm.sec.json(fm.URL_SUBMISSIONS.format(cik10=cik)) or {}
                rec = (sub.get("filings") or {}).get("recent") or {}
                for j, form in enumerate(rec.get("form") or []):
                    if not missing or budget <= 0 or rec["filingDate"][j] < cutoff:
                        break
                    if form not in PROSPECTUS_FORMS or not rec["primaryDocument"][j]:
                        continue
                    adsh, doc = rec["accessionNumber"][j], rec["primaryDocument"][j]
                    if fm.already_processed(adsh, "EXPENSE"):
                        continue
                    budget -= 1
                    meta = {"accession": adsh, "cik": cik, "form": form,
                            "filing_date": rec["filingDate"][j], "url": fm._archive(cik, adsh, doc)}
                    series = [x["series_id"] for x in fm.fetch_series_header(cik, adsh)]
                    if series and not (set(series) & missing):
                        fm.mark_processed(adsh, "EXPENSE", cik, form, meta["filing_date"], "SKIPPED")
                        continue
                    html = fm.sec.text(meta["url"])
                    if not html:
                        continue                                  # transient; retried next run
                    docs += 1
                    tables = _fill_ids(extract_fee_tables(html, by_class, by_series, series), u)
                    n = store(sb, tables, meta)
                    written += n
                    missing -= {r.get("series_id") for r in tables.values()}
                    fm.mark_processed(adsh, "EXPENSE", cik, form, meta["filing_date"],
                                      "OK" if n else "NO_FEE_TABLE", n_series=len(series), n_items=n)
            except Exception as e:
                fm.log_poller_error("run_expense_backfill", e, {"cik": cik})
        fm.log(f"expense backfill: {docs} prospectuses read, {written} ETF fee tables stored, "
               f"{sum(len(v) for v in missing_by_cik.values())} series still without one")
    except Exception as e:
        logger.exception("[ETF-FEES] backfill failed: %s", e)
