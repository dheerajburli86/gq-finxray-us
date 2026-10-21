"""
mf_poller.py
GQ FinXray US — mutual funds: scheme master + quarterly portfolio alerts.

  refresh_mf_master()   daily   SEC fund ticker registry (every mutual fund share
                                class with a ticker) + SEC's series/class file
                                (fund, class and registrant names) -> mf_master.
                                Watched funds also get FMP's fund info (family,
                                category, AUM, expense ratio) when FMP has it.
  run_mf_holdings()     daily   For each WATCHED mutual fund: its newest public
                                N-PORT filing from SEC EDGAR. A new one is diffed
                                against the last -> holdings added / exited /
                                resized (Feature 18, source MF_PORTFOLIO).

N-PORT is the only full holdings disclosure a US mutual fund makes: public once
a quarter, filed up to 60 days after quarter-end. So these alerts are quarterly
and late by design -- there is no daily mutual-fund holdings feed to use.

Fund manager changes (Feature 15) and fund actions (Feature 16) already cover
mutual funds; they only needed mutual funds to be watchable (telegram_bot /add
now accepts them from mf_master). Fee-table changes come from
etf_expense_extractor (MF_EXPENSE, Feature 18).
"""

import csv
import io
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET_XML
from datetime import date, datetime, timedelta, timezone

import etf_data_poller as edp
import etf_fmp_client as fmp
import fund_manager_poller as fm
from feature_map import tag_extra

logger = logging.getLogger(__name__)
POLLER = "mf_poller"
SOURCE = "MF_PORTFOLIO"

SERIES_CLASS_BASE = "https://www.sec.gov/files/investment/data/other/"
MIN_MF_TICKERS = 5000
FMP_REFRESH_DAYS = 7
NPORT_LOOKBACK_DAYS = 200
HEADER_BUDGET = 2500                  # filing headers read per run
DOC_BUDGET = 1500                     # N-PORT documents downloaded per run
RUN_SECONDS = 90 * 60                 # stop a run after this long, resume next run
RECHECK_WATCHED_DAYS = 1              # how often a registrant with a watched fund is re-listed
RECHECK_DAYS = 3                      # ... and any other registrant
DUE_SLACK = timedelta(hours=3)        # absorbs run-start jitter in _due()
ERROR_RETRY_DAYS = 7                  # a fund whose filing could not be read waits this long
MAX_STORED = 500                      # heaviest positions kept per fund
ALERT_MIN_WEIGHT = 0.5                # percent of net assets
ALERT_HIGH_WEIGHT = 5.0
MAX_CHURN = 0.6
MIN_KEEP_FRACTION = 0.5
DEBT_SKIP_SHARE = 0.5                 # bond funds: store, do not alert adds/exits
LIST_LIMIT = 8
DEDUP_DAYS = 200


# fund_type in mf_master: which rows can produce portfolio alerts.
#   MUTUAL_FUND  ordinary open-end funds                      -> alerts
#   MONEY_MARKET stable-NAV funds; they file N-MFP, not N-PORT -> listed, no alerts
#   INSURANCE    sold only inside annuities/policies           -> listed, no alerts
INSURANCE_RE = re.compile(r"variable|separate account|college retirement|insurance (products|series|trust|fund)"
                          r"|annuity|life insurance|\bVIT\b", re.I)
MONEY_MARKET_RE = re.compile(r"money market|treasury (obligations|fund|money)|cash (reserves|management|trust)"
                             r"|government (obligations|cash|money)|prime (obligations|fund|reserves)|liquidity"
                             r"|municipal money|tax.exempt (money|cash)|reserve fund|sweep", re.I)
ALERT_FUND_TYPES = ("MUTUAL_FUND",)
RETIRE_MAX_FRACTION = 0.15            # more than this "gone" at once = a bad download


def classify_mf(ticker, entity_name=None, series_name=None):
    t = (ticker or "").upper()
    if not (len(t) == 5 and t.endswith("X") and t.isalpha()) or INSURANCE_RE.search(entity_name or ""):
        return "INSURANCE"
    if MONEY_MARKET_RE.search(series_name or ""):
        return "MONEY_MARKET"
    return "MUTUAL_FUND"


