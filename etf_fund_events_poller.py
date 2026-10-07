"""
etf_fund_events_poller.py
GQ FinXray US — Feature 16: ETF Fund Actions (SEC EDGAR).

The four fund-level corporate actions a holder must hear about, from the
filing the fund is legally required to make:

  ETF_LIQUIDATION      board approved closing / liquidating / delisting the fund
  ETF_MERGER           plan of reorganization: the fund merges into another
  ETF_STRATEGY_CHANGE  new underlying index, new investment objective, new name
  ETF_FEE_CHANGE       management fee cut or raise, fee waiver ending

Where these come from: Form 497 prospectus supplements ("stickers") and, for
mergers, Form N-14. Found with EDGAR full-text search, the same transport and
fair-access budget fund_manager_poller.py uses.

WATCHLIST FIRST
---------------
Nothing is fetched for a fund nobody watches:
  1. No watched ETF in SEC's fund registry  -> no EDGAR request at all.
  2. A search hit whose filer CIK owns no watched fund is dropped from the
     search result itself, before any document is downloaded.
  3. For the survivors, the SGML header says which fund series the filing
     covers. When it covers several, the watched fund must be named (series
     name or ticker) in the SAME SENTENCE as the triggering phrase, else the
     same paragraph. Anywhere-in-the-document is not enough: a supplement
     headed "to the Prospectus for Alpha ETF and Beta ETF" that liquidates
     only Alpha names Beta too, and must not tell Beta's holders it is closing.

NOT EVERY 497 IS A SUPPLEMENT
-----------------------------
Form 497 also carries full prospectus re-filings, whose boilerplate says
things like "the fee waiver may not be terminated before…". Only documents
that identify themselves as a supplement near the top are considered (N-14
merger registrations are exempt — the form itself is the event).

ONE LLM PASS PER DOCUMENT
-------------------------
Each matching document becomes ONE raw_filings row (source=SEC_FUND). The AI
pipeline summarises it once; if the same document covers several watched
tickers, ai_pipeline copies the finished alert to the others from
extra.fanout_tickers instead of summarising the same text again.

IMPACT FLOOR
------------
extra.impact_floor is honoured by ai_pipeline: a liquidation or merger is
never filed below HIGH, a fee or strategy change never below MEDIUM, whatever
the impact model says.

Schedule: hourly (main.py), 2-day lookback. etf_fund_events.doc_key makes the
overlap free: a document is examined once, ever.
"""

import os
import re
import traceback
from datetime import date, datetime, timedelta, timezone

import fund_manager_extractor as fx
import fund_manager_poller as fm

SOURCE = "SEC_FUND"
DAYS_BACK = int(os.getenv("FUND_EVENTS_DAYS_BACK", "2"))
# 100 hits per page. Most phrases return one page over two days; the ceiling is
# there so a broad phrase cannot silently truncate at 300 and hide a filing.
MAX_PAGES = int(os.getenv("FUND_EVENTS_MAX_PAGES", "10"))
# Recorded so they are visible, but retried on the next poll: each is a
# transient failure (header or document fetch, a DB write), not a verdict.
RETRYABLE = {"NO_SERIES", "NO_DOC", "ERROR"}
EXCERPT_CHARS = int(os.getenv("FUND_EVENTS_EXCERPT_CHARS", "7000"))
WINDOW = 1200

# Highest priority first. A supplement that liquidates a fund and also mentions
# its fee waiver is a liquidation.
PRIORITY = ["ETF_LIQUIDATION", "ETF_MERGER", "ETF_STRATEGY_CHANGE", "ETF_FEE_CHANGE"]

LABEL = {
    "ETF_LIQUIDATION": "fund liquidation",
    "ETF_MERGER": "fund reorganization",
    "ETF_STRATEGY_CHANGE": "index / strategy change",
    "ETF_FEE_CHANGE": "fee change",
}

