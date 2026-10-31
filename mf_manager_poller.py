"""
mf_manager_poller.py
GQ FinXray US — Feature 19: MUTUAL FUND manager changes (SEC EDGAR only).

Who runs the fund is the single most-watched fact about an actively managed
mutual fund: a manager leaving is why holders re-examine a position. US mutual
funds disclose it in two places, and this poller reads both.

  1. SUPPLEMENTS (Form 497 / 497K "stickers"). The fund must file one when a
     portfolio manager joins or leaves, usually within days. Found by EDGAR
     full-text search on the phrases those supplements actually use. Fastest
     signal there is — ahead of every data vendor.

  2. PROSPECTUS ROSTERS (Form 485BPOS / 497K). The annual prospectus lists the
     current management team. Parsed and diffed against the roster on file, so
     a change that never got its own sticker is still caught, a quarter late.

This is the mutual-fund poller. ETFs have their own (fund_manager_poller.py)
and the two never cross: this one reads mf_master and ignores any series that
is not an ordinary open-end mutual fund, and fund_manager_poller skips mutual
funds. Separate sources, separate feature ids, separate schedules — an ETF
holder and a fund holder get wording written for the product they own.

Jobs (scheduled separately in main.py)
--------------------------------------
  run_mf_manager_events()     hourly   497 / 497K supplements, 2-day lookback
  run_mf_manager_rosters()    daily    new 485BPOS / 497K prospectuses -> roster
  run_mf_manager_backfill()   weekly   every fund registrant, so each series has
                                       a current roster to diff the first change against

ALERTS ARE WATCHLIST-GATED. Rosters are collected for every ordinary mutual
fund (so a fund added to a watchlist tomorrow already has a baseline), but an
alert row is written only for a ticker someone watches. Rule-based throughout:
no LLM, zero tokens.

Tables: mf_managers (current roster), mf_manager_events (every change),
mf_filings_processed (one document, once).
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import fund_manager_extractor as fx          # pure SEC-document parser
import mf_common as mc

POLLER = "mf_manager_poller"
TAG = "MF_MGR"
SOURCE = "MF_MANAGER"

# Phrases that appear in a manager-change supplement. Each is one EFTS query
# (full-text search matches exact phrases); the results are unioned.
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

DAYS_BACK = int(os.getenv("MF_MGR_DAYS_BACK", "2"))
MAX_PAGES = int(os.getenv("MF_MGR_MAX_PAGES", "10"))
ROSTER_DOC_BUDGET = int(os.getenv("MF_MGR_ROSTER_DOCS", "400"))
BACKFILL_CIK_BUDGET = int(os.getenv("MF_MGR_BACKFILL_CIKS", "150"))
BACKFILL_LOOKBACK_DAYS = int(os.getenv("MF_MGR_BACKFILL_DAYS", "460"))
EVENT_MEMORY_DAYS = int(os.getenv("MF_MGR_EVENT_MEMORY_DAYS", "180"))

# How often a long run says where it is. A backfill reads hundreds of large
# prospectuses at 3 SEC requests a second; without this the only output is the
# summary line at the very end and the run is indistinguishable from hung.
PROGRESS_EVERY = int(os.getenv("MF_PROGRESS_EVERY", "10"))


def log(msg):
    mc.log(TAG, msg)


def _err(job, e, ctx=None):
    mc.log_poller_error(POLLER, job, e, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Roster storage
# ─────────────────────────────────────────────────────────────────────────────

def current_roster(series_id) -> tuple[list[dict], str | None]:
    try:
        r = (mc.sb().table("mf_managers").select("*")
             .eq("series_id", series_id).eq("is_current", True).execute())
        rows = r.data or []
        asof = max((x.get("filing_date") or "" for x in rows), default=None) or None
        return [{"name": x["manager_name"], **x} for x in rows], asof
    except Exception as e:
        _err("current_roster", e, {"series_id": series_id})
        return [], None


def save_roster(series_id, managers, meta) -> None:
    """Upsert the roster this prospectus lists; anyone missing is marked gone."""
    keep, rows = set(), []
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
            "cik": meta["cik"], "last_seen_at": mc.now_iso(),
        })
    try:
        if rows:
            mc.sb().table("mf_managers").upsert(rows, on_conflict="series_id,manager_key").execute()
        cur, _ = current_roster(series_id)
        gone = [c["manager_key"] for c in cur if c["manager_key"] not in keep]
        if gone:
            (mc.sb().table("mf_managers").update({"is_current": False, "removed_at": mc.now_iso()})
             .eq("series_id", series_id).in_("manager_key", gone).execute())
    except Exception as e:
        _err("save_roster", e, {"series_id": series_id})


def apply_event(series_id, ev, meta):
    try:
        if ev["event_type"] == "REMOVED":
            (mc.sb().table("mf_managers").update({"is_current": False, "removed_at": mc.now_iso()})
             .eq("series_id", series_id).eq("manager_key", ev["manager_key"]).execute())
        else:
            mc.sb().table("mf_managers").upsert({
                "series_id": series_id, "manager_key": ev["manager_key"],
                "manager_name": ev["manager"], "is_current": True, "removed_at": None,
                "source_form": meta["form"], "source_accession": meta["accession"],
                "source_url": meta["url"], "filing_date": meta["filing_date"],
                "cik": meta["cik"], "last_seen_at": mc.now_iso(),
            }, on_conflict="series_id,manager_key").execute()
    except Exception as e:
        _err("apply_event", e, {"series_id": series_id})


def recent_event_exists(series_id, manager_key, event_type, days=EVENT_MEMORY_DAYS) -> bool:
    """A trust files the same sticker under several accessions, one per
    prospectus. Without this the second copy reads as a new change."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        r = (mc.sb().table("mf_manager_events").select("id")
             .eq("series_id", series_id).eq("manager_key", manager_key)
             .eq("event_type", event_type).gte("created_at", since).limit(1).execute())
        return bool(r.data)
    except Exception:
        return False