def sb():
    return fm.sb()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _paged(query_fn, size=1000):
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


def _num(v):
    try:
        return None if v in (None, "", "N/A") else float(v)
    except (TypeError, ValueError):
        return None


# ── Scheme master ────────────────────────────────────────────────────────────
def series_class_urls(year):
    for y in (year, year - 1):
        for sub in ("investment-company-series-class-information/",
                    "investment-company-series-and-class-information/"):
            for fn in (f"investment-company-series-class-{y}.csv",
                       f"investment_company_series_class_{y}.csv"):
                yield SERIES_CLASS_BASE + sub + fn


def parse_series_class_csv(text):
    """SEC series/class CSV -> {class_id: {...}, series_id: {...}} (headers matched loosely)."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        return {}
    norm = [re.sub(r"[^a-z]", "", h.lower()) for h in header]

    def col(*keys):
        for k in keys:
            for i, h in enumerate(norm):
                if h == k:
                    return i
        for k in keys:
            for i, h in enumerate(norm):
                if k in h:
                    return i
        return None

    ix = {"entity": col("entityname", "nameofinvestmentcompany"), "series_id": col("seriesid"),
          "series_name": col("seriesname"), "class_id": col("classid"), "class_name": col("classname"),
          "city": col("city"), "state": col("state")}
    if ix["series_id"] is None:
        return {}
    out = {}
    for row in reader:
        def g(k):
            i = ix[k]
            return (row[i].strip() or None) if i is not None and i < len(row) else None
        rec = {"entity_name": g("entity"), "series_id": g("series_id"), "series_name": g("series_name"),
               "class_id": g("class_id"), "class_name": g("class_name"), "city": g("city"), "state": g("state")}
        if rec["class_id"]:
            out[rec["class_id"]] = rec
        if rec["series_id"] and rec["series_id"] not in out:
            out[rec["series_id"]] = rec
    return out


def fetch_series_class():
    for url in series_class_urls(date.today().year):
        r = fm.sec.get(url, timeout=120)
        if r is not None and r.status_code == 200 and len(r.content) > 100_000:
            try:
                names = parse_series_class_csv(r.content.decode("utf-8", "ignore"))
            except csv.Error as e:
                fm.log_poller_error("mf_series_class_parse", e, {"url": url})
                continue
            if names:
                fm.log(f"series/class names: {len(names):,} rows from {url.rsplit('/', 1)[1]}")
                return names
    fm.log_poller_error("mf_series_class", "SEC series/class CSV unavailable")
    return {}


def _cik_plain(cik):
    """mf_master keeps CIKs without leading zeros; SEC URLs want them padded."""
    try:
        return str(int(cik))
    except (TypeError, ValueError):
        return str(cik)


def master_rows(universe, names):
    rows = []
    for t, v in universe["by_ticker"].items():
        if v.get("fund_type") != "MF":
            continue
        n = names.get(v.get("class_id")) or names.get(v.get("series_id")) or {}
        rows.append({"ticker": t, "cik": _cik_plain(v["cik"]), "series_id": v["series_id"], "class_id": v.get("class_id"),
                     "entity_name": n.get("entity_name"), "series_name": n.get("series_name"),
                     "fund_type": classify_mf(t, n.get("entity_name"), n.get("series_name")),
                     "class_name": n.get("class_name") if n.get("class_id") == v.get("class_id") else None,
                     "city": n.get("city"), "state": n.get("state"), "is_active": True, "updated_at": _now()})
    return rows


def watched_mfs(universe=None):
    """{ticker: universe row} for watched tickers that are mutual funds."""
    u = universe or fm.load_universe()
    out = {t: u["by_ticker"][t] for t in fm.watched_tickers()
           if t in u["by_ticker"] and u["by_ticker"][t].get("fund_type") == "MF"}
    if not out:
        return out
    types = {}
    ticks = sorted(out)
    for i in range(0, len(ticks), 200):
        for r in (sb().table("mf_master").select("ticker, fund_type").in_("ticker", ticks[i:i + 200])
                  .execute().data or []):
            types[r["ticker"]] = r.get("fund_type")
    # Money market and insurance-only funds are listed so they can be found,
    # but they have no N-PORT book to alert on. Unknown (not in mf_master yet) passes.
    return {t: v for t, v in out.items() if types.get(t) in (None,) + ALERT_FUND_TYPES}


def refresh_mf_master():
    try:
        u = fm.load_universe(force=True)
        names = fetch_series_class()
        rows = master_rows(u, names)
        if len(rows) < MIN_MF_TICKERS:
            fm.log_poller_error("refresh_mf_master", f"only {len(rows)} mutual fund tickers -- skipped")
            return
        for i in range(0, len(rows), 1000):
            sb().table("mf_master").upsert(rows[i:i + 1000], on_conflict="ticker").execute()
        live = {r["ticker"] for r in rows}
        # Insurance-only funds may be absent from the registry; they are never retired here.
        stored = {r["ticker"] for r in _paged(lambda: sb().table("mf_master").select("ticker, fund_type")
                                               .eq("is_active", True).order("ticker"))
                  if r.get("fund_type") != "INSURANCE"}
        gone = sorted(stored - live)
        if stored and len(gone) > RETIRE_MAX_FRACTION * len(stored):
            fm.log_poller_error("refresh_mf_master", f"{len(gone)} of {len(stored)} funds missing from the "
                                "registry -- looks like a bad download, retiring none")
            gone = []
        for i in range(0, len(gone), 100):
            sb().table("mf_master").update({"is_active": False}).in_("ticker", gone[i:i + 100]).execute()
        enriched = enrich_watched(u)
        fm.log(f"mf_master: {len(rows):,} share classes, {len(gone)} retired, {enriched} enriched from FMP")
    except Exception as e:
        fm.log_poller_error("refresh_mf_master", e)


def enrich_watched(u):
    """FMP fund info for watched mutual funds (weekly). FMP's coverage of mutual
    funds is partial; a miss just leaves the SEC fields."""
    watched = sorted(watched_mfs(u))
    if not watched:
        return 0
    have = {r["ticker"]: r for r in sb().table("mf_master").select("ticker, fmp_checked_at")
            .in_("ticker", watched).execute().data or []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=FMP_REFRESH_DAYS)
    n = 0
    for t in watched:
        at = (have.get(t) or {}).get("fmp_checked_at")
        try:
            if at and datetime.fromisoformat(str(at).replace("Z", "+00:00")) > cutoff:
                continue
        except ValueError:
            pass
        try:
            info = fmp.raw_info(t) or {}
        except Exception as e:
            fm.log_poller_error("mf_fmp_info", e, {"ticker": t})
            break
        upd = {"fmp_checked_at": _now()}
        if info:
            aum = fmp.num(info.get("assetsUnderManagement") or info.get("aum") or info.get("netAssets"))
            upd.update(fund_family=(info.get("etfCompany") or info.get("fundFamily") or None),
                       category=(info.get("assetClass") or info.get("category") or None),
                       aum=aum, expense_ratio=fmp.expense_ratio(info),
                       inception_date=fmp._date_or_none(info.get("inceptionDate")))
            n += 1
        sb().table("mf_master").update(upd).eq("ticker", t).execute()
    return n


# ── N-PORT parsing ───────────────────────────────────────────────────────────
def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _txt(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else None


def parse_nport(xml_text):
    """
    Public N-PORT primary document -> {"series_id", "rep_date", "net_assets",
    "holdings": {key: {...}}} or None. Lines for the same security are merged.
    """
    try:
        root = _strip_ns(ET_XML.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text))
    except ET_XML.ParseError:
        return None
    gen = root.find(".//genInfo")
    if gen is None:
        return None
    holdings = {}
    for s in root.iter("invstOrSec"):
        name, title = _txt(s, "name"), _txt(s, "title")
        cusip = _txt(s, "cusip")
        cusip = cusip if cusip and cusip.upper() not in ("N/A", "000000000", "0") else None
        isin = ticker = None
        ids = s.find("identifiers")
        if ids is not None:
            for c in ids:
                v = (c.get("value") or "").strip() or None
                if c.tag == "isin" and v:
                    isin = v
                elif c.tag == "ticker" and v:
                    ticker = v.upper()
        key = isin or cusip or ticker or (f"{name}|{title}" if name else None)
        if not key:
            continue
        pct, val, bal = _num(_txt(s, "pctVal")), _num(_txt(s, "valUSD")), _num(_txt(s, "balance"))
        if key in holdings:
            h = holdings[key]
            h["portfolio_pct"] = (h["portfolio_pct"] or 0) + (pct or 0)
            h["value_usd"] = (h["value_usd"] or 0) + (val or 0)
            h["balance"] = (h["balance"] or 0) + (bal or 0)
            continue
        holdings[key] = {"security_key": key, "security_name": name, "title": title, "ticker": ticker,
                         "cusip": cusip, "isin": isin, "asset_category": _txt(s, "assetCat"),
                         "portfolio_pct": pct, "value_usd": val, "balance": bal}
    fund = root.find(".//fundInfo")
    return {"series_id": _txt(gen, "seriesId"), "series_name": _txt(gen, "seriesName"),
            "rep_date": (_txt(gen, "repPdDate") or "")[:10] or None,
            "net_assets": _num(_txt(fund, "netAssets")) if fund is not None else None,
            "holdings": holdings}


# ── Diff (pure) ──────────────────────────────────────────────────────────────
def _as_etf_rows(h):
    """Shape for etf_data_poller's diff helpers: shares = balance, symbol = ticker."""
    return {k: dict(v, security_symbol=v.get("ticker"), shares=v.get("balance")) for k, v in h.items()}


