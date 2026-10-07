"""
fund_manager_poller.py
GQ FinXray US — Feature: Fund Manager Changes (ETFs + mutual funds), SEC EDGAR only.

Coverage
--------
Universe = every registered open-end fund and ETF share class with a ticker in
SEC's `company_tickers_mf.json` (cik, seriesId, classId, symbol). That file is
the SEC's own ticker registry for investment companies, so it covers NYSE Arca,
Nasdaq and Cboe BZX ETFs and all ticker-bearing mutual funds — no OTC equities,
because it contains no operating companies at all.

Three jobs
----------
1. refresh_universe()          daily   — ticker/series/CIK master -> fund_universe
2. run_event_poll(days_back)   hourly  — EDGAR full-text search over 497/497K
                                         "portfolio manager change stickers".
                                         Fastest signal: lands within days of the
                                         change, before any vendor updates.
3. run_roster_incremental()    daily   — new 485BPOS/497K prospectuses -> rosters,
                                         diffed against stored roster (catches
                                         changes that never got a sticker).
   run_roster_backfill()       weekly  — every fund CIK via the submissions API,
                                         so every series has a current roster.

All data is collected universe-wide (complete coverage). ALERTS are watchlist-
gated, per the platform rule: an alert is written only for tickers that are on
at least one user's watchlist. Zero LLM tokens — parsing is deterministic
(see fund_manager_extractor.py).

Env: SUPABASE_URL, SUPABASE_KEY, optional SEC_USER_AGENT, optional FMP_API_KEY
(used only to label ETF vs mutual fund; falls back to the ticker heuristic).
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

import fund_manager_extractor as fx

try:
    from feature_map import tag_extra
except Exception:                                  # keep importable in isolation
    def tag_extra(extra, source, filing_type):
        return dict(extra or {})

load_dotenv()

SOURCE = "FUND_MANAGER"
SEC_UA = os.getenv("SEC_USER_AGENT", "GQFinXray/1.0 dheerajburli86@gmail.com")
FMP_API_KEY = os.getenv("FMP_API_KEY")

URL_MF_TICKERS = "https://www.sec.gov/files/company_tickers_mf.json"
URL_EFTS = "https://efts.sec.gov/LATEST/search-index"
URL_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
URL_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{name}"

# Phrases that appear in PM-change supplements. Each is a separate EFTS query
# (EFTS matches exact phrases); results are unioned and de-duplicated.
EVENT_QUERIES = [
    '"no longer serves as a portfolio manager"',
    '"will no longer serve as a portfolio manager"',
    '"no longer serve as portfolio managers"',
    '"no longer serves as portfolio manager"',
    '"has been added as a portfolio manager"',
    '"added as a portfolio manager"',
    '"will join" "portfolio manager"',
    '"retire" "portfolio manager"',
    '"all references to" "portfolio manager"',
    '"replaced with the following" "portfolio managers"',
    '"co-portfolio manager"',
    '"portfolio management team" "no longer"',
]
EVENT_FORMS = "497,497K"
ROSTER_FORMS = ("485BPOS", "497K")

MAX_DOC_BYTES = 40_000_000

_sb = None


def sb():
    global _sb
    if _sb is None:
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def log(msg):
    print(f"[FUND_MGR {datetime.now().strftime('%H:%M:%S')}] {msg}")


def log_poller_error(job_name, error, context=None):
    """Same pattern as every other poller: failures land in poller_error_log."""
    log(f"ERROR {job_name}: {error}")
    try:
        sb().table("poller_error_log").insert({
            "poller_name": "fund_manager_poller",
            "job_name": job_name,
            "error_message": str(error)[:2000],
            "error_traceback": traceback.format_exc()[:8000],
            "context": context or {},
        }).execute()
    except Exception as log_err:
        log(f"failed to write poller_error_log: {log_err}")


# ─────────────────────────────────────────────────────────────────────────────
# SEC transport — token bucket at 8 req/s (SEC fair-access limit is 10)
# ─────────────────────────────────────────────────────────────────────────────

#
# ONE BUDGET, SHARED WITH THE FAST LANE. SEC counts requests per egress IP, and
# the 8-K / 10-Q / 10-K / Form 4 pollers already pace themselves through
# sec_client's sliding window (SEC_MAX_RPS, default 5/s). A second, private
# bucket here at 8/s would put the process at ~13/s — over SEC's 10/s policy,
# which gets the whole IP throttled and silences the fast lane with it.
#
# So this transport takes its slots from that SAME window, but only while it
# holds fewer than FUND_SEC_MAX_SHARE of them (default 3 of 5). A long roster
# backfill therefore always leaves room for an 8-K to be fetched immediately,
# and any 429 seen here parks every SEC caller via the shared cooldown.
try:
    import sec_client as _sc
except Exception:                                  # keep importable in isolation
    _sc = None

FUND_SEC_MAX_SHARE = float(os.getenv("FUND_SEC_MAX_SHARE", "3"))
FUND_SEC_RPS = float(os.getenv("FUND_SEC_RPS", "3"))   # only used if sec_client is absent


def _shared_slot() -> bool:
    """Block until a slot in sec_client's window is ours. False = no shared window."""
    if _sc is None or not hasattr(_sc, "_request_times"):
        return False
    cap = max(1.0, min(FUND_SEC_MAX_SHARE, float(getattr(_sc, "MAX_RPS", 5))))
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
    def __init__(self, rate: float = FUND_SEC_RPS):
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


