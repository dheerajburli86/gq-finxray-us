"""
stock_master_poller.py
GQ FinXray US — stock company master + reference-change alerts (Features 18, 19).

The India system has a CMOTS company master (name, ISIN, exchange codes,
sector, market-cap class, listing flags). The US side had only `stocks`
(ticker, exchange, sector) from a one-time CSV import. This builds the US
equivalent daily, from three sources:

  SEC   company_tickers_exchange.json   ticker, CIK, name, exchange  (backbone)
  Massive /v3/reference/tickers         primary exchange (MIC), security type,
                                         FIGI, and the "still trading" check
  FMP   company-screener                sector, industry, market cap, country
  FMP   sp500/nasdaq/dowjones-constituent  index membership
  FMP   profile (a few hundred a day)   ISIN, CUSIP, IPO date

Changes it detects (each one confirmed before it counts):

  Feature 18  STOCK_SYMBOL_CHANGE   same CIK, one ticker swapped for another
              STOCK_NAME_CHANGE     company renamed (seen on 2 separate days)
              STOCK_EXCHANGE_CHANGE listing moved, e.g. NYSE -> Nasdaq, or -> OTC
              STOCK_DELISTED        gone from SEC's list AND Massive's active
                                     list on 2 separate days
  Feature 19  STOCK_MCAP_CLASS_UP / _DOWN  mega/large/mid/small/micro/nano,
                                     5% past the boundary, on 2 separate days
              STOCK_SECTOR_CHANGE   FMP sector changed, on 2 separate days
              STOCK_INDEX_ADDED / _REMOVED  S&P 500, Nasdaq-100, Dow 30

Every confirmed change goes to stock_master_events. An alert is written only
for a ticker on someone's watchlist, and only when STOCK_ALERTS_ENABLED=true:
these are new features and other users' feeds stay as they are until the
switch is flipped. Data is collected either way.

Run by hand:
    python stock_master_poller.py            one pass (alerts per the switch)
    python stock_master_poller.py dry        one pass, print events, write nothing
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

import fmp_client
import massive_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
POLLER = "stock_master_poller"
SOURCE = "STOCK_MASTER"


def _env_bool(name, default=False):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


CONFIRM_DAYS = int(os.getenv("STOCK_CONFIRM_DAYS", "2"))
MCAP_HYSTERESIS = float(os.getenv("STOCK_MCAP_HYSTERESIS", "0.05"))
PROFILE_BATCH = int(os.getenv("STOCK_PROFILE_BATCH", "400"))
PROFILE_REFRESH_DAYS = int(os.getenv("STOCK_PROFILE_REFRESH_DAYS", "30"))
FMP_PAUSE = float(os.getenv("STOCK_FMP_PAUSE", "0.25"))
MIN_SEC_ROWS = int(os.getenv("STOCK_MIN_SEC_ROWS", "5000"))
MIN_SCREENER_ROWS = int(os.getenv("STOCK_MIN_SCREENER_ROWS", "2000"))
MAX_INDEX_CHANGES = int(os.getenv("STOCK_MAX_INDEX_CHANGES", "10"))
DEDUP_DAYS = 45

SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_UA = os.getenv("SEC_USER_AGENT", "GQuants FinXray admin@gquants.com")
MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "CBOE"}
ETF_TYPES = {"ETF", "ETN", "ETV", "ETS", "FUND"}

# Lower bound of each class, largest first. Standard US conventions.
MCAP_BANDS = [("MEGA", 200e9), ("LARGE", 10e9), ("MID", 2e9),
              ("SMALL", 300e6), ("MICRO", 50e6), ("NANO", 0.0)]
MCAP_RANK = {name: i for i, (name, _) in enumerate(MCAP_BANDS)}   # 0 = biggest
MCAP_LABEL = {"MEGA": "Mega-cap", "LARGE": "Large-cap", "MID": "Mid-cap",
              "SMALL": "Small-cap", "MICRO": "Micro-cap", "NANO": "Nano-cap"}

INDEXES = {"SP500": "sp500-constituent", "NASDAQ100": "nasdaq-constituent",
           "DOW30": "dowjones-constituent"}
INDEX_LABEL = {"SP500": "S&P 500", "NASDAQ100": "Nasdaq-100", "DOW30": "Dow Jones Industrial Average"}
INDEX_MIN_SIZE = {"SP500": 480, "NASDAQ100": 95, "DOW30": 28}

# Event types that can alert. LISTED is stored only: a new listing cannot be on
# anyone's watchlist yet.
ALERTABLE = {
    "SYMBOL_CHANGE": "STOCK_SYMBOL_CHANGE",
    "NAME_CHANGE": "STOCK_NAME_CHANGE",
    "EXCHANGE_CHANGE": "STOCK_EXCHANGE_CHANGE",
    "DELISTED": "STOCK_DELISTED",
    "MCAP_CLASS_CHANGE": None,        # UP / DOWN decided per event
    "SECTOR_CHANGE": "STOCK_SECTOR_CHANGE",
    "INDEX_ADDED": "STOCK_INDEX_ADDED",
    "INDEX_REMOVED": "STOCK_INDEX_REMOVED",
}


# ── Helpers ──────────────────────────────────────────────────────────────────
_sb = None


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _today_et():
    return datetime.now(ET).date().isoformat()


def norm_ticker(t):
    """Canonical form is SEC's: BRK-B, not BRK.B or BRK/B."""
    return (t or "").strip().upper().replace(".", "-").replace("/", "-")


