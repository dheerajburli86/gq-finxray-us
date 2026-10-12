"""
stock_ownership_poller.py
GQ FinXray US — who owns your stocks (Features 20, 21).

Feature 20  ETF Ownership Changes          (source ETF_HOLDER)
    The stock-side mirror of Feature 14. etf_data_poller already diffs every
    ETF's portfolio into etf_holdings_changes; this reads those rows the other
    way round -- "which funds just added or dropped AAPL" -- for stocks on a
    watchlist. No new API calls. One alert per stock per batch of changes,
    sized in dollars from the fund's AUM so a $40M fund adding 0.6% (~$240K)
    does not read like QQQ adding a name.
        STOCK_ETF_ADDED / STOCK_ETF_REMOVED

Feature 21  Institutional Ownership (13F)  (source INST_13F)
    FMP's 13F positions summary per watched stock, once per quarter, after the
    45-day filing deadline plus a settling margin (late filers keep moving the
    totals until then). Holders, ownership %, new/closed positions.
        INST_OWNERSHIP_UP / INST_OWNERSHIP_DOWN

Both only write alerts when STOCK_ALERTS_ENABLED=true (shared with
stock_master_poller). 13F data is stored regardless.
"""

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import fmp_client
from feature_map import tag_extra
from watchlist_util import get_watched_tickers, log_poller_error

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
POLLER = "stock_ownership_poller"
SRC_ETF = "ETF_HOLDER"
SRC_13F = "INST_13F"


def _env_bool(name, default=False):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def alerts_enabled():
    return _env_bool("STOCK_ALERTS_ENABLED", False)


MIN_WEIGHT = float(os.getenv("STOCK_ETF_MIN_WEIGHT", "0.5"))          # % of the fund
MIN_USD = float(os.getenv("STOCK_ETF_MIN_USD", "5e6"))                # below -> LOW
HIGH_USD = float(os.getenv("STOCK_ETF_HIGH_USD", "250e6"))
HIGH_WEIGHT = float(os.getenv("STOCK_ETF_HIGH_WEIGHT", "5.0"))
MAX_BATCH = int(os.getenv("STOCK_ETF_MAX_BATCH", "50000"))
LIST_LIMIT = 8

F13_DEADLINE_DAYS = 45
F13_SETTLE_DAYS = int(os.getenv("STOCK_13F_SETTLE_DAYS", "5"))
F13_WINDOW_DAYS = int(os.getenv("STOCK_13F_WINDOW_DAYS", "21"))
F13_PAUSE = float(os.getenv("STOCK_13F_PAUSE", "0.25"))
F13_HIGH_PTS = float(os.getenv("STOCK_13F_HIGH_PTS", "3.0"))
F13_MED_PTS = float(os.getenv("STOCK_13F_MED_PTS", "0.5"))
F13_HIGH_SHARES_PCT = float(os.getenv("STOCK_13F_HIGH_SHARES_PCT", "10"))
F13_MED_SHARES_PCT = float(os.getenv("STOCK_13F_MED_SHARES_PCT", "2"))

_sb = None


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    return _sb


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_ticker(t):
    return (t or "").strip().upper().replace(".", "-").replace("/", "-")