# ─────────────────────────────────────────────────────────────────────────────
# Universe
# ─────────────────────────────────────────────────────────────────────────────

_universe_cache: dict | None = None


def _etf_symbols() -> set[str]:
    if not FMP_API_KEY:
        return set()
    try:
        r = requests.get("https://financialmodelingprep.com/stable/etf-list",
                         params={"apikey": FMP_API_KEY}, timeout=60)
        return {(row.get("symbol") or "").upper() for row in r.json() or []}
    except Exception as e:
        log_poller_error("etf_symbols", e)
        return set()


def classify(ticker: str, etf_set: set[str]) -> str:
    """ETF if FMP lists it; otherwise the NASD convention: mutual fund tickers
    are 5 letters ending in X, exchange-traded tickers are not."""
    if ticker in etf_set:
        return "ETF"
    return "MF" if (len(ticker) == 5 and ticker.endswith("X")) else "ETF"


def load_universe(force: bool = False) -> dict:
    """
    Returns {
      "by_ticker": {ticker: {cik, series_id, class_id, fund_type}},
      "by_series": {series_id: {cik, tickers:[...], fund_type}},
      "ciks": set of fund-registrant CIKs,
    }
    """
    global _universe_cache
    if _universe_cache and not force:
        return _universe_cache
    data = sec.json(URL_MF_TICKERS) or {}
    fields = data.get("fields") or ["cik", "seriesId", "classId", "symbol"]
    idx = {f: i for i, f in enumerate(fields)}
    etfs = _etf_symbols()
    by_ticker, by_series = {}, {}
    for row in data.get("data") or []:
        try:
            cik = str(row[idx["cik"]]).zfill(10)
            sid, cid, sym = row[idx["seriesId"]], row[idx["classId"]], (row[idx["symbol"]] or "").upper()
        except (KeyError, IndexError, TypeError):
            continue
        if not sym or not sid:
            continue
        ftype = classify(sym, etfs)
        by_ticker[sym] = {"cik": cik, "series_id": sid, "class_id": cid, "fund_type": ftype}
        s = by_series.setdefault(sid, {"cik": cik, "tickers": [], "fund_type": ftype})
        s["tickers"].append(sym)
        if ftype == "ETF":
            s["fund_type"] = "ETF"
    if not by_ticker:
        # A failed or empty download must never become the cache. Caching it
        # made every later call a cache hit on nothing, blinding Features 15
        # and 16 until the next daily refresh — and a failed daily refresh
        # replaced a good cache with an empty one. Keep the last good copy
        # (or nothing) and let the next call try again.
        log_poller_error("load_universe", "company_tickers_mf.json empty or unavailable")
        return _universe_cache or {"by_ticker": {}, "by_series": {}, "ciks": set()}
    _universe_cache = {"by_ticker": by_ticker, "by_series": by_series,
                       "ciks": {v["cik"] for v in by_series.values()}}
    log(f"universe: {len(by_ticker):,} tickers, {len(by_series):,} series, "
        f"{len(_universe_cache['ciks']):,} registrants "
        f"({sum(1 for v in by_ticker.values() if v['fund_type'] == 'ETF'):,} ETF / "
        f"{sum(1 for v in by_ticker.values() if v['fund_type'] == 'MF'):,} MF)")
    return _universe_cache