# (event_type, impact_floor, forms, EFTS queries). Each query is EFTS syntax:
# every quoted phrase must appear. Phrases are change-specific on purpose —
# a generic one ("underlying index") matches every prospectus ever filed.
RULES = [
    ("ETF_LIQUIDATION", "HIGH", "497", [
        '"plan of liquidation"',
        '"will be liquidated"',
        '"close and liquidate"',
        '"liquidation of the fund"',
        '"liquidate the fund"',
        '"last day of trading"',
        '"liquidation date"',
    ]),
    ("ETF_MERGER", "HIGH", "497,N-14,N-14 8C", [
        '"agreement and plan of reorganization"',
        '"plan of reorganization"',
        '"will be reorganized into"',
        '"reorganization of the fund"',
        '"merger of the fund"',
    ]),
    ("ETF_STRATEGY_CHANGE", "MEDIUM", "497", [
        '"change to the underlying index"',
        '"new underlying index"',
        '"underlying index will change"',
        '"will change its underlying index"',
        '"investment objective will change"',
        '"change in investment objective"',
        '"will change its name"',
    ]),
    # Announcement wording only. Generic pairs like "fee waiver" + "terminate"
    # match the standing "may not be terminated before…" disclosure in every
    # prospectus that carries a waiver.
    ("ETF_FEE_CHANGE", "MEDIUM", "497", [
        '"management fee will be reduced"',
        '"will reduce its management fee"',
        '"management fee will be increased"',
        '"management fee will increase"',
        '"reduction in the management fee"',
        '"fee waiver will terminate"',
        '"expense limitation agreement will terminate"',
    ]),
]
FLOOR = {r[0]: r[1] for r in RULES}


def log(msg):
    print(f"[FUND_EVENTS {datetime.now().strftime('%H:%M:%S')}] {msg}")


def _log_error(job, error, context=None):
    log(f"ERROR {job}: {error}")
    try:
        fm.sb().table("poller_error_log").insert({
            "poller_name": "etf_fund_events_poller", "job_name": job,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {},
        }).execute()
    except Exception:
        pass


# ── Text helpers (pure) ───────────────────────────────────────────────────────
def query_terms(q):
    """'"fee waiver" "terminate"' -> ['fee waiver', 'terminate']"""
    return [t for t in re.findall(r'"([^"]+)"', q)] or [q.strip()]


def _norm(s):
    return re.sub(r"\s+", " ", fx._norm_for_search(s or ""))


def verify_events(text, rules_hit):
    """
    EFTS stems and matches across exhibits; re-check against the document we
    actually downloaded. -> {event_type: [phrases confirmed in the text]}
    """
    norm = _norm(text)
    out = {}
    for event_type, queries in rules_hit.items():
        for q in queries:
            terms = query_terms(q)
            if all(_norm(t) in norm for t in terms):
                out.setdefault(event_type, []).extend(terms)
    return out


def pick_event(verified):
    for et in PRIORITY:
        if et in verified:
            return et
    return None


def mentions_ticker(text, ticker):
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", text or ""))


def is_supplement(text, form):
    """Shared with fund_manager_poller (see fund_manager_extractor.is_supplement)."""
    return fx.is_supplement(text, form)


def _sentence_span(s, i, j):
    a = max(s.rfind(". ", 0, i), s.rfind("\n", 0, i))
    a = 0 if a < 0 else a + 1
    ends = [k for k in (s.find(". ", j), s.find("\n", j)) if k >= 0]
    return a, (min(ends) + 1 if ends else len(s))


def _line_span(s, i, j):
    a = s.rfind("\n", 0, i)
    b = s.find("\n", j)
    return (0 if a < 0 else a + 1), (len(s) if b < 0 else b)


def attribute_tickers(text, series, phrases, candidates):
    """
    Which candidate tickers the event is actually ABOUT.

    For every occurrence of a confirmed phrase, look for a candidate fund
    (series name via SeriesLocator, or its ticker) in the same sentence; only
    if the sentence names none, widen to the same paragraph/line. A fund named
    only in the document heading or in an unrelated paragraph is not attributed.
    `candidates` is {ticker: {series_id, ...}}.
    """
    loc = fx.SeriesLocator(text, series)
    norm = loc.norm                       # length-preserving: offsets match `text`
    out = set()
    for p in phrases:
        words = _norm(p).split()
        if not words:
            continue
        pat = re.compile(r"\s+".join(re.escape(w) for w in words))
        for m in pat.finditer(norm):
            for a, b in (_sentence_span(norm, m.start(), m.end()), _line_span(norm, m.start(), m.end())):
                sids = loc.in_span(a, b)
                seg = text[a:b]
                found = {t for t, f in candidates.items()
                         if f["series_id"] in sids or mentions_ticker(seg, t)}
                if found:
                    out |= found
                    break
            else:
                out |= _fallback_attribution(norm, m.start(), m.end(), loc, candidates)
    return out


_PLURAL = re.compile(r"\b(?:each\s+(?:of\s+the\s+)?funds?|all\s+(?:of\s+the\s+)?funds|the\s+funds)\b")