def store_event(series_id, ev, meta, detection) -> bool:
    """Insert one change. False = the unique constraint already had it."""
    u = mc.load_mf_universe()
    s = u["by_series"].get(series_id) or {}
    try:
        mc.sb().table("mf_manager_events").insert({
            "series_id": series_id, "cik": meta["cik"], "manager_key": ev["manager_key"],
            "manager_name": ev["manager"], "event_type": ev["event_type"],
            "effective_date": ev.get("effective_date"),
            "effective_date_is_filing_date": ev.get("effective_date_is_filing_date", True),
            "detection_source": detection, "accession": meta["accession"],
            "form": meta["form"], "filing_date": meta["filing_date"],
            "filing_url": meta["url"], "evidence": ev.get("evidence"),
            "tickers": s.get("tickers") or [],
            "series_name": meta.get("series_names", {}).get(series_id) or s.get("series_name"),
            "fund_family": s.get("entity_name"),
        }).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        _err("store_event", e, {"series_id": series_id, "accession": meta["accession"]})
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────────────

def build_alert(fund_name, family, added, removed, remaining, eff, meta):
    parts = []
    if removed:
        parts.append(f"{', '.join(e['manager'] for e in removed)} "
                     f"{'is' if len(removed) == 1 else 'are'} leaving as portfolio manager"
                     f"{'' if len(removed) == 1 else 's'}")
    if added:
        parts.append(f"{', '.join(e['manager'] for e in added)} "
                     f"{'joins' if len(added) == 1 else 'join'} the portfolio management team")
    who = f" at {family}" if family and not mc.same_name(family, fund_name) else ""
    summary = f"Portfolio manager change at {fund_name}, a mutual fund{who}: " + "; ".join(parts) + "."
    if eff:
        summary += f" Effective {mc.fmt_date(eff)}."
    if remaining:
        summary += f" Current team: {', '.join(remaining)}."
    elif removed and not added:
        summary += " No remaining named manager is disclosed in this filing — watch for a replacement."
    summary += (" A mutual fund's manager change takes effect on the date above; the fund keeps"
                " trading at its daily NAV throughout.")
    summary += f" Source: SEC Form {meta['form']} filed {mc.fmt_date(meta['filing_date'])}."

    lead_left = any((e.get("role") or "").startswith("lead") for e in removed)
    # Additions are MEDIUM, not LOW: delivery defaults every user to
    # min_impact=MEDIUM, so a LOW alert would be detected, stored, then dropped.
    impact = "HIGH" if removed and (not remaining or lead_left) else "MEDIUM"
    ftype = "MF_PM_CHANGE" if (added and removed) else ("MF_PM_REMOVED" if removed else "MF_PM_ADDED")
    return summary, impact, ftype