def refresh_universe():
    """Persist the universe so the frontend / SQL can join on it."""
    try:
        u = load_universe(force=True)
        rows = [{"ticker": t, "cik": v["cik"], "series_id": v["series_id"],
                 "class_id": v["class_id"], "fund_type": v["fund_type"],
                 "updated_at": _now()} for t, v in u["by_ticker"].items()]
        for i in range(0, len(rows), 1000):
            sb().table("fund_universe").upsert(rows[i:i + 1000], on_conflict="ticker").execute()
        log(f"fund_universe upserted: {len(rows):,}")
    except Exception as e:
        log_poller_error("refresh_universe", e)


# ─────────────────────────────────────────────────────────────────────────────
# Filing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


def _archive(cik, adsh, name):
    return URL_ARCHIVE.format(cik=int(cik), nodash=adsh.replace("-", ""), name=name)


def fetch_series_header(cik, adsh) -> list[dict]:
    for name in (f"{adsh}.hdr.sgml", f"{adsh}-index-headers.html"):
        txt = sec.text(_archive(cik, adsh, name))
        if txt and "<SERIES>" in txt.upper():
            return fx.parse_series_header(txt)
    return []


# Statuses that record a TRANSIENT failure (a 429 on the header or document, a
# crash mid-filing). They are kept for visibility but do not count as handled,
# so the next poll inside the lookback window tries the filing again instead
# of losing a real manager change to one throttled request.
RETRYABLE_STATUSES = {"NO_DOC", "ERROR", "NO_SERIES"}


def already_processed(adsh, purpose) -> bool:
    try:
        r = sb().table("fund_filings_processed").select("accession, status") \
            .eq("accession", adsh).eq("purpose", purpose).limit(1).execute()
        return bool(r.data) and (r.data[0].get("status") not in RETRYABLE_STATUSES)
    except Exception:
        return False


def mark_processed(adsh, purpose, cik, form, filing_date, status, n_series=0, n_items=0, error=None):
    try:
        sb().table("fund_filings_processed").upsert({
            "accession": adsh, "purpose": purpose, "cik": cik, "form": form,
            "filing_date": filing_date, "status": status, "n_series": n_series,
            "n_items": n_items, "error": (error or "")[:1000] or None,
            "processed_at": _now(),
        }, on_conflict="accession,purpose").execute()
    except Exception as e:
        log_poller_error("mark_processed", e, {"accession": adsh})


# ─────────────────────────────────────────────────────────────────────────────
# Roster storage + diff
# ─────────────────────────────────────────────────────────────────────────────

def get_current_roster(series_id) -> tuple[list[dict], str | None]:
    try:
        r = sb().table("fund_managers").select("*").eq("series_id", series_id) \
            .eq("is_current", True).execute()
        rows = r.data or []
        asof = max((x.get("filing_date") or "" for x in rows), default=None) or None
        return [{"name": x["manager_name"], **x} for x in rows], asof
    except Exception as e:
        log_poller_error("get_current_roster", e, {"series_id": series_id})
        return [], None


