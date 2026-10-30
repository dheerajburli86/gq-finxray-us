"""
mf_events_poller.py
GQ FinXray US — Feature 21: MUTUAL FUND actions (SEC EDGAR).

The fund-level events a mutual fund holder has to hear about, taken from the
filing the fund is legally required to make — Form 497 supplements ("stickers")
and Form N-14 for reorganizations.

  MF_LIQUIDATION       the board approved closing and liquidating the fund
  MF_MERGER            plan of reorganization: the fund merges into another
  MF_ADVISER_CHANGE    a new investment adviser or sub-adviser runs the money
  MF_CLOSED            the fund closes to new investors (a "soft close") or to
                       all purchases (a "hard close")
  MF_REOPENED          a closed fund reopens to new investors
  MF_SHARE_CLASS       a share class is converted, merged into another class or
                       closed — the holder's own ticker may be the one retired
  MF_STRATEGY_CHANGE   new investment objective, principal strategy or fund name
  MF_FEE_CHANGE        an announced management-fee change or a waiver ending

Four of those have NO ETF equivalent, which is why mutual funds need their own
poller rather than a share of the ETF one: an ETF never closes to new investors,
never converts a share class, and has no load-bearing adviser/sub-adviser split
to change. The wording, the impact floors and the holder's question ("can I
still buy? what happens to MY class?") are all different.

WATCHLIST FIRST — nothing is fetched for a fund nobody watches
--------------------------------------------------------------
  1. No watched mutual fund at a registrant -> no EDGAR request for it at all.
  2. A search hit filed by a CIK that runs no watched fund is dropped from the
     search result, before any document is downloaded.
  3. For the survivors, the SGML header says which funds the filing covers.
     When it covers several, the watched fund must be named in the SAME
     SENTENCE as the triggering phrase, else the same paragraph. "Anywhere in
     the document" is not enough: a supplement headed "to the Prospectus for
     the Alpha Fund and the Beta Fund" that liquidates only Alpha names Beta
     too, and must not tell Beta's holders their fund is closing.

ONE LLM PASS PER DOCUMENT
-------------------------
Each matching document becomes ONE raw_filings row (source=MF_EVENT) for
ai_pipeline to summarise. If the document covers several watched share classes,
the finished alert is copied to the rest from extra.fanout_tickers rather than
summarising the same text again. extra.impact_floor is honoured by the
pipeline: a liquidation or merger is never filed below HIGH.

Schedule: hourly (main.py), 2-day lookback. mf_fund_events.doc_key makes the
overlap free — a document is examined once, ever.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

import fund_manager_extractor as fx          # pure SEC-document parser
import mf_common as mc

POLLER = "mf_events_poller"
TAG = "MF_EVENTS"
SOURCE = "MF_EVENT"

DAYS_BACK = int(os.getenv("MF_EVENTS_DAYS_BACK", "2"))
MAX_PAGES = int(os.getenv("MF_EVENTS_MAX_PAGES", "10"))
EXCERPT_CHARS = int(os.getenv("MF_EVENTS_EXCERPT_CHARS", "7000"))
WINDOW = 1200
RETRYABLE = {"NO_SERIES", "NO_DOC", "ERROR"}

# Highest priority first. A supplement that liquidates a fund and also mentions
# its fee waiver is a liquidation, not a fee change.
PRIORITY = ["MF_LIQUIDATION", "MF_MERGER", "MF_ADVISER_CHANGE", "MF_CLOSED",
            "MF_REOPENED", "MF_SHARE_CLASS", "MF_STRATEGY_CHANGE", "MF_FEE_CHANGE"]

LABEL = {
    "MF_LIQUIDATION": "fund liquidation",
    "MF_MERGER": "fund reorganization",
    "MF_ADVISER_CHANGE": "investment adviser change",
    "MF_CLOSED": "fund closing to investors",
    "MF_REOPENED": "fund reopening to investors",
    "MF_SHARE_CLASS": "share class change",
    "MF_STRATEGY_CHANGE": "investment objective / strategy change",
    "MF_FEE_CHANGE": "fee change",
}

# (event_type, impact_floor, forms, EFTS phrases). Every quoted phrase in one
# query must appear. Phrases are announcement-specific on purpose: a generic one
# ("investment objective") matches every prospectus ever filed.
RULES = [
    ("MF_LIQUIDATION", "HIGH", "497", [
        '"plan of liquidation"',
        '"will be liquidated"',
        '"close and liquidate"',
        '"liquidation of the fund"',
        '"liquidate the fund"',
        '"liquidation date"',
        '"will cease operations"',
        '"final liquidating distribution"',
    ]),
    ("MF_MERGER", "HIGH", "497,N-14,N-14 8C", [
        '"agreement and plan of reorganization"',
        '"plan of reorganization"',
        '"will be reorganized into"',
        '"reorganization of the fund"',
        '"merger of the fund"',
        '"acquiring fund" "target fund"',
    ]),
    # Who actually manages the money. A sub-adviser swap changes the strategy
    # in substance while the fund's name and ticker stay the same.
    ("MF_ADVISER_CHANGE", "HIGH", "497", [
        '"new investment adviser"',
        '"new sub-adviser"',
        '"new subadviser"',
        '"will serve as the investment adviser"',
        '"will serve as sub-adviser"',
        '"replaced as sub-adviser"',
        '"no longer serve as the sub-adviser"',
        '"no longer serves as investment adviser"',
        '"change in control of the adviser"',
        '"new investment advisory agreement"',
    ]),
    # Mutual-fund only: an ETF cannot stop you buying it on an exchange.
    ("MF_CLOSED", "HIGH", "497", [
        '"closed to new investors"',
        '"will close to new investors"',
        '"will be closed to new investors"',
        '"closed to new accounts"',
        '"closed to all purchases"',
        '"will no longer accept purchase orders"',
        '"suspend the sale of shares"',
        '"suspension of sales"',
    ]),
    ("MF_REOPENED", "MEDIUM", "497", [
        '"reopened to new investors"',
        '"will reopen to new investors"',
        '"will again be available to new investors"',
        '"resume the sale of shares"',
    ]),
    # Mutual-fund only: a holder's own ticker can be retired or converted into
    # another class of the same fund.
    ("MF_SHARE_CLASS", "MEDIUM", "497", [
        '"will be converted into" "shares of the fund"',
        '"automatic conversion of" "shares"',
        '"class" "will be closed"',
        '"will be merged into" "class"',
        '"converted into class"',
        '"share class will be eliminated"',
        '"will no longer offer class"',
    ]),
    ("MF_STRATEGY_CHANGE", "MEDIUM", "497", [
        '"investment objective will change"',
        '"change in investment objective"',
        '"will change its investment objective"',
        '"change to the principal investment strategy"',
        '"new principal investment strategy"',
        '"will change its name"',
        '"the fund will be renamed"',
    ]),
    # Announcement wording only. A generic pair ("fee waiver" + "terminate")
    # matches the standing "may not be terminated before…" line in every
    # prospectus that carries a waiver.
    ("MF_FEE_CHANGE", "MEDIUM", "497", [
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
    mc.log(TAG, msg)


def _err(job, e, ctx=None):
    mc.log_poller_error(POLLER, job, e, ctx)


# ── Text helpers (pure) ───────────────────────────────────────────────────────

def query_terms(q):
    """'"fee waiver" "terminate"' -> ['fee waiver', 'terminate']"""
    return [t for t in re.findall(r'"([^"]+)"', q)] or [q.strip()]


def _norm(s):
    return re.sub(r"\s+", " ", fx._norm_for_search(s or ""))


def verify_events(text, rules_hit):
    """EFTS stems and searches exhibits too; re-check against the document we
    actually downloaded. -> {event_type: [phrases confirmed in this text]}"""
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


def _sentence_span(s, i, j):
    a = max(s.rfind(". ", 0, i), s.rfind("\n", 0, i))
    a = 0 if a < 0 else a + 1
    ends = [k for k in (s.find(". ", j), s.find("\n", j)) if k >= 0]
    return a, (min(ends) + 1 if ends else len(s))


def _line_span(s, i, j):
    a = s.rfind("\n", 0, i)
    b = s.find("\n", j)
    return (0 if a < 0 else a + 1), (len(s) if b < 0 else b)


_PLURAL = re.compile(r"\b(?:each\s+(?:of\s+the\s+)?funds?|all\s+(?:of\s+the\s+)?funds|the\s+funds)\b")


def _fallback_attribution(norm, i, j, loc, candidates):
    """
    The sentence and the paragraph name no fund. Two layouts are still
    unambiguous:
      * "…a plan of liquidation for each Fund" — every fund the filing covers.
      * the fund's name as a heading line above the paragraph, but only when
        that line names exactly ONE fund. A line listing two ("Supplement to
        the Prospectus for the Alpha Fund and the Beta Fund") is a cover list,
        not a section heading.
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


def attribute_tickers(text, series, phrases, candidates):
    """Which candidate tickers the event is actually ABOUT (see module docstring)."""
    loc = fx.SeriesLocator(text, series)
    norm = loc.norm                         # length-preserving: offsets match `text`
    out = set()
    for p in phrases:
        words = _norm(p).split()
        if not words:
            continue
        pat = re.compile(r"\s+".join(re.escape(w) for w in words))
        for m in pat.finditer(norm):
            for a, b in (_sentence_span(norm, m.start(), m.end()),
                         _line_span(norm, m.start(), m.end())):
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
            rows = (mc.sb().table("mf_fund_events").select("doc_key,status")
                    .in_("doc_key", keys[i:i + 50]).execute().data or [])
            seen |= {r["doc_key"] for r in rows if r.get("status") not in RETRYABLE}
        except Exception as e:
            # Unreadable ledger: treat everything as seen this tick rather than
            # risk queueing (and summarising, and alerting) a document twice.
            _err("seen_keys", e)
            return set(keys)
    return seen


def _record(meta, status, **fields):
    row = {"doc_key": meta["key"], "accession": meta["accession"], "cik": meta.get("cik"),
           "form": meta.get("form"), "filing_date": meta.get("filing_date"),
           "filing_url": meta.get("url"), "filer": meta.get("filer"), "status": status}
    row.update(fields)
    try:
        mc.sb().table("mf_fund_events").upsert(row, on_conflict="doc_key").execute()
    except Exception as e:
        _err("record_event", e, {"doc_key": meta.get("key"), "status": status})


def _queue_filing(row):
    mc.sb().table("raw_filings").insert(row).execute()


# ── Core ──────────────────────────────────────────────────────────────────────

def watched_funds():
    """{TICKER: {series_id, cik, series_name, entity_name}} — watched mutual funds."""
    return mc.watched_mf_funds()


def collect_candidates(funds, start, end):
    """Run every phrase; keep only hits filed by a CIK that runs a watched fund.
    -> (candidates, phrases whose search failed)"""
    watched_ciks = {v["cik"] for v in funds.values()}
    cands, failed = {}, []
    for event_type, _floor, forms, queries in RULES:
        for q in queries:
            for h in mc.efts_hits_safe(q, forms, start, end, max_pages=MAX_PAGES, failed=failed):
                src = h.get("_source") or {}
                own = {mc.cik_plain(c) for c in (src.get("ciks") or [])} & watched_ciks
                if not own:
                    continue
                m = mc.hit_meta(h)
                if not m:
                    continue
                m["cik"] = sorted(own)[0]
                c = cands.setdefault(m["key"], dict(m, rules={}))
                c["rules"].setdefault(event_type, [])
                if q not in c["rules"][event_type]:
                    c["rules"][event_type].append(q)
    return cands, failed


def process_document(meta, funds) -> int:
    """Examine one candidate document. 1 if a filing was queued, else 0."""
    u = mc.load_mf_universe()
    series = mc.fetch_series_header(meta["cik"], meta["accession"])
    if not series:
        own = u["by_cik"].get(mc.cik_plain(meta["cik"])) or []
        if len(own) == 1:
            series = [{"series_id": own[0],
                       "series_name": (u["by_series"][own[0]].get("series_name") or ""),
                       "classes": []}]
        else:
            _record(meta, "NO_SERIES")
            return 0

    in_filing = {s["series_id"] for s in series}
    names = {s["series_id"]: (s.get("series_name") or "") for s in series}
    # A trust can run ETFs and mutual funds side by side and file one document
    # for both. Only the mutual funds are this poller's business.
    hits = {t: f for t, f in funds.items() if f["series_id"] in in_filing}
    if not hits:
        _record(meta, "NOT_WATCHED", series_ids=sorted(in_filing))
        return 0

    html = mc.sec.text(meta["url"])
    if not html:
        _record(meta, "NO_DOC", series_ids=sorted(in_filing))
        return 0
    text = fx.lines_to_text(fx.html_to_lines(html))

    # Form 497 also carries full prospectus re-filings, whose boilerplate says
    # things like "the fee waiver may not be terminated before…". Only a
    # document that calls itself a supplement announces anything. N-14 is
    # exempt: the form itself is the event.
    if not fx.is_supplement(text, meta.get("form")):
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
    name_of = {t: (names.get(hits[t]["series_id"]) or hits[t].get("series_name") or t)
               for t in tickers}
    fund_name = name_of[primary]
    family = hits[primary].get("entity_name") or meta.get("filer") or "the trust"
    hit_names = sorted({n for n in name_of.values() if n})
    excerpt = build_excerpt(text, verified[event_type], hit_names)
    funds_line = "; ".join(f"{name_of[t]} ({t})" for t in tickers)
    header = (f"{'Mutual fund' if len(tickers) == 1 else 'Mutual funds'}: {funds_line}. "
              f"Fund family: {mc.family_text(family)}. "
              f"SEC Form {meta.get('form')} filed {meta.get('filing_date')}. "
              f"This filing concerns a {LABEL[event_type]}.\n\n")

    row = {
        "source": SOURCE,
        "filing_type": event_type,
        "company_name": fund_name,
        "ticker": primary,
        "raw_text": header + excerpt,
        "filing_url": meta["url"],
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING",
        "extra": {
            "title": f"{fund_name}: SEC Form {meta.get('form')} {LABEL[event_type]} ({meta['accession']})",
            "company_name": fund_name,
            "fund_family": family,
            "fund_type": "MUTUAL_FUND",
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
            series_ids=sorted(in_filing), tickers=tickers, excerpt=excerpt[:4000])
    log(f"QUEUED {event_type} {', '.join(tickers)} | {fund_name} | {meta['accession']}")
    return 1


def run_mf_fund_events_poller(days_back: int = DAYS_BACK):
    """Hourly: fund-action supplements for watched mutual funds."""
    try:
        funds = watched_funds()
        if not funds:
            log("no watched mutual funds — no EDGAR request made")
            return 0
        end = date.today()
        start = end - timedelta(days=days_back)
        cands, failed = collect_candidates(funds, start.isoformat(), end.isoformat())
        miss = f", {len(failed)} phrase search(es) FAILED" if failed else ""
        if not cands:
            log(f"{len(funds)} watched funds, no candidate filings{miss}")
            return 0
        seen = _seen_keys(cands)
        todo = [m for k, m in cands.items() if k not in seen]
        queued = 0
        for m in todo:
            try:
                queued += process_document(m, funds)
            except Exception as e:
                _err("process_document", e, {"doc_key": m.get("key")})
                _record(m, "ERROR", error=str(e)[:500])
        log(f"fund actions: {len(funds)} watched funds, {len(cands)} candidates, "
            f"{len(todo)} new documents, {queued} queued for summary{miss}")
        return queued
    except Exception as e:
        _err("run_mf_fund_events_poller", e)
        return 0


if __name__ == "__main__":
    import sys
    run_mf_fund_events_poller(int(sys.argv[1]) if len(sys.argv) > 1 else DAYS_BACK)
