"""
etf_listing_poller.py
GQ FinXray US — ETF launches and closures (scheme master upkeep).

etf_master was loaded once from a TradingView CSV and only ever gets refreshed
for funds already in it, so new ETFs never entered the universe and closed ones
were never marked. This compares it daily with Massive's list of every actively
trading exchange-listed ticker:

  launch   an ETF-type ticker (ETF / ETN / ETV / ETS) Massive lists that is not
           in etf_master, seen on two separate days -> added to etf_master, so
           Features 14-17 baseline it on their next pass. Stored as a LAUNCHED
           event; no alert (nobody can have it on a watchlist yet).
  closure  an etf_master fund that was trading and is gone from Massive's list
           on two separate days -> etf_master.is_actively_trading = false, a
           CLOSED event, and an alert to anyone watching it
           (Feature 16, ETF_LISTING / ETF_DELISTED).

First run is a silent baseline: ETFs missing from etf_master are added without
events, and etf_master tickers Massive has never listed are parked as
NOT_LISTED so they can never read as closures.

Guards: the whole list must arrive (any failed page aborts the run), it must be
a plausible size, and more than ETF_LISTING_MAX_CLOSURES closures (or
ETF_LISTING_MAX_LAUNCHES launches) in one run is treated as a vendor glitch.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import massive_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
POLLER = "etf_listing_poller"
SOURCE = "ETF_LISTING"

ETF_TYPES = {"ETF", "ETN", "ETV", "ETS"}
CONFIRM_DAYS = int(os.getenv("ETF_LISTING_CONFIRM_DAYS", "2"))
MIN_ACTIVE = int(os.getenv("ETF_LISTING_MIN_ACTIVE", "8000"))
MAX_CLOSURES = int(os.getenv("ETF_LISTING_MAX_CLOSURES", "25"))
MAX_LAUNCHES = int(os.getenv("ETF_LISTING_MAX_LAUNCHES", "60"))
DEDUP_DAYS = 60
MIC_NAMES = {"XNAS": "NASDAQ", "XNYS": "NYSE", "ARCX": "NYSE ARCA", "BATS": "CBOE",
             "XASE": "NYSE AMERICAN", "XCBO": "CBOE", "EDGX": "CBOE"}

_sb = None


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def norm(t):
    return (t or "").strip().upper().replace(".", "-").replace("/", "-")


def _paged(query_fn, size=1000):
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size:
            return out
        page += 1


# ── Pure diff ────────────────────────────────────────────────────────────────
def _step(st, today):
    """Count one more distinct day of evidence. -> confirmed?"""
    if st.get("confirm_last") != today:
        st["confirm_count"] = int(st.get("confirm_count") or 0) + 1
        st["confirm_last"] = today
    return st["confirm_count"] >= CONFIRM_DAYS


def diff_listings(master, state, active, today):
    """
    master  {ticker: {"name", "is_actively_trading"}}   etf_master
    state   {ticker: etf_listing_state row}             empty on first run
    active  {canonical ticker: {"ticker","name","type","primary_exchange"}}
    -> (state_rows {ticker: row}, events [..], master_inserts [..],
        master_updates [{"ticker", "is_actively_trading"}], notes [..])
    """
    baseline = not state
    rows, events, inserts, updates, notes = {}, [], [], [], []
    master_norm = {norm(t): t for t in master}

    def st_for(t, default_status):
        r = dict(state.get(t) or {"ticker": t, "status": default_status, "confirm_count": 0,
                                  "confirm_last": None, "first_seen_at": _now_iso()})
        rows[t] = r
        return r

    def new_master_row(a):
        return {"ticker": a["ticker"], "name": (a.get("name") or a["ticker"])[:500],
                "exchange": MIC_NAMES.get(a.get("primary_exchange") or "", a.get("primary_exchange")),
                "is_actively_trading": True}

    # 1. Funds already in etf_master
    closing = []
    for t, m in master.items():
        listed = norm(t) in active
        r = st_for(t, "ACTIVE" if listed else "NOT_LISTED")
        if baseline or t not in state:
            r.update(status="ACTIVE" if listed else "NOT_LISTED", confirm_count=0, confirm_last=None)
            continue
        if listed:
            if r["status"] == "CLOSED":
                events.append({"ticker": t, "event_type": "RELISTED", "name": m.get("name")})
                updates.append({"ticker": t, "is_actively_trading": True})
            r.update(status="ACTIVE", confirm_count=0, confirm_last=None, closed_at=None)
        elif r["status"] in ("ACTIVE", "PENDING_CLOSE"):
            r["status"] = "PENDING_CLOSE"
            closing.append(t)

    # 2. ETF-type listings etf_master does not have
    launching = []
    for k, a in active.items():
        if k in master_norm or (a.get("type") or "").upper() not in ETF_TYPES:
            continue
        t = a["ticker"]
        r = st_for(t, "PENDING_LAUNCH")
        r["primary_exchange"], r["name"] = a.get("primary_exchange"), a.get("name")
        if baseline:
            r.update(status="ACTIVE", confirm_count=0, confirm_last=None)
            inserts.append(new_master_row(a))
        else:
            r["status"] = "PENDING_LAUNCH" if r.get("status") != "ACTIVE" else "ACTIVE"
            launching.append((t, a))

    # A launch candidate that vanished again starts over if it comes back.
    for t, r in state.items():
        if r.get("status") == "PENDING_LAUNCH" and t not in rows and norm(t) not in active:
            rows[t] = dict(r, confirm_count=0, confirm_last=None)

    # 3. Confirm, behind the glitch guards
    would_close = [t for t in closing if _step(dict(rows[t]), today)]
    if len(would_close) > MAX_CLOSURES:
        notes.append(f"{len(would_close)} closures in one run -- held as a vendor glitch")
    else:
        for t in closing:
            r = rows[t]
            if _step(r, today):
                r.update(status="CLOSED", closed_at=_now_iso(), confirm_count=0, confirm_last=None)
                events.append({"ticker": t, "event_type": "CLOSED", "name": master[t].get("name")})
                updates.append({"ticker": t, "is_actively_trading": False})
    if len(launching) > MAX_LAUNCHES and not baseline:
        notes.append(f"{len(launching)} launches in one run -- held as a vendor glitch")
    else:
        for t, a in launching:
            r = rows[t]
            if _step(r, today):
                r.update(status="ACTIVE", confirm_count=0, confirm_last=None)
                events.append({"ticker": t, "event_type": "LAUNCHED", "name": a.get("name"),
                               "exchange": MIC_NAMES.get(a.get("primary_exchange") or "",
                                                         a.get("primary_exchange"))})
                inserts.append(new_master_row(a))
    return rows, events, inserts, updates, notes


def closure_alert(t, name, watch_t):
    fund = name or t
    summary = (f"{fund} ({t}) is no longer trading. It has dropped off the list of actively "
               f"traded exchange-listed securities on {CONFIRM_DAYS} separate days. This usually "
               f"means the fund has liquidated, merged into another fund or changed its ticker. "
               f"A liquidation or merger filing, when one exists, is reported separately.")
    key = f"{SOURCE}:{t}:CLOSED"
    extra = {"dedup_key": key, "headline": f"{fund} ({t}) has stopped trading",
             "company_name": fund, "etf_ticker": t, "filed_at": _now_iso()}
    return {"ticker": watch_t, "summary": summary, "impact": "HIGH", "source": SOURCE,
            "filing_type": "ETF_DELISTED", "delivered": False,
            "filing_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={t}&type=497",
            "extra": tag_extra(extra, SOURCE, "ETF_DELISTED")}


# ── I/O ──────────────────────────────────────────────────────────────────────
def fetch_active():
    try:
        rows = massive_client.get_reference_tickers("stocks", active=True)
    except Exception as e:
        log_poller_error(POLLER, "fetch_active", e)
        return None
    if not rows:
        return None
    return {norm(r["ticker"]): {"ticker": r["ticker"].upper(), "name": r.get("name"),
                                "type": r.get("type"), "primary_exchange": r.get("primary_exchange")}
            for r in rows if r.get("ticker")}


def load_sent():
    since = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - DEDUP_DAYS * 86400,
                                   timezone.utc).isoformat()
    try:
        rows = _paged(lambda: sb().table("alerts").select("id, extra")
                      .eq("source", SOURCE).gte("created_at", since).order("id"))
    except Exception as e:
        log_poller_error(POLLER, "load_sent", e)
        return None
    return {(r.get("extra") or {}).get("dedup_key") for r in rows} - {None}


_STATE_COLS = ("ticker", "name", "status", "confirm_count", "confirm_last", "primary_exchange",
               "first_seen_at", "closed_at")


def run_etf_listing_poller(today=None):
    """Daily. Logs and swallows every failure."""
    try:
        today = today or datetime.now(ET).date().isoformat()
        active = fetch_active()
        if active is None or len(active) < MIN_ACTIVE:
            logger.error("[ETF-LIST] Massive list unavailable or short (%s) -- skipped",
                         None if active is None else len(active))
            log_poller_error(POLLER, "run", f"active list {None if active is None else len(active)}")
            return
        master = {r["ticker"].upper(): r for r in
                  _paged(lambda: sb().table("etf_master").select("ticker, name, is_actively_trading")
                         .order("ticker")) if r.get("ticker")}
        state = {r["ticker"]: r for r in _paged(lambda: sb().table("etf_listing_state").select("*")
                                                .order("ticker"))}
        rows, events, inserts, updates, notes = diff_listings(master, state, active, today)
        for n in notes:
            logger.error("[ETF-LIST] %s", n)
            log_poller_error(POLLER, "guard", n)

        closed = [e for e in events if e["event_type"] == "CLOSED"]
        if closed:
            watched = {t.upper() for t in get_watched_tickers(force=True)}
            alerts = [closure_alert(e["ticker"], e.get("name"), e["ticker"]) for e in closed
                      if e["ticker"] in watched]
            if alerts:
                sent = load_sent()
                if sent is None:
                    return                      # fail closed: state not advanced, retried tomorrow
                alerts = [a for a in alerts if a["extra"]["dedup_key"] not in sent]
                if alerts:
                    sb().table("alerts").insert(alerts).execute()

        if events:
            sb().table("etf_listing_events").upsert(
                [{"ticker": e["ticker"], "event_type": e["event_type"], "name": e.get("name"),
                  "exchange": e.get("exchange"), "detected_on": today} for e in events],
                on_conflict="ticker,event_type,detected_on", ignore_duplicates=True).execute()
        for i in range(0, len(inserts), 500):
            sb().table("etf_master").upsert(inserts[i:i + 500], on_conflict="ticker",
                                            ignore_duplicates=True).execute()
        for u in updates:
            sb().table("etf_master").update({"is_actively_trading": u["is_actively_trading"]}) \
                .eq("ticker", u["ticker"]).execute()
        payload = [dict({k: r.get(k) for k in _STATE_COLS}, updated_at=_now_iso()) for r in rows.values()]
        for i in range(0, len(payload), 500):
            sb().table("etf_listing_state").upsert(payload[i:i + 500], on_conflict="ticker").execute()
        logger.info("[ETF-LIST] %d listed, %d in etf_master, %d added, %d events (%s)",
                    len(active), len(master), len(inserts), len(events),
                    ", ".join(sorted({e["event_type"] for e in events})) or "none")
    except Exception as e:
        log_poller_error(POLLER, "run_etf_listing_poller", e)
        logger.exception("[ETF-LIST] run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etf_listing_poller(sys.argv[1] if len(sys.argv) > 1 else None)