def diff_snapshot(old, new):
    """
    old, new  {key: holding}   (old as stored: heaviest MAX_STORED)
    -> {"added", "removed", "resized", "kept", "rebaseline": reason or None}
    """
    if old and len(new) < MIN_KEEP_FRACTION * len(old) and len(old) >= 10:
        return {"added": [], "removed": [], "resized": [], "kept": None,
                "rebaseline": f"partial filing ({len(new)} vs {len(old)} positions) ignored"}
    o, n = _as_etf_rows(old), _as_etf_rows(new)
    added_k, removed_k, kept = edp.diff_holdings(o, n, MAX_STORED)
    if old and len(old) >= 20 and (len(added_k) + len(removed_k)) / len(old) > MAX_CHURN:
        return {"added": [], "removed": [], "resized": [], "kept": kept,
                "rebaseline": "churn too high -- treated as re-keying, re-baselined"}
    added = [dict(n[k], weight_new=edp._w(n[k]), weight_prev=None) for k in added_k]
    removed = [dict(o[k], weight_prev=edp._w(o[k]), weight_new=None) for k in removed_k]
    resized = edp.weight_shifts(o, n)
    return {"added": added, "removed": removed, "resized": resized, "kept": kept, "rebaseline": None}


def is_debt_fund(holdings):
    tot = sum((h.get("portfolio_pct") or 0) for h in holdings.values()) or 1.0
    debt = sum((h.get("portfolio_pct") or 0) for h in holdings.values()
               if (h.get("asset_category") or "").upper() in ("DBT", "ABS-MBS", "ABS-O", "ABS-CBDO", "ABS-APCP", "LON"))
    return debt / tot >= DEBT_SKIP_SHARE