def save_roster(series_id, managers, meta) -> None:
    """Upsert the current roster; managers not in it are marked not-current."""
    keep = set()
    rows = []
    for m in managers:
        k = fx.normalize_name(m["name"])
        keep.add(k)
        rows.append({
            "series_id": series_id, "manager_key": k, "manager_name": m["name"],
            "title": m.get("title"), "role": m.get("role"),
            "since_year": m.get("since_year"), "since_month": m.get("since_month"),
            "since_raw": m.get("since_raw"), "is_inception": bool(m.get("is_inception")),
            "is_current": True, "removed_at": None,
            "source_form": meta["form"], "source_accession": meta["accession"],
            "source_url": meta["url"], "filing_date": meta["filing_date"],
            "cik": meta["cik"], "last_seen_at": _now(),
        })
    try:
        if rows:
            sb().table("fund_managers").upsert(rows, on_conflict="series_id,manager_key").execute()
        cur, _ = get_current_roster(series_id)
        gone = [c["manager_key"] for c in cur if c["manager_key"] not in keep]
        if gone:
            sb().table("fund_managers").update({"is_current": False, "removed_at": _now()}) \
                .eq("series_id", series_id).in_("manager_key", gone).execute()
    except Exception as e:
        log_poller_error("save_roster", e, {"series_id": series_id})


def apply_event_to_roster(series_id, ev, meta):
    try:
        if ev["event_type"] == "REMOVED":
            sb().table("fund_managers").update({"is_current": False, "removed_at": _now()}) \
                .eq("series_id", series_id).eq("manager_key", ev["manager_key"]).execute()
        else:
            sb().table("fund_managers").upsert({
                "series_id": series_id, "manager_key": ev["manager_key"],
                "manager_name": ev["manager"], "is_current": True, "removed_at": None,
                "source_form": meta["form"], "source_accession": meta["accession"],
                "source_url": meta["url"], "filing_date": meta["filing_date"],
                "cik": meta["cik"], "last_seen_at": _now(),
            }, on_conflict="series_id,manager_key").execute()
    except Exception as e:
        log_poller_error("apply_event_to_roster", e, {"series_id": series_id})


def recent_event_exists(series_id, manager_key, event_type, days=180) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        r = sb().table("fund_manager_events").select("id").eq("series_id", series_id) \
            .eq("manager_key", manager_key).eq("event_type", event_type) \
            .gte("created_at", since).limit(1).execute()
        return bool(r.data)
    except Exception:
        return False


def store_event(series_id, ev, meta, detection) -> bool:
    """Insert one event row. Returns True if it was new (unique constraint)."""
    u = load_universe()
    tickers = (u["by_series"].get(series_id) or {}).get("tickers", [])
    try:
        sb().table("fund_manager_events").insert({
            "series_id": series_id, "cik": meta["cik"], "manager_key": ev["manager_key"],
            "manager_name": ev["manager"], "event_type": ev["event_type"],
            "effective_date": ev.get("effective_date"),
            "effective_date_is_filing_date": ev.get("effective_date_is_filing_date", True),
            "detection_source": detection, "accession": meta["accession"],
            "form": meta["form"], "filing_date": meta["filing_date"],
            "filing_url": meta["url"], "evidence": ev.get("evidence"),
            "tickers": tickers, "series_name": meta.get("series_names", {}).get(series_id),
        }).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        log_poller_error("store_event", e, {"series_id": series_id, "accession": meta["accession"]})
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Alerts — watchlist-gated, one alert per (filing, fund, watched ticker)
# ─────────────────────────────────────────────────────────────────────────────

def watched_tickers() -> set[str]:
    # watchlist_util paginates; a bare select stops at PostgREST's 1,000-row cap
    # and silently drops every watcher past it once the user base grows.
    # watchlist_util swallows its own errors and returns an empty set, so an
    # empty answer falls through to a direct read rather than being trusted.
    try:
        from watchlist_util import get_watched_tickers
        watched = {t.upper() for t in get_watched_tickers(force=True)}
        if watched:
            return watched
    except Exception:
        pass
    try:
        rows = sb().table("watchlists").select("ticker").execute().data or []
        return {(r.get("ticker") or "").upper() for r in rows if r.get("ticker")}
    except Exception as e:
        log_poller_error("watched_tickers", e)
        return set()


def _fmt_date(d):
    try:
        return datetime.fromisoformat(d).strftime("%b %d, %Y")
    except Exception:
        return d or "n/a"


