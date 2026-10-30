"""
mf_scheme_poller.py
GQ FinXray US — Feature 22: MUTUAL FUND scheme & AMC register changes.

The India platform has an AMC master and a scheme master that users are alerted
off. This is the US equivalent, built on SEC's own registers rather than a
vendor list:

  company_tickers_mf.json                    every fund share class with a ticker
  investment-company-series-class-<year>.csv fund, class and registrant names

mf_poller.refresh_mf_master() keeps mf_master in step with those files daily.
This poller watches what CHANGED between yesterday's register and today's, and
turns the changes a holder would care about into alerts:

  MF_NEW_SHARE_CLASS   a NEW ticker appeared for a fund someone already holds.
                       This is the one that matters most in practice: a fund
                       launching an institutional or a no-load class means the
                       same portfolio is now available at a different fee, and
                       a holder in an A class can often convert into it.
  MF_TICKER_RETIRED    a watched ticker left SEC's live register — the share
                       class was liquidated, merged or converted away. The
                       holder's own symbol is going to stop quoting.
  MF_FUND_RENAMED      the fund or its share class was renamed in the register.
  MF_FAMILY_CHANGE     the registrant's name changed — an AMC was bought,
                       merged or rebranded, and the funds now sit with a
                       different manager.

ALERTS ARE WATCHLIST-GATED and fund-scoped: a new share class alerts the people
who hold ANOTHER class of the SAME fund, because that is who can act on it.

Why this is a mutual-fund poller and not an ETF one: share classes are a
mutual-fund construct. An ETF has exactly one ticker and no classes, so none of
these four events can happen to it, and the ETF side has its own listing poller
for the one thing that can (a fund closing).

NEVER ALERTS ON THE FIRST RUN. The first pass stores today's register as the
baseline and sends nothing — otherwise every fund in the book would read as
"new" the day this poller went live.

Rule-based: no LLM, zero tokens.

Job (scheduled separately in main.py)
-------------------------------------
  run_mf_scheme_poll()   daily, after refresh_mf_master

Tables: mf_scheme_snapshot (the register as last seen), mf_scheme_events.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import mf_common as mc

POLLER = "mf_scheme_poller"
TAG = "MF_SCHEME"
SOURCE = "MF_SCHEME"

# A register download that loses a big share of the book is a bad file, not a
# wave of closures. Above this fraction the run reports and changes nothing.
MAX_RETIRE_FRACTION = float(os.getenv("MF_SCHEME_MAX_RETIRE", "0.10"))
# A new fund family files its whole line-up at once; one alert per fund is
# right, but a registrant that adds more classes than this in a day is a data
# event (a bulk re-registration), not news.
MAX_NEW_PER_SERIES = int(os.getenv("MF_SCHEME_MAX_NEW_PER_SERIES", "6"))


def log(msg):
    mc.log(TAG, msg)


def _err(job, e, ctx=None):
    mc.log_poller_error(POLLER, job, e, ctx)


def _norm_name(s):
    """
    Compare names without being fooled by punctuation, case or spacing.

    The runs of whitespace MUST be collapsed. Replacing punctuation with a
    space and stopping there left "A.B.C. Fund" as "a b c  fund" against "ABC
    Fund" as "abc fund" -- so a registrant merely tidying the punctuation in a
    fund or firm name read as a rename, and every holder was told their fund
    had been renamed to the same name. The family variant was worse: one
    "Advisors, Inc." -> "Advisors Inc" edit in the register fanned a
    MF_FAMILY_CHANGE out to every watched ticker in the house.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def _squash(s):
    """Letters and digits only — "A.B.C. Fund" and "ABC Fund" both -> "abcfund"."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _same_name(a, b) -> bool:
    """
    True when two register spellings are the same name.

    Both forms are checked because neither alone is enough. The token form
    ("gamma advisors inc") handles dropped commas and hyphens but splits a
    de-dotted initialism; the squashed form ("abcfund") handles the initialism
    but would ignore word boundaries on its own. A rename only alerts when the
    name differs under BOTH, so a cosmetic edit between two vintages of SEC's
    register — which happens on every refresh — never reaches a holder as
    "your fund was renamed" to what reads as the same name.
    """
    return _norm_name(a) == _norm_name(b) or _squash(a) == _squash(b)


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot of the register as last seen
# ─────────────────────────────────────────────────────────────────────────────

def load_snapshot() -> dict:
    try:
        rows = mc.paged(lambda: mc.sb().table("mf_scheme_snapshot")
                        .select("ticker,series_id,class_id,cik,series_name,class_name,"
                                "entity_name,fund_type,is_active").order("ticker"))
    except Exception as e:
        _err("load_snapshot", e)
        return {}
    return {r["ticker"]: r for r in rows if r.get("ticker")}


def save_snapshot(rows) -> int:
    payload = [{
        "ticker": t, "series_id": r.get("series_id"), "class_id": r.get("class_id"),
        "cik": r.get("cik"), "series_name": r.get("series_name"),
        "class_name": r.get("class_name"), "entity_name": r.get("entity_name"),
        "fund_type": r.get("fund_type"), "is_active": True, "updated_at": mc.now_iso(),
    } for t, r in rows.items()]
    n = 0
    try:
        for i in range(0, len(payload), 500):
            (mc.sb().table("mf_scheme_snapshot")
             .upsert(payload[i:i + 500], on_conflict="ticker").execute())
            n += len(payload[i:i + 500])
    except Exception as e:
        _err("save_snapshot", e)
        return 0
    return n


def mark_retired(tickers) -> None:
    tickers = list(tickers)
    try:
        for i in range(0, len(tickers), 100):
            (mc.sb().table("mf_scheme_snapshot")
             .update({"is_active": False, "updated_at": mc.now_iso()})
             .in_("ticker", tickers[i:i + 100]).execute())
    except Exception as e:
        _err("mark_retired", e)


def record_event(event_type, row, detail) -> bool:
    """One row per change. False means it was already recorded."""
    try:
        mc.sb().table("mf_scheme_events").insert({
            "event_type": event_type, "ticker": row.get("ticker"),
            "series_id": row.get("series_id"), "class_id": row.get("class_id"),
            "cik": row.get("cik"), "series_name": row.get("series_name"),
            "class_name": row.get("class_name"), "entity_name": row.get("entity_name"),
            "detail": detail,
        }).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return False
        _err("record_event", e, {"event_type": event_type, "ticker": row.get("ticker")})
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────────────

def _new_class_alert(new_row, sibling_tickers, watched):
    """A new share class of a fund someone already holds."""
    hits = sorted(t for t in sibling_tickers if t in watched)
    if not hits:
        return 0
    fund = new_row.get("series_name") or new_row.get("series_id")
    cls = new_row.get("class_name")
    cls_txt = mc.class_suffix(fund, cls)
    summary = (
        f"{fund} has registered a new share class with SEC: {new_row['ticker']}{cls_txt}. "
        f"It holds the same portfolio as the class you follow ({', '.join(hits)}), "
        f"under a different ticker and, usually, a different fee schedule — share classes of "
        f"one mutual fund differ by sales load and 12b-1 fee, not by what the fund owns. "
        f"Check the prospectus fee table and the minimum investment before assuming it is cheaper, "
        f"and ask the fund whether an exchange between classes is available to you."
    )
    if new_row.get("entity_name"):
        summary += f" Fund family: {mc.family_text(new_row['entity_name'])}."
    summary += " Source: SEC investment company series and class register."
    return mc.emit_alerts(
        SOURCE, hits, summary, "MEDIUM", "MF_NEW_SHARE_CLASS",
        {"series_id": new_row.get("series_id"), "series_name": fund, "company_name": fund,
         "fund_family": new_row.get("entity_name"), "fund_type": "MUTUAL_FUND",
         "new_ticker": new_row["ticker"], "new_class_id": new_row.get("class_id"),
         "new_class_name": cls, "held_tickers": hits,
         "headline": f"{fund}: new share class {new_row['ticker']}"},
        dedup_key=f"{SOURCE}:NEW_CLASS:{new_row['ticker']}", poller=POLLER)


def _retired_alert(old_row, watched):
    t = old_row.get("ticker")
    if t not in watched:
        return 0
    fund = old_row.get("series_name") or old_row.get("series_id")
    cls = old_row.get("class_name")
    summary = (
        f"{t}{mc.class_suffix(fund, cls)} ({fund}) is no longer in SEC's live register of fund "
        f"share classes. A share class leaves that register when it is liquidated, merged into "
        f"another class of the same fund, or converted — so this ticker is on its way to not "
        f"quoting. Your money is not lost: in a conversion or merger the shares become shares of "
        f"the surviving class. Check the fund's latest Form 497 supplement for which class you "
        f"will hold and from what date."
    )
    if old_row.get("entity_name"):
        summary += f" Fund family: {mc.family_text(old_row['entity_name'])}."
    summary += " Source: SEC investment company series and class register."
    return mc.emit_alerts(
        SOURCE, [t], summary, "HIGH", "MF_TICKER_RETIRED",
        {"series_id": old_row.get("series_id"), "series_name": fund, "company_name": fund,
         "fund_family": old_row.get("entity_name"), "fund_type": "MUTUAL_FUND",
         "class_id": old_row.get("class_id"), "class_name": cls,
         "headline": f"{fund}: {t} left SEC's register"},
        dedup_key=f"{SOURCE}:RETIRED:{t}", poller=POLLER)


def _renamed_alert(old_row, new_row, watched):
    t = new_row.get("ticker")
    if t not in watched:
        return 0
    old_n, new_n = old_row.get("series_name"), new_row.get("series_name")
    summary = (
        f"{t} changed its name in SEC's register: \"{old_n}\" is now \"{new_n}\". "
        f"A mutual fund renames itself when its mandate changes, when it is rebranded after the "
        f"fund family is bought, or when a strategy is broadened or narrowed. The ticker and your "
        f"holding are unchanged. The fund's Form 497 supplement says which of those it is — worth "
        f"reading if the new name points at a different kind of portfolio."
    )
    if new_row.get("entity_name"):
        summary += f" Fund family: {mc.family_text(new_row['entity_name'])}."
    summary += " Source: SEC investment company series and class register."
    return mc.emit_alerts(
        SOURCE, [t], summary, "MEDIUM", "MF_FUND_RENAMED",
        {"series_id": new_row.get("series_id"), "series_name": new_n, "company_name": new_n,
         "previous_name": old_n, "fund_family": new_row.get("entity_name"),
         "fund_type": "MUTUAL_FUND",
         "headline": f"{new_n}: fund renamed"},
        dedup_key=f"{SOURCE}:RENAMED:{t}:{_norm_name(new_n)[:40]}", poller=POLLER)


def _family_alert(old_name, new_name, rows, watched):
    """The registrant (the AMC) was renamed, bought or rebranded."""
    hits = sorted(t for t in rows if t in watched)
    if not hits:
        return 0
    summary = (
        f"The fund family behind {', '.join(hits)} changed its name with SEC: "
        f"\"{old_name}\" is now \"{new_name}\". A registrant renames itself when the asset manager "
        f"is acquired, merges, or rebrands — the funds keep their tickers and portfolios, but the "
        f"firm managing them, and often the management contracts and fees that follow, is now a "
        f"different house. Watch for a Form 497 supplement naming a new investment adviser."
    )
    summary += " Source: SEC investment company series and class register."
    return mc.emit_alerts(
        SOURCE, hits, summary, "MEDIUM", "MF_FAMILY_CHANGE",
        {"fund_family": new_name, "previous_family": old_name, "company_name": new_name,
         "fund_type": "MUTUAL_FUND", "affected_tickers": hits,
         "headline": f"{new_name}: fund family renamed"},
        dedup_key=f"{SOURCE}:FAMILY:{_norm_name(old_name)[:30]}:{_norm_name(new_name)[:30]}",
        poller=POLLER)


# ─────────────────────────────────────────────────────────────────────────────
# Job
# ─────────────────────────────────────────────────────────────────────────────

def diff_register(old, new):
    """
    old, new   {ticker: row}
    -> {"added": [...], "retired": [...], "renamed": [(old,new)], "families": [(old,new,[tickers])]}
    """
    added = [new[t] for t in new if t not in old]
    retired = [old[t] for t in old if t not in new and old[t].get("is_active", True)]
    renamed = [(old[t], new[t]) for t in new
               if t in old and old[t].get("series_name") and new[t].get("series_name")
               and not _same_name(old[t]["series_name"], new[t]["series_name"])]
    fam = {}
    for t, r in new.items():
        o = old.get(t)
        if not o or not o.get("entity_name") or not r.get("entity_name"):
            continue
        if not _same_name(o["entity_name"], r["entity_name"]):
            fam.setdefault((o["entity_name"], r["entity_name"]), []).append(t)
    return {"added": added, "retired": retired, "renamed": renamed,
            "families": [(a, b, ts) for (a, b), ts in fam.items()]}


def run_mf_scheme_poll():
    """Daily, after refresh_mf_master: what changed in SEC's fund register."""
    try:
        # Every classification: a money-market or insurance fund appearing or
        # leaving is still a register change the holder of a sibling class
        # should see. Alerting is gated on the watchlist, not on fund_type.
        u = mc.load_mf_universe(force=True, types=None)
        new = u["by_ticker"]
        if len(new) < 1000:
            log(f"only {len(new)} tickers in mf_master — refusing to diff against a partial register")
            return 0
        old = load_snapshot()
        if not old:
            n = save_snapshot(new)
            log(f"first run: {n} share classes stored as the baseline, no alerts sent")
            return 0

        d = diff_register(old, new)
        live_old = sum(1 for r in old.values() if r.get("is_active", True))
        if live_old and len(d["retired"]) / live_old > MAX_RETIRE_FRACTION:
            log(f"{len(d['retired'])} of {live_old} share classes missing "
                f"(> {MAX_RETIRE_FRACTION:.0%}) — treating as a bad register download, "
                f"no retirements recorded")
            d["retired"] = []

        watched = mc.watched_tickers()
        alerts = 0

        # New share classes -> the holders of the fund's other classes.
        by_series = {}
        for r in d["added"]:
            if r.get("series_id"):
                by_series.setdefault(r["series_id"], []).append(r)
        for sid, rows in by_series.items():
            if len(rows) > MAX_NEW_PER_SERIES:
                continue                       # bulk re-registration, not a launch
            siblings = [t for t, r in old.items()
                        if r.get("series_id") == sid and r.get("is_active", True)]
            for r in rows:
                if not siblings:
                    continue                   # a brand-new fund: nobody holds it yet
                if record_event("NEW_SHARE_CLASS", r, {"siblings": siblings[:20]}):
                    alerts += _new_class_alert(r, siblings, watched)

        for r in d["retired"]:
            if record_event("TICKER_RETIRED", r, {"last_seen": r.get("updated_at")}):
                alerts += _retired_alert(r, watched)
        if d["retired"]:
            mark_retired([r["ticker"] for r in d["retired"]])

        for o, n in d["renamed"]:
            if record_event("FUND_RENAMED", n, {"previous_name": o.get("series_name")}):
                alerts += _renamed_alert(o, n, watched)

        for old_name, new_name, ts in d["families"]:
            rep = new[ts[0]]
            if record_event("FAMILY_CHANGE", rep,
                            {"previous_family": old_name, "tickers": ts[:50]}):
                alerts += _family_alert(old_name, new_name, ts, watched)

        save_snapshot(new)
        log(f"scheme register: {len(new)} share classes, {len(d['added'])} new, "
            f"{len(d['retired'])} retired, {len(d['renamed'])} renamed, "
            f"{len(d['families'])} fund families renamed, {alerts} alerts")
        return alerts
    except Exception as e:
        _err("run_mf_scheme_poll", e)
        return 0


def coverage() -> dict:
    u = mc.load_mf_universe(force=True, types=None)
    snap = load_snapshot()
    return {"register_tickers": len(u["by_ticker"]),
            "register_funds": len(u["by_series"]),
            "snapshot_tickers": len(snap),
            "snapshot_live": sum(1 for r in snap.values() if r.get("is_active", True))}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "coverage":
        print(coverage())
    else:
        run_mf_scheme_poll()