def _fallback_attribution(norm, i, j, loc, candidates):
    """
    The sentence and paragraph name no fund. Two layouts are still unambiguous:
      * "…a plan of liquidation for each Fund" — every fund the filing covers.
      * the fund's name as a heading line above the paragraph — that fund, but
        only when the heading line names exactly ONE fund. A line listing two
        ("Supplement to the Prospectus for Alpha ETF and Beta ETF") is a cover
        list, not a section heading, and attributing from it is the exact
        mistake that sent Beta holders an alert about Alpha's liquidation.
    """
    a, b = _sentence_span(norm, i, j)
    if _PLURAL.search(norm[a:b]):
        return set(candidates)
    best = None
    for off, sid in loc.hits:
        if off >= i:
            break
        la, lb = _line_span(norm, off, off)
        if len(loc.in_span(la, lb)) == 1:
            best = sid
    if best:
        return {t for t, f in candidates.items() if f["series_id"] == best}
    return set()


def build_excerpt(text, phrases, names, limit=EXCERPT_CHARS):
    """Windows of text around each confirmed phrase and each named fund, merged."""
    norm = _norm(text)
    flat = re.sub(r"\s+", " ", text or "")
    spans = []
    for p in phrases:
        needle = _norm(p)
        start = 0
        for _ in range(3):
            i = norm.find(needle, start)
            if i < 0:
                break
            spans.append((max(0, i - WINDOW), min(len(flat), i + len(needle) + WINDOW)))
            start = i + len(needle)
    for n in names:
        needle = _norm(n)
        if len(needle) < 6:
            continue
        i = norm.find(needle)
        if i >= 0:
            spans.append((max(0, i - 300), min(len(flat), i + len(needle) + 600)))
    if not spans:
        return flat[:limit]
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts, used = [], 0
    for a, b in merged:
        chunk = flat[a:b].strip()
        if used + len(chunk) > limit:
            chunk = chunk[:max(0, limit - used)]
        if chunk:
            parts.append(chunk)
            used += len(chunk)
        if used >= limit:
            break
    return " … ".join(parts)


# ── Storage ───────────────────────────────────────────────────────────────────
def _seen_keys(keys):
    """Documents already settled. A RETRYABLE status is not settled: a filing
    whose header or body fetch failed on a 429 is examined again next poll."""
    seen = set()
    keys = list(keys)
    for i in range(0, len(keys), 50):
        try:
            rows = fm.sb().table("etf_fund_events").select("doc_key, status") \
                .in_("doc_key", keys[i:i + 50]).execute().data or []
            seen |= {r["doc_key"] for r in rows if r.get("status") not in RETRYABLE}
        except Exception as e:
            # Unreadable ledger: treat everything as seen this tick rather than
            # risk queueing (and summarising, and alerting) a document twice.
            _log_error("seen_keys", e)
            return set(keys)
    return seen


def _record(meta, status, **fields):
    row = {
        "doc_key": meta["key"], "accession": meta["accession"], "cik": meta.get("cik"),
        "form": meta.get("form"), "filing_date": meta.get("filing_date"),
        "filing_url": meta.get("url"), "filer": meta.get("filer"), "status": status,
    }
    row.update(fields)
    try:
        fm.sb().table("etf_fund_events").upsert(row, on_conflict="doc_key").execute()
    except Exception as e:
        _log_error("record_event", e, {"doc_key": meta.get("key"), "status": status})


def _queue_filing(row):
    fm.sb().table("raw_filings").insert(row).execute()


# ── Core ──────────────────────────────────────────────────────────────────────
URL_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_trust_cache = {"map": None, "at": 0.0}


