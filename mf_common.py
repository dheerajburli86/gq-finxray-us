"""
mf_common.py
GQ FinXray US — shared plumbing for the MUTUAL FUND pollers only.

Every mutual-fund poller (mf_poller, mf_manager_poller, mf_expense_poller,
mf_events_poller, mf_scheme_poller, mf_company_data) imports from here and from
nothing in the ETF stack. ETFs and mutual funds are different products with
different filings (ETFs: daily holdings from the sponsor; mutual funds: Form
N-PORT once a quarter), different alert wording and different users, so the two
families keep separate pollers, separate alert sources, separate feature ids and
separate schedules in main.py.

What lives here
---------------
  SEC transport      rate-limited, sharing sec_client's window so the mutual-fund
                     jobs can never push the process past SEC's 10 req/s policy
                     and silence the 8-K lane.
  Universe           mf_master (loaded by mf_poller.refresh_mf_master) -> by
                     ticker / series / registrant. ORDINARY funds only by
                     default: money-market funds file N-MFP, not N-PORT, and
                     insurance-dedicated funds cannot be bought by the public.
  Watchlist          which mutual fund tickers at least one user watches.
  Filing index       EDGAR full-text search, submissions API, SGML series header.
  Bookkeeping        mf_filings_processed, poller_error_log.
  Holdings maths     diff / weight-shift helpers used by the portfolio alerts.

The one import outside this family is fund_manager_extractor, which is a pure
SEC-document parser (HTML to text, SGML series headers, person names, roster
tables). It holds no ETF logic and no alert text.
"""

from __future__ import annotations

import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

import fund_manager_extractor as fx        # pure SEC-document parser, no ETF logic

load_dotenv()

SEC_UA = os.getenv("SEC_USER_AGENT", "GQFinXray/1.0 dheerajburli86@gmail.com")

URL_EFTS = "https://efts.sec.gov/LATEST/search-index"
URL_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
URL_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{name}"

MAX_DOC_BYTES = 40_000_000

# fund_type values written into mf_master by mf_poller.refresh_mf_master.
MUTUAL_FUND = "MUTUAL_FUND"      # ordinary open-end fund: alerts
MONEY_MARKET = "MONEY_MARKET"    # files N-MFP, stable NAV: stored, no alerts
INSURANCE = "INSURANCE"          # only inside annuities/policies: stored, no alerts
ALERTABLE_TYPES = (MUTUAL_FUND,)

_sb = None


def sb():
    global _sb
    if _sb is None:
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def log(tag, msg):
    print(f"[{tag} {datetime.now().strftime('%H:%M:%S')}] {msg}")