def _paged(query_fn, size=1000, cap=None):
    out, page = [], 0
    while True:
        chunk = query_fn().range(page * size, page * size + size - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < size or (cap and len(out) >= cap):
            return out
        page += 1


def _usd(v):
    if v is None:
        return None
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{unit}"
    return f"${v:,.0f}"


def watched_map(watched):
    """{canonical ticker: ticker as written on the watchlist}"""
    return {norm_ticker(t): t for t in watched}


def get_state(key):
    r = sb().table("stock_ref_state").select("value").eq("key", key).limit(1).execute().data
    return (r[0].get("value") if r else None)


def set_state(key, value):
    sb().table("stock_ref_state").upsert({"key": key, "value": value, "updated_at": _now_iso()},
                                         on_conflict="key").execute()


def load_sent(source, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = _paged(lambda: sb().table("alerts").select("id, extra")
                      .eq("source", source).gte("created_at", since).order("id"))
    except Exception as e:
        log_poller_error(POLLER, "load_sent", e, {"source": source})
        return None
    return {(r.get("extra") or {}).get("dedup_key") for r in rows} - {None}


def write_alerts(rows):
    ok = True
    for i in range(0, len(rows), 50):
        try:
            sb().table("alerts").insert(rows[i:i + 50]).execute()
        except Exception as e:
            ok = False
            log_poller_error(POLLER, "write_alerts", e)
    return ok


def _row(ticker, source, ftype, impact, summary, extra, link=None):
    # Same keys on every row: PostgREST rejects mixed-key bulk inserts.
    return {"ticker": ticker, "summary": summary, "impact": impact, "source": source,
            "filing_type": ftype, "delivered": False, "filing_url": link,
            "extra": tag_extra(extra, source, ftype)}


# ── Feature 20: ETF ownership changes ────────────────────────────────────────
def etf_holder_alerts(changes, funds, wmap, names, sent):
    """
    changes  etf_holdings_changes rows (id, ticker=ETF, security_symbol, action,
             weight_prev, weight_new)
    funds    {ETF: {"name", "aum"}}
    wmap     {canonical stock ticker: watchlist form}
    names    {canonical stock ticker: company name}
    -> alert rows (pure)
    """
    groups = {}
    for c in changes:
        t = norm_ticker(c.get("security_symbol"))
        if not t or t not in wmap:
            continue
        w = _num(c.get("weight_new") if c.get("action") == "ADDED" else c.get("weight_prev"))
        if w is None or w < MIN_WEIGHT:
            continue
        f = funds.get(c.get("ticker")) or {}
        aum = _num(f.get("aum"))
        g = groups.setdefault(t, {"ADDED": [], "REMOVED": [], "ids": []})
        g[c["action"]].append({"etf": c.get("ticker"), "fund": f.get("name") or c.get("ticker"),
                               "weight": w, "usd": aum * w / 100 if aum else None})
        g["ids"].append(int(c["id"]))

    out = []
    for t, g in sorted(groups.items()):
        key = f"{SRC_ETF}:{t}:{max(g['ids'])}"
        if key in sent:
            continue
        adds = sorted(g["ADDED"], key=lambda x: -(x["usd"] or 0))
        rems = sorted(g["REMOVED"], key=lambda x: -(x["usd"] or 0))
        allx = adds + rems
        big = max((x["usd"] or 0) for x in allx)
        if big >= HIGH_USD or max(x["weight"] for x in allx) >= HIGH_WEIGHT:
            impact = "HIGH"
        elif big >= MIN_USD or any(x["usd"] is None for x in allx):
            impact = "MEDIUM"
        else:
            impact = "LOW"
        wt, name = wmap[t], names.get(t) or wmap[t]
        ftype = "STOCK_ETF_ADDED" if len(adds) >= len(rems) else "STOCK_ETF_REMOVED"
        net_usd = sum(x["usd"] or 0 for x in adds) - sum(x["usd"] or 0 for x in rems)

        def line(x):
            size = f", ≈{_usd(x['usd'])}" if x["usd"] else ""
            return f"• {x['etf']} — {x['fund']} ({x['weight']:.2f}% of fund{size})"

        parts = []
        if adds:
            parts.append(f"Added by {len(adds)} ETF{'s' if len(adds) > 1 else ''}:")
            parts += [line(x) for x in adds[:LIST_LIMIT]]
            if len(adds) > LIST_LIMIT:
                parts.append(f"• +{len(adds) - LIST_LIMIT} more")
        if rems:
            parts.append(f"Dropped by {len(rems)} ETF{'s' if len(rems) > 1 else ''}:")
            parts += [line(x) for x in rems[:LIST_LIMIT]]
            if len(rems) > LIST_LIMIT:
                parts.append(f"• +{len(rems) - LIST_LIMIT} more")
        if adds and rems and net_usd:
            parts.append(f"Net: {'+' if net_usd > 0 else '-'}{_usd(abs(net_usd))} of {wt} across these funds.")
        if adds and not rems:
            headline = (f"{adds[0]['etf']} added {wt}" if len(adds) == 1
                        else f"{len(adds)} ETFs added {wt}")
        elif rems and not adds:
            headline = (f"{rems[0]['etf']} dropped {wt}" if len(rems) == 1
                        else f"{len(rems)} ETFs dropped {wt}")
        else:
            headline = f"{wt}: {len(adds)} ETF{'s' if len(adds) > 1 else ''} added, {len(rems)} dropped"
        extra = {"dedup_key": key, "headline": headline, "company_name": name,
                 "added": adds, "removed": rems, "change_ids": sorted(g["ids"]),
                 "net_usd": round(net_usd) if net_usd else None, "filed_at": _now_iso()}
        out.append(_row(wt, SRC_ETF, ftype, impact, "\n".join(parts), extra,
                        f"https://www.tradingview.com/chart/?symbol={wt}"))
    return out


def run_stock_etf_holders():
    """Reads new etf_holdings_changes rows past the watermark. Never replays a
    backlog: the first run (or any run with alerts off) just moves the mark."""
    try:
        last = get_state("etf_holder_last_id")
        top = (sb().table("etf_holdings_changes").select("id").order("id", desc=True)
               .limit(1).execute().data or [{}])[0].get("id")
        if top is None:
            return
        if last is None or not alerts_enabled():
            set_state("etf_holder_last_id", int(top))
            return
        last = int(last)
        if int(top) <= last:
            return
        changes = _paged(lambda: sb().table("etf_holdings_changes")
                         .select("id, ticker, security_symbol, security_name, action, weight_prev, weight_new")
                         .gte("id", last + 1).order("id"), cap=MAX_BATCH)
        if not changes:
            return
        hi = max(int(c["id"]) for c in changes)
        wmap = watched_map({t.upper() for t in get_watched_tickers(force=True)})
        relevant = [c for c in changes if norm_ticker(c.get("security_symbol")) in wmap]
        if relevant:
            etfs = sorted({c["ticker"] for c in relevant})
            funds = {}
            for i in range(0, len(etfs), 200):
                for r in (sb().table("etf_master").select("ticker, name, aum")
                          .in_("ticker", etfs[i:i + 200]).execute().data or []):
                    funds[r["ticker"]] = r
            names = {norm_ticker(c["security_symbol"]): c.get("security_name") for c in relevant}
            sent = load_sent(SRC_ETF, 30)
            if sent is None:
                return                                   # fail closed; retry next run
            rows = etf_holder_alerts(relevant, funds, wmap, names, sent)
            if rows and not write_alerts(rows):
                return                                   # watermark stays; dedup prevents doubles
            logger.info("[OWN] ETF holders: %d changes, %d relevant, %d alerts",
                        len(changes), len(relevant), len(rows))
        set_state("etf_holder_last_id", hi)
    except Exception as e:
        log_poller_error(POLLER, "run_stock_etf_holders", e)
        logger.exception("[OWN] ETF holders run failed: %s", e)


# ── Feature 21: 13F institutional ownership ──────────────────────────────────
def quarter_end(y, q):
    return date(y, 3 * q, 31 if q in (1, 4) else 30)


def reportable_quarter(today):
    """(year, quarter) whose 13F totals have settled and are still fresh enough
    to send, else None. Q2 (Jun 30) becomes sendable Aug 19 and stays so for
    F13_WINDOW_DAYS; outside that window nothing is fetched at all."""
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(3):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        ready = quarter_end(y, q) + timedelta(days=F13_DEADLINE_DAYS + F13_SETTLE_DAYS)
        if ready <= today:
            return (y, q) if (today - ready).days <= F13_WINDOW_DAYS else None
    return None


def _pick(d, *names):
    for n in names:
        if d.get(n) is not None:
            return _num(d.get(n))
    return None


def _int(v):
    return None if v is None else int(round(v))


def parse_13f(raw):
    """FMP positions summary -> our columns. Field names follow FMP's
    institutional-ownership schema; anything missing stays None. Count fields
    are ints: Postgres rejects 5200.0 for an INT column."""
    own = _pick(raw, "ownershipPercent")
    own_ch = _pick(raw, "ownershipPercentChange")
    out = {
        "investors_holding": _pick(raw, "investorsHolding"),
        "investors_holding_change": _pick(raw, "investorsHoldingChange"),
        "shares_13f": _pick(raw, "numberOf13Fshares"),
        "shares_13f_change": _pick(raw, "numberOf13FsharesChange"),
        "ownership_pct": own,
        "ownership_pct_change": own_ch,
        "new_positions": _pick(raw, "newPositions"),
        "closed_positions": _pick(raw, "closedPositions"),
        "increased_positions": _pick(raw, "increasedPositions"),
        "reduced_positions": _pick(raw, "reducedPositions"),
        "put_call_ratio": _pick(raw, "putCallRatio"),
    }
    for k in ("investors_holding", "investors_holding_change", "new_positions",
              "closed_positions", "increased_positions", "reduced_positions"):
        out[k] = _int(out[k])
    return out


def inst_alert(ticker, wt, name, y, q, s, sent):
    key = f"{SRC_13F}:{norm_ticker(ticker)}:{y}Q{q}"
    if key in sent or s.get("investors_holding") is None:
        return None
    sh, sh_ch = s.get("shares_13f"), s.get("shares_13f_change")
    prev_sh = (sh - sh_ch) if (sh is not None and sh_ch is not None) else None
    sh_pct = (sh_ch / prev_sh * 100) if prev_sh else None
    own_ch = s.get("ownership_pct_change")
    if (own_ch is not None and abs(own_ch) >= F13_HIGH_PTS) or (sh_pct is not None and abs(sh_pct) >= F13_HIGH_SHARES_PCT):
        impact = "HIGH"
    elif (own_ch is not None and abs(own_ch) >= F13_MED_PTS) or (sh_pct is not None and abs(sh_pct) >= F13_MED_SHARES_PCT):
        impact = "MEDIUM"
    else:
        impact = "LOW"
    up = (sh_ch if sh_ch is not None else (own_ch or 0)) >= 0
    ftype = "INST_OWNERSHIP_UP" if up else "INST_OWNERSHIP_DOWN"

    def i(v):
        return "n/a" if v is None else f"{int(v):,}"

    def sgn(v, fmt):
        return "" if v is None else f" ({'+' if v >= 0 else ''}{fmt(v)})"

    lines = [f"Q{q} {y} 13F filings (institutions with >$100M under management):",
             f"• Institutions holding: {i(s['investors_holding'])}"
             + sgn(s.get("investors_holding_change"), lambda v: f"{int(v):,}") + f" vs Q{q - 1 if q > 1 else 4}"]
    if s.get("ownership_pct") is not None:
        lines.append(f"• Institutional ownership: {s['ownership_pct']:.1f}%"
                     + sgn(own_ch, lambda v: f"{v:.1f} pts"))
    if sh is not None:
        lines.append(f"• Shares held: {sh / 1e6:,.1f}M" + sgn(sh_pct, lambda v: f"{v:.1f}%"))
    if s.get("new_positions") is not None or s.get("closed_positions") is not None:
        lines.append(f"• New positions {i(s.get('new_positions'))}, closed {i(s.get('closed_positions'))}; "
                     f"increased {i(s.get('increased_positions'))}, reduced {i(s.get('reduced_positions'))}")
    if s.get("put_call_ratio") is not None:
        lines.append(f"• Put/call ratio: {s['put_call_ratio']:.2f}")
    direction = "added to" if up else "cut"
    headline = f"Institutions {direction} {wt} in Q{q} {y}"
    extra = dict(s, dedup_key=key, headline=headline, company_name=name or wt,
                 year=y, quarter=q, shares_change_pct=None if sh_pct is None else round(sh_pct, 2),
                 filed_at=_now_iso())
    return _row(wt, SRC_13F, ftype, impact, "\n".join(lines), extra,
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR")


def fetch_13f(ticker, y, q):
    data = fmp_client._get("institutional-ownership/symbol-positions-summary",
                           {"symbol": ticker, "year": y, "quarter": q})
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def run_stock_13f(today=None):
    try:
        enabled = alerts_enabled()
        today = today or datetime.now(ET).date()
        yq = reportable_quarter(today)
        if not yq:
            return
        y, q = yq
        watched = {t.upper() for t in get_watched_tickers(force=True)}
        etfs = set()
        wl = sorted(watched)
        for i in range(0, len(wl), 200):
            etfs |= {r["ticker"] for r in (sb().table("etf_master").select("ticker")
                                           .in_("ticker", wl[i:i + 200]).execute().data or [])}
        stocks = [t for t in wl if t not in etfs]
        if not stocks:
            return
        if enabled:
            sent = load_sent(SRC_13F, 150)
            if sent is None:
                return                                    # fail closed
        else:
            # Alerts off: still collect, once per stock per quarter.
            sent = set()
            for i in range(0, len(stocks), 200):
                for r in (sb().table("stock_13f_summary").select("ticker").eq("year", y).eq("quarter", q)
                          .in_("ticker", [norm_ticker(t) for t in stocks[i:i + 200]]).execute().data or []):
                    sent.add(f"{SRC_13F}:{r['ticker']}:{y}Q{q}")
        names = {}
        for i in range(0, len(stocks), 200):
            for r in (sb().table("stock_master").select("ticker, name")
                      .in_("ticker", [norm_ticker(t) for t in stocks[i:i + 200]]).execute().data or []):
                names[r["ticker"]] = r.get("name")
        rows = []
        for t in stocks:
            if f"{SRC_13F}:{norm_ticker(t)}:{y}Q{q}" in sent:
                continue
            try:
                raw = fetch_13f(norm_ticker(t), y, q)
            except Exception as e:
                log_poller_error(POLLER, "fetch_13f", e, {"ticker": t})
                break                                     # likely rate-limited; resume tomorrow
            time.sleep(F13_PAUSE)
            if not raw:
                continue
            s = parse_13f(raw)
            try:
                sb().table("stock_13f_summary").upsert(
                    dict(s, ticker=norm_ticker(t), year=y, quarter=q, raw=raw, fetched_at=_now_iso()),
                    on_conflict="ticker,year,quarter").execute()
            except Exception as e:
                log_poller_error(POLLER, "store_13f", e, {"ticker": t})
            a = inst_alert(t, t, names.get(norm_ticker(t)), y, q, s, sent) if enabled else None
            if a:
                rows.append(a)
        if rows:
            write_alerts(rows)
        logger.info("[OWN] 13F Q%d %d: %d stocks checked, %d alerts", q, y, len(stocks), len(rows))
    except Exception as e:
        log_poller_error(POLLER, "run_stock_13f", e)
        logger.exception("[OWN] 13F run failed: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("etf", "both"):
        run_stock_etf_holders()
    if which in ("13f", "both"):
        run_stock_13f()