def _trust_ciks():
    """{TICKER: cik10} from SEC's exchange-ticker file, cached for a day.

    company_tickers_mf.json lists only series-based funds. Unit investment
    trusts and grantor trusts — SPY, QQQ, DIA, GLD, IBIT — are not series
    funds, so without this they were silently never searched at all."""
    import time as _t
    if _trust_cache["map"] is not None and _t.monotonic() - _trust_cache["at"] < 86400:
        return _trust_cache["map"]
    data = fm.sec.json(URL_COMPANY_TICKERS) or {}
    m = {}
    for row in (data.values() if isinstance(data, dict) else []):
        try:
            m[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
        except (KeyError, TypeError):
            continue
    if m:
        _trust_cache.update(map=m, at=_t.monotonic())
    return m or (_trust_cache["map"] or {})


def watched_funds():
    """{ticker: {cik, series_id, fund_type}} for watched tickers SEC knows as funds.

    Series funds come from the fund registry. A watched ETF missing from it but
    present in etf_master is looked up as a single-entity trust (series_id
    "CIK<cik>"), so a UIT's own 497 / N-14 filings are still searched."""
    u = fm.load_universe()
    watched = fm.watched_tickers()
    out = {t: u["by_ticker"][t] for t in watched if t in u["by_ticker"]}
    missing = watched - set(out)
    if missing:
        etfs = _etf_master_tickers(missing)
        if etfs:
            trusts = _trust_ciks()
            for t in sorted(etfs):
                if t in trusts:
                    out[t] = {"cik": trusts[t], "series_id": f"CIK{trusts[t]}",
                              "class_id": None, "fund_type": "ETF", "trust": True}
            unknown = sorted(etfs - set(out))
            if unknown:
                log(f"watched ETFs with no SEC registrant found (no fund-action alerts): {', '.join(unknown)}")
    return out


def _etf_master_tickers(tickers):
    """Which of these tickers are ETFs (in etf_master). Stocks are skipped."""
    found, tickers = set(), sorted(tickers)
    for i in range(0, len(tickers), 100):
        try:
            rows = fm.sb().table("etf_master").select("ticker") \
                .in_("ticker", tickers[i:i + 100]).execute().data or []
            found |= {r["ticker"].upper() for r in rows if r.get("ticker")}
        except Exception as e:
            _log_error("etf_master_lookup", e)
    return found


def collect_candidates(funds, start, end):
    """Run every EFTS query; keep only hits filed by a CIK that owns a watched fund."""
    watched_ciks = {v["cik"] for v in funds.values()}
    cands = {}
    for event_type, _floor, forms, queries in RULES:
        for q in queries:
            for h in fm.efts_hits(q, forms, start, end, max_pages=MAX_PAGES):
                src = h.get("_source") or {}
                ciks = {str(c).zfill(10) for c in (src.get("ciks") or [])}
                own = ciks & watched_ciks
                if not own:
                    continue
                m = fm._hit_meta(h)
                if not m:
                    continue
                m["cik"] = sorted(own)[0]
                c = cands.setdefault(m["key"], dict(m, rules={}))
                c["rules"].setdefault(event_type, [])
                if q not in c["rules"][event_type]:
                    c["rules"][event_type].append(q)
    return cands


def process_document(meta, funds):
    """Examine one candidate document. Returns 1 if a filing was queued, else 0."""
    u = fm.load_universe()
    series = fm.fetch_series_header(meta["cik"], meta["accession"])
    if not series:
        own = [sid for sid, v in u["by_series"].items() if v["cik"] == meta["cik"]]
        trusts = [f["series_id"] for f in funds.values() if f.get("trust") and f["cik"] == meta["cik"]]
        if len(own) == 1:
            series = [{"series_id": own[0], "series_name": "", "classes": []}]
        elif not own and len(set(trusts)) == 1:
            # A UIT / grantor trust has no series: the filer IS the fund.
            # EFTS display name: "SPDR S&P 500 ETF TRUST (SPY) (CIK 0000884394)"
            filer = re.sub(r"\s*\((?:CIK\s*)?[^)]*\)", "", meta.get("filer") or "").strip()
            series = [{"series_id": trusts[0], "series_name": filer, "classes": []}]
        else:
            _record(meta, "NO_SERIES")
            return 0
    in_filing = {s["series_id"] for s in series}
    names = {s["series_id"]: (s.get("series_name") or "") for s in series}

    hits = {t: f for t, f in funds.items() if f["series_id"] in in_filing}
    if not hits:
        _record(meta, "NOT_WATCHED", series_ids=sorted(in_filing))
        return 0

    html = fm.sec.text(meta["url"])
    if not html:
        _record(meta, "NO_DOC", series_ids=sorted(in_filing))
        return 0
    text = fx.lines_to_text(fx.html_to_lines(html))

    if not is_supplement(text, meta.get("form")):
        _record(meta, "NO_MATCH", series_ids=sorted(in_filing),
                matched_phrases=["(not a supplement: full prospectus re-filing)"])
        return 0

    verified = verify_events(text, meta.get("rules") or {})
    event_type = pick_event(verified)
    if not event_type:
        _record(meta, "NO_MATCH", series_ids=sorted(in_filing),
                matched_phrases=[q for qs in (meta.get("rules") or {}).values() for q in qs])
        return 0

    if len(in_filing) > 1:
        about = attribute_tickers(text, series, verified[event_type], hits)
        hits = {t: f for t, f in hits.items() if t in about}
        if not hits:
            _record(meta, "NOT_WATCHED", event_type=event_type, series_ids=sorted(in_filing),
                    matched_phrases=verified[event_type])
            return 0

    tickers = sorted(hits)
    primary, fanout = tickers[0], tickers[1:]
    name_of = {t: (names.get(hits[t]["series_id"]) or t) for t in tickers}
    fund_name = name_of[primary]
    hit_names = sorted({n for n in name_of.values() if n})
    excerpt = build_excerpt(text, verified[event_type], hit_names)
    funds_line = "; ".join(f"{name_of[t]} ({t})" for t in tickers)
    header = (f"{'Fund' if len(tickers) == 1 else 'Funds'}: {funds_line}. "
              f"SEC Form {meta.get('form')} filed {meta.get('filing_date')} by {meta.get('filer') or 'the trust'}. "
              f"This filing concerns a {LABEL[event_type]}.\n\n")
    raw_text = header + excerpt

    row = {
        "source": SOURCE,
        "filing_type": event_type,
        "company_name": fund_name,
        "ticker": primary,
        "raw_text": raw_text,
        "filing_url": meta["url"],
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING",
        "extra": {
            "title": f"{fund_name}: SEC Form {meta.get('form')} {LABEL[event_type]} ({meta['accession']})",
            "company_name": fund_name,
            "accession": meta["accession"], "cik": meta.get("cik"), "form": meta.get("form"),
            "filing_date": meta.get("filing_date"), "filer": meta.get("filer"),
            "series_ids": sorted({f["series_id"] for f in hits.values()}),
            "event_type": event_type,
            "impact_floor": FLOOR[event_type],
            "fanout_tickers": fanout,
            # Each fan-out copy is rendered with ITS fund's name, not the primary's.
            "fanout_names": {t: name_of[t] for t in fanout},
            "event_label": LABEL[event_type],
            "matched_phrases": verified[event_type],
            "url": meta["url"],
        },
    }
    _queue_filing(row)
    _record(meta, "QUEUED", event_type=event_type, matched_phrases=verified[event_type],
            series_ids=sorted(in_filing), tickers=tickers, watched_tickers=tickers,
            excerpt=excerpt[:4000])
    log(f"QUEUED {event_type} {', '.join(tickers)} | {fund_name} | {meta['accession']}")
    return 1


def run_etf_fund_events_poller(days_back=None):
    """Hourly. Returns the number of filings queued for the AI pipeline."""
    days_back = DAYS_BACK if days_back is None else days_back
    try:
        funds = watched_funds()
        if not funds:
            log("no watched ETF is an SEC registrant — nothing to search")
            return 0
        end = date.today()
        start = end - timedelta(days=days_back)
        cands = collect_candidates(funds, start.isoformat(), end.isoformat())
        if not cands:
            log(f"{start}..{end}: no candidate filings from {len({v['cik'] for v in funds.values()})} watched trust(s)")
            return 0
        seen = _seen_keys(cands)
        fresh = {k: v for k, v in cands.items() if k not in seen}
        queued = 0
        for key, meta in fresh.items():
            try:
                queued += process_document(meta, funds)
            except Exception as e:
                _log_error("process_document", e, {"doc_key": key})
                _record(meta, "ERROR")
        log(f"{start}..{end}: {len(cands)} candidate(s), {len(fresh)} new, {queued} queued")
        return queued
    except Exception as e:
        _log_error("run_etf_fund_events_poller", e)
        return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        # Search only: prints what would be examined, writes nothing.
        f = watched_funds()
        print(f"watched SEC-registered funds: {sorted(f)}")
        e = date.today()
        lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        c = collect_candidates(f, (e - timedelta(days=lookback)).isoformat(), e.isoformat())
        for k, v in c.items():
            print(f"  {v.get('filing_date')} {(v.get('form') or ''):8} {(v.get('filer') or '')[:50]:50} "
                  f"{sorted(v['rules'])}  {v['url']}")
        print(f"{len(c)} candidate document(s)")
    else:
        run_etf_fund_events_poller(int(sys.argv[1]) if len(sys.argv) > 1 else None)