def build_alert(series_name, fund_type, added, removed, remaining, eff, meta):
    kind = "ETF" if fund_type == "ETF" else "mutual fund"
    parts = []
    if removed:
        parts.append(f"{', '.join(e['manager'] for e in removed)} "
                     f"{'is' if len(removed) == 1 else 'are'} leaving as portfolio manager"
                     f"{'' if len(removed) == 1 else 's'}")
    if added:
        parts.append(f"{', '.join(e['manager'] for e in added)} "
                     f"{'joins' if len(added) == 1 else 'join'} the portfolio management team")
    summary = f"Portfolio manager change at {series_name} ({kind}): " + "; ".join(parts) + "."
    if eff:
        summary += f" Effective {_fmt_date(eff)}."
    if remaining:
        summary += f" Current team: {', '.join(remaining)}."
    elif removed and not added:
        summary += " No remaining named manager disclosed in this filing — watch for a replacement."
    summary += f" Source: SEC Form {meta['form']} filed {_fmt_date(meta['filing_date'])}."

    lead_left = any((e.get("role") or "").startswith("lead") for e in removed)
    if removed and (not remaining or lead_left):
        impact = "HIGH"
    else:
        # Additions are MEDIUM, not LOW. delivery.py defaults every user to
        # min_impact=MEDIUM, so a LOW alert reaches nobody who has not changed
        # their settings — a new manager joining a watched fund would be
        # detected, stored and then silently dropped for every default user.
        impact = "MEDIUM"
    ftype = "PM_CHANGE" if (added and removed) else ("PM_REMOVED" if removed else "PM_ADDED")
    return summary, impact, ftype


def emit_alerts(series_id, new_events, meta, watched):
    if not new_events:
        return 0
    u = load_universe()
    s = u["by_series"].get(series_id) or {}
    hits = [t for t in s.get("tickers", []) if t in watched]
    if not hits:
        return 0
    added = [e for e in new_events if e["event_type"] == "ADDED"]
    removed = [e for e in new_events if e["event_type"] == "REMOVED"]
    roster, _ = get_current_roster(series_id)
    remaining = [r["manager_name"] for r in roster]
    eff = next((e.get("effective_date") for e in new_events if not e.get("effective_date_is_filing_date")), None) \
        or meta["filing_date"]
    name = meta.get("series_names", {}).get(series_id) or series_id
    summary, impact, ftype = build_alert(name, s.get("fund_type"), added, removed, remaining, eff, meta)
    group = (f"{SOURCE}:{series_id}:{ftype}:"
             + ",".join(sorted(e["manager_key"] for e in new_events)))
    rows = []
    for t in hits:
        try:
            dup = sb().table("alerts").select("id").eq("ticker", t).eq("source", SOURCE) \
                .eq("extra->>fanout_group", group).limit(1).execute()
            if dup.data:
                continue
        except Exception as e:
            log_poller_error("emit_alerts_dedup", e, {"ticker": t, "accession": meta["accession"]})
            continue
        rows.append({
            "ticker": t, "summary": summary, "impact": impact, "source": SOURCE,
            "filing_type": ftype, "delivered": False,
            "filing_url": meta["url"], "link": meta["url"],
            "extra": tag_extra({
                "series_id": series_id, "series_name": name, "company_name": name,
                "fund_type": s.get("fund_type"),
                "accession": meta["accession"], "form": meta["form"],
                "filing_date": meta["filing_date"], "effective_date": eff,
                "added": [e["manager"] for e in added], "removed": [e["manager"] for e in removed],
                "current_team": remaining, "detection": meta.get("detection"),
                # Same change, any filing, any watched share class: one group.
                # Delivery sends a user who watches several classes one message.
                "fanout_group": group,
            }, SOURCE, ftype),
        })
    if not rows:
        return 0
    try:
        # One insert for every watched class, so a delivery cycle never sees
        # half of the group.
        sb().table("alerts").insert(rows).execute()
    except Exception as e:
        log_poller_error("emit_alerts", e, {"series_id": series_id, "accession": meta["accession"]})
        return 0
    for r in rows:
        log(f"ALERT {impact} {r['ticker']} | {ftype} | {name}")
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Job 2 — event poll (497 / 497K stickers via EDGAR full-text search)
# ─────────────────────────────────────────────────────────────────────────────