def build_alert(fund, ticker, d, rep_date, filing_date, debt):
    adds = sorted((c for c in d["added"] if c["weight_new"] >= ALERT_MIN_WEIGHT and edp.is_alertable_line(c)),
                  key=lambda c: -c["weight_new"])
    rems = sorted((c for c in d["removed"] if c["weight_prev"] >= ALERT_MIN_WEIGHT and edp.is_alertable_line(c)),
                  key=lambda c: -c["weight_prev"])
    if debt:
        adds, rems = [], []                        # bond books turn over constantly
    resz = d["resized"]
    if not (adds or rems or resz):
        return None

    # Company data behind each line (mf_company_data, filled daily): sector and
    # market-cap class, so a holder reads "Nvidia (NVDA), large-cap Technology"
    # rather than the bare issuer string the filing used. Best-effort — a line
    # with nothing on file is named exactly as before.
    try:
        import mf_company_data as mcd
        desc = mcd.describe([c.get("security_key") for c in (adds + rems + resz)])
    except Exception:
        desc = {}

    def lbl(c):
        base = edp._label(c)
        d = desc.get(c.get("security_key"))
        return f"{base}, {d}" if d else base

    parts = [f"{fund} ({ticker}) quarterly portfolio, as of {rep_date} (SEC Form N-PORT, filed {filing_date})."]
    if adds:
        parts.append("Added: " + "; ".join(f"{lbl(c)} at {c['weight_new']:.2f}% of the fund" for c in adds[:LIST_LIMIT])
                     + (f"; and {len(adds) - LIST_LIMIT} more" if len(adds) > LIST_LIMIT else "") + ".")
    if rems:
        parts.append("Exited: " + "; ".join(f"{lbl(c)}, previously {c['weight_prev']:.2f}%" for c in rems[:LIST_LIMIT])
                     + (f"; and {len(rems) - LIST_LIMIT} more" if len(rems) > LIST_LIMIT else "") + ".")
    if resz:
        parts.append("Resized: " + "; ".join(
            f"{lbl(c)} {c['weight_prev']:.2f}% -> {c['weight_new']:.2f}% "
            f"(position {'+' if c['shares_vs_flow_pct'] > 0 else ''}{c['shares_vs_flow_pct']:.0f}% beyond fund flows)"
            for c in resz[:LIST_LIMIT]) + (f"; and {len(resz) - LIST_LIMIT} more" if len(resz) > LIST_LIMIT else "") + ".")
    parts.append("Mutual funds disclose full holdings once a quarter, up to 60 days after quarter-end.")
    top = max([c["weight_new"] for c in adds] + [c["weight_prev"] for c in rems] + [0])
    top_shift = max([abs(c["weight_new"] - c["weight_prev"]) for c in resz] + [0])
    impact = "HIGH" if top >= ALERT_HIGH_WEIGHT or top_shift >= edp.SHIFT_HIGH_PTS else "MEDIUM"
    if adds and not rems and not resz:
        ftype = "MF_HOLDINGS_ADDED"
    elif rems and not adds and not resz:
        ftype = "MF_HOLDINGS_REMOVED"
    elif resz and not adds and not rems:
        ftype = "MF_WEIGHT_SHIFT"
    else:
        ftype = "MF_HOLDINGS_CHANGE"
    lists = {"added": [{"name": lbl(c), "weight": c["weight_new"]} for c in adds],
             "removed": [{"name": lbl(c), "weight_prev": c["weight_prev"]} for c in rems],
             "resized": [{"name": lbl(c), "weight_prev": c["weight_prev"], "weight_new": c["weight_new"]}
                         for c in resz]}
    return " ".join(parts), impact, ftype, lists