def emit(series_id, new_events, meta, watched) -> int:
    if not new_events:
        return 0
    u = mc.load_mf_universe()
    s = u["by_series"].get(series_id) or {}
    hits = [t for t in s.get("tickers", []) if t in watched]
    if not hits:
        return 0                                   # collected, not alerted
    added = [e for e in new_events if e["event_type"] == "ADDED"]
    removed = [e for e in new_events if e["event_type"] == "REMOVED"]
    roster, _ = current_roster(series_id)
    remaining = [r["manager_name"] for r in roster]
    # The first event carrying a REAL effective date the filing stated. Testing
    # the flag alone picked the first event whose flag was merely absent, whose
    # effective_date is then None -- so a supplement saying "effective November
    # 1" was announced to holders as effective on its own filing date, because
    # an unrelated addition happened to sort first in the list.
    eff = next((e["effective_date"] for e in new_events
                if e.get("effective_date")
                and not e.get("effective_date_is_filing_date")),
               None) or meta["filing_date"]
    name = meta.get("series_names", {}).get(series_id) or s.get("series_name") or series_id
    summary, impact, ftype = build_alert(name, s.get("entity_name"), added, removed,
                                         remaining, eff, meta)
    # The event, not the ticker: the same change filed again under another
    # accession, or seen by the roster job after the sticker job, is one alert.
    key = f"{SOURCE}:{series_id}:{ftype}:" + ",".join(sorted(e["manager_key"] for e in new_events))
    return mc.emit_alerts(
        SOURCE, hits, summary, impact, ftype,
        {"series_id": series_id, "series_name": name, "company_name": name,
         "fund_family": s.get("entity_name"), "fund_type": "MUTUAL_FUND",
         "accession": meta["accession"], "form": meta["form"],
         "filing_date": meta["filing_date"], "effective_date": eff,
         "added": [e["manager"] for e in added], "removed": [e["manager"] for e in removed],
         "current_team": remaining, "detection": meta.get("detection"),
         "headline": f"{name}: portfolio manager change"},
        url=meta["url"], dedup_key=key, poller=POLLER)


# ─────────────────────────────────────────────────────────────────────────────
# Job 1 — supplements (EDGAR full-text search)
# ─────────────────────────────────────────────────────────────────────────────

def _mf_ciks():
    return set(mc.load_mf_universe()["by_cik"])


def process_event_filing(meta, watched) -> int:
    series = mc.fetch_series_header(meta["cik"], meta["accession"])
    if not series:
        # Header unreadable (usually a 429). Fall back to the registry ONLY for
        # a single-series trust, where there is nothing to attribute. In a
        # multi-fund trust the fallback has no names, so attribution would
        # degrade to "every fund" and one manager leaving one fund would alert
        # holders of all of them. Retry instead.
        own = mc.load_mf_universe()["by_cik"].get(mc.cik_plain(meta["cik"])) or []
        if len(own) != 1:
            mc.mark_processed(meta["key"], "MGR_EVENT", meta, "NO_SERIES")
            return 0
        series = [{"series_id": own[0], "series_name": "", "classes": []}]

    mf_series = set(mc.load_mf_universe()["by_series"])
    series = [s for s in series if s["series_id"] in mf_series]
    if not series:
        mc.mark_processed(meta["key"], "MGR_EVENT", meta, "NOT_MUTUAL_FUND")
        return 0

    meta["series_names"] = {s["series_id"]: s.get("series_name") for s in series}
    html = mc.sec.text(meta["url"])
    if not html:
        mc.mark_processed(meta["key"], "MGR_EVENT", meta, "NO_DOC")
        return 0
    # Full and summary prospectuses are also filed as 497/497K and carry manager
    # bios ("joined the Fund as co-portfolio manager in 2021") that parse as
    # additions. Only a supplement announces a change.
    if not fx.is_supplement(fx.lines_to_text(fx.html_to_lines(html)), meta.get("form")):
        mc.mark_processed(meta["key"], "MGR_EVENT", meta, "NOT_SUPPLEMENT")
        return 0

    out = fx.extract_change_events(html, series, meta["filing_date"])
    meta["detection"] = "STICKER_497"
    per_series = defaultdict(list)
    for ev in out["events"]:
        for sid in ev["series_ids"]:
            if sid not in mf_series or recent_event_exists(sid, ev["manager_key"], ev["event_type"]):
                continue
            if store_event(sid, ev, meta, "STICKER_497"):
                apply_event(sid, ev, meta)
                per_series[sid].append(ev)
    # "the table is replaced with the following" -> diff against stored roster
    for sid, new_roster in out["replacement_rosters"].items():
        if sid not in mf_series:
            continue
        old, _ = current_roster(sid)
        if old:
            d = fx.diff_rosters(old, new_roster)
            for kind, lst in (("ADDED", d["added"]), ("REMOVED", d["removed"])):
                for m in lst:
                    nm = m.get("manager_name") or m["name"]
                    ev = {"event_type": kind, "manager": nm, "manager_key": fx.normalize_name(nm),
                          "effective_date": meta["filing_date"], "effective_date_is_filing_date": True,
                          "evidence": "Portfolio manager table replaced in supplement.",
                          "role": m.get("role")}
                    if recent_event_exists(sid, ev["manager_key"], kind):
                        continue
                    if store_event(sid, ev, meta, "REPLACEMENT_TABLE"):
                        per_series[sid].append(ev)
        save_roster(sid, new_roster, meta)

    n = sum(emit(sid, evs, meta, watched) for sid, evs in per_series.items())
    mc.mark_processed(meta["key"], "MGR_EVENT", meta, "OK",
                      n_series=len(series), n_events=sum(len(v) for v in per_series.values()))
    return n