def efts_hits(query, forms, start, end, max_pages=20):
    """Yield EFTS hits for one query, paginating 100 at a time."""
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


def _hit_meta(h):
    src = h.get("_source") or {}
    hid = h.get("_id") or ""
    adsh, _, fname = hid.partition(":")
    ciks = src.get("ciks") or []
    if not adsh or not fname or not ciks:
        return None
    cik = ciks[0]
    return {"accession": adsh, "cik": cik.zfill(10), "form": src.get("form") or src.get("file_type"),
            "filing_date": src.get("file_date"), "filename": fname,
            "url": _archive(cik, adsh, fname), "filer": (src.get("display_names") or [""])[0],
            "key": f"{adsh}:{fname}"}


def process_event_filing(meta, watched) -> int:
    series = fetch_series_header(meta["cik"], meta["accession"])
    if not series:
        # Header unavailable (often just a 429). Fall back to the registry ONLY
        # for a single-series trust, where there is nothing to attribute. For a
        # multi-series trust the fallback series have no names, so attribution
        # would degrade to "every series" and one manager leaving one fund
        # would alert holders of every fund the trust runs. Retry instead.
        u = load_universe()
        own = [sid for sid, v in u["by_series"].items() if v["cik"] == meta["cik"]]
        if len(own) != 1:
            mark_processed(meta["key"], "EVENT", meta["cik"], meta["form"], meta["filing_date"], "NO_SERIES")
            return 0
        series = [{"series_id": own[0], "series_name": "", "classes": []}]
    meta["series_names"] = {s["series_id"]: s.get("series_name") for s in series}
    html = sec.text(meta["url"])
    if not html:
        mark_processed(meta["key"], "EVENT", meta["cik"], meta["form"], meta["filing_date"], "NO_DOC")
        return 0
    # Full and summary prospectuses (also filed as 497 / 497K) carry manager
    # bios — "joined the Fund as co-portfolio manager in 2021" — that parse as
    # additions. Only a supplement announces a change.
    if not fx.is_supplement(fx.lines_to_text(fx.html_to_lines(html)), meta.get("form")):
        mark_processed(meta["key"], "EVENT", meta["cik"], meta["form"], meta["filing_date"], "NOT_SUPPLEMENT")
        return 0
    out = fx.extract_change_events(html, series, meta["filing_date"])
    meta["detection"] = "STICKER_497"
    per_series = defaultdict(list)
    for ev in out["events"]:
        for sid in ev["series_ids"]:
            # Trusts file the same sticker under several accessions (one per
            # prospectus / share class). The unique key includes the accession,
            # so without this the second copy was a "new" event and a second alert.
            if recent_event_exists(sid, ev["manager_key"], ev["event_type"]):
                continue
            if store_event(sid, ev, meta, "STICKER_497"):
                apply_event_to_roster(sid, ev, meta)
                per_series[sid].append(ev)
    # "table replaced with the following" -> diff against stored roster
    for sid, new_roster in out["replacement_rosters"].items():
        old, _ = get_current_roster(sid)
        if old:
            d = fx.diff_rosters(old, new_roster)
            for kind, lst in (("ADDED", d["added"]), ("REMOVED", d["removed"])):
                for m in lst:
                    ev = {"event_type": kind, "manager": m.get("manager_name") or m["name"],
                          "manager_key": fx.normalize_name(m.get("manager_name") or m["name"]),
                          "effective_date": meta["filing_date"], "effective_date_is_filing_date": True,
                          "evidence": "Portfolio manager table replaced in supplement.", "role": m.get("role")}
                    if recent_event_exists(sid, ev["manager_key"], kind):
                        continue
                    if store_event(sid, ev, meta, "REPLACEMENT_TABLE"):
                        per_series[sid].append(ev)
        save_roster(sid, new_roster, meta)
    n_alerts = sum(emit_alerts(sid, evs, meta, watched) for sid, evs in per_series.items())
    mark_processed(meta["key"], "EVENT", meta["cik"], meta["form"], meta["filing_date"], "OK",
                   n_series=len(series), n_items=sum(len(v) for v in per_series.values()))
    return n_alerts