def log_poller_error(poller, job_name, error, context=None):
    log(poller.upper(), f"ERROR {job_name}: {error}")
    try:
        sb().table("poller_error_log").insert({
            "poller_name": poller,
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {},
        }).execute()
    except Exception as log_err:                      # RLS on some keys; never fatal
        log(poller.upper(), f"failed to write poller_error_log: {log_err}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def paged(query_fn, size=1000):
    """PostgREST caps a select at 1,000 rows; walk the whole table."""
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


def num(v):
    try:
        return None if v in (None, "", "N/A") else float(v)
    except (TypeError, ValueError):
        return None


def fmt_date(d):
    try:
        return datetime.fromisoformat(str(d)).strftime("%b %d, %Y")
    except Exception:
        return d or "n/a"


# ─────────────────────────────────────────────────────────────────────────────
# SEC transport
#
# SEC counts requests per egress IP. sec_client already paces the 8-K / 10-Q /
# 10-K / Form 4 lane through a sliding window; a private bucket here would stack
# on top of it and get the whole IP throttled. So every mutual-fund request
# takes a slot from that SAME window, and only while the mutual-fund jobs hold
# fewer than MF_SEC_MAX_SHARE of them — leaving the fast lane room to fetch an
# 8-K the moment it appears, however long a backfill is running.
# ─────────────────────────────────────────────────────────────────────────────
try:
    import sec_client as _sc
except Exception:                                   # keep importable in isolation
    _sc = None

MF_SEC_MAX_SHARE = float(os.getenv("MF_SEC_MAX_SHARE", "3"))
MF_SEC_RPS = float(os.getenv("MF_SEC_RPS", "3"))    # only used if sec_client is absent


def _shared_slot() -> bool:
    """Block until a slot in sec_client's window is ours. False = no shared window."""
    if _sc is None or not hasattr(_sc, "_request_times"):
        return False
    cap = max(1.0, min(MF_SEC_MAX_SHARE, float(getattr(_sc, "MAX_RPS", 5))))
    while True:
        with _sc._rate_lock:
            now = time.monotonic()
            cooling = _sc._cooldown_until[0] - now
            if cooling > 0:
                wait = cooling
            else:
                while _sc._request_times and (now - _sc._request_times[0]) >= 1.0:
                    _sc._request_times.popleft()
                if len(_sc._request_times) < cap:
                    _sc._request_times.append(now)
                    return True
                wait = 1.0 - (now - _sc._request_times[0]) + 0.01
        time.sleep(min(max(wait, 0.01), 5.0))


def _signal_throttle(seconds: float):
    if _sc is not None and hasattr(_sc, "_set_cooldown"):
        try:
            _sc._set_cooldown(seconds)
        except Exception:
            pass


class SEC:
    def __init__(self, rate: float = MF_SEC_RPS):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
        self.min_gap = 1.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0

    def _wait(self):
        if _shared_slot():
            return
        with self._lock:
            now = time.monotonic()
            gap = self._last + self.min_gap - now
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def get(self, url, params=None, timeout=30, retries=4):
        for attempt in range(retries):
            self._wait()
            try:
                r = self.s.get(url, params=params, timeout=timeout)
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                backoff = 2 ** attempt + 0.5
                try:
                    backoff = max(backoff, min(float(r.headers.get("Retry-After") or 0), 60.0))
                except (TypeError, ValueError):
                    pass
                _signal_throttle(backoff)
                time.sleep(backoff)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(2 ** attempt + 0.5)
                continue
            return None
        return None

    def json(self, url, params=None):
        r = self.get(url, params=params)
        try:
            return r.json() if r is not None else None
        except ValueError:
            return None

    def text(self, url):
        r = self.get(url, timeout=60)
        if r is None:
            return None
        if len(r.content) > MAX_DOC_BYTES:
            return r.content[:MAX_DOC_BYTES].decode(r.encoding or "utf-8", "ignore")
        return r.text


sec = SEC()


def cik_plain(cik):
    try:
        return str(int(str(cik).strip()))
    except (TypeError, ValueError):
        return str(cik or "").lstrip("0") or "0"


def archive(cik, adsh, name):
    return URL_ARCHIVE.format(cik=int(cik_plain(cik)), nodash=str(adsh).replace("-", ""), name=name)


def fetch_series_header(cik, adsh) -> list[dict]:
    """Which fund series a filing covers, from its SGML header."""
    for name in (f"{adsh}.hdr.sgml", f"{adsh}-index-headers.html"):
        txt = sec.text(archive(cik, adsh, name))
        if txt and "<SERIES>" in txt.upper():
            return fx.parse_series_header(txt)
    return []


def efts_hits(query, forms, start, end, max_pages=10):
    """Yield EDGAR full-text-search hits for one phrase, 100 at a time."""
    frm = 0
    for _ in range(max_pages):
        data = sec.json(URL_EFTS, params={"q": query, "forms": forms, "dateRange": "custom",
                                          "startdt": start, "enddt": end, "from": frm})
        if not data:
            return
        hits = (data.get("hits") or {}).get("hits") or []
        for h in hits:
            yield h
        total = ((data.get("hits") or {}).get("total") or {}).get("value", 0)
        frm += len(hits)
        if not hits or frm >= total:
            return


def hit_meta(h):
    src = h.get("_source") or {}
    hid = h.get("_id") or ""
    adsh, _, fname = hid.partition(":")
    ciks = src.get("ciks") or []
    if not adsh or not fname or not ciks:
        return None
    cik = str(ciks[0])
    return {"accession": adsh, "cik": cik.zfill(10), "form": src.get("form") or src.get("file_type"),
            "filing_date": src.get("file_date"), "filename": fname,
            "url": archive(cik, adsh, fname), "filer": (src.get("display_names") or [""])[0],
            "key": f"{adsh}:{fname}"}


def submissions(cik):
    return sec.json(URL_SUBMISSIONS.format(cik10=str(cik).zfill(10)))


def recent_filings(cik, forms, days_back):
    """[{accession, form, filing_date, primary_doc}] from the submissions API."""
    data = submissions(cik)
    if not data:
        return []
    r = (data.get("filings") or {}).get("recent") or {}
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat()
    out = []
    for form, adsh, fdate, pdoc in zip(r.get("form", []), r.get("accessionNumber", []),
                                       r.get("filingDate", []), r.get("primaryDocument", [])):
        if form not in forms or (fdate or "") < cutoff:
            continue
        out.append({"accession": adsh, "form": form, "filing_date": fdate, "primary_doc": pdoc})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Universe — mutual funds only, straight out of mf_master
# ─────────────────────────────────────────────────────────────────────────────

_uni_cache: dict | None = None
_uni_at = 0.0
UNIVERSE_TTL = float(os.getenv("MF_UNIVERSE_TTL", "900"))


def load_mf_universe(force: bool = False, types=ALERTABLE_TYPES) -> dict:
    """
    {"by_ticker": {TICKER: row}, "by_series": {S000…: {...tickers}}, "by_cik": {cik: [series]}}

    `types` is which fund_type rows to include. A row whose fund_type is NULL
    predates the classifier and is treated as an ordinary fund, which is the
    same rule mf_poller uses — a fund is only excluded once it has actually
    been classified as money-market or insurance-dedicated.
    """
    global _uni_cache, _uni_at
    if _uni_cache is not None and not force and (time.monotonic() - _uni_at) < UNIVERSE_TTL:
        return _uni_cache
    rows = paged(lambda: sb().table("mf_master")
                 .select("ticker,cik,series_id,class_id,entity_name,series_name,class_name,fund_type")
                 .eq("is_active", True).order("ticker"))
    by_ticker, by_series, by_cik = {}, {}, {}
    for r in rows:
        ft = r.get("fund_type") or MUTUAL_FUND
        if types and ft not in types:
            continue
        t = (r.get("ticker") or "").upper()
        sid = r.get("series_id")
        if not t or not sid:
            continue
        r = dict(r, ticker=t, fund_type=ft, cik=cik_plain(r.get("cik")))
        by_ticker[t] = r
        s = by_series.setdefault(sid, {"series_id": sid, "cik": r["cik"], "tickers": [],
                                       "class_ids": [], "series_name": r.get("series_name"),
                                       "entity_name": r.get("entity_name"), "fund_type": ft})
        s["tickers"].append(t)
        if r.get("class_id"):
            s["class_ids"].append(r["class_id"])
        by_cik.setdefault(r["cik"], set()).add(sid)
    _uni_cache = {"by_ticker": by_ticker, "by_series": by_series,
                  "by_cik": {k: sorted(v) for k, v in by_cik.items()}}
    _uni_at = time.monotonic()
    return _uni_cache


def watched_tickers() -> set[str]:
    """Every ticker on at least one user's watchlist (all asset types)."""
    try:
        from watchlist_util import get_watched_tickers
        w = {t.upper() for t in get_watched_tickers(force=True)}
        if w:
            return w
    except Exception:
        pass
    try:
        rows = paged(lambda: sb().table("watchlists").select("ticker").order("ticker"))
        return {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
    except Exception as e:
        log_poller_error("mf_common", "watched_tickers", e)
        return set()


def watched_mf_funds(universe=None) -> dict:
    """{TICKER: universe row} for watched tickers that are ordinary mutual funds."""
    u = universe or load_mf_universe()
    return {t: u["by_ticker"][t] for t in watched_tickers() if t in u["by_ticker"]}


def watched_mf_series(universe=None) -> set[str]:
    return {v["series_id"] for v in watched_mf_funds(universe).values()}


# ─────────────────────────────────────────────────────────────────────────────
# Per-document bookkeeping (mf_filings_processed)
# ─────────────────────────────────────────────────────────────────────────────

# A transient failure — a 429 on the header, an unreadable document, a crash
# mid-filing. Recorded so it is visible, but NOT treated as handled: the next
# poll inside the lookback window tries it again rather than losing a real fund
# event to one throttled request.
RETRYABLE_STATUSES = {"NO_DOC", "ERROR", "NO_SERIES"}


def already_processed(doc_key, purpose) -> bool:
    try:
        r = (sb().table("mf_filings_processed").select("doc_key,status")
             .eq("doc_key", doc_key).eq("purpose", purpose).limit(1).execute())
        return bool(r.data) and (r.data[0].get("status") not in RETRYABLE_STATUSES)
    except Exception:
        return False


def processed_keys(purpose, keys) -> set:
    """Which of these doc_keys are already handled. One query per 150 keys."""
    done = set()
    keys = list(keys)
    for i in range(0, len(keys), 150):
        chunk = keys[i:i + 150]
        try:
            r = (sb().table("mf_filings_processed").select("doc_key,status")
                 .eq("purpose", purpose).in_("doc_key", chunk).execute())
            done |= {x["doc_key"] for x in (r.data or [])
                     if x.get("status") not in RETRYABLE_STATUSES}
        except Exception as e:
            log_poller_error("mf_common", "processed_keys", e, {"purpose": purpose})
    return done


def mark_processed(doc_key, purpose, meta, status, **extra):
    try:
        sb().table("mf_filings_processed").upsert({
            "doc_key": doc_key, "purpose": purpose,
            "accession": meta.get("accession"), "cik": meta.get("cik"),
            "form": meta.get("form"), "filing_date": meta.get("filing_date"),
            "status": status, "detail": extra or None, "processed_at": now_iso(),
        }, on_conflict="doc_key,purpose").execute()
    except Exception as e:
        log_poller_error("mf_common", "mark_processed", e, {"doc_key": doc_key})


# ─────────────────────────────────────────────────────────────────────────────
# Alert writing — watchlist-gated, de-duplicated
# ─────────────────────────────────────────────────────────────────────────────

try:
    from feature_map import tag_extra
except Exception:                                   # keep importable in isolation
    def tag_extra(extra, source, filing_type):
        return dict(extra or {})


def alert_exists(source, dedup_key) -> bool:
    try:
        r = (sb().table("alerts").select("id").eq("source", source)
             .eq("extra->>dedup_key", dedup_key).limit(1).execute())
        return bool(r.data)
    except Exception:
        return False


def emit_alerts(source, tickers, summary, impact, filing_type, extra, url=None,
                dedup_key=None, poller="mf_common") -> int:
    """
    One alert row per watched ticker, inserted together so a delivery cycle
    never sees half a fan-out. `dedup_key` identifies the EVENT, not the
    ticker, so the same change arriving through a second filing is written once.
    """
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not tickers:
        return 0
    if dedup_key and alert_exists(source, dedup_key):
        return 0
    body = dict(extra or {})
    if dedup_key:
        body["dedup_key"] = dedup_key
    rows = [{
        "ticker": t, "summary": summary, "impact": impact, "source": source,
        "filing_type": filing_type, "delivered": False,
        "filing_url": url, "link": url,
        "extra": tag_extra(dict(body, fanout_group=dedup_key or filing_type), source, filing_type),
    } for t in tickers]
    try:
        sb().table("alerts").insert(rows).execute()
    except Exception as e:
        log_poller_error(poller, "emit_alerts", e, {"source": source, "tickers": tickers})
        return 0
    log(poller.upper(), f"ALERT {impact} {', '.join(tickers)} | {filing_type}")
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Holdings maths (portfolio alerts)
# ─────────────────────────────────────────────────────────────────────────────

SHIFT_MIN_WEIGHT = float(os.getenv("MF_SHIFT_MIN_WEIGHT", "1.0"))   # % of fund, either side
SHIFT_MIN_PTS = float(os.getenv("MF_SHIFT_MIN_PTS", "0.5"))         # weight change, points
SHIFT_MIN_REL = float(os.getenv("MF_SHIFT_MIN_REL", "0.25"))        # 25% beyond fund flow
SHIFT_HIGH_PTS = float(os.getenv("MF_SHIFT_HIGH_PTS", "3.0"))
SHIFT_MIN_PAIRS = int(os.getenv("MF_SHIFT_MIN_PAIRS", "8"))

# Cash, FX forwards, repos, margin and the fund's own sweep vehicle move every
# quarter in every fund and are not an investment decision.
_NON_SECURITY = re.compile(
    r"\b(cash|collateral|repurchase agreement|reverse repo|repo\b|margin|"
    r"net other assets|other assets|receivab|payab|forward (foreign )?currency|"
    r"currency contract|futures? contract|swap\b|money market fund|"
    r"liquidity fund|sweep|deposit account|u\.?s\.? treasury bill)\b", re.I)


def weight_of(row):
    w = num(row.get("portfolio_pct"))
    return w if w is not None else 0.0


def is_alertable_line(change) -> bool:
    name = f"{change.get('security_name') or ''} {change.get('security_key') or ''} {change.get('title') or ''}"
    return not _NON_SECURITY.search(name)


def label(c) -> str:
    name = (c.get("security_name") or c.get("title") or "").strip()
    sym = (c.get("ticker") or c.get("security_symbol") or "").strip()
    if name and sym and sym.upper() not in name.upper():
        return f"{name} ({sym})"
    return name or sym or (c.get("security_key") or "a position")


def diff_holdings(old, new_full, max_rows):
    """
    old       {key: row} as stored (at most max_rows, the heaviest ones)
    new_full  {key: row} every line this filing reported
    -> (added_keys, removed_keys, new_kept)

    Storage keeps only the heaviest max_rows positions, so a position sliding
    below the cut-off must not read as an exit, and one climbing above it must
    not read as a new buy.
    """
    ranked = sorted(new_full.items(), key=lambda kv: -weight_of(kv[1]))
    new_kept = dict(ranked[:max_rows] if max_rows > 0 else ranked)
    old_truncated = max_rows > 0 and len(old) >= max_rows
    old_cut = min((weight_of(v) for v in old.values()), default=0.0) if old_truncated else None
    removed = [k for k in old if k not in new_full]
    added = [k for k in new_kept
             if k not in old and (not old_truncated or weight_of(new_full[k]) > old_cut)]
    return added, removed, new_kept


def weight_shifts(old, new_rows):
    """
    Positions held in both filings whose SHARE COUNT moved beyond the fund's own
    flow — an active add or trim — rather than a weight that drifted with price.

    flow = median of new_shares / old_shares over all paired positions. A fund
    taking in money scales every line by roughly the same factor, so a line
    tracking `flow` was not traded; one 25% away from it was.
    """
    pairs = []
    for k, n in new_rows.items():
        o = old.get(k)
        if not o:
            continue
        so, sn = num(o.get("balance")), num(n.get("balance"))
        if not so or not sn or so <= 0 or sn <= 0:
            continue
        pairs.append((k, o, n, sn / so))
    if len(pairs) < SHIFT_MIN_PAIRS:
        return []
    ratios = sorted(r for _, _, _, r in pairs)
    mid = len(ratios) // 2
    flow = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    if flow <= 0:
        return []
    out = []
    for k, o, n, r in pairs:
        wo, wn = weight_of(o), weight_of(n)
        if max(wo, wn) < SHIFT_MIN_WEIGHT:
            continue
        rel, dw = r / flow - 1.0, wn - wo
        if abs(rel) < SHIFT_MIN_REL or abs(dw) < SHIFT_MIN_PTS or (rel > 0) != (dw > 0):
            continue
        c = {"security_key": k, "ticker": n.get("ticker") or o.get("ticker"),
             "security_name": n.get("security_name") or o.get("security_name"),
             "title": n.get("title") or o.get("title"),
             "weight_prev": round(wo, 4), "weight_new": round(wn, 4),
             "shares_vs_flow_pct": round(rel * 100, 1)}
        if is_alertable_line(c):
            out.append(c)
    out.sort(key=lambda c: -abs(c["weight_new"] - c["weight_prev"]))
    return out