# ── N-PORT discovery ─────────────────────────────────────────────────────────
def nport_filings(cik, budget, incomplete=None):
    """Recent public N-PORT filings of one registrant -> [{accession, series_ids,
    filing_date, primary_doc}], newest first. Series membership is read once
    per filing from its header and kept in mf_nport_filings."""
    sub = fm.sec.json(fm.URL_SUBMISSIONS.format(cik10=cik)) or {}
    rec = (sub.get("filings") or {}).get("recent") or {}
    cutoff = (date.today() - timedelta(days=NPORT_LOOKBACK_DAYS)).isoformat()
    cands = [{"accession": rec["accessionNumber"][j], "filing_date": rec["filingDate"][j],
              "primary_doc": rec["primaryDocument"][j]}
             for j, f in enumerate(rec.get("form") or [])
             if f == "NPORT-P" and rec["filingDate"][j] >= cutoff]
    if not cands:
        return []
    acc = [c["accession"] for c in cands]
    known = {}
    for i in range(0, len(acc), 100):
        for r in (sb().table("mf_nport_filings").select("accession, series_ids")
                  .in_("accession", acc[i:i + 100]).execute().data or []):
            known[r["accession"]] = r.get("series_ids") or []
    for c in cands:
        if c["accession"] not in known and budget[0] <= 0 and incomplete is not None:
            incomplete.append(c["accession"])           # header not read: series membership unknown
        if c["accession"] not in known and budget[0] > 0:
            budget[0] -= 1
            sids = [s["series_id"] for s in fm.fetch_series_header(cik, c["accession"])]
            known[c["accession"]] = sids
            sb().table("mf_nport_filings").upsert(
                {"accession": c["accession"], "cik": cik, "series_ids": sids,
                 "filing_date": c["filing_date"], "primary_doc": c["primary_doc"]},
                on_conflict="accession").execute()
        c["series_ids"] = known.get(c["accession"])
    return sorted([c for c in cands if c.get("series_ids") is not None],
                  key=lambda c: c["filing_date"], reverse=True)