def run_mf_manager_events(days_back: int = DAYS_BACK):
    """Hourly: manager-change supplements filed by mutual fund registrants."""
    try:
        watched = mc.watched_tickers()
        ciks = _mf_ciks()
        if not ciks:
            log("mf_master is empty — run mf_poller master first")
            return 0
        end = date.today()
        start = end - timedelta(days=days_back)
        cands, failed = {}, []
        for q in EVENT_QUERIES:
            for h in mc.efts_hits_safe(q, EVENT_FORMS, start.isoformat(), end.isoformat(),
                                       max_pages=MAX_PAGES, failed=failed):
                src = h.get("_source") or {}
                own = {mc.cik_plain(c) for c in (src.get("ciks") or [])} & ciks
                if not own:
                    continue                       # not a mutual fund registrant
                m = mc.hit_meta(h)
                if m:
                    m["cik"] = sorted(own)[0]
                    cands.setdefault(m["key"], m)
        done = mc.processed_keys("MGR_EVENT", cands)
        todo = [m for k, m in cands.items() if k not in done]
        alerts = 0
        for m in todo:
            try:
                alerts += process_event_filing(m, watched)
            except Exception as e:
                _err("process_event_filing", e, {"accession": m["accession"]})
                mc.mark_processed(m["key"], "MGR_EVENT", m, "ERROR")
        log(f"manager events: {len(cands)} hits, {len(todo)} new documents, {alerts} alerts"
            + (f", {len(failed)} phrase search(es) FAILED" if failed else ""))
        return alerts
    except Exception as e:
        _err("run_mf_manager_events", e)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Job 2 / 3 — prospectus rosters
# ─────────────────────────────────────────────────────────────────────────────