def run_event_poll(days_back: int = 2):
    """Hourly. Finds every PM-change supplement filed in the window."""
    end = date.today()
    start = end - timedelta(days=days_back)
    log(f"event poll {start}..{end}")
    try:
        load_universe()
        watched = watched_tickers()
        seen = {}
        for q in EVENT_QUERIES:
            for h in efts_hits(q, EVENT_FORMS, start.isoformat(), end.isoformat()):
                m = _hit_meta(h)
                if m and m["key"] not in seen:
                    seen[m["key"]] = m
        log(f"{len(seen)} candidate documents")
        alerts = 0
        for key, m in seen.items():
            if already_processed(key, "EVENT"):
                continue
            try:
                alerts += process_event_filing(m, watched)
            except Exception as e:
                log_poller_error("process_event_filing", e, {"accession": m["accession"]})
                mark_processed(key, "EVENT", m["cik"], m["form"], m["filing_date"], "ERROR", error=str(e))
        log(f"event poll done — {alerts} alerts")
    except Exception as e:
        log_poller_error("run_event_poll", e)


# ─────────────────────────────────────────────────────────────────────────────
# Job 3 — rosters from 485BPOS / 497K
# ─────────────────────────────────────────────────────────────────────────────

def process_roster_filing(meta, watched, emit_diffs=True) -> dict:
    """Extract rosters for every series in one prospectus filing; diff + store."""
    series = fetch_series_header(meta["cik"], meta["accession"])
    if not series:
        mark_processed(meta["accession"], "ROSTER", meta["cik"], meta["form"], meta["filing_date"], "NO_SERIES")
        return {}
    meta["series_names"] = {s["series_id"]: s.get("series_name") for s in series}
    html = sec.text(meta["url"])
    if not html:
        mark_processed(meta["accession"], "ROSTER", meta["cik"], meta["form"], meta["filing_date"], "NO_DOC")
        return {}
    rosters = fx.extract_rosters(html, series, meta["filing_date"])
    rosters.pop("_unattributed", None)
    meta["detection"] = "ROSTER_DIFF"
    for sid, new in rosters.items():
        old, old_asof = get_current_roster(sid)
        if old_asof and meta["filing_date"] and old_asof > meta["filing_date"]:
            continue                               # never overwrite with an older prospectus
        new_events = []
        if old and emit_diffs:
            d = fx.diff_rosters(old, new)
            for kind, lst in (("ADDED", d["added"]), ("REMOVED", d["removed"])):
                for m in lst:
                    nm = m.get("manager_name") or m["name"]
                    k = fx.normalize_name(nm)
                    if recent_event_exists(sid, k, kind):  # sticker already reported it
                        continue
                    ev = {"event_type": kind, "manager": nm, "manager_key": k,
                          "effective_date": meta["filing_date"], "effective_date_is_filing_date": True,
                          "evidence": f"Roster in {meta['form']} differs from previously stored roster.",
                          "role": m.get("role")}
                    if store_event(sid, ev, meta, "ROSTER_DIFF"):
                        new_events.append(ev)
        save_roster(sid, new, meta)
        emit_alerts(sid, new_events, meta, watched)
    mark_processed(meta["accession"], "ROSTER", meta["cik"], meta["form"], meta["filing_date"], "OK",
                   n_series=len(series), n_items=sum(len(v) for v in rosters.values()))
    return rosters