def load_sent():
    since = (datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)).isoformat()
    rows = _paged(lambda: sb().table("alerts").select("id, extra").eq("source", SOURCE)
                  .gte("created_at", since).order("id"))
    return {(r.get("extra") or {}).get("dedup_key") for r in rows} - {None}


def _record_error(series_id, message):
    try:
        sb().table("mf_holdings_state").upsert(
            {"series_id": series_id, "checked_at": _now(), "last_error": str(message)[:200]},
            on_conflict="series_id").execute()
    except Exception as e:                     # never let bookkeeping stop a run
        fm.log_poller_error("mf_record_error", e, {"series_id": series_id})


def touch_checked(series_ids):
    rows = [{"series_id": s, "checked_at": _now()} for s in series_ids]
    for i in range(0, len(rows), 500):
        sb().table("mf_holdings_state").upsert(rows[i:i + 500], on_conflict="series_id").execute()


def process_series(series_id, tickers, filing, cik, sent, docs=None):
    """One new N-PORT for one series. Every ordinary fund is stored; alerts are
    written only for `tickers` (the share classes somebody watches). -> alerts written"""
    st = (sb().table("mf_holdings_state").select("*").eq("series_id", series_id).limit(1)
          .execute().data or [None])[0]
    if st and st.get("accession") == filing["accession"]:
        return 0                                   # already processed: no re-download
    # EDGAR's submissions list the HTML rendering of an N-PORT
    # ("xslFormNPORT-P_X01/primary_doc.xml"); the data is the plain XML beside it.
    listed = filing["primary_doc"] or "primary_doc.xml"
    raw = listed.rsplit("/", 1)[-1] or "primary_doc.xml"
    snap = url = None
    for name in ([raw] + ([listed] if listed != raw else [])):
        url = fm._archive(cik, filing["accession"], name)
        if docs is not None:
            docs[0] -= 1
        xml = fm.sec.text(url)
        snap = parse_nport(xml) if xml else None
        if snap and snap["holdings"]:
            break
    if not snap or not snap["holdings"]:
        fm.log_poller_error("mf_nport_parse", f"empty or unreadable N-PORT: {url}", {"url": url})
        _record_error(series_id, f"empty or unreadable N-PORT: {url}")
        return 0
    if snap.get("series_id") and snap["series_id"] != series_id:
        return 0
    if st and str(st.get("rep_date") or "") >= (snap["rep_date"] or ""):
        return 0                                   # older or same period: nothing new
    old = {r["security_key"]: r for r in _paged(lambda: sb().table("mf_holdings").select("*")
                                                .eq("series_id", series_id).order("security_key"))}
    written = 0
    d = None
    if st and old:
        d = diff_snapshot(old, snap["holdings"])
        if d["rebaseline"]:
            fm.log(f"{series_id}: {d['rebaseline']}")
        else:
            names = {r["ticker"]: r for r in (sb().table("mf_master").select("ticker, series_name, class_name")
                                             .in_("ticker", sorted(tickers)).execute().data or [])} if tickers else {}
            debt = is_debt_fund(snap["holdings"])
            rows = []
            for t in sorted(tickers):
                fund = (names.get(t) or {}).get("series_name") or snap.get("series_name") or t
                built = build_alert(fund, t, d, snap["rep_date"], filing["filing_date"], debt)
                if not built:
                    continue
                summary, impact, ftype, lists = built
                key = f"{SOURCE}:{t}:{snap['rep_date']}"
                if key in sent:
                    continue
                extra = dict(lists, dedup_key=key, company_name=fund, series_id=series_id,
                             rep_date=snap["rep_date"], rep_date_prev=str(st.get("rep_date")),
                             accession=filing["accession"], filed_at=_now(),
                             headline=f"{fund}: quarterly portfolio changes")
                rows.append({"ticker": t, "summary": summary, "impact": impact, "source": SOURCE,
                             "filing_type": ftype, "delivered": False, "filing_url": url,
                             "extra": tag_extra(extra, SOURCE, ftype)})
            if rows:
                sb().table("alerts").insert(rows).execute()   # raises -> state not advanced
                sent |= {r["extra"]["dedup_key"] for r in rows}
                written = len(rows)
            ch = ([{"series_id": series_id, "security_key": c["security_key"], "security_name": c.get("security_name"),
                    "action": "ADDED", "weight_prev": None, "weight_new": c["weight_new"]} for c in d["added"]] +
                  [{"series_id": series_id, "security_key": c["security_key"], "security_name": c.get("security_name"),
                    "action": "REMOVED", "weight_prev": c["weight_prev"], "weight_new": None} for c in d["removed"]] +
                  [{"series_id": series_id, "security_key": c["security_key"], "security_name": c.get("security_name"),
                    "action": "RESIZED", "weight_prev": c["weight_prev"], "weight_new": c["weight_new"]}
                   for c in d["resized"]])
            for c in ch:
                c.update(rep_date_from=str(st.get("rep_date")), rep_date_to=snap["rep_date"])
            for i in range(0, len(ch), 500):
                sb().table("mf_holdings_changes").upsert(
                    ch[i:i + 500], on_conflict="series_id,security_key,action,rep_date_to",
                    ignore_duplicates=True).execute()

    ranked = sorted(snap["holdings"].values(), key=lambda h: -(h.get("portfolio_pct") or 0))[:MAX_STORED]
    keep = {h["security_key"] for h in ranked}
    rows = [dict(h, series_id=series_id, rep_date=snap["rep_date"], accession=filing["accession"],
                 updated_at=_now()) for h in ranked]
    for i in range(0, len(rows), 500):
        sb().table("mf_holdings").upsert(rows[i:i + 500], on_conflict="series_id,security_key").execute()
    stale = [k for k in old if k not in keep]
    for i in range(0, len(stale), 25):
        sb().table("mf_holdings").delete().eq("series_id", series_id).in_("security_key", stale[i:i + 25]).execute()
    sb().table("mf_holdings_state").upsert(
        {"series_id": series_id, "accession": filing["accession"], "rep_date": snap["rep_date"],
         "net_assets": snap.get("net_assets"), "holdings_count": len(snap["holdings"]),
         "checked_at": _now(), "updated_at": _now(), "last_error": None}, on_conflict="series_id").execute()
    return written