def _num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|the|"
                     r"sa|nv|ag|se|lp|llc|holdings?|group|class [a-z])\b")


def norm_name(name):
    """Ignore punctuation, case and corporate suffixes: 'Apple Inc.' == 'APPLE INC'."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    s = _SUFFIX.sub(" ", s)
    return " ".join(s.split())


def mcap_class(cap):
    if cap is None or cap <= 0:
        return None
    for name, lo in MCAP_BANDS:
        if cap >= lo:
            return name
    return "NANO"


def sticky_mcap_class(cap, current):
    """New class only once the cap is MCAP_HYSTERESIS past the boundary, so a
    stock sitting on $10B does not flip large/mid every other day."""
    new = mcap_class(cap)
    if current not in MCAP_RANK:
        return new
    if new is None or new == current:
        return current
    rank = MCAP_RANK[current]
    if MCAP_RANK[new] < rank:                                # bigger class
        boundary = MCAP_BANDS[rank - 1][1]
        return new if cap >= boundary * (1 + MCAP_HYSTERESIS) else current
    boundary = MCAP_BANDS[rank][1]                           # smaller class
    return new if cap < boundary * (1 - MCAP_HYSTERESIS) else current


def confirm(row, field, value, today, need=None):
    """Count a proposed change once per calendar day; True on the day it has
    been seen `need` separate days in a row. A different value restarts it."""
    need = CONFIRM_DAYS if need is None else need
    p = row.setdefault("pending", {})
    ent = p.get(field)
    if ent and ent.get("value") == value:
        if ent.get("last") != today:
            ent["count"] = int(ent.get("count") or 0) + 1
            ent["last"] = today
    else:
        ent = p[field] = {"value": value, "count": 1, "first": today, "last": today}
    if ent["count"] >= need:
        p.pop(field, None)
        return True
    return False


def clear(row, field):
    (row.get("pending") or {}).pop(field, None)


# ── Pure diff ────────────────────────────────────────────────────────────────
def diff_master(prev, sec, mas, fmp, idx, prev_idx, today):
    """
    prev      {ticker: stock_master row}            (may be empty: baseline)
    sec       {ticker: {"cik","name","exchange"}}   required
    mas       {ticker: {"primary_exchange","type","composite_figi"}} or None
    fmp       {ticker: {"sector","industry","market_cap","country"}} or None
    idx       {code: set(tickers) or None}   None = that index unavailable
    prev_idx  {code: set(tickers)}           from stock_index_members
    -> (rows {ticker: row}, events [dict])
    """
    baseline = not prev
    out, events = {}, []

    def ev(t, row, kind, old, new):
        events.append({"ticker": t, "cik": row.get("cik"), "name": row.get("name") or t,
                       "event_type": kind, "old_value": None if old is None else str(old),
                       "new_value": None if new is None else str(new)})

    def copy(t):
        r = dict(prev[t])
        r["pending"] = dict(r.get("pending") or {})
        r["indexes"] = list(r.get("indexes") or [])
        return r

    # Index guard: a vendor hiccup that swaps a dozen names at once is not news.
    idx = dict(idx)
    for code, members in list(idx.items()):
        before = prev_idx.get(code) or set()
        if members is None:
            continue
        if len(members) < INDEX_MIN_SIZE.get(code, 1):
            logger.warning("[STOCK] %s list has %d names -- ignored this run", code, len(members))
            idx[code] = None
        elif before and len(members ^ before) > MAX_INDEX_CHANGES:
            logger.warning("[STOCK] %s changed by %d names in one run -- ignored", code, len(members ^ before))
            idx[code] = None

    universe = {t for t, s in sec.items()
                if (s.get("exchange") or "").upper() in MAJOR_EXCHANGES or t in prev}

    # Symbol changes: a CIK that lost exactly one ticker and gained exactly one.
    prev_by_cik, new_by_cik = {}, {}
    for t, r in prev.items():
        if r.get("is_active", True) and not r.get("renamed_to") and r.get("cik"):
            prev_by_cik.setdefault(str(r["cik"]), set()).add(t)
    for t in universe:
        if sec[t].get("cik"):
            new_by_cik.setdefault(str(sec[t]["cik"]), set()).add(t)
    renamed = {}                                          # new -> old
    for cik, before in prev_by_cik.items():
        after = new_by_cik.get(cik)
        if not after:
            continue
        gone, came = before - after, after - before
        if len(gone) == 1 and len(came) == 1:
            new_t = next(iter(came))
            if new_t not in prev:
                renamed[new_t] = next(iter(gone))

    for t in sorted(universe):
        s = sec[t]
        if t in renamed:
            old_t = renamed[t]
            row = copy(old_t)
            row.update(ticker=t, renamed_to=None, pending={}, first_seen_at=_now_iso())
            old_row = copy(old_t)
            old_row.update(is_active=False, renamed_to=t, pending={})
            out[old_t] = old_row
            ev(old_t, old_row, "SYMBOL_CHANGE", old_t, t)
        elif t in prev:
            row = copy(t)
        else:
            row = {"ticker": t, "pending": {}, "indexes": [], "is_active": True,
                   "first_seen_at": _now_iso()}
            if not baseline:
                ev(t, {"cik": s.get("cik"), "name": s.get("name")}, "LISTED", None,
                   s.get("exchange"))
        was_known = t in prev or t in renamed

        row["cik"] = str(s["cik"]) if s.get("cik") is not None else row.get("cik")
        row["is_active"], row["delisted_at"], row["renamed_to"] = True, None, None
        clear(row, "missing")

        # Name
        new_name = (s.get("name") or "").strip()
        if new_name:
            if not was_known or not row.get("name"):
                row["name"] = new_name
            elif norm_name(new_name) == norm_name(row["name"]):
                row["name"] = new_name
                clear(row, "name")
            elif confirm(row, "name", new_name, today):
                ev(t, row, "NAME_CHANGE", row["name"], new_name)
                row["name"] = new_name

        # Exchange (SEC)
        new_ex = (s.get("exchange") or "").strip()
        if new_ex:
            old_ex = row.get("exchange")
            if not was_known or not old_ex:
                row["exchange"] = new_ex
            elif old_ex.upper() == new_ex.upper():
                clear(row, "exchange")
            elif confirm(row, "exchange", new_ex, today):
                ev(t, row, "EXCHANGE_CHANGE", old_ex, new_ex)
                row["exchange"] = new_ex

        # Massive: listing venue, type, FIGI
        m = (mas or {}).get(t)
        if m:
            row["primary_exchange"] = m.get("primary_exchange") or row.get("primary_exchange")
            row["security_type"] = m.get("type") or row.get("security_type")
            row["composite_figi"] = m.get("composite_figi") or row.get("composite_figi")
            row["is_etf"] = (m.get("type") or "").upper() in ETF_TYPES

        # FMP: sector, industry, market cap
        f = (fmp or {}).get(t)
        if f:
            row["industry"] = f.get("industry") or row.get("industry")
            row["country"] = f.get("country") or row.get("country")
            cap = _num(f.get("market_cap"))
            if cap and cap > 0:
                row["market_cap"] = cap
                cur = row.get("mcap_class")
                new_cls = sticky_mcap_class(cap, cur)
                if not cur or not was_known:
                    row["mcap_class"] = new_cls
                elif new_cls == cur:
                    clear(row, "mcap_class")
                elif confirm(row, "mcap_class", new_cls, today):
                    ev(t, row, "MCAP_CLASS_CHANGE", cur, new_cls)
                    row["mcap_class"] = new_cls
            sector = (f.get("sector") or "").strip()
            if sector:
                cur = row.get("sector")
                if not cur or not was_known:
                    row["sector"] = sector
                elif cur == sector:
                    clear(row, "sector")
                elif confirm(row, "sector", sector, today):
                    ev(t, row, "SECTOR_CHANGE", cur, sector)
                    row["sector"] = sector

        # Index membership
        for code, members in idx.items():
            if members is None:
                continue
            inside, was = t in members, code in row["indexes"]
            if inside == was:
                continue
            if inside:
                row["indexes"].append(code)
            else:
                row["indexes"].remove(code)
            if prev_idx.get(code) and was_known:
                ev(t, row, "INDEX_ADDED" if inside else "INDEX_REMOVED", None, code)
        row["indexes"] = sorted(set(row["indexes"]))
        out[t] = row

    # Tickers that dropped out of SEC's list (and were not renamed).
    renamed_old = set(renamed.values())
    for t, r in prev.items():
        if t in universe or t in renamed_old or not r.get("is_active", True) or r.get("renamed_to"):
            continue
        row = copy(t)
        out[t] = row
        if mas is None or t in mas:
            continue              # cannot confirm, or still trading per Massive
        if confirm(row, "missing", "1", today):
            row.update(is_active=False, delisted_at=_now_iso(), indexes=[])
            ev(t, row, "DELISTED", row.get("exchange"), None)
    return out, events


# ── Alert text ───────────────────────────────────────────────────────────────
def build_alert(e, watch_ticker):
    """-> (filing_type, impact, headline, summary)"""
    kind, t, name = e["event_type"], e["ticker"], e.get("name") or e["ticker"]
    old, new = e.get("old_value"), e.get("new_value")
    if kind == "SYMBOL_CHANGE":
        return ("STOCK_SYMBOL_CHANGE", "HIGH", f"{name} now trades as {new}",
                f"{name} changed its ticker from {old} to {new} (same SEC registrant). "
                f"Your watchlist still lists {watch_ticker}; add {new} to keep receiving alerts.")
    if kind == "NAME_CHANGE":
        return ("STOCK_NAME_CHANGE", "MEDIUM", f"{t} renamed to {new}",
                f"{t} is now registered with the SEC as {new} (previously {old}).")
    if kind == "EXCHANGE_CHANGE":
        otc = (new or "").upper() == "OTC"
        return ("STOCK_EXCHANGE_CHANGE", "HIGH" if otc else "MEDIUM",
                f"{name} moved from {old} to {new}",
                f"{name} ({t}) now lists on {new}, previously {old}."
                + (" A move to OTC usually follows a delisting from a major exchange." if otc else ""))
    if kind == "DELISTED":
        return ("STOCK_DELISTED", "HIGH", f"{name} ({t}) no longer listed",
                f"{t} has dropped off the SEC's list of exchange-traded companies and Massive "
                f"no longer shows it as trading (last listed on {old or 'an exchange'}). "
                f"This usually means a delisting, an acquisition closing or a ticker retirement.")
    if kind == "MCAP_CLASS_CHANGE":
        up = MCAP_RANK.get(new, 9) < MCAP_RANK.get(old, 9)
        return ("STOCK_MCAP_CLASS_UP" if up else "STOCK_MCAP_CLASS_DOWN", "MEDIUM",
                f"{name} is now {MCAP_LABEL.get(new, new)}",
                f"{name} ({t}) moved from {MCAP_LABEL.get(old, old)} to {MCAP_LABEL.get(new, new)} "
                f"by market capitalisation ({_cap_text(e.get('market_cap'))}).")
    if kind == "SECTOR_CHANGE":
        return ("STOCK_SECTOR_CHANGE", "MEDIUM", f"{name} reclassified to {new}",
                f"{name} ({t}) is now classified in {new}, previously {old}.")
    if kind in ("INDEX_ADDED", "INDEX_REMOVED"):
        added = kind == "INDEX_ADDED"
        label = INDEX_LABEL.get(new, new)
        return ("STOCK_INDEX_ADDED" if added else "STOCK_INDEX_REMOVED",
                "HIGH" if new == "SP500" else "MEDIUM",
                f"{name} {'joins' if added else 'leaves'} the {label}",
                f"{name} ({t}) has been {'added to' if added else 'removed from'} the {label}. "
                + ("Index funds tracking it will buy the shares." if added
                   else "Index funds tracking it will sell the shares."))
    return None


def _cap_text(cap):
    cap = _num(cap)
    if not cap:
        return "market cap unavailable"
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if cap >= div:
            return f"${cap / div:,.1f}{unit}"
    return f"${cap:,.0f}"


def watch_form(t, watched):
    """The ticker as it appears on the watchlist (BRK-B vs BRK.B), or None."""
    for cand in (t, t.replace("-", "."), t.replace("-", "/")):
        if cand in watched:
            return cand
    return None


def alert_rows(events, rows, watched, today, sent):
    out = []
    for e in events:
        if e["event_type"] not in ALERTABLE:
            continue
        wt = watch_form(e["ticker"], watched)
        if not wt:
            continue
        e = dict(e, market_cap=(rows.get(e["ticker"]) or {}).get("market_cap"))
        built = build_alert(e, wt)
        if not built:
            continue
        ftype, impact, headline, summary = built
        key = f"{SOURCE}:{e['ticker']}:{e['event_type']}:{e.get('new_value') or ''}"
        if key in sent:
            continue
        sent.add(key)
        extra = {"dedup_key": key, "headline": headline, "company_name": e.get("name") or wt,
                 "event_type": e["event_type"], "old_value": e.get("old_value"),
                 "new_value": e.get("new_value"), "cik": e.get("cik"), "detected_on": today,
                 "filed_at": _now_iso()}
        link = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={e['cik']}"
                if e.get("cik") else None)
        # Every row carries the same keys: PostgREST rejects a bulk insert
        # whose objects differ (PGRST102).
        out.append({"ticker": wt, "summary": summary, "impact": impact, "source": SOURCE,
                    "filing_type": ftype, "delivered": False, "filing_url": link,
                    "extra": tag_extra(extra, SOURCE, ftype)})
    return out


# ── Fetchers ─────────────────────────────────────────────────────────────────
def fetch_sec():
    try:
        r = requests.get(SEC_URL, headers={"User-Agent": SEC_UA}, timeout=60)
        r.raise_for_status()
        data = r.json()
        fields = data.get("fields") or []
        i_cik, i_name, i_t, i_ex = (fields.index(k) for k in ("cik", "name", "ticker", "exchange"))
        out = {}
        for rec in data.get("data") or []:
            t = norm_ticker(rec[i_t])
            if t and t not in out:
                out[t] = {"cik": str(rec[i_cik]), "name": rec[i_name], "exchange": rec[i_ex]}
    except Exception as e:
        log_poller_error(POLLER, "fetch_sec", e)
        logger.error("[STOCK] SEC ticker list unavailable: %s", e)
        return None
    if len(out) < MIN_SEC_ROWS:
        log_poller_error(POLLER, "fetch_sec", f"only {len(out)} rows")
        return None
    return out


def fetch_massive():
    try:
        rows = massive_client.get_reference_tickers("stocks", active=True)
    except Exception as e:
        log_poller_error(POLLER, "fetch_massive", e)
        rows = None
    if not rows:
        return None
    return {norm_ticker(r.get("ticker")): {"primary_exchange": r.get("primary_exchange"),
                                           "type": r.get("type"),
                                           "composite_figi": r.get("composite_figi")}
            for r in rows if r.get("ticker")}


def fetch_fmp():
    out = {}
    for ex in ("NYSE", "NASDAQ", "AMEX"):
        try:
            rows = fmp_client.screener({"exchange": ex, "isEtf": "false", "isFund": "false",
                                        "isActivelyTrading": "true", "limit": 10000})
        except Exception as e:
            log_poller_error(POLLER, "fetch_fmp", e, {"exchange": ex})
            return None
        for r in rows or []:
            t = norm_ticker(r.get("symbol"))
            if t:
                out[t] = {"sector": r.get("sector"), "industry": r.get("industry"),
                          "market_cap": r.get("marketCap"), "country": r.get("country")}
        time.sleep(FMP_PAUSE)
    if len(out) < MIN_SCREENER_ROWS:
        log_poller_error(POLLER, "fetch_fmp", f"only {len(out)} rows")
        return None
    return out


def fetch_indexes():
    out = {}
    for code, path in INDEXES.items():
        try:
            rows = fmp_client._get(path)
        except Exception as e:
            log_poller_error(POLLER, "fetch_index", e, {"index": code})
            rows = None
        out[code] = ({norm_ticker(r.get("symbol")) for r in rows if r.get("symbol")}
                     if isinstance(rows, list) and rows else None)
        time.sleep(FMP_PAUSE)
    return out


# ── Persistence ──────────────────────────────────────────────────────────────
def _paged(query_fn, size=1000):
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


def load_prev():
    rows = _paged(lambda: sb().table("stock_master").select("*").order("ticker"))
    out = {}
    for r in rows:
        p = r.get("pending")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        r["pending"] = p or {}
        r["indexes"] = list(r.get("indexes") or [])
        out[r["ticker"]] = r
    return out


def load_prev_idx():
    rows = _paged(lambda: sb().table("stock_index_members").select("index_code, ticker").order("ticker"))
    out = {}
    for r in rows:
        out.setdefault(r["index_code"], set()).add(r["ticker"])
    return out


def load_sent():
    since = datetime.fromtimestamp(time.time() - DEDUP_DAYS * 86400, timezone.utc).isoformat()
    try:
        rows = _paged(lambda: sb().table("alerts").select("id, extra")
                      .eq("source", SOURCE).gte("created_at", since).order("id"))
    except Exception as e:
        log_poller_error(POLLER, "load_sent", e)
        return None
    return {(r.get("extra") or {}).get("dedup_key") for r in rows} - {None}


_COLS = ("ticker", "cik", "name", "exchange", "primary_exchange", "security_type", "is_etf",
         "sector", "industry", "country", "market_cap", "mcap_class", "indexes", "isin", "cusip",
         "composite_figi", "ipo_date", "is_active", "delisted_at", "renamed_to", "pending",
         "profile_at", "first_seen_at")


def save_rows(rows):
    now = _now_iso()
    payload = [dict({k: r.get(k) for k in _COLS}, updated_at=now) for r in rows.values()]
    for p in payload:
        p["pending"] = p.get("pending") or {}
        p["indexes"] = p.get("indexes") or []
        p["is_etf"] = bool(p.get("is_etf"))
        p["is_active"] = p.get("is_active") is not False
    for i in range(0, len(payload), 500):
        sb().table("stock_master").upsert(payload[i:i + 500], on_conflict="ticker").execute()


def save_events(events, today):
    rows = [{"ticker": e["ticker"], "cik": e.get("cik"), "event_type": e["event_type"],
             "old_value": e.get("old_value"), "new_value": e.get("new_value") or "",
             "detected_on": today} for e in events]
    for i in range(0, len(rows), 500):
        sb().table("stock_master_events").upsert(
            rows[i:i + 500], on_conflict="ticker,event_type,new_value,detected_on",
            ignore_duplicates=True).execute()


def save_index_members(idx):
    for code, members in idx.items():
        if members is None:
            continue
        old = {r["ticker"] for r in sb().table("stock_index_members").select("ticker")
               .eq("index_code", code).execute().data or []}
        add = [{"index_code": code, "ticker": t, "updated_at": _now_iso()} for t in sorted(members - old)]
        for i in range(0, len(add), 500):
            sb().table("stock_index_members").upsert(add[i:i + 500], on_conflict="index_code,ticker").execute()
        gone = sorted(old - members)
        for i in range(0, len(gone), 50):
            sb().table("stock_index_members").delete().eq("index_code", code).in_("ticker", gone[i:i + 50]).execute()


def write_alerts(rows):
    ok = True
    for i in range(0, len(rows), 50):
        try:
            sb().table("alerts").insert(rows[i:i + 50]).execute()
        except Exception as e:
            ok = False
            log_poller_error(POLLER, "write_alerts", e)
    return ok


def enrich_profiles(rows, watched, today):
    """ISIN, CUSIP, IPO date from FMP profile -- a few hundred a day, watched first."""
    def due(r):
        if not r.get("is_active", True) or r.get("is_etf"):
            return False
        at = str(r.get("profile_at") or "")[:10]
        if not at:
            return True
        try:
            age = (datetime.fromisoformat(today) - datetime.fromisoformat(at)).days
        except ValueError:
            return True
        return age >= PROFILE_REFRESH_DAYS
    cands = [t for t, r in rows.items() if due(r)]
    cands.sort(key=lambda t: (watch_form(t, watched) is None, rows[t].get("isin") is not None, t))
    done = 0
    for t in cands[:PROFILE_BATCH]:
        try:
            p = fmp_client.get_profile(t)
        except Exception as e:
            log_poller_error(POLLER, "profile", e, {"ticker": t})
            break                                    # likely rate-limited: stop for today
        r = rows[t]
        if p:
            r["isin"] = p.get("isin") or r.get("isin")
            r["cusip"] = p.get("cusip") or r.get("cusip")
            ipo = str(p.get("ipoDate") or "")[:10]
            r["ipo_date"] = ipo if re.match(r"\d{4}-\d{2}-\d{2}$", ipo) else r.get("ipo_date")
            r["sector"] = r.get("sector") or p.get("sector")
            r["industry"] = r.get("industry") or p.get("industry")
        r["profile_at"] = _now_iso()
        done += 1
        time.sleep(FMP_PAUSE)
    return done


# ── Entry point ──────────────────────────────────────────────────────────────
def run_stock_master(dry=False):
    """One daily pass. Logs and swallows every failure."""
    try:
        today = _today_et()
        sec = fetch_sec()
        if sec is None:
            logger.error("[STOCK] SEC list unavailable -- skipping today's run")
            return
        mas, fmp, idx = fetch_massive(), fetch_fmp(), fetch_indexes()
        prev, prev_idx = load_prev(), load_prev_idx()
        rows, events = diff_master(prev, sec, mas, fmp, idx, prev_idx, today)
        watched = {t.upper() for t in get_watched_tickers(force=True)}

        if dry:
            for e in events:
                print(e)
            print(f"{len(rows)} rows, {len(events)} events, baseline={not prev}")
            return

        if not prev:
            # First run: store the baseline before anything else, so a slow
            # profile pass cannot leave the table empty.
            save_rows(rows)

        alerts_ok = True
        if _env_bool("STOCK_ALERTS_ENABLED", False) and events:
            sent = load_sent()
            if sent is None:
                alerts_ok = False
                logger.error("[STOCK] alert ledger unreadable -- no alerts this run")
            else:
                new_alerts = alert_rows(events, rows, watched, today, sent)
                alerts_ok = write_alerts(new_alerts) if new_alerts else True
                logger.info("[STOCK] %d alerts written", len(new_alerts))
        if not alerts_ok:
            # Keep yesterday's master so the same changes are re-detected and
            # re-confirmed; dedup keys stop a double send.
            log_poller_error(POLLER, "run", "alerts failed; master not advanced")
            return

        save_events([e for e in events], today)
        enriched = enrich_profiles(rows, watched, today)
        save_rows(rows)
        save_index_members(idx)
        logger.info("[STOCK] master: %d rows, %d events, %d profiles, sources sec=%d massive=%s fmp=%s",
                    len(rows), len(events), enriched, len(sec),
                    "ok" if mas is not None else "DOWN", "ok" if fmp is not None else "DOWN")
    except Exception as e:
        log_poller_error(POLLER, "run_stock_master", e)
        logger.exception("[STOCK] run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_stock_master(dry=len(sys.argv) > 1 and sys.argv[1] == "dry")