def process_roster_filing(meta, watched, emit_diffs=True) -> dict:
    """Parse one prospectus, store each fund's roster, alert on real diffs."""
    series = mc.fetch_series_header(meta["cik"], meta["accession"])
    mf_series = set(mc.load_mf_universe()["by_series"])
    if not series:
        own = mc.load_mf_universe()["by_cik"].get(mc.cik_plain(meta["cik"])) or []
        if len(own) != 1:
            mc.mark_processed(meta["key"], "MGR_ROSTER", meta, "NO_SERIES")
            return {"series": 0, "alerts": 0}
        series = [{"series_id": own[0], "series_name": "", "classes": []}]
    series = [s for s in series if s["series_id"] in mf_series]
    if not series:
        mc.mark_processed(meta["key"], "MGR_ROSTER", meta, "NOT_MUTUAL_FUND")
        return {"series": 0, "alerts": 0}

    html = mc.sec.text(meta["url"])
    if not html:
        mc.mark_processed(meta["key"], "MGR_ROSTER", meta, "NO_DOC")
        return {"series": 0, "alerts": 0}

    meta["series_names"] = {s["series_id"]: s.get("series_name") for s in series}
    meta["detection"] = "PROSPECTUS_ROSTER"
    rosters = fx.extract_rosters(html, series, meta.get("filing_date"))
    alerts = n = 0
    for sid, roster in (rosters or {}).items():
        if sid not in mf_series or not roster:
            continue
        n += 1
        old, _ = current_roster(sid)
        save_roster(sid, roster, meta)
        if not (old and emit_diffs):
            continue                               # first roster seen: baseline, silently
        d = fx.diff_rosters(old, roster)
        evs = []
        for kind, lst in (("ADDED", d["added"]), ("REMOVED", d["removed"])):
            for m in lst:
                nm = m.get("manager_name") or m["name"]
                ev = {"event_type": kind, "manager": nm, "manager_key": fx.normalize_name(nm),
                      "effective_date": meta["filing_date"], "effective_date_is_filing_date": True,
                      "evidence": "Management team in the new prospectus differs from the one on file.",
                      "role": m.get("role")}
                if recent_event_exists(sid, ev["manager_key"], kind):
                    continue
                if store_event(sid, ev, meta, "PROSPECTUS_ROSTER"):
                    evs.append(ev)
        alerts += emit(sid, evs, meta, watched)
    mc.mark_processed(meta["key"], "MGR_ROSTER", meta, "OK", n_series=n, n_alerts=alerts)
    return {"series": n, "alerts": alerts}


def _roster_filings_for(ciks, days_back, budget, priority=()):
    """
    Prospectuses from these registrants: a registrant in `priority` first, then
    everything else, newest first inside each group.

    `priority` exists because the date sort alone THREW AWAY the caller's
    ordering. A caller that carefully put the registrants behind watched funds
    at the front of `ciks` still had their documents re-sorted purely by filing
    date, so a watched fund's eleven-month-old prospectus lost its slot to 150
    newer ones from trusts nobody follows — and the funds that can actually
    alert were the last to get a roster.
    """
    priority = {mc.cik_plain(c) for c in priority}
    out = []
    for cik in ciks:
        if len(out) >= budget:
            break
        try:
            for f in mc.recent_filings(cik, ROSTER_FORMS, days_back):
                name = (f.get("primary_doc") or "").rsplit("/", 1)[-1]
                if not name:
                    continue
                out.append({"accession": f["accession"], "cik": mc.cik_plain(cik),
                            "form": f["form"], "filing_date": f["filing_date"],
                            "url": mc.archive(cik, f["accession"], name),
                            "key": f"{f['accession']}:{name}"})
        except Exception as e:
            _err("roster_filings", e, {"cik": cik})
    # Two stable passes: newest-first, then priority-first. Python's sort is
    # stable, so the date order survives inside each group.
    out.sort(key=lambda m: m.get("filing_date") or "", reverse=True)
    if priority:
        out.sort(key=lambda m: m["cik"] not in priority)
    return out[:budget]


def run_mf_manager_rosters(days_back: int = 2):
    """Daily: prospectuses filed in the last few days -> rosters + diffs."""
    try:
        watched = mc.watched_tickers()
        u = mc.load_mf_universe()
        # Registrants that run a watched fund first: their diffs are the ones
        # that can alert today, and the budget should never be spent before
        # reaching them.
        watched_ciks = {v["cik"] for t, v in u["by_ticker"].items() if t in watched}
        ciks = sorted(watched_ciks) + sorted(set(u["by_cik"]) - watched_ciks)
        metas = _roster_filings_for(ciks, days_back, ROSTER_DOC_BUDGET,
                                    priority=watched_ciks)
        done = mc.processed_keys("MGR_ROSTER", {m["key"] for m in metas})
        todo = [m for m in metas if m["key"] not in done]
        series = alerts = 0
        for i, m in enumerate(todo, 1):
            try:
                r = process_roster_filing(m, watched, emit_diffs=True)
                series += r["series"]
                alerts += r["alerts"]
            except Exception as e:
                _err("process_roster_filing", e, {"accession": m["accession"]})
                mc.mark_processed(m["key"], "MGR_ROSTER", m, "ERROR")
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                log(f"manager rosters: {i}/{len(todo)} documents read, "
                    f"{series} funds, {alerts} alerts")
        log(f"manager rosters: {len(todo)} documents, {series} funds, {alerts} alerts")
        return alerts
    except Exception as e:
        _err("run_mf_manager_rosters", e)
        return 0