def mf_series_universe():
    """{series_id: {"cik", "tickers"}} for every ordinary mutual fund in mf_master."""
    rows = _paged(lambda: sb().table("mf_master").select("ticker, cik, series_id, fund_type")
                  .eq("is_active", True).order("ticker"))
    out = {}
    for r in rows:
        if r.get("fund_type") not in (None,) + ALERT_FUND_TYPES or not r.get("series_id"):
            continue
        out.setdefault(r["series_id"], {"cik": str(r["cik"]).zfill(10), "tickers": set()})["tickers"].add(r["ticker"])
    return out


def _due(state, watched, now):
    """Has this fund's registrant waited long enough since we last looked?"""
    at = (state or {}).get("checked_at")
    if not at:
        return True
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    days = ERROR_RETRY_DAYS if (state or {}).get("last_error") else (RECHECK_WATCHED_DAYS if watched else RECHECK_DAYS)
    # Slack, because the schedule is fixed but the touch time is not. A run at
    # 01:40:00 sees "checked 01:40:07 yesterday" as 23h59m old and skips the
    # fund; with runs at 12:40 and 01:40 UTC a "daily" watched fund then waits
    # 35 hours. Seen 2026-10-21: OAKMX and PRGFX last checked Oct 20 while the
    # other 38 watched funds were checked that morning.
    return now - t >= timedelta(days=days) - DUE_SLACK