def run_roster_incremental(days_back: int = 2):
    """Daily. Every new 485BPOS/497K prospectus that names portfolio managers."""
    end = date.today()
    start = end - timedelta(days=days_back)
    log(f"roster incremental {start}..{end}")
    try:
        load_universe()
        watched = watched_tickers()
        seen = {}
        for q in ('"portfolio managers"', '"portfolio manager"'):
            for h in efts_hits(q, ",".join(ROSTER_FORMS), start.isoformat(), end.isoformat(), max_pages=50):
                m = _hit_meta(h)
                if m and m["accession"] not in seen:
                    seen[m["accession"]] = m
        for adsh, m in seen.items():
            if already_processed(adsh, "ROSTER"):
                continue
            try:
                process_roster_filing(m, watched)
            except Exception as e:
                log_poller_error("process_roster_filing", e, {"accession": adsh})
        log(f"roster incremental done — {len(seen)} filings")
    except Exception as e:
        log_poller_error("run_roster_incremental", e)


def run_roster_backfill(max_ciks: int | None = None, lookback_days: int = 460, emit_diffs: bool = False):
    """
    Weekly (and first run). Walks every fund registrant CIK, newest prospectus
    first, until every series of that CIK has a roster. emit_diffs=False on the
    very first run so the backfill itself doesn't fire thousands of alerts.
    """
    u = load_universe(force=True)
    watched = watched_tickers()
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    ciks = sorted(u["ciks"])[: max_ciks or None]
    log(f"roster backfill over {len(ciks):,} registrants")
    covered_total = 0
    for i, cik in enumerate(ciks, 1):
        try:
            want = {sid for sid, v in u["by_series"].items() if v["cik"] == cik}
            got: set[str] = set()
            sub = sec.json(URL_SUBMISSIONS.format(cik10=cik)) or {}
            rec = (sub.get("filings") or {}).get("recent") or {}
            forms = rec.get("form") or []
            for j, form in enumerate(forms):
                if form not in ROSTER_FORMS:
                    continue
                fdate = rec["filingDate"][j]
                if fdate < cutoff or want <= got:
                    break
                adsh, doc = rec["accessionNumber"][j], rec["primaryDocument"][j]
                if not doc or already_processed(adsh, "ROSTER"):
                    continue
                meta = {"accession": adsh, "cik": cik, "form": form, "filing_date": fdate,
                        "filename": doc, "url": _archive(cik, adsh, doc)}
                got |= set(process_roster_filing(meta, watched, emit_diffs=emit_diffs).keys())
            covered_total += len(got & want)
            if i % 25 == 0:
                log(f"backfill {i}/{len(ciks)} registrants — {covered_total:,} series with rosters")
        except Exception as e:
            log_poller_error("run_roster_backfill", e, {"cik": cik})
    log(f"backfill done — {covered_total:,} series with rosters this pass")


def coverage_report() -> dict:
    """Quick health check: how much of the universe has a current roster."""
    u = load_universe()
    try:
        rows = sb().table("fund_managers").select("series_id").eq("is_current", True).execute().data or []
    except Exception:
        rows = []
    have = {r["series_id"] for r in rows}
    etf = {s for s, v in u["by_series"].items() if v["fund_type"] == "ETF"}
    mf = set(u["by_series"]) - etf
    rep = {"series_total": len(u["by_series"]), "series_with_roster": len(have & set(u["by_series"])),
           "etf_series": len(etf), "etf_with_roster": len(have & etf),
           "mf_series": len(mf), "mf_with_roster": len(have & mf)}
    log(f"coverage: {rep}")
    return rep


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler entry points (main.py). Distinct names because main.job() keys its
# "previous run still active" guard on __name__, and the coverage test looks
# for these literal names in main.py.
# ─────────────────────────────────────────────────────────────────────────────

def run_fund_universe_refresh():
    refresh_universe()


def run_fund_manager_events():
    run_event_poll(days_back=2)


def run_fund_manager_rosters():
    run_roster_incremental(days_back=2)


def run_fund_manager_backfill():
    # emit_diffs=False: a backfill builds baselines, it must never alert.
    run_roster_backfill(emit_diffs=False)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "events"
    if cmd == "universe":
        refresh_universe()
    elif cmd == "events":
        run_event_poll(int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    elif cmd == "rosters":
        run_roster_incremental(int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    elif cmd == "backfill":
        run_roster_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "coverage":
        coverage_report()