def run_mf_manager_backfill(max_ciks: int | None = None,
                            lookback_days: int = BACKFILL_LOOKBACK_DAYS):
    """
    Weekly: give every mutual fund a roster on file, oldest coverage first.

    emit_diffs=False on purpose. The first roster for a fund is a baseline, not
    a change — alerting on it would tell every holder their managers "changed"
    the week this poller went live.
    """
    try:
        u = mc.load_mf_universe()
        have = set()
        try:
            have = {r["series_id"] for r in mc.paged(
                lambda: mc.sb().table("mf_managers").select("series_id")
                .eq("is_current", True).order("series_id"))}
        except Exception as e:
            _err("backfill_coverage", e)
        # WATCHED FUNDS FIRST. This walked the register in CIK order, so with
        # ~6,000 funds missing a roster and 150 registrants a week, a fund
        # somebody actually watches could wait months for the baseline that
        # Feature 19 needs before it can alert at all — while the budget went
        # on funds nobody follows. The watched registrants are a handful, so
        # putting them at the front costs the rest almost nothing.
        watched = mc.watched_tickers()
        watched_ciks = {v["cik"] for t, v in u["by_ticker"].items() if t in watched}
        missing_ciks = sorted({v["cik"] for sid, v in u["by_series"].items()
                               if sid not in have})
        missing_ciks.sort(key=lambda c: c not in watched_ciks)
        budget = max_ciks if max_ciks is not None else BACKFILL_CIK_BUDGET
        # Announced before the slow part: finding the prospectuses is one SEC
        # submissions call per registrant, so the run is busy for minutes
        # before it opens a document. With only an end-of-run log line it
        # looked hung.
        n_search = min(len(missing_ciks), budget * 3)
        log(f"manager backfill: {len(u['by_series']) - len(have)} funds without a roster; "
            f"searching {n_search} registrants for prospectuses "
            f"(one SEC call each, ~{n_search / 3:.0f}s)")
        metas = _roster_filings_for(missing_ciks[:budget * 3], lookback_days,
                                    budget * 4, priority=watched_ciks)
        done = mc.processed_keys("MGR_ROSTER", {m["key"] for m in metas})
        todo = [m for m in metas if m["key"] not in done][:budget * 2]
        log(f"manager backfill: {len(todo)} documents to read "
            f"({len(metas) - len(todo)} already done)")
        series = 0
        for i, m in enumerate(todo, 1):
            try:
                series += process_roster_filing(m, set(), emit_diffs=False)["series"]
            except Exception as e:
                _err("backfill_roster", e, {"accession": m["accession"]})
                mc.mark_processed(m["key"], "MGR_ROSTER", m, "ERROR")
            if i % PROGRESS_EVERY == 0 or i == len(todo):
                log(f"manager backfill: {i}/{len(todo)} documents read, "
                    f"{series} funds baselined")
        log(f"manager backfill: {len(todo)} documents, {series} funds baselined, "
            f"{len(u['by_series']) - len(have)} still without a roster")
        return series
    except Exception as e:
        _err("run_mf_manager_backfill", e)
        return 0


def coverage() -> dict:
    u = mc.load_mf_universe()
    try:
        have = {r["series_id"] for r in mc.paged(
            lambda: mc.sb().table("mf_managers").select("series_id")
            .eq("is_current", True).order("series_id"))}
    except Exception:
        have = set()
    return {"funds": len(u["by_series"]), "with_roster": len(have & set(u["by_series"])),
            "without_roster": len(set(u["by_series"]) - have)}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "events"
    if cmd == "events":
        run_mf_manager_events()
    elif cmd == "rosters":
        run_mf_manager_rosters()
    elif cmd == "backfill":
        run_mf_manager_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "coverage":
        print(coverage())
    else:
        print("usage: python mf_manager_poller.py events|rosters|backfill [n]|coverage")