def _load_states():
    try:
        rows = _paged(lambda: sb().table("mf_holdings_state").select("series_id, checked_at, last_error")
                      .order("series_id"))
    except Exception:                      # last_error column not added yet
        rows = _paged(lambda: sb().table("mf_holdings_state").select("series_id, checked_at").order("series_id"))
    return {r["series_id"]: r for r in rows}


def run_mf_holdings():
    """Twice a day. Every ordinary mutual fund's newest public N-PORT is read and
    stored (heaviest MAX_STORED positions), working through a few thousand
    documents a run until all funds are covered, then re-checking each registrant
    every few days. Funds somebody watches go first and are the only ones that
    alert; the first filing seen for a fund is its silent baseline."""
    try:
        started = time.monotonic()
        u = fm.load_universe()
        watched = watched_mfs(u)
        watched_by_series = {}
        for t, v in watched.items():
            watched_by_series.setdefault(v["series_id"], set()).add(t)
        by_series = mf_series_universe()
        for t, v in watched.items():                 # watched funds are covered even if mf_master lags
            by_series.setdefault(v["series_id"], {"cik": str(v["cik"]).zfill(10), "tickers": set()})
        if not by_series:
            return
        for sid, s_ in by_series.items():
            s_["watched"] = watched_by_series.get(sid, set())
        states = _load_states()
        now = datetime.now(timezone.utc)
        by_cik = {}
        for sid, s_ in by_series.items():
            by_cik.setdefault(s_["cik"], []).append(sid)

        def cik_key(cik):
            sids = by_cik[cik]
            return (0 if any(by_series[x]["watched"] for x in sids) else 1,
                    min(str((states.get(x) or {}).get("checked_at") or "") for x in sids))

        order = [c for c in sorted(by_cik, key=cik_key)
                 if any(_due(states.get(x), bool(by_series[x]["watched"]), now) for x in by_cik[c])]
        sent = load_sent()
        hb, docs = [HEADER_BUDGET], [DOC_BUDGET]
        alerts = covered = registrants = 0
        for cik in order:
            if hb[0] <= 0 or docs[0] <= 0 or time.monotonic() - started > RUN_SECONDS:
                break
            incomplete = []
            try:
                filings = nport_filings(cik, hb, incomplete)
            except Exception as e:
                fm.log_poller_error("mf_nport_list", e, {"cik": cik})
                continue
            registrants += 1
            handled = []
            for sid in by_cik[cik]:
                if not _due(states.get(sid), bool(by_series[sid]["watched"]), now):
                    continue
                newest = next((f for f in filings if sid in (f.get("series_ids") or [])), None)
                if newest:
                    if docs[0] <= 0:
                        break
                    try:
                        alerts += process_series(sid, by_series[sid]["watched"], newest, cik, sent, docs)
                    except Exception as e:
                        fm.log_poller_error("mf_process_series", e, {"series_id": sid})
                        continue                     # not marked checked: retried next run
                    handled.append(sid)
                elif not incomplete:
                    handled.append(sid)              # no public N-PORT yet; look again later
            if handled:
                touch_checked(handled)
                covered += len(handled)
        fm.log(f"mf holdings: {registrants}/{len(order)} due registrants, {covered} funds covered of "
               f"{len(by_series)}, {alerts} alerts, {DOC_BUDGET - docs[0]} documents and "
               f"{HEADER_BUDGET - hb[0]} headers read")
    except Exception as e:
        fm.log_poller_error("run_mf_holdings", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("master", "both"):
        refresh_mf_master()
    if which in ("holdings", "both"):
        run_mf_holdings()
